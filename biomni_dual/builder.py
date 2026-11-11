"""Persistent approval/build workflow; only validated exact digest can publish."""
from __future__ import annotations
import asyncio
import json
import sqlite3
import shutil
import os
import time
from pathlib import Path
from uuid import uuid4
from dataclasses import asdict
from .contracts import BuildError
from .sandbox import DockerSandbox, Validation
from .models import Capability, CapabilityKind
from .registry import CapabilityRegistry
from .reliability import AuditLog

class BuildStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS builds (
                id TEXT PRIMARY KEY, capability TEXT NOT NULL, kind TEXT NOT NULL,
                path TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL,
                evidence TEXT, approver TEXT, created REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS build_idempotency (
                key TEXT PRIMARY KEY, capability TEXT NOT NULL, kind TEXT NOT NULL,
                digest TEXT NOT NULL, build_id TEXT NOT NULL)''')
            AuditLog.install(db)
            db.execute('''CREATE TABLE IF NOT EXISTS published (
                capability TEXT NOT NULL, digest TEXT NOT NULL, build_id TEXT UNIQUE NOT NULL,
                created REAL NOT NULL, PRIMARY KEY(capability, digest))''')
    def _db(self): return sqlite3.connect(self.path, timeout=15)
    def create(self, capability: str, kind: str, path: str, digest: str, *, idempotency_key: str | None = None) -> str:
        build_id = uuid4().hex
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if idempotency_key is not None:
                if not isinstance(idempotency_key,str) or not 1 <= len(idempotency_key) <= 200:
                    raise ValueError('Invalid idempotency key')
                old = db.execute('SELECT capability,kind,digest,build_id FROM build_idempotency WHERE key=?',
                                 (idempotency_key,)).fetchone()
                if old:
                    if old[:3] != (capability,kind,digest):
                        raise BuildError('Build idempotency key reused with different artifact')
                    return old[3]
                db.execute('INSERT INTO build_idempotency VALUES (?,?,?,?,?)',
                           (idempotency_key,capability,kind,digest,build_id))
            db.execute('INSERT INTO builds VALUES (?,?,?,?,?,?,?,?,?)',
                (build_id,capability,kind,path,digest,'PROPOSED',None,None,time.time()))
            AuditLog.append_in_transaction(db, actor='builder', action='build.proposed',
                 subject=build_id, details={'capability':capability,'kind':kind,'digest':digest})
        return build_id
    def get(self, build_id: str) -> dict:
        with self._db() as db:
            row = db.execute('SELECT * FROM builds WHERE id=?',(build_id,)).fetchone()
        if row is None: raise KeyError('Unknown build')
        keys=('id','capability','kind','path','digest','status','evidence','approver','created')
        return dict(zip(keys,row))
    def transition(self, build_id: str, expected: str, target: str, *, evidence=None, approver=None):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            rc = db.execute('UPDATE builds SET status=?,evidence=COALESCE(?,evidence),approver=COALESCE(?,approver) WHERE id=? AND status=?',
                      (target,evidence,approver,build_id,expected)).rowcount
            if rc!=1: raise BuildError(f'Illegal or duplicate state transition {expected}->{target}')
            AuditLog.append_in_transaction(db, actor=approver or 'builder', action='build.'+target.lower(),
                subject=build_id, details={'from':expected,'to':target})
    def recover_interrupted(self) -> int:
        """Fail closed on coordinator restart: TESTING cannot imply success."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("UPDATE builds SET status='FAILED',evidence=? WHERE status='TESTING'",
                (json.dumps({'error':'Interrupted validation; requires fresh proposal'}),))
            return rows.rowcount

    def record_published(self, build_id: str, artifact_path: str):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT capability,digest,status FROM builds WHERE id=?',(build_id,)).fetchone()
            if not row or row[2]!='APPROVED': raise BuildError('Only approved builds can publish')
            if db.execute('SELECT 1 FROM published WHERE capability=? AND digest=?',(row[0],row[1])).fetchone():
                raise BuildError('Artifact already published')
            db.execute('INSERT INTO published VALUES (?,?,?,?)',(row[0],row[1],build_id,time.time()))
            db.execute('UPDATE builds SET status=?,path=? WHERE id=?',('PUBLISHED',artifact_path,build_id))
            AuditLog.append_in_transaction(db,actor='builder',action='build.published',
                subject=build_id,details={'capability':row[0],'digest':row[1]})

class SandboxBuilder:
    def __init__(self, store: BuildStore, sandbox: DockerSandbox, registry: CapabilityRegistry):
        self.store,self.sandbox,self.registry=store,sandbox,registry

    def propose(self, capability_id: str, kind: str, folder: str | Path, *, idempotency_key: str | None = None) -> str:
        if kind not in ('skill','mcp'): raise ValueError('Unsupported capability type')
        if not capability_id or not all(c.isascii() and (c.isalnum() or c in '._-:') for c in capability_id):
            raise ValueError('Invalid capability id')
        path = Path(folder).resolve(strict=True)
        return self.store.create(capability_id,kind,str(path),self.sandbox.digest_tree(path), idempotency_key=idempotency_key)

    async def validate(self, build_id: str) -> dict:
        job = self.store.get(build_id)
        if job['status']!='PROPOSED': raise BuildError('Build already processed')
        self.store.transition(build_id,'PROPOSED','TESTING')
        try:
            result = await self.sandbox.validate(job['path'],kind=job['kind'])
            if result.digest!=job['digest']: raise BuildError('Digest mismatch; no publication')
            evidence=json.dumps(asdict(result),ensure_ascii=False)
            self.store.transition(build_id,'TESTING','REVIEW' if result.passed else 'FAILED',evidence=evidence)
        except Exception as exc:
            self.store.transition(build_id,'TESTING','FAILED',evidence=json.dumps({'error':str(exc)}))
            raise
        return self.store.get(build_id)

    def approve(self, build_id: str, approver: str, *, approved: bool):
        if not approver.strip(): raise ValueError('Explicit approver required')
        self.store.transition(build_id,'REVIEW','APPROVED' if approved else 'REJECTED',approver=approver)

    def publish(self, build_id: str) -> Capability:
        job=self.store.get(build_id)
        if job['status']=='PUBLISHED':
            if self.sandbox.digest_tree(job['path']) != job['digest']:
                raise BuildError('Previously published artifact no longer matches')
            cap=self.registry.get(job['capability'])
            if cap and cap.metadata.get('digest')==job['digest']:
                return cap
            self.restore_published()
            cap=self.registry.get(job['capability'])
            if cap and cap.metadata.get('digest')==job['digest']:
                return cap
            raise BuildError('Published artifact not registered')
        if job['status']!='APPROVED': raise PermissionError('Unapproved build')
        if self.sandbox.digest_tree(job['path'])!=job['digest']:
            raise BuildError('Artifact changed since validation')
        evidence=json.loads(job['evidence'] or '{}')
        if evidence.get('passed') is not True or evidence.get('digest')!=job['digest']:
            raise BuildError('No matching successful sandbox evidence')
        # Register from independently verified manifest; no code executes here.
        cap=Capability(id=job['capability'], description='Sandbox validated '+job['kind'],
                       kind=CapabilityKind(job['kind']),
                       metadata={'digest':job['digest'],'build_id':build_id,'validation':evidence})
        # Snapshot the validated bytes into a content-addressed artifact directory.
        # The coordinator owns this directory; running workers receive read-only
        # copies, not the writable staging tree.
        store_root=Path(self.store.path).resolve().parent/'published_artifacts'
        store_root.mkdir(parents=True,exist_ok=True)
        destination=store_root/job['digest']
        if destination.exists():
            if self.sandbox.digest_tree(destination)!=job['digest']:
                raise BuildError('Existing artifact storage has unexpected bytes')
        else:
            temp=store_root/(job['digest']+'.staging-'+uuid4().hex)
            try:
                shutil.copytree(job['path'],temp,symlinks=False)
                if self.sandbox.digest_tree(temp)!=job['digest']:
                    raise BuildError('Artifact snapshot changed during copy')
                os.rename(temp,destination)
            finally:
                if temp.exists(): shutil.rmtree(temp)
        self.store.record_published(build_id,str(destination))
        cap.metadata['artifact_path']=str(destination)
        self.registry.register(cap)
        return cap

    def restore_published(self) -> int:
        """Restore previously published capability metadata on coordinator restart."""
        loaded=0
        with self.store._db() as db:
            rows=db.execute('''SELECT b.id,b.capability,b.kind,b.digest,b.evidence,b.path
                               FROM builds b JOIN published p ON p.build_id=b.id
                               WHERE b.status='PUBLISHED' ''').fetchall()
        for build_id,name,kind,digest,evidence,path in rows:
            if self.sandbox.digest_tree(path) != digest:
                continue  # fail closed if source mutated or unavailable
            data=json.loads(evidence or '{}')
            if not data.get('passed') or data.get('digest')!=digest:
                continue
            cap=Capability(name,'Restored validated '+kind,CapabilityKind(kind),
                           metadata={'digest':digest,'build_id':build_id,'validation':data,'artifact_path':path})
            self.registry.register(cap,replace=True)
            loaded+=1
        return loaded
