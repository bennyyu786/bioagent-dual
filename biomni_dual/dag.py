"""Versioned, additive DAG edits. One SQLite transaction per patch.

Never rewrites running/completed jobs. Caller supplies new tasks and/or dependencies
for pending jobs only. This does not provide distributed graph editing.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
import sqlite3
import time
from uuid import uuid4
from .models import Task, TaskStatus
from .scheduler import TaskCenter

@dataclass(frozen=True)
class DAGPatch:
    additions: tuple[dict, ...] = ()  # {key,prompt,depends_on,required_capabilities,priority,tenant}
    dependencies: tuple[tuple[str, str], ...] = ()  # (existing pending task id, prerequisite id)
    reason: str = 'replan'
    recovery: tuple[str, str] | None = None  # (failed_task_id, new recovery step key)

class DAGVersionManager:
    def __init__(self, center: TaskCenter):
        self.center = center
        with center._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS dag_versions (
                version INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
                reason TEXT NOT NULL, snapshot TEXT NOT NULL)''')

    def versions(self) -> list[dict]:
        with self.center._connect() as db:
            return [dict(version=r[0], created=r[1], reason=r[2]) for r in db.execute(
                'SELECT version,created,reason FROM dag_versions ORDER BY version')]

    def snapshot(self, version: int | None = None) -> dict:
        with self.center._connect() as db:
            if version is None:
                row = db.execute('SELECT snapshot FROM dag_versions ORDER BY version DESC LIMIT 1').fetchone()
            else:
                row = db.execute('SELECT snapshot FROM dag_versions WHERE version=?', (version,)).fetchone()
        if row is None:
            raise KeyError('Unknown DAG version')
        return json.loads(row[0])

    def commit(self, patch: DAGPatch) -> dict[str, str]:
        if len(patch.additions) + len(patch.dependencies) > 100:
            raise ValueError('Patch too large')
        if not patch.additions and not patch.dependencies:
            raise ValueError('Empty patch')
        mapping: dict[str, str] = {}
        with self.center._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT id,payload FROM tasks').fetchall()
            tasks = {i: json.loads(payload) for i, payload in rows}
            now = time.time()
            for entry in patch.additions:
                key = entry['key']
                if not isinstance(key, str) or not key or key in mapping:
                    raise ValueError('Duplicate or invalid new task key')
                tid = str(uuid4())
                mapping[key] = tid
                dependencies = [mapping.get(d, d) for d in entry.get('depends_on', [])]
                if any(dep not in tasks for dep in dependencies):
                    raise ValueError('New task dependency must exist or precede this step')
                if not isinstance(entry.get('prompt'), str) or not entry['prompt'].strip():
                    raise ValueError('Invalid task prompt')
                t = Task(id=tid, prompt=entry['prompt'], depends_on=dependencies,
                         required_capabilities=list(entry.get('required_capabilities', [])),
                         priority=int(entry.get('priority', 0)), tenant=entry.get('tenant', 'default'),
                         created_at=now, updated_at=now)
                data = asdict(t); data['status'] = (
                    TaskStatus.WAITING_APPROVAL.value if entry.get('requires_approval', False)
                    else t.status.value)
                tasks[tid] = data
            for task_id, dep_id in patch.dependencies:
                target = mapping.get(task_id, task_id)
                dep = mapping.get(dep_id, dep_id)
                if target not in tasks or dep not in tasks:
                    raise KeyError('Unknown task in patch')
                if tasks[target]['status'] != TaskStatus.PENDING.value:
                    raise ValueError('Cannot change dependencies of active or finished tasks')
                if dep not in tasks[target]['depends_on']:
                    tasks[target]['depends_on'].append(dep)
            if patch.recovery is not None:
                failed_id, recovery_key = patch.recovery
                recovery_id = mapping.get(recovery_key)
                if recovery_id is None or failed_id not in tasks:
                    raise ValueError('Recovery requires a newly created step and existing failed task')
                failed = tasks[failed_id]
                if failed['status'] != TaskStatus.FAILED.value:
                    raise ValueError('Recovery target must be failed')
                if any(event == 'replanned' for (event,) in db.execute(
                        'SELECT event FROM events WHERE task_id=?', (failed_id,))):
                    raise ValueError('Task already automatically recovered')
                failed['depends_on'].append(recovery_id)
                failed['status'] = TaskStatus.PENDING.value
                failed['attempts'] = 0
                failed['error'] = None
                failed['approved'] = False
                db.execute('INSERT INTO events(task_id,ts,event,details) VALUES (?,?,?,?)',
                           (failed_id,now,'replanned',recovery_id))
            # Full graph validation includes all old tasks; cycles are rejected before writes.
            state: dict[str, int] = {}
            def visit(node: str):
                if state.get(node) == 1:
                    raise ValueError('DAG cycle')
                if state.get(node) == 2:
                    return
                state[node] = 1
                for dep in tasks[node]['depends_on']:
                    if dep not in tasks:
                        raise ValueError('Missing DAG dependency')
                    visit(dep)
                state[node] = 2
            for node in tasks:
                visit(node)
            for tid, data in tasks.items():
                # old completed/running task payloads remain bytewise unchanged
                original = next((r[1] for r in rows if r[0] == tid), None)
                serialized = json.dumps(data, ensure_ascii=False)
                if original != serialized:
                    db.execute('INSERT OR REPLACE INTO tasks(id,payload) VALUES (?,?)', (tid, serialized))
                    db.execute('INSERT INTO events(task_id,ts,event,details) VALUES (?,?,?,?)',
                               (tid, now, 'dag_patch', patch.reason[:2000]))
            db.execute('INSERT INTO dag_versions(created,reason,snapshot) VALUES (?,?,?)',
                       (now, patch.reason[:2000], json.dumps(tasks, ensure_ascii=False)))
        return mapping
