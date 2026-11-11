"""Durable agent checkpoints, error taxonomy and tamper-evident audit records.

The audit chain detects accidental or unauthorized edits in the SQLite database;
it is not a substitute for off-host immutable logs or cryptographic signatures.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from uuid import uuid4


class ErrorKind(str, Enum):
    TRANSIENT = 'transient'
    RATE_LIMIT = 'rate_limit'
    TIMEOUT = 'timeout'
    AUTH = 'authentication'
    PERMISSION = 'permission'
    INVALID_INPUT = 'invalid_input'
    DEPENDENCY = 'dependency'
    RESOURCE = 'resource_exhausted'
    CANCELLED = 'cancelled'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class ErrorInfo:
    kind: ErrorKind
    retryable: bool
    code: str
    message: str


class AgentExecutionError(RuntimeError):
    def __init__(self, message: str, *, kind: ErrorKind, code: str = 'agent_error'):
        super().__init__(message)
        self.kind, self.code = kind, code


def classify_error(exc: BaseException) -> ErrorInfo:
    import asyncio
    if isinstance(exc, AgentExecutionError):
        kind, code = exc.kind, exc.code
    elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        kind, code = ErrorKind.TIMEOUT, 'timeout'
    elif isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
        kind, code = ErrorKind.CANCELLED, 'cancelled'
    elif isinstance(exc, PermissionError):
        kind, code = ErrorKind.PERMISSION, 'permission_denied'
    elif isinstance(exc, (ValueError, TypeError)):
        kind, code = ErrorKind.INVALID_INPUT, 'invalid_input'
    elif isinstance(exc, (FileNotFoundError, ImportError, ModuleNotFoundError)):
        kind, code = ErrorKind.DEPENDENCY, 'missing_dependency'
    elif isinstance(exc, ConnectionError):
        kind, code = ErrorKind.TRANSIENT, 'connection_error'
    elif isinstance(exc, MemoryError):
        kind, code = ErrorKind.RESOURCE, 'memory_exhausted'
    else:
        # Unknown errors are retried within Task.max_attempts for compatibility.
        kind, code = ErrorKind.UNKNOWN, 'unclassified'
    retryable = kind in (ErrorKind.TRANSIENT, ErrorKind.RATE_LIMIT, ErrorKind.TIMEOUT, ErrorKind.RESOURCE, ErrorKind.UNKNOWN)
    return ErrorInfo(kind, retryable, code, str(exc)[:2000])


def _canon(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)


class AuditLog:
    """Append-only hash-linked events. Use SQLite transaction for atomic writes."""
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self.connect() as db:
            self.install(db)

    def connect(self):
        return sqlite3.connect(self.path, timeout=20)

    @staticmethod
    def install(db):
        db.execute('''CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL,
            details TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL)''')

    @staticmethod
    def append_in_transaction(db, *, actor: str, action: str, subject: str, details=None):
        AuditLog.install(db)
        row = db.execute('SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1').fetchone()
        previous = row[0] if row else '0' * 64
        # Never put secrets, prompts or raw tool responses in audit details.
        safe = _canon(details or {})
        ts = time.time()
        obj = {'ts': ts, 'actor': actor, 'action': action, 'subject': subject,
               'details': safe, 'previous_hash': previous}
        digest = hashlib.sha256(_canon(obj).encode()).hexdigest()
        db.execute('INSERT INTO audit_log(ts,actor,action,subject,details,previous_hash,hash) VALUES (?,?,?,?,?,?,?)',
                   (ts, actor, action, subject, safe, previous, digest))
        return digest

    def append(self, action: str, subject: str, *, actor='system', details=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self.append_in_transaction(db, actor=actor, action=action, subject=subject, details=details)

    def verify(self) -> bool:
        with self.connect() as db:
            rows = db.execute('SELECT ts,actor,action,subject,details,previous_hash,hash FROM audit_log ORDER BY id').fetchall()
        previous = '0' * 64
        for ts, actor, action, subject, details, prev, digest in rows:
            if prev != previous:
                return False
            obj = {'ts': ts, 'actor': actor, 'action': action, 'subject': subject,
                   'details': details, 'previous_hash': prev}
            if hashlib.sha256(_canon(obj).encode()).hexdigest() != digest:
                return False
            previous = digest
        return True

    def list(self, *, subject: str | None = None):
        with self.connect() as db:
            if subject is None:
                rows = db.execute('SELECT id,ts,actor,action,subject,details,hash FROM audit_log ORDER BY id').fetchall()
            else:
                rows = db.execute('SELECT id,ts,actor,action,subject,details,hash FROM audit_log WHERE subject=? ORDER BY id', (subject,)).fetchall()
        return [dict(id=r[0], ts=r[1], actor=r[2], action=r[3], subject=r[4], details=json.loads(r[5]), hash=r[6]) for r in rows]


class SessionStore:
    """Persist completed SDK sessions and uncertain in-flight requests.

    session_id is stored only after the SDK returns it. A crash before the SDK
    returns cannot guarantee automatic remote-session recovery; such requests
    are flagged UNCERTAIN and require operator reconciliation.
    """
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS agent_sessions (
                task_id TEXT PRIMARY KEY, runtime TEXT NOT NULL, session_id TEXT,
                status TEXT NOT NULL, attempt_id TEXT NOT NULL,
                result TEXT, updated REAL NOT NULL)''')

    def _db(self):
        return sqlite3.connect(self.path, timeout=20)

    def get(self, task_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute('SELECT task_id,runtime,session_id,status,attempt_id,result,updated FROM agent_sessions WHERE task_id=?', (task_id,)).fetchone()
        return dict(zip(('task_id','runtime','session_id','status','attempt_id','result','updated'),row)) if row else None

    def begin(self, task_id: str, runtime: str, *, resume: bool = True) -> tuple[str, str | None]:
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT runtime,session_id,status FROM agent_sessions WHERE task_id=?',(task_id,)).fetchone()
            if old and old[0] != runtime:
                raise ValueError('Cannot resume a session with another runtime')
            if old and old[2] in ('RUNNING','UNCERTAIN'):
                raise RuntimeError('Session running or uncertain; recover and reconcile before retry')
            session_id = old[1] if old and resume else None
            attempt_id = uuid4().hex
            db.execute('''INSERT INTO agent_sessions(task_id,runtime,session_id,status,attempt_id,result,updated)
                          VALUES (?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                          status=excluded.status, attempt_id=excluded.attempt_id,
                          session_id=excluded.session_id, result=NULL, updated=excluded.updated''',
                       (task_id, runtime, session_id, 'RUNNING', attempt_id, None, time.time()))
            return attempt_id, session_id

    def checkpoint(self, task_id: str, attempt_id: str, *, session_id: str | None,
                   status: str, result: str | None = None):
        if status not in ('COMPLETED','FAILED','UNCERTAIN'):
            raise ValueError('Invalid checkpoint state')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('''UPDATE agent_sessions SET session_id=COALESCE(?,session_id),
                status=?,result=?,updated=? WHERE task_id=? AND attempt_id=? AND status='RUNNING' ''',
                (session_id,status,result,time.time(),task_id,attempt_id)).rowcount
            if count != 1:
                raise RuntimeError('Stale or already finalized session attempt')

    def recover(self) -> int:
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            return db.execute("UPDATE agent_sessions SET status='UNCERTAIN',updated=? WHERE status='RUNNING'",(time.time(),)).rowcount

    def uncertain_task_ids(self) -> set[str]:
        with self._db() as db:
            return {r[0] for r in db.execute("SELECT task_id FROM agent_sessions WHERE status='UNCERTAIN'")}

    def reconcile(self, task_id: str, *, retry_allowed: bool):
        """Operator decision after confirming side effects are safe to retry."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            status = 'FAILED' if retry_allowed else 'UNCERTAIN'
            rowcount = db.execute('UPDATE agent_sessions SET status=?,updated=? WHERE task_id=? AND status=?',
                                  (status,time.time(),task_id,'UNCERTAIN')).rowcount
            if rowcount != 1:
                raise ValueError('Task is not in uncertain state')
