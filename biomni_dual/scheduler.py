from __future__ import annotations
"""Durable SQLite task DAG + retry/approval/fairness scheduling."""
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
from .models import Task, TaskStatus
from .reliability import AuditLog

class TaskCenter:
    def __init__(self, db_path: str | Path = 'biomni_dual.db'):
        self.db_path = str(db_path)
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, payload TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS idempotency (
              key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task_id TEXT NOT NULL)''')
            AuditLog.install(db)
            db.execute('''CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, ts REAL, event TEXT, details TEXT)''')

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=15)

    def _save(self, task: Task, event: str, details: str = '') -> None:
        task.updated_at = time.time()
        data = asdict(task)
        data['status'] = task.status.value
        with self._connect() as db:
            db.execute('INSERT OR REPLACE INTO tasks (id, payload) VALUES (?,?)', (task.id, json.dumps(data, ensure_ascii=False)))
            db.execute('INSERT INTO events (task_id,ts,event,details) VALUES (?,?,?,?)', (task.id, time.time(), event, details))
            AuditLog.append_in_transaction(db,actor='task_center',action=event,subject=task.id,details={'status':task.status.value,'attempts':task.attempts})

    def get(self, id: str) -> Task | None:
        with self._connect() as db:
            row = db.execute('SELECT payload FROM tasks WHERE id=?', (id,)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        data['status'] = TaskStatus(data['status'])
        return Task(**data)

    def list(self) -> list[Task]:
        with self._connect() as db:
            ids = [x[0] for x in db.execute('SELECT id FROM tasks')]
        return [self.get(i) for i in ids]

    def lifecycle_events(self, task_id: str, limit: int = 100) -> list[dict]:
        """Read task-local state transitions; authorization belongs to caller."""
        with self._connect() as db:
            rows=db.execute('SELECT id,ts,event FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?',
                            (task_id,max(1,min(100,int(limit))))).fetchall()
        return [{'id':r[0], 'created':r[1], 'kind':'task_'+r[2],
                 'subject':task_id, 'source':'task_center'} for r in reversed(rows)]

    def submit(self, prompt: str, *, depends_on=None, required_capabilities=None,
               priority=0, tenant='default', max_attempts=2, task_id=None, idempotency_key=None) -> Task:
        import hashlib
        fingerprint = hashlib.sha256(json.dumps({'prompt':prompt,'depends_on':depends_on or [],
            'required_capabilities':required_capabilities or [],'priority':priority,
            'tenant':tenant,'max_attempts':max_attempts},sort_keys=True).encode()).hexdigest()
        if idempotency_key is not None:
            if not isinstance(idempotency_key,str) or not 1 <= len(idempotency_key) <= 200:
                raise ValueError('Invalid idempotency key')
            with self._connect() as db:
                old=db.execute('SELECT fingerprint,task_id FROM idempotency WHERE key=?',(idempotency_key,)).fetchone()
            if old:
                if old[0]!=fingerprint: raise ValueError('Idempotency key reused with different payload')
                return self.get(old[1])
        id = task_id or str(uuid4())
        if self.get(id):
            raise ValueError('Duplicate task ID')
        deps = list(depends_on or [])
        if id in deps:
            raise ValueError('Task cannot depend on itself')
        if any(self.get(dep) is None for dep in deps):
            raise ValueError('Dependencies must be submitted first')
        task = Task(id=id, prompt=prompt, depends_on=deps,
                    required_capabilities=list(required_capabilities or []),
                    priority=priority, tenant=tenant, max_attempts=max_attempts,
                    created_at=time.time())
        if idempotency_key is None:
            self._save(task, 'submitted')
        else:
            # Atomic claim + task creation under SQLite's single-writer lock.
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                old=db.execute('SELECT fingerprint,task_id FROM idempotency WHERE key=?',(idempotency_key,)).fetchone()
                if old:
                    if old[0]!=fingerprint: raise ValueError('Idempotency key reused with different payload')
                    return self.get(old[1])
                db.execute('INSERT INTO idempotency VALUES (?,?,?)',(idempotency_key,fingerprint,id))
                data=asdict(task);data['status']=task.status.value
                db.execute('INSERT INTO tasks VALUES (?,?)',(id,json.dumps(data,ensure_ascii=False)))
                db.execute('INSERT INTO events(task_id,ts,event,details) VALUES (?,?,?,?)',(id,time.time(),'submitted',''))
                AuditLog.append_in_transaction(db,actor='task_center',action='submitted',subject=id,
                    details={'status':task.status.value,'idempotency_key_sha256':hashlib.sha256(idempotency_key.encode()).hexdigest()})
        return task

    def bind_approved_execution(self, task_id: str, tenant: str, capability: str) -> Task:
        """Bind a preapproved, explicit capability to an unclaimed legacy task.

        The HTTP caller must already have administrator authorization and
        independently check capability approval; this method never approves it.
        """
        if not capability or not isinstance(capability,str):
            raise ValueError('Capability required')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row:
                raise ValueError('Task not found')
            data=json.loads(row[0])
            if data['tenant']!=tenant or data['status']!='pending' or data['attempts']!=0:
                raise ValueError('Only unclaimed pending task in the same workspace can be configured')
            current=data.get('required_capabilities',[])
            if current and current!=[capability]:
                raise ValueError('Existing capability must not be replaced implicitly')
            if not current:
                data['required_capabilities']=[capability]
                data['updated_at']=time.time()
                db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(data,ensure_ascii=False),task_id))
                db.execute('INSERT INTO events(task_id,ts,event,details) VALUES (?,?,?,?)',
                           (task_id,time.time(),'execution_capability_bound',capability))
                AuditLog.append_in_transaction(db,actor='task_center',action='execution_capability_bound',
                                               subject=task_id,details={'capability':capability})
        return self.get(task_id)

    def _ancestors(self, id: str, seen=None) -> set[str]:
        seen = seen or set()
        task = self.get(id)
        if task is None:
            return seen
        for dep in task.depends_on:
            if dep not in seen:
                seen.add(dep)
                self._ancestors(dep, seen)
        return seen

    def add_dependency(self, task_id: str, dep_id: str):
        task = self.get(task_id)
        dep = self.get(dep_id)
        if task is None or dep is None:
            raise KeyError('Unknown task/dependency')
        if task.status != TaskStatus.PENDING:
            raise ValueError('Only pending tasks can change dependencies')
        if task_id == dep_id or task_id in self._ancestors(dep_id):
            raise ValueError('Dependency cycle')
        if dep_id not in task.depends_on:
            task.depends_on.append(dep_id)
            self._save(task, 'dependency_added', dep_id)

    def ready(self) -> list[Task]:
        tasks = self.list()
        completed = {t.id for t in tasks if t.status == TaskStatus.COMPLETED}
        ready = [t for t in tasks if t.status == TaskStatus.PENDING and set(t.depends_on).issubset(completed)]
        # Higher priority first, then aging. Deterministic tie-break.
        now = time.time()
        return sorted(ready, key=lambda t: (-(t.priority + (now - t.created_at) / 3600), t.created_at, t.id))

    def claim(self, task_id: str) -> Task:
        # Atomic claim: concurrent schedulers cannot claim the same task twice.
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row: raise ValueError('Task is not ready')
            data=json.loads(row[0])
            if data['status']!=TaskStatus.PENDING.value:
                raise ValueError('Task is not ready')
            for dependency in data['depends_on']:
                dep=db.execute('SELECT payload FROM tasks WHERE id=?',(dependency,)).fetchone()
                if not dep or json.loads(dep[0])['status'] != TaskStatus.COMPLETED.value:
                    raise ValueError('Task dependencies not completed')
            data['status']=TaskStatus.RUNNING.value
            data['attempts']+=1
            data['updated_at']=time.time()
            db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(data,ensure_ascii=False),task_id))
            db.execute('INSERT INTO events(task_id,ts,event,details) VALUES (?,?,?,?)',(task_id,time.time(),'started',''))
            AuditLog.append_in_transaction(db,actor='task_center',action='started',subject=task_id,
                details={'status':'running','attempts':data['attempts']})
        data['status']=TaskStatus.RUNNING
        return Task(**data)

    def finish(self, id: str, *, result: str | None = None,
               error: str | None = None, retryable: bool = True) -> Task:
        """Atomic transition, with idempotent replay of successful completion."""
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM tasks WHERE id=?',(id,)).fetchone()
            if row is None:
                raise ValueError('Unknown task')
            data=json.loads(row[0])
            if data['status']==TaskStatus.COMPLETED.value and error is None and data['result']==result:
                data['status']=TaskStatus.COMPLETED
                return Task(**data)
            if data['status']!=TaskStatus.RUNNING.value:
                raise ValueError('Task must be running')
            data['result'],data['error']=result,error
            if error:
                data['status']=(TaskStatus.PENDING.value if retryable and
                                data['attempts']<data['max_attempts'] else TaskStatus.FAILED.value)
            else:
                data['status']=TaskStatus.COMPLETED.value
            data['updated_at']=time.time()
            db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(data,ensure_ascii=False),id))
            db.execute('INSERT INTO events(task_id,ts,event,details) VALUES (?,?,?,?)',
                       (id,time.time(),data['status'],error or ''))
            AuditLog.append_in_transaction(db,actor='task_center',action=data['status'],subject=id,
                details={'status':data['status'],'attempts':data['attempts'],
                         'error_present':bool(error),'retryable':retryable if error else False})
        data['status']=TaskStatus(data['status'])
        return Task(**data)

    def block_running(self, id: str, *, reason: str = 'uncertain_external_effect') -> Task:
        """Atomically quarantine a running task with unknown remote effects."""
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM tasks WHERE id=?',(id,)).fetchone()
            if row is None or json.loads(row[0])['status'] != TaskStatus.RUNNING.value:
                raise ValueError('Task must be running before quarantine')
            data=json.loads(row[0]);data['status']=TaskStatus.BLOCKED.value
            data['error']=reason[:500];data['updated_at']=time.time()
            db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(data,ensure_ascii=False),id))
            db.execute('INSERT INTO events(task_id,ts,event,details) VALUES (?,?,?,?)',
                       (id,time.time(),'blocked',reason[:500]))
            AuditLog.append_in_transaction(db,actor='task_center',action='blocked',subject=id,
                                           details={'reason':reason[:120]})
        data['status']=TaskStatus.BLOCKED
        return Task(**data)

    def require_approval(self, id: str):
        task = self.get(id)
        if task is None or task.status != TaskStatus.PENDING:
            raise ValueError('Only pending tasks may request approval')
        task.status = TaskStatus.WAITING_APPROVAL
        self._save(task, 'approval_requested')

    def approve(self, id: str):
        task = self.get(id)
        if task is None or task.status != TaskStatus.WAITING_APPROVAL:
            raise ValueError('Task is not awaiting approval')
        task.status = TaskStatus.PENDING
        task.approved = True
        self._save(task, 'approved')

    def reset_failed_with_dependency(self, task_id: str, dep_id: str) -> Task:
        """Requeue a terminally failed task after a recovery job, once only."""
        task = self.get(task_id)
        dep = self.get(dep_id)
        if not task or not dep or task.status != TaskStatus.FAILED:
            raise ValueError('Expected failed task and existing recovery task')
        if any(e[1] == 'replanned' for e in self.events(task_id)):
            raise ValueError('Task already replanned')
        if task_id == dep_id or task_id in self._ancestors(dep_id):
            raise ValueError('Dependency cycle')
        task.depends_on.append(dep_id)
        task.status = TaskStatus.PENDING
        task.attempts = 0
        task.error = None
        task.approved = False
        self._save(task, 'replanned', dep_id)
        return task

    def recover_interrupted(self, unsafe_ids: set[str] | None = None) -> int:
        """Explicit restart recovery: previously running tasks return to pending or fail."""
        count = 0
        unsafe_ids = unsafe_ids or set()
        for task in self.list():
            if task.status == TaskStatus.RUNNING:
                if task.id in unsafe_ids:
                    task.status = TaskStatus.BLOCKED
                    self._save(task, 'recovery_needs_reconciliation')
                else:
                    task.status = TaskStatus.PENDING if task.attempts < task.max_attempts else TaskStatus.FAILED
                    self._save(task, 'recovered')
                count += 1
        return count

    def reconcile_blocked(self, task_id: str, *, retry_allowed: bool) -> Task:
        task = self.get(task_id)
        if task is None or task.status != TaskStatus.BLOCKED:
            raise ValueError('Task not blocked for reconciliation')
        if retry_allowed:
            task.status = TaskStatus.PENDING
            # A known-safe retry must still have one attempt left.
            if task.attempts >= task.max_attempts:
                task.status = TaskStatus.FAILED
        else:
            task.status = TaskStatus.FAILED
        self._save(task, 'reconciled', 'retry' if retry_allowed else 'abort')
        return task

    def events(self, task_id: str):
        with self._connect() as db:
            rows = db.execute('SELECT ts,event,details FROM events WHERE task_id=? ORDER BY id', (task_id,)).fetchall()
        return rows
