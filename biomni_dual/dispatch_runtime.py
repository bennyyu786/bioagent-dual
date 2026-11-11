"""Resource-aware dispatch protocol. Control plane records are not execution proof.

An offer and a task claim are different state transitions. A worker result remains
pending independent review; no untrusted worker may complete TaskCenter directly.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from uuid import uuid4

from .models import TaskStatus
from .runtime_relations import RESOURCES, resources, RuntimeRelationStore


class DispatchConflict(ValueError):
    pass


@dataclass(frozen=True)
class WorkerCandidate:
    worker: str
    available: dict
    capabilities: tuple[str, ...]
    load: float


class ResourceAwareDispatcher:
    def __init__(self, ledger: RuntimeRelationStore, tasks):
        self.ledger = ledger
        self.tasks = tasks
        with self.ledger.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS worker_skills(
              workspace TEXT NOT NULL, worker TEXT NOT NULL, capability TEXT NOT NULL,
              PRIMARY KEY(workspace,worker,capability));
            CREATE TABLE IF NOT EXISTS dispatch_jobs(
              id TEXT PRIMARY KEY, workspace TEXT NOT NULL, task TEXT NOT NULL,
              worker TEXT NOT NULL, reservation TEXT NOT NULL UNIQUE,
              request_key TEXT NOT NULL, state TEXT NOT NULL,
              required TEXT NOT NULL, submitted REAL NOT NULL,
              claimed REAL, result TEXT, reported_state TEXT, result_hash TEXT, received REAL,
              reviewer TEXT, evidence_ref TEXT, reviewed REAL,
              UNIQUE(workspace,request_key));
            CREATE INDEX IF NOT EXISTS dispatch_jobs_worker ON dispatch_jobs(workspace,worker,state);
            ''')

    def set_capabilities(self, ws, worker, ids):
        if not isinstance(ids, list) or len(ids)>100 or any(not isinstance(x,str) or not 1<=len(x)<=128 for x in ids):
            raise ValueError('Invalid capabilities')
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM worker_capacity WHERE workspace=? AND worker=?',(ws,worker)).fetchone():
                raise ValueError('Worker requires configured capacity')
            db.execute('DELETE FROM worker_skills WHERE workspace=? AND worker=?',(ws,worker))
            db.executemany('INSERT INTO worker_skills VALUES(?,?,?)',[(ws,worker,c) for c in sorted(set(ids))])
            self.ledger._log(db,ws,'worker_capabilities_updated',worker)

    def candidates(self, ws, required, amount, registered):
        req=resources(amount)
        out=[]
        with self.ledger.db() as db:
            for worker,*v in db.execute('SELECT worker,cpu_milli,memory_mb,gpu_units FROM worker_capacity WHERE workspace=? ORDER BY worker',(ws,)):
                if worker not in registered:continue
                caps={r[0] for r in db.execute('SELECT capability FROM worker_skills WHERE workspace=? AND worker=?',(ws,worker))}
                if not set(required).issubset(caps):continue
                capacity=dict(zip(RESOURCES,v));used=self.ledger._used(db,ws,worker)
                available={k:capacity[k]-used[k] for k in RESOURCES}
                if any(available[k]<req[k] for k in RESOURCES):continue
                # Prefer the worker with the largest free fraction; stable tie-break.
                load=max((used[k]/capacity[k] for k in RESOURCES if capacity[k]>0),default=0.0)
                out.append(WorkerCandidate(worker,available,tuple(sorted(caps)),load))
        return sorted(out,key=lambda w:(w.load,w.worker))

    def plan_and_offer(self, ws, task_id, request_id, amount, registered, worker=None):
        req=resources(amount)
        task=self.tasks.get(task_id)
        if task is None or task.tenant!=ws or task.status != TaskStatus.PENDING:
            raise DispatchConflict('Task unavailable for dispatch')
        if len(task.required_capabilities)!=1:
            raise DispatchConflict('Exactly one approved execution or composite capability is required')
        with self.ledger.db() as db:
            approved={r[0] for r in db.execute("SELECT capability FROM capability_evidence WHERE workspace=? AND lifecycle='approved'",(ws,))}
        if not set(task.required_capabilities).issubset(approved):
            raise DispatchConflict('Task capability is not approved for execution')
        if any(self.tasks.get(d) is None or self.tasks.get(d).status!=TaskStatus.COMPLETED for d in task.depends_on):
            raise DispatchConflict('Task dependencies incomplete')
        with self.ledger.db() as db:
            old=db.execute('SELECT id,task,worker,required,reservation FROM dispatch_jobs WHERE workspace=? AND request_key=?',(ws,request_id)).fetchone()
            if old:
                if old[1]!=task_id or json.loads(old[3])!=req or (worker is not None and old[2]!=worker):
                    raise DispatchConflict('Conflicting dispatch idempotency key')
                return {'id':old[0],'task_id':task_id,'worker':old[2],'reservation_id':old[4], 'state':'existing'}
        options=self.candidates(ws,task.required_capabilities,req,registered)
        if worker is not None:options=[x for x in options if x.worker==worker]
        if not options:raise DispatchConflict('No eligible worker with verified resource allowance and capabilities')
        # Capacity and uniqueness are checked again under a single BEGIN IMMEDIATE.
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT id,task,worker,required,reservation FROM dispatch_jobs WHERE workspace=? AND request_key=?',(ws,request_id)).fetchone()
            if old:
                if old[1]!=task_id or json.loads(old[3])!=req or (worker is not None and old[2]!=worker):
                    raise DispatchConflict('Conflicting dispatch idempotency key')
                return {'id':old[0],'task_id':task_id,'worker':old[2],'reservation_id':old[4],'state':'existing'}
            if db.execute("SELECT 1 FROM reservations WHERE workspace=? AND task=? AND state IN ('reserved','claimed','uncertain')",(ws,task_id)).fetchone():
                raise DispatchConflict('Task already holds a reservation')
            for candidate in options:
                capacity=db.execute('SELECT cpu_milli,memory_mb,gpu_units FROM worker_capacity WHERE workspace=? AND worker=?',(ws,candidate.worker)).fetchone()
                if capacity is None:continue
                used=self.ledger._used(db,ws,candidate.worker)
                if any(used[k]+req[k]>capacity[i] for i,k in enumerate(RESOURCES)):continue
                rid=uuid4().hex;did=uuid4().hex;now=time.time()
                db.execute('''INSERT INTO reservations(id,workspace,task,worker,request_key,cpu_milli,memory_mb,gpu_units,state,created)
                              VALUES(?,?,?,?,?,?,?,?,?,?)''',
                           (rid,ws,task_id,candidate.worker,'dispatch:'+request_id,*[req[k] for k in RESOURCES],'reserved',now))
                db.execute('''INSERT INTO dispatch_jobs(id,workspace,task,worker,reservation,request_key,state,required,submitted)
                              VALUES(?,?,?,?,?,?,?,?,?)''',
                           (did,ws,task_id,candidate.worker,rid,request_id,'offered',json.dumps(req,sort_keys=True),now))
                self.ledger._log(db,ws,'dispatch_offered',did)
                return {'id':did,'task_id':task_id,'worker':candidate.worker,'reservation_id':rid,'state':'offered'}
        raise DispatchConflict('Capacity exhausted by concurrent reservations')

    def list(self,ws,limit=100):
        with self.ledger.db() as db:
            rows=db.execute('''SELECT id,task,worker,reservation,state,submitted,claimed,received,result_hash,evidence_ref,reviewer
                 FROM dispatch_jobs WHERE workspace=? ORDER BY submitted DESC,id DESC LIMIT ?''',(ws,min(max(int(limit),1),500))).fetchall()
        keys=('id','task_id','worker','reservation_id','state','submitted','claimed','received','result_hash','evidence_ref','reviewer')
        return [dict(zip(keys,row)) for row in rows]

    def cancel_offer(self,ws,dispatch_id):
        """Only never-claimed offers can release resource reservations safely."""
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('''SELECT d.reservation,d.state,r.state FROM dispatch_jobs d JOIN reservations r ON r.id=d.reservation
                WHERE d.workspace=? AND d.id=?''',(ws,dispatch_id)).fetchone()
            if not row or row[1]!='offered' or row[2]!='reserved':raise DispatchConflict('Only unclaimed offers can be cancelled')
            db.execute("UPDATE dispatch_jobs SET state='cancelled' WHERE id=?",(dispatch_id,))
            db.execute("UPDATE reservations SET state='reconciled',finished=? WHERE id=? AND state='reserved'",(time.time(),row[0]))
            self.ledger._log(db,ws,'dispatch_offer_cancelled',dispatch_id)
        return {'state':'cancelled'}

    def validate_and_review(self,ws,dispatch_id,reviewer,validator):
        """Server-installed domain validator, not worker-provided success claims."""
        e=self.evidence(ws,dispatch_id)
        if e['state']!='review_required':raise DispatchConflict('Not awaiting verification')
        task=self.tasks.get(e['task_id'])
        if task is None or task.tenant!=ws:raise DispatchConflict('Unknown task')
        decision=validator(task,e['result'])
        if (not isinstance(decision,tuple) or len(decision)!=2 or
                type(decision[0]) is not bool or not isinstance(decision[1],str) or
                not 8<=len(decision[1])<=256):
            raise ValueError('Validator must return (bool, evidence reference)')
        return self.review(ws,dispatch_id,reviewer,accepted=decision[0],evidence_ref=decision[1],expected_sha256=e['sha256'])

    def evidence(self,ws,dispatch_id):
        with self.ledger.db() as db:
            row=db.execute('SELECT task,worker,state,result,result_hash,reported_state FROM dispatch_jobs WHERE workspace=? AND id=?',
                           (ws,dispatch_id)).fetchone()
        if not row:raise DispatchConflict('Dispatch does not exist')
        return dict(zip(('task_id','worker','state','result','sha256','worker_state'),row))

    def offer_for_worker(self,ws,worker):
        with self.ledger.db() as db:
            row=db.execute("SELECT id,task FROM dispatch_jobs WHERE workspace=? AND worker=? AND state='offered' ORDER BY submitted,id LIMIT 1",(ws,worker)).fetchone()
        return {'id':row[0],'task_id':row[1]} if row else None

    def claim(self,ws,worker,dispatch_id,ttl=300):
        with self.ledger.db() as db:
            row=db.execute('SELECT reservation,task,state FROM dispatch_jobs WHERE id=? AND workspace=? AND worker=?',(dispatch_id,ws,worker)).fetchone()
            if not row or row[2]!='offered':raise DispatchConflict('Offer unavailable')
        task=self.tasks.get(row[1])
        if task is None or task.tenant!=ws or task.status!=TaskStatus.PENDING:
            raise DispatchConflict('Task not pending')
        # First durable lease; if TaskCenter claim fails, quarantine, never auto reassign.
        lease=self.ledger.claim(ws,worker,row[0],ttl)
        try:
            claimed_task=self.tasks.claim(row[1])
        except Exception:
            with self.ledger.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE reservations SET state='uncertain' WHERE id=? AND state='claimed'",(row[0],))
                db.execute("UPDATE dispatch_jobs SET state='uncertain' WHERE id=?",(dispatch_id,))
                self.ledger._log(db,ws,'dispatch_claim_uncertain',dispatch_id)
            raise DispatchConflict('Task claim failed; lease quarantined')
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE dispatch_jobs SET state='running',claimed=? WHERE id=? AND state='offered'",(time.time(),dispatch_id))
            self.ledger._log(db,ws,'dispatch_running',dispatch_id)
        return {'id':dispatch_id,'task_id':task.id,'prompt':claimed_task.prompt,
                'capabilities':list(claimed_task.required_capabilities), 'lease_expires':lease['expires'],
                'resources':lease['resources']}

    def report(self,ws,worker,dispatch_id,state,result):
        if state not in ('completed','failed') or not isinstance(result,str) or len(result)>32000:
            raise ValueError('Invalid report')
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('''SELECT d.state,r.state,r.expires FROM dispatch_jobs d JOIN reservations r ON r.id=d.reservation
                  WHERE d.id=? AND d.workspace=? AND d.worker=?''',(dispatch_id,ws,worker)).fetchone()
            if not row or row[0]!='running' or row[1]!='claimed':raise DispatchConflict('Dispatch is not running')
            if row[2]<=time.time():
                db.execute("UPDATE dispatch_jobs SET state='uncertain' WHERE id=?",(dispatch_id,))
                db.execute("UPDATE reservations SET state='uncertain' WHERE id=(SELECT reservation FROM dispatch_jobs WHERE id=?)",(dispatch_id,))
                self.ledger._log(db,ws,'dispatch_expired',dispatch_id)
                return {'state':'uncertain'}
            fingerprint=hashlib.sha256(result.encode()).hexdigest()
            db.execute("UPDATE reservations SET state='reported' WHERE id=(SELECT reservation FROM dispatch_jobs WHERE id=?)",(dispatch_id,))
            db.execute('UPDATE dispatch_jobs SET state=?,result=?,reported_state=?,result_hash=?,received=? WHERE id=?',
                       ('review_required',result,state,fingerprint,time.time(),dispatch_id))
            self.ledger._log(db,ws,'dispatch_reported',dispatch_id)
            return {'state':'review_required','sha256':fingerprint}

    def finalize_delivery(self, ws, dispatch_id):
        """Automatically close a durably reported job, without claiming scientific validation.

        Only a received report from the authenticated, lease-holding worker qualifies.
        The result hash is checked again before crossing the TaskCenter store boundary.
        A mismatched state or cross-store failure is quarantined instead of retried.
        """
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("""SELECT d.task,d.worker,d.reservation,d.state,d.result,
                    d.result_hash,d.reported_state,r.state FROM dispatch_jobs d
                    JOIN reservations r ON r.id=d.reservation
                    WHERE d.id=? AND d.workspace=?""",(dispatch_id,ws)).fetchone()
            if not row:raise DispatchConflict('Unknown dispatch')
            task_id,worker,rid,state,result,fingerprint,reported,reservation_state=row
            if state in ('delivered','failed'):
                return {'state':state,'task_id':task_id,'idempotent':True}
            if state!='review_required' or reservation_state!='reported':
                raise DispatchConflict('Dispatch is not ready for automatic delivery')
            if reported not in ('completed','failed') or not isinstance(result,str) or not fingerprint or hashlib.sha256(result.encode()).hexdigest()!=fingerprint:
                db.execute("UPDATE dispatch_jobs SET state='uncertain' WHERE id=?",(dispatch_id,))
                db.execute("UPDATE reservations SET state='uncertain' WHERE id=?",(rid,))
                self.ledger._log(db,ws,'dispatch_delivery_invalid',dispatch_id)
                return {'state':'uncertain','task_id':task_id}
            task=self.tasks.get(task_id)
            if task is None or task.tenant!=ws or task.status!=TaskStatus.RUNNING:
                db.execute("UPDATE dispatch_jobs SET state='uncertain' WHERE id=?",(dispatch_id,))
                db.execute("UPDATE reservations SET state='uncertain' WHERE id=?",(rid,))
                self.ledger._log(db,ws,'dispatch_delivery_inconsistent',dispatch_id)
                return {'state':'uncertain','task_id':task_id}
            # Empty or status-only outputs do not count as successful answers.
            valid_output=bool(result.strip()) and result.strip().lower() not in (
                'ok','done','success','completed','true','null','none','n/a')
            success=(reported=='completed' and valid_output)
            db.execute("UPDATE dispatch_jobs SET state='finalizing',reviewer=?,evidence_ref=?,reviewed=? WHERE id=?",
                       ('auto:delivery','delivery:sha256:'+fingerprint,time.time(),dispatch_id))
            self.ledger._log(db,ws,'dispatch_delivery_started',dispatch_id)
        try:
            if success:
                self.tasks.finish(task_id,result=result)
            else:
                self.tasks.finish(task_id,error=('Worker reported failure: ' if reported=='failed' else 'Missing usable worker result: ')+result[:1000],retryable=False)
            self.ledger.finish(ws,worker,rid,'completed' if success else 'failed','delivery:'+fingerprint[:64])
        except Exception:
            with self.ledger.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE dispatch_jobs SET state='uncertain' WHERE id=?",(dispatch_id,))
                db.execute("UPDATE reservations SET state='uncertain' WHERE id=? AND state IN ('reserved','claimed','reported')",(rid,))
                self.ledger._log(db,ws,'dispatch_delivery_uncertain',dispatch_id)
            raise DispatchConflict('Automatic delivery transition uncertain; reconciliation required')
        final='delivered' if success else 'failed'
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE dispatch_jobs SET state=? WHERE id=? AND state='finalizing'",(final,dispatch_id))
            self.ledger._log(db,ws,'dispatch_'+final,dispatch_id)
        return {'state':final,'task_id':task_id,'sha256':fingerprint,
                'note':'Execution delivered; not independently verified'}

    def review(self,ws,dispatch_id,reviewer,*,accepted,evidence_ref,expected_sha256):
        if not isinstance(accepted,bool) or not isinstance(evidence_ref,str) or not 8<=len(evidence_ref)<=256:
            raise ValueError('Independent review evidence is required')
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT task,worker,reservation,state,result,result_hash,reported_state FROM dispatch_jobs WHERE id=? AND workspace=?',(dispatch_id,ws)).fetchone()
            if not row or row[3]!='review_required':raise DispatchConflict('Result not awaiting review')
            if row[5]!=expected_sha256:raise DispatchConflict('Result hash does not match reviewed artifact')
            if accepted and row[6]!='completed':raise DispatchConflict('Failed worker report cannot be accepted')
            if self.tasks.get(row[0]).status!=TaskStatus.RUNNING:raise DispatchConflict('Task is not running')
            # Cross-database TaskCenter cannot commit atomically with this ledger.
            # Claim reviewing first; a crash stays quarantined for human reconciliation.
            db.execute("UPDATE dispatch_jobs SET state='reviewing',reviewer=?,evidence_ref=?,reviewed=? WHERE id=?",
                       (reviewer,evidence_ref,time.time(),dispatch_id))
            self.ledger._log(db,ws,'dispatch_review_started',dispatch_id)
        try:
            if accepted:self.tasks.finish(row[0],result=row[4])
            else:self.tasks.finish(row[0],error='Independent review rejected',retryable=False)
            self.ledger.finish(ws,row[1],row[2],'completed' if accepted else 'failed','review:'+evidence_ref[:150])
        except Exception:
            # Not retryable automatically: task and reservation transitions may differ.
            with self.ledger.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE dispatch_jobs SET state='uncertain' WHERE id=?",(dispatch_id,))
                db.execute("UPDATE reservations SET state='uncertain' WHERE id=? AND state IN ('reserved','claimed')",(row[2],))
                self.ledger._log(db,ws,'dispatch_review_uncertain',dispatch_id)
            raise DispatchConflict('Cross-store review incomplete; manual reconciliation required')
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE dispatch_jobs SET state=? WHERE id=? AND state='reviewing'",
                       ('verified' if accepted else 'rejected',dispatch_id))
            self.ledger._log(db,ws,'dispatch_reviewed',dispatch_id)
        return {'state':'verified' if accepted else 'rejected','task_id':row[0]}

    def quarantine_expired(self,ws):
        self.ledger.quarantine_expired(ws)
        with self.ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('''SELECT d.id FROM dispatch_jobs d JOIN reservations r ON r.id=d.reservation
                      WHERE d.workspace=? AND r.state='uncertain' AND d.state NOT IN ('verified','rejected','uncertain')''',(ws,)).fetchall()
            for (did,) in rows:
                db.execute("UPDATE dispatch_jobs SET state='uncertain' WHERE id=?",(did,))
                self.ledger._log(db,ws,'dispatch_uncertain',did)
        return len(rows)
