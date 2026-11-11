"""Auditable tenant-scoped reservation ledger and verified runtime graph evidence.

Reservations are CONTROL-PLANE INTENTS, not proof of execution.  Worker claims
and reports are explicit authenticated evidence.  Expired claims are quarantined
and continue consuming capacity until an administrator reconciles them.
"""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

RESOURCES = ('cpu_milli', 'memory_mb', 'gpu_units')
MAX_RESOURCE = {'cpu_milli': 1_000_000, 'memory_mb': 10_000_000, 'gpu_units': 256}


def resources(raw, *, allow_zero=False):
    if not isinstance(raw, dict) or set(raw) != set(RESOURCES):
        raise ValueError('Specify cpu_milli, memory_mb, gpu_units')
    for k in RESOURCES:
        v = raw[k]
        if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= MAX_RESOURCE[k]):
            raise ValueError('Invalid resource quantity')
    if not allow_zero and not any(raw.values()):
        raise ValueError('Resource request must not be empty')
    return {k: raw[k] for k in RESOURCES}


class RuntimeRelationStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS worker_capacity(
                workspace TEXT NOT NULL, worker TEXT NOT NULL, cpu_milli INTEGER NOT NULL,
                memory_mb INTEGER NOT NULL, gpu_units INTEGER NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(workspace,worker));
            CREATE TABLE IF NOT EXISTS reservations(
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, task TEXT NOT NULL,
                worker TEXT NOT NULL, request_key TEXT NOT NULL,
                cpu_milli INTEGER NOT NULL, memory_mb INTEGER NOT NULL,gpu_units INTEGER NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, claimed REAL,
                expires REAL, finished REAL, receipt TEXT,
                UNIQUE(workspace,request_key));
            CREATE INDEX IF NOT EXISTS res_worker ON reservations(workspace,worker,state);
            DROP INDEX IF EXISTS one_active_reservation_per_task;
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_reservation_per_task
              ON reservations(workspace,task) WHERE state IN ('reserved','claimed','reported','uncertain');
            CREATE TABLE IF NOT EXISTS capability_evidence(
                workspace TEXT NOT NULL, capability TEXT NOT NULL,
                lifecycle TEXT NOT NULL, evidence_ref TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(workspace,capability));
            CREATE TABLE IF NOT EXISTS repair_relations(
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, task TEXT NOT NULL,
                repair_task TEXT NOT NULL, state TEXT NOT NULL,
                evidence_ref TEXT NOT NULL, created REAL NOT NULL,
                UNIQUE(workspace,task,repair_task));
            CREATE TABLE IF NOT EXISTS relation_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL,
                kind TEXT NOT NULL, subject TEXT NOT NULL, created REAL NOT NULL);
            ''')

    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA busy_timeout=15000')
        return db

    @staticmethod
    def _log(db, ws, kind, subject):
        db.execute('INSERT INTO relation_events(workspace,kind,subject,created) VALUES(?,?,?,?)',
                   (ws,kind,subject,time.time()))

    def configure_worker(self, ws, worker, capacity):
        cap = resources(capacity)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            used = self._used(db,ws,worker)
            if any(used[k] > cap[k] for k in RESOURCES):
                raise ValueError('Cannot shrink capacity below active reservations')
            db.execute('''INSERT INTO worker_capacity VALUES(?,?,?,?,?,?)
                ON CONFLICT(workspace,worker) DO UPDATE SET cpu_milli=excluded.cpu_milli,
                memory_mb=excluded.memory_mb,gpu_units=excluded.gpu_units,updated=excluded.updated''',
                (ws,worker,*[cap[k] for k in RESOURCES],time.time()))
            self._log(db,ws,'capacity_configured',worker)

    @staticmethod
    def _used(db,ws,worker):
        row = db.execute('''SELECT COALESCE(SUM(cpu_milli),0),COALESCE(SUM(memory_mb),0),
          COALESCE(SUM(gpu_units),0) FROM reservations
          WHERE workspace=? AND worker=? AND state IN ('reserved','claimed','reported','uncertain')''',
          (ws,worker)).fetchone()
        return dict(zip(RESOURCES,row))

    def reserve(self, ws, task, worker, request_key, amount):
        request = resources(amount)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id,task,worker,cpu_milli,memory_mb,gpu_units FROM reservations WHERE workspace=? AND request_key=?',
                                  (ws,request_key)).fetchone()
            if existing:
                if existing[1:] != (task,worker,*[request[k] for k in RESOURCES]):
                    raise ValueError('Conflicting idempotency key')
                return existing[0]
            if db.execute("SELECT 1 FROM reservations WHERE workspace=? AND task=? AND state IN ('reserved','claimed','reported','uncertain')", (ws,task)).fetchone():
                raise ValueError('Task already has an active or uncertain reservation')
            capacity = db.execute('SELECT cpu_milli,memory_mb,gpu_units FROM worker_capacity WHERE workspace=? AND worker=?',
                                  (ws,worker)).fetchone()
            if capacity is None:
                raise ValueError('Worker has no configured capacity')
            used = self._used(db,ws,worker)
            if any(used[k]+request[k] > capacity[i] for i,k in enumerate(RESOURCES)):
                raise ValueError('Insufficient resources')
            rid = uuid4().hex
            db.execute('INSERT INTO reservations(id,workspace,task,worker,request_key,cpu_milli,memory_mb,gpu_units,state,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (rid,ws,task,worker,request_key,*[request[k] for k in RESOURCES],'reserved',time.time()))
            self._log(db,ws,'reservation_created',rid)
            return rid

    def claim(self,ws,worker,rid,ttl=300):
        if isinstance(ttl,bool) or not isinstance(ttl,int) or not 10 <= ttl <= 3600:
            raise ValueError('Invalid lease TTL')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,task,cpu_milli,memory_mb,gpu_units FROM reservations WHERE id=? AND workspace=? AND worker=?',
                             (rid,ws,worker)).fetchone()
            if not row or row[0] != 'reserved':
                raise ValueError('Reservation unavailable or already claimed')
            now=time.time()
            db.execute("UPDATE reservations SET state='claimed',claimed=?,expires=? WHERE id=?",
                       (now,now+ttl,rid))
            self._log(db,ws,'reservation_claimed',rid)
            return {'id':rid,'task_id':row[1],'resources':dict(zip(RESOURCES,row[2:])), 'expires':now+ttl}

    def finish(self,ws,worker,rid,state,receipt=''):
        if state not in ('completed','failed') or not isinstance(receipt,str) or len(receipt)>2000:
            raise ValueError('Invalid completion')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT state,expires FROM reservations WHERE id=? AND workspace=? AND worker=?',
                           (rid,ws,worker)).fetchone()
            if not row or row[0] not in ('claimed','reported'):
                raise ValueError('Only claimed or reported reservations may be completed')
            if row[0]=='claimed' and row[1]<=time.time():
                db.execute("UPDATE reservations SET state='uncertain' WHERE id=?",(rid,))
                self._log(db,ws,'reservation_uncertain',rid)
                expired = True
            else:
                db.execute('UPDATE reservations SET state=?,finished=?,receipt=? WHERE id=?',
                           (state,time.time(),receipt,rid))
                self._log(db,ws,'reservation_'+state,rid)
                expired = False
        if expired:
            raise ValueError('Lease expired; reconcile before release')

    def quarantine_expired(self,ws):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT id FROM reservations WHERE workspace=? AND state='claimed' AND expires<=?",(ws,time.time())).fetchall()
            for (rid,) in rows:
                db.execute("UPDATE reservations SET state='uncertain' WHERE id=?",(rid,))
                self._log(db,ws,'reservation_uncertain',rid)
        return len(rows)

    def reconcile(self,ws,rid,*,verified_no_side_effect=False):
        if verified_no_side_effect is not True:
            raise ValueError('Verified external-side-effect clearance is required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state=db.execute('SELECT state FROM reservations WHERE workspace=? AND id=?',(ws,rid)).fetchone()
            if not state or state[0] != 'uncertain':
                raise ValueError('Only uncertain reservations can be reconciled')
            db.execute("UPDATE reservations SET state='reconciled',finished=? WHERE id=?",(time.time(),rid))
            self._log(db,ws,'reservation_reconciled',rid)

    def capability(self,ws,cid,lifecycle,evidence_ref):
        if lifecycle not in ('discovered','sandbox_verified','approved','retired'):
            raise ValueError('Unknown lifecycle')
        if not isinstance(evidence_ref,str) or not 8<=len(evidence_ref)<=256:
            raise ValueError('Evidence reference required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO capability_evidence VALUES(?,?,?,?,?) ON CONFLICT(workspace,capability)
                DO UPDATE SET lifecycle=excluded.lifecycle,evidence_ref=excluded.evidence_ref,updated=excluded.updated''',
                (ws,cid,lifecycle,evidence_ref,time.time()))
            self._log(db,ws,'capability_'+lifecycle,cid)

    def link_repair(self,ws,task,repair_task,evidence_ref):
        if task==repair_task:raise ValueError('Self-link forbidden')
        if not isinstance(evidence_ref,str) or not 8<=len(evidence_ref)<=256:raise ValueError('Evidence required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO repair_relations VALUES(?,?,?,?,?,?,?)',
                       (uuid4().hex,ws,task,repair_task,'proposed',evidence_ref,time.time()))
            self._log(db,ws,'repair_link',task)

    def snapshot(self,ws):
        with self.db() as db:
            caps=[]
            for worker,*vals in db.execute('SELECT worker,cpu_milli,memory_mb,gpu_units FROM worker_capacity WHERE workspace=? ORDER BY worker',(ws,)):
                total=dict(zip(RESOURCES,vals)); used=self._used(db,ws,worker)
                caps.append({'worker':worker,'capacity':total,'reserved':used,
                             'available':{k:total[k]-used[k] for k in RESOURCES}})
            rows=db.execute('''SELECT id,task,worker,state,cpu_milli,memory_mb,gpu_units,created,expires
                               FROM reservations WHERE workspace=? ORDER BY created,id''',(ws,)).fetchall()
            assignments=[{'id':r[0],'task':r[1],'worker':r[2],'state':r[3],
                          'resources':dict(zip(RESOURCES,r[4:7])),'created':r[7],'expires':r[8]} for r in rows]
            abilities=[{'id':r[0],'lifecycle':r[1],'evidence_ref':r[2]} for r in db.execute(
                'SELECT capability,lifecycle,evidence_ref FROM capability_evidence WHERE workspace=? ORDER BY capability',(ws,))]
            repairs=[{'task':r[0],'repair_task':r[1],'state':r[2],'evidence_ref':r[3]} for r in db.execute(
                'SELECT task,repair_task,state,evidence_ref FROM repair_relations WHERE workspace=? ORDER BY task,repair_task',(ws,))]
        return {'capacity':caps,'assignments':assignments,'capabilities':abilities,'repairs':repairs,
                'semantics':'Reservations are intents; claimed indicates worker acknowledgment, not verified task execution.'}
