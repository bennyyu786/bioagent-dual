"""v0.4.1 durability, retries, duplicate suppression, audit, session recovery."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import pytest
from biomni_dual import (TaskCenter, TaskStatus, AuditLog, SessionStore,
    ErrorKind, AgentExecutionError, classify_error, SDKWorker,
    AgentResult, CapabilityRegistry, ParallelScheduler, WorkerPool, ResourcePool,
    SandboxBuilder, BuildStore, DockerSandbox, CapabilityKind)


def test_idempotent_submit_and_key_reuse(tmp_path):
    tasks=TaskCenter(tmp_path/'t.db')
    first=tasks.submit('qc',idempotency_key='user-42')
    second=tasks.submit('qc',idempotency_key='user-42')
    assert first.id==second.id
    assert len(tasks.list())==1
    with pytest.raises(ValueError):tasks.submit('not qc',idempotency_key='user-42')
    audit=AuditLog(tmp_path/'t.db')
    assert audit.verify()
    assert len(audit.list(subject=first.id))==1


def test_concurrent_submission_dedup(tmp_path):
    path=tmp_path/'t.db';TaskCenter(path)
    with ThreadPoolExecutor(max_workers=8) as ex:
        ids=list(ex.map(lambda _:TaskCenter(path).submit('same',idempotency_key='K').id,range(20)))
    assert len(set(ids))==1
    assert len(TaskCenter(path).list())==1


def test_atomic_claim_across_two_coordinators(tmp_path):
    path=tmp_path/'t.db'; task=TaskCenter(path).submit('work')
    def claim(_):
        try:return TaskCenter(path).claim(task.id).id
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as ex:
        results=list(ex.map(claim,range(2)))
    assert results.count(task.id)==1


def test_audit_hash_detects_edit(tmp_path):
    store=TaskCenter(tmp_path/'tasks.db')
    task=store.submit('q')
    store.claim(task.id)
    store.finish(task.id,result='ok')
    audit=AuditLog(tmp_path/'tasks.db')
    assert audit.verify()
    with sqlite3.connect(tmp_path/'tasks.db') as db:
        db.execute("UPDATE audit_log SET action='tampered' WHERE id=1")
    assert not audit.verify()


def test_error_classification_and_fail_fast(tmp_path):
    assert classify_error(PermissionError('deny')).kind==ErrorKind.PERMISSION
    assert not classify_error(PermissionError('deny')).retryable
    assert classify_error(TimeoutError('timeout')).retryable
    assert classify_error(AgentExecutionError('rate',kind=ErrorKind.RATE_LIMIT)).retryable
    class Broken:
        def run(self,task,caps):raise PermissionError('denied')
    tasks=TaskCenter(tmp_path/'tasks.db');t=tasks.submit('restricted',max_attempts=9)
    out=ParallelScheduler(tasks,CapabilityRegistry(),WorkerPool(Broken()),ResourcePool(cpu=1)).run_until_idle()
    assert len(out)==1
    assert tasks.get(t.id).status==TaskStatus.FAILED
    assert 'permission' in tasks.get(t.id).error


def test_session_resume_and_cached_completion(tmp_path):
    store=SessionStore(tmp_path/'sessions.db')
    class Runtime:
        def __init__(self):self.calls=[]
        async def run(self,request):
            self.calls.append(request.session_id)
            return AgentResult(True,'done',request.session_id or 'remote-session-id')
    worker=SDKWorker(Runtime(),session_store=store,runtime_name='codex')
    class Task: id='task1';prompt='hello'
    assert worker.run(Task(),[])=='done'
    assert store.get('task1')['session_id']=='remote-session-id'
    assert worker.run(Task(),[])=='done'
    assert worker.runtime.calls==[None]
    attempt,sid=store.begin('task1','codex')
    assert sid=='remote-session-id'
    store.checkpoint('task1',attempt,session_id='remote-new',status='FAILED')
    assert store.get('task1')['session_id']=='remote-new'


def test_crash_recovery_is_fail_closed_for_uncertain(tmp_path):
    sessions=SessionStore(tmp_path/'sessions.db')
    tasks=TaskCenter(tmp_path/'tasks.db')
    uncertain=tasks.submit('external side effect');safe=tasks.submit('no sdk')
    tasks.claim(uncertain.id);tasks.claim(safe.id)
    attempt,_=sessions.begin(uncertain.id,'codex')
    assert sessions.recover()==1
    assert sessions.uncertain_task_ids()=={uncertain.id}
    assert tasks.recover_interrupted(sessions.uncertain_task_ids())==2
    assert tasks.get(uncertain.id).status==TaskStatus.BLOCKED
    assert tasks.get(safe.id).status==TaskStatus.PENDING
    with pytest.raises(ValueError):tasks.claim(uncertain.id)
    sessions.reconcile(uncertain.id,retry_allowed=True)
    tasks.reconcile_blocked(uncertain.id,retry_allowed=True)
    assert tasks.claim(uncertain.id).status==TaskStatus.RUNNING


def test_build_idempotency_and_audit(tmp_path):
    folder=tmp_path/'skill';folder.mkdir();(folder/'SKILL.md').write_text('hello')
    store=BuildStore(tmp_path/'builds.db')
    builder=SandboxBuilder(store,DockerSandbox(enforce_rootless=False),CapabilityRegistry())
    first=builder.propose('skill.demo','skill',folder,idempotency_key='proposal1')
    assert builder.propose('skill.demo','skill',folder,idempotency_key='proposal1')==first
    (folder/'SKILL.md').write_text('new')
    with pytest.raises(Exception):builder.propose('skill.demo','skill',folder,idempotency_key='proposal1')
    assert AuditLog(tmp_path/'builds.db').verify()
    assert len(AuditLog(tmp_path/'builds.db').list(subject=first))==1


def test_uncertain_session_requires_reconciliation(tmp_path):
    store=SessionStore(tmp_path/'s.db')
    a,_=store.begin('t','claude');store.recover()
    with pytest.raises(RuntimeError):store.begin('t','claude')
    with pytest.raises(RuntimeError):store.checkpoint('t',a,session_id='x',status='COMPLETED')
    with pytest.raises(ValueError):store.begin('t','codex')
    store.reconcile('t',retry_allowed=True)
    b,_=store.begin('t','claude')
    assert b != a


def test_idempotent_finish_success_does_not_duplicate_audit(tmp_path):
    path=tmp_path/'tasks.db';tasks=TaskCenter(path)
    task=tasks.submit('finish once');tasks.claim(task.id)
    assert tasks.finish(task.id,result='ok').status==TaskStatus.COMPLETED
    count=len(AuditLog(path).list(subject=task.id))
    assert tasks.finish(task.id,result='ok').status==TaskStatus.COMPLETED
    assert len(AuditLog(path).list(subject=task.id))==count
    with pytest.raises(ValueError):tasks.finish(task.id,result='different')


def test_build_approval_and_publication_audited(tmp_path):
    import json
    from biomni_dual import CapabilityRegistry
    store=BuildStore(tmp_path/'b.db')
    folder=tmp_path/'skill';(folder/'tests').mkdir(parents=True)
    (folder/'SKILL.md').write_text('# simple skill')
    (folder/'tests'/'test_ok.py').write_text('def test_ok(): assert 1 == 1')
    class FakeDocker:
        async def __call__(self, argv, stdin):
            if argv[1]=='info':return (0,'["name=rootless"]','')
            return (0,'1 passed','')
    builder=SandboxBuilder(store,DockerSandbox(runner=FakeDocker()),CapabilityRegistry())
    job=builder.propose('skill.a','skill',folder)
    asyncio.run(builder.validate(job));builder.approve(job,'reviewer',approved=True)
    builder.publish(job)
    log=AuditLog(tmp_path/'b.db');assert log.verify()
    actions=[entry['action'] for entry in log.list(subject=job)]
    assert actions==['build.proposed','build.testing','build.review','build.approved','build.published']
