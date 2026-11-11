"""v0.5: controlled autonomous Skill/MCP acquisition lifecycle.

Discovery uses operator-controlled local directories and optional HTTPS indexes.
Generation is restricted to inert JSON files; all execution is delegated to
SandboxBuilder. Publication always requires an explicit human approval.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import urlopen
from uuid import uuid4

from .builder import SandboxBuilder
from .capability_agent import CapabilityAgent
from .contracts import BuildError
from .models import TaskStatus
from .reliability import AuditLog
from .marketplaces import FederatedDiscovery, MarketCandidate, MarketplaceError

CAP_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,120}$')


@dataclass(frozen=True)
class CatalogEntry:
    capability_id: str
    kind: str
    source: str
    digest: str
    description: str = ''


class TrustedCatalog:
    """Search pinned, operator-selected catalogs; does not execute fetched content."""
    def __init__(self, entries: list[CatalogEntry], *, allowed_hosts: tuple[str, ...] = (),
                 download_root: str | Path | None = None):
        self.entries = entries
        self.allowed_hosts = tuple(allowed_hosts)
        self.download_root = Path(download_root or tempfile.gettempdir()).resolve()
        self.download_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_json(cls, path: str | Path, **kwargs):
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(raw, list): raise ValueError('Catalog must be a list')
        entries = []
        for item in raw:
            if not isinstance(item, dict): raise ValueError('Invalid catalog entry')
            entries.append(CatalogEntry(item['capability_id'], item['kind'],
                                        item['source'], item['digest'], item.get('description','')))
        return cls(entries, **kwargs)

    @classmethod
    def from_https_index(cls, url: str, *, allowed_hosts: tuple[str, ...],
                         download_root: str | Path | None = None):
        """Refresh an operator-allowlisted HTTPS index; artifact digests still pin bytes."""
        origin = urlparse(url)
        if origin.scheme != 'https' or origin.hostname not in allowed_hosts or origin.username or origin.password:
            raise PermissionError('Catalog index host not allowlisted')
        with urlopen(url, timeout=15) as response:
            target = urlparse(response.geturl())
            if target.scheme != 'https' or target.hostname not in allowed_hosts:
                raise PermissionError('Catalog index redirect not allowlisted')
            contents = response.read(512 * 1024 + 1)
        if len(contents) > 512 * 1024: raise BuildError('Catalog index exceeds size limit')
        raw = json.loads(contents)
        if not isinstance(raw,list) or len(raw) > 1000: raise BuildError('Invalid catalog index')
        entries=[]
        for item in raw:
            if not isinstance(item,dict): raise BuildError('Invalid catalog item')
            entries.append(CatalogEntry(item['capability_id'],item['kind'],
                            item['source'],item['digest'],item.get('description','')))
        return cls(entries,allowed_hosts=allowed_hosts,download_root=download_root)

    def search(self, capability_id: str, kind: str) -> list[CatalogEntry]:
        if not CAP_ID.fullmatch(capability_id) or kind not in ('skill', 'mcp'):
            raise ValueError('Invalid capability query')
        return [x for x in self.entries if x.capability_id == capability_id and x.kind == kind]

    def materialize(self, entry: CatalogEntry, digest_tree) -> Path:
        if not re.fullmatch(r'[0-9a-f]{64}', entry.digest):
            raise BuildError('Catalog entry requires a pinned SHA256 tree digest')
        u = urlparse(entry.source)
        if u.scheme in ('', 'file'):
            if u.scheme == 'file': raise BuildError('file: URLs are not supported; use a local path')
            p = Path(entry.source).resolve(strict=True)
            if not p.is_dir() or digest_tree(p) != entry.digest:
                raise BuildError('Local catalog artifact digest mismatch')
            return p
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.port not in (None,443):
            raise BuildError('Only approved HTTPS catalog archives are supported')
        if u.hostname not in self.allowed_hosts:
            raise PermissionError('Catalog host not allowlisted')
        # Only pinned, limited-size tarballs; TLS and redirect destination checked.
        with urlopen(entry.source, timeout=15) as response:
            actual = urlparse(response.geturl())
            if actual.scheme != 'https' or actual.hostname not in self.allowed_hosts:
                raise BuildError('Unexpected catalog redirect')
            blob = response.read(5 * 1024 * 1024 + 1)
        if len(blob) > 5 * 1024 * 1024: raise BuildError('Catalog archive too large')
        dest = Path(tempfile.mkdtemp(prefix='trusted-catalog-', dir=self.download_root))
        archive = dest / 'archive.tar.gz'
        payload = dest / 'payload'
        payload.mkdir()
        try:
            archive.write_bytes(blob)
            with tarfile.open(archive, 'r:gz') as tf:
                members = tf.getmembers()
                if len(members) > 100 or sum(m.size for m in members) > 10 * 1024 * 1024:
                    raise BuildError('Catalog archive has too many or too large members')
                for member in members:
                    rel = Path(member.name)
                    if (not member.isfile() and not member.isdir()) or rel.is_absolute() or '..' in rel.parts:
                        raise BuildError('Unsafe archive member')
                    if member.isfile():
                        target = payload / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with tf.extractfile(member) as src, target.open('wb') as out:
                            shutil.copyfileobj(src, out)
            if digest_tree(payload) != entry.digest:
                raise BuildError('Downloaded catalog artifact digest mismatch')
            archive.unlink()
            return payload
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise


class AutonomousBuildCoordinator:
    """Durable search->generate->test->review->publish coordinator.

    Each request has exactly one active build. Retried requests reuse the same
    build; a failed candidate is repaired only by a separate bounded call.
    """
    def __init__(self, db_path: str | Path, catalog: TrustedCatalog,
                 agent: CapabilityAgent, builder: SandboxBuilder, tasks=None,
                 *, max_repairs: int = 2, marketplaces: FederatedDiscovery | None = None):
        if max_repairs < 0 or max_repairs > 5: raise ValueError('Invalid repair limit')
        self.db_path = str(db_path)
        self.catalog, self.agent, self.builder, self.tasks = catalog, agent, builder, tasks
        self.max_repairs = max_repairs
        self.marketplaces = marketplaces
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS autonomous_builds (
                id TEXT PRIMARY KEY, capability_id TEXT NOT NULL, kind TEXT NOT NULL,
                requirement TEXT NOT NULL, request_key TEXT UNIQUE NOT NULL,
                state TEXT NOT NULL, build_id TEXT, attempts INTEGER NOT NULL,
                task_id TEXT, last_error TEXT, created REAL NOT NULL, updated REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS marketplace_discovered (
                request_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                provider TEXT NOT NULL, candidate_json TEXT NOT NULL,
                discovered_at REAL NOT NULL,
                PRIMARY KEY(request_id, candidate_id, provider))''')
            db.execute('''CREATE TABLE IF NOT EXISTS marketplace_selections (
                request_id TEXT PRIMARY KEY, candidate_json TEXT NOT NULL,
                selected_at REAL NOT NULL)''')
            AuditLog.install(db)

    def _db(self): return sqlite3.connect(self.db_path, timeout=15)

    def _get(self, request_id: str) -> dict:
        with self._db() as db:
            row = db.execute('SELECT * FROM autonomous_builds WHERE id=?',(request_id,)).fetchone()
        if not row: raise KeyError('Unknown autonomous build')
        return dict(zip(('id','capability_id','kind','requirement','request_key','state',
                         'build_id','attempts','task_id','last_error','created','updated'),row))

    def request(self, capability_id: str, kind: str, requirement: str, *,
                request_key: str, task_id: str | None = None) -> dict:
        if not CAP_ID.fullmatch(capability_id) or kind not in ('skill','mcp'):
            raise ValueError('Invalid capability request')
        if not request_key or len(request_key)>200 or not requirement.strip():
            raise ValueError('Missing request key or requirement')
        if task_id and self.tasks:
            task=self.tasks.get(task_id)
            if not task or capability_id not in task.required_capabilities:
                raise ValueError('Capability not required by task')
        id=uuid4().hex; now=time.time()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT id,capability_id,kind,requirement,task_id FROM autonomous_builds WHERE request_key=?',(request_key,)).fetchone()
            if old:
                if old[1:] != (capability_id,kind,requirement,task_id):
                    raise BuildError('Idempotency key reused for different autonomous request')
                return self._get(old[0])
            db.execute('INSERT INTO autonomous_builds VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (id,capability_id,kind,requirement,request_key,'REQUESTED',None,0,task_id,None,now,now))
            AuditLog.append_in_transaction(db,actor='capability_center',action='autonomy.requested',
                 subject=id,details={'capability_id':capability_id,'kind':kind,'task_id':task_id})
        return self._get(id)

    def _transition(self, id: str, expected: str, state: str, *, build_id=None, error=None, increment=False):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            count=db.execute('''UPDATE autonomous_builds SET state=?,build_id=COALESCE(?,build_id),
                last_error=?,attempts=attempts+?,updated=? WHERE id=? AND state=?''',
                (state,build_id,error[:2000] if error else None,int(increment),time.time(),id,expected)).rowcount
            if count != 1: raise BuildError(f'Conflicting transition {expected}->{state}')
            AuditLog.append_in_transaction(db,actor='capability_center',action='autonomy.'+state.lower(),
                 subject=id,details={'previous':expected,'build_id':build_id,'error':bool(error)})
        return self._get(id)

    def discover_marketplaces(self, request_id: str, *, query: str | None = None, limit: int = 10) -> dict:
        """Read-only federated discovery. Does not download, execute or publish."""
        if self.marketplaces is None:
            raise MarketplaceError('No P0 marketplace adapters configured')
        record = self._get(request_id)
        result = self.marketplaces.search(query or record['requirement'], record['kind'], limit)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            for candidate in result['candidates']:
                db.execute('INSERT OR REPLACE INTO marketplace_discovered VALUES (?,?,?,?,?)',
                    (request_id, candidate.id, candidate.provider,
                     json.dumps(candidate.to_dict(), sort_keys=True), time.time()))
            AuditLog.append_in_transaction(db, actor='capability_center',
                action='marketplace.discovered', subject=request_id,
                details={'providers': result['providers'], 'candidate_count': len(result['candidates']),
                         'errors': list(result['errors'])})
        return result

    def select_market_candidate(self, request_id: str, candidate: MarketCandidate):
        """Opt in to staging one discovered Skill. MCP remains metadata-only."""
        record = self._get(request_id)
        if record['state'] != 'REQUESTED':
            raise BuildError('Marketplace selection requires REQUESTED state')
        if self.marketplaces is None or candidate.provider not in self.marketplaces.providers:
            raise MarketplaceError('Unknown marketplace source')
        if record['kind'] != 'skill' or candidate.kind != 'skill':
            raise MarketplaceError('Only Skill snapshots can be staged from markets')
        provider = self.marketplaces.providers[candidate.provider]
        if provider.kind != 'skill':
            raise MarketplaceError('Invalid provider kind')
        # The candidate itself is data, not authorization to publish or run.
        raw = json.dumps(candidate.to_dict(), sort_keys=True)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            known = db.execute('SELECT candidate_json FROM marketplace_discovered WHERE request_id=? AND candidate_id=? AND provider=?',
                               (request_id, candidate.id, candidate.provider)).fetchone()
            if not known or known[0] != raw:
                raise MarketplaceError('Candidate must match a previously discovered marketplace record')
            old = db.execute('SELECT candidate_json FROM marketplace_selections WHERE request_id=?',
                             (request_id,)).fetchone()
            if old and old[0] != raw:
                raise BuildError('Candidate already selected; use a new request to change source')
            db.execute('INSERT OR IGNORE INTO marketplace_selections VALUES (?,?,?)',
                       (request_id, raw, time.time()))
            AuditLog.append_in_transaction(db, actor='capability_center',
                action='marketplace.selected', subject=request_id,
                details={'id': candidate.id, 'provider': candidate.provider, 'source': candidate.source_url})

    def _market_candidate(self, request_id: str) -> MarketCandidate | None:
        with self._db() as db:
            row = db.execute('SELECT candidate_json FROM marketplace_selections WHERE request_id=?',
                             (request_id,)).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        return MarketCandidate(**value)

    async def prepare(self, id: str) -> dict:
        record=self._get(id)
        if record['state'] not in ('REQUESTED','REPAIR_PENDING'):
            return record  # idempotent, do not regenerate
        state=record['state']
        previous_error=record['last_error']
        record=self._transition(id,state,'PREPARING',increment=True)
        try:
            candidates=self.catalog.search(record['capability_id'],record['kind']) if state=='REQUESTED' else []
            selected = self._market_candidate(id) if state == 'REQUESTED' else None
            if candidates:
                folder=await asyncio.to_thread(self.catalog.materialize,candidates[0],self.builder.sandbox.digest_tree)
            elif selected is not None:
                folder=await self.marketplaces.stage_async(selected)
            else:
                req=record['requirement']
                if previous_error: req+='\nPrevious sandbox failure: '+previous_error[:2000]
                folder=await self.agent.propose(record['capability_id'],record['kind'],req)
            build_id=self.builder.propose(record['capability_id'],record['kind'],folder,
                                           idempotency_key=f'autonomy:{id}:{record["attempts"]}')
            self._transition(id,'PREPARING','VALIDATING',build_id=build_id)
            result=await self.builder.validate(build_id)
            if result['status']=='REVIEW':
                return self._transition(id,'VALIDATING','AWAITING_APPROVAL')
            error=(result['evidence'] or '')[-2000:]
            return self._failed_validation(id,error)
        except Exception as exc:
            # If external execution interrupted, fail closed; operator can reconcile.
            current=self._get(id)
            if current['state'] in ('PREPARING','VALIDATING'):
                self._transition(id,current['state'],'BLOCKED',error=str(exc))
            raise

    def _failed_validation(self,id,error):
        record=self._get(id)
        target='REPAIR_PENDING' if record['attempts'] <= self.max_repairs else 'FAILED'
        return self._transition(id,'VALIDATING',target,error=error)

    def recover(self) -> int:
        """An interrupted search/build/test is ambiguous: operator review required."""
        n=0
        self.builder.store.recover_interrupted()
        for row in self.list():
            if row['state'] in ('PREPARING','VALIDATING'):
                self._transition(row['id'],row['state'],'BLOCKED',error='Coordinator restarted during preparation/validation')
                n+=1
        return n

    def list(self) -> list[dict]:
        with self._db() as db:
            ids=[r[0] for r in db.execute('SELECT id FROM autonomous_builds ORDER BY created,id')]
        return [self._get(id) for id in ids]

    def approve_and_publish(self,id: str, *, approver: str, approve: bool) -> dict:
        record=self._get(id)
        if record['state']=='PUBLISHED': return record
        if record['state']!='AWAITING_APPROVAL': raise PermissionError('Build not ready for human review')
        if not approver or not approver.strip(): raise ValueError('Named human approver required')
        job=self.builder.store.get(record['build_id'])
        if job['status']=='PUBLISHED':
            # Crash between durable artifact publication and coordinator status update.
            # Verify bytes and restore registry before reconciling; do not re-publish.
            self.builder.publish(job['id'])
            if not approve:
                raise BuildError('Cannot reject an artifact already published; perform revocation')
        else:
            if job['status']=='REVIEW':
                self.builder.approve(job['id'],approver,approved=approve)
                job=self.builder.store.get(job['id'])
            if not approve:
                if job['status'] != 'REJECTED':
                    raise BuildError('Cannot reject an already approved artifact')
                return self._transition(id,'AWAITING_APPROVAL','REJECTED')
            if job['status']=='REJECTED': raise PermissionError('Cannot publish rejected build')
            if job['status']!='APPROVED': raise BuildError('Inconsistent build review state')
            self.builder.publish(job['id'])
        self._transition(id,'AWAITING_APPROVAL','PUBLISHED')
        if self.tasks and record['task_id']:
            task=self.tasks.get(record['task_id'])
            if task and task.status == TaskStatus.WAITING_APPROVAL:
                if not self.builder.registry.missing(task.required_capabilities):
                    self.tasks.approve(task.id)
        return self._get(id)
