"""Offline reliability demonstration; no external SDK or Docker required."""
from pathlib import Path
from tempfile import TemporaryDirectory
from biomni_dual import TaskCenter, SessionStore, AuditLog, TaskStatus

with TemporaryDirectory() as tmp:
    root=Path(tmp)
    tasks=TaskCenter(root/'tasks.db')
    sessions=SessionStore(root/'sessions.db')
    task=tasks.submit('Interpret a candidate variant',max_attempts=3,idempotency_key='request-2026-11-09-001')
    assert tasks.submit('Interpret a candidate variant',max_attempts=3,idempotency_key='request-2026-11-09-001').id==task.id
    tasks.claim(task.id)
    attempt,_=sessions.begin(task.id,'codex')
    # Process crashes here, before remote agent returns outcome or session id.
    print('uncertain remote attempts:',sessions.recover())
    tasks.recover_interrupted(sessions.uncertain_task_ids())
    assert tasks.get(task.id).status==TaskStatus.BLOCKED
    print('after crash:',tasks.get(task.id).status.value)
    # A real operator first checks the remote action did not have side effects.
    sessions.reconcile(task.id,retry_allowed=True)
    tasks.reconcile_blocked(task.id,retry_allowed=True)
    tasks.claim(task.id)
    tasks.finish(task.id,result='Validated result')
    audit=AuditLog(root/'tasks.db')
    print('final status:',tasks.get(task.id).status.value)
    print('audit verified:',audit.verify())
    print('audit entries:',len(audit.list(subject=task.id)))
