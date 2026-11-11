import asyncio
import sqlite3
import time
import pytest
from biomni_dual import (TaskCenter,AdaptiveStore,AdaptiveController,CallbackPlanner,
    PlanProposal,Action,GoalState,EventKind,PersistentRuntimeStore,
    PersistentAutonomousRuntime,EnvironmentMonitor,CallbackProbe,TaskStatus)


def go(value): return asyncio.run(value)


class Worker:
    def __init__(self): self.calls=0
    async def run(self,task,capability_ids):
        self.calls+=1
        return 'validated-result'


class Validator:
    async def validate(self,task,output): return True,'approved-evidence'


@pytest.fixture
def setup(tmp_path):
    path=str(tmp_path/'runtime.sqlite')
    tasks=TaskCenter(path)
    store=AdaptiveStore(path)
    store.create_goal('research',goal_id='g',max_cycles=40)
    worker=Worker()
    ctl=AdaptiveController(goal_id='g',store=store,tasks=tasks,
        planner=CallbackPlanner(lambda s:PlanProposal(Action.ASK_HUMAN,reason='needs_human')),
        worker=worker,validator=Validator())
    runtime_store=PersistentRuntimeStore(path)
    runtime=PersistentAutonomousRuntime(runtime_store,{'g':ctl},
        idle_interval=3600,poll_seconds=.001,lease_seconds=.12)
    return path,tasks,store,worker,ctl,runtime_store,runtime


def test_event_dedup_and_payload_immutability(setup):
    _,_,_,_,_,rs,_=setup
    a=rs.submit('g',EventKind.DATA_CHANGED,'a',{'a':1})
    b=rs.submit('g',EventKind.DATA_CHANGED,'a',{'a':1})
    assert a[1] and b==(a[0],False)
    with pytest.raises(ValueError): rs.submit('g',EventKind.DATA_CHANGED,'a',{'a':2})
    with pytest.raises(ValueError): rs.submit('g',EventKind.DATA_CHANGED,'a',{'a':'x'*8000})


def test_event_driven_tick_and_observation(setup):
    _,tasks,adaptive,worker,ctl,rs,rt=setup
    task=tasks.submit('go')
    adaptive.bind_tasks('g',[task.id],tasks)
    eid,_=rs.submit('g',EventKind.DATA_CHANGED,'new-matrix')
    assert go(rt.step('g'))==Action.EXECUTE
    assert worker.calls==1 and tasks.get(task.id).status==TaskStatus.COMPLETED
    assert rs.pending('g') is None
    obs=adaptive.observations('g')
    assert any(o.evidence.get('runtime_event_id')==eid for o in obs)
    assert go(rt.step('g')) is None


def test_cooperative_leases_expiry_and_token(setup):
    _,_,_,_,_,rs,_=setup
    tok=rs.lease('g','owner-1',.06)
    assert tok and rs.lease('g','owner-2',.06) is None
    assert not rs.renew('g','wrong',1)
    time.sleep(.07)
    nexttok=rs.lease('g','owner-2',2)
    assert nexttok and nexttok!=tok
    rs.release('g',tok)
    assert rs.lease('g','third',1) is None
    rs.release('g',nexttok)


def test_probe_change_and_return_to_first_value(setup):
    _,_,_,_,_,rs,_=setup
    val={'x':1}
    monitor=EnvironmentMonitor(rs)
    monitor.add('dataset',EventKind.DATA_CHANGED,CallbackProbe(lambda:val['x']))
    assert go(monitor.poll('g'))==[]  # baseline
    val['x']=2
    first=go(monitor.poll('g'))
    assert len(first)==1
    val['x']=1
    second=go(monitor.poll('g'))
    assert len(second)==1 and second!=first
    assert len(go(monitor.poll('g')))==0


def test_probes_only_keep_digest(setup):
    path,_,_,_,_,rs,_=setup
    state={'secret':'hidden value'}
    monitor=EnvironmentMonitor(rs)
    monitor.add('health',EventKind.RESOURCE_CHANGED,CallbackProbe(lambda:state))
    go(monitor.poll('g'))
    state['secret']='another value'
    go(monitor.poll('g'))
    with sqlite3.connect(path) as db:
        rows=db.execute('SELECT payload FROM runtime_events').fetchall()
    assert len(rows)==1 and 'another value' not in str(rows)


def test_probe_error_emits_no_secret(setup):
    _,_,_,_,_,rs,_=setup
    class Err:
        async def sample(self): raise ConnectionError('password=123')
    monitor=EnvironmentMonitor(rs)
    monitor.add('mcp',EventKind.HEALTH_CHANGED,Err())
    assert go(monitor.poll('g'))==[]
    # Error class is stable; same error should not flood events.
    assert go(monitor.poll('g'))==[]


def test_pause_stop_and_operator_resume(setup):
    _,_,_,_,_,rs,rt=setup
    rs.set_state('g',GoalState.PAUSED,operator=True)
    assert go(rt.step('g',force=True)) is None
    with pytest.raises(PermissionError): rs.set_state('g',GoalState.ACTIVE)
    with pytest.raises(PermissionError): rt.resume_goal('g')
    rt.resume_goal('g',operator=True)
    assert rs.state('g')==GoalState.ACTIVE
    rs.set_state('g',GoalState.STOPPED,operator=True)
    assert go(rt.step('g',force=True)) is None


def test_startup_quarantines_running(setup):
    _,tasks,adaptive,_,_,rs,rt=setup
    t=tasks.submit('external API may change remote data')
    adaptive.bind_tasks('g',[t.id],tasks)
    tasks.claim(t.id)
    assert rt.reconcile_startup('g')==1
    assert tasks.get(t.id).status==TaskStatus.BLOCKED
    assert rs.state('g')==GoalState.WAITING
    with pytest.raises(RuntimeError): rt.resume_goal('g',operator=True)
    tasks.reconcile_blocked(t.id,retry_allowed=True)
    rt.resume_goal('g',operator=True)
    assert rs.state('g')==GoalState.ACTIVE


def test_startup_recovery_refuses_active_lease(setup):
    _,_,_,_,_,rs,rt=setup
    tok=rs.lease('g','another',20)
    with pytest.raises(RuntimeError): rt.reconcile_startup('g')
    rs.release('g',tok)


def test_restart_preserves_event_and_goal_state(setup):
    path,_,_,_,_,rs,_=setup
    eid,_=rs.submit('g',EventKind.CAPABILITY_CHANGED,'cap-v1')
    again=PersistentRuntimeStore(path)
    again.register_goal('g')
    assert again.pending('g').id==eid
    again.set_state('g',GoalState.PAUSED,operator=True)
    assert PersistentRuntimeStore(path).state('g')==GoalState.PAUSED


def test_waiting_human_does_not_spin(setup):
    _,_,_,_,_,rs,rt=setup
    assert go(rt.step('g',force=True))==Action.ASK_HUMAN
    assert rs.state('g')==GoalState.WAITING
    assert go(rt.step('g',force=True)) is None


def test_service_loop_can_stop(setup):
    _,_,_,_,_,rs,rt=setup
    # Single cycle creates waiting approval and exits cleanly.
    go(rt.serve(max_loops=1))
    assert rs.state('g')==GoalState.WAITING


def test_operator_resumption_requires_approval_resolution(setup):
    _,tasks,adaptive,_,_,rs,rt=setup
    t=tasks.submit('approval task')
    adaptive.bind_tasks('g',[t.id],tasks)
    tasks.require_approval(t.id)
    rs.set_state('g',GoalState.WAITING,operator=True)
    with pytest.raises(RuntimeError): rt.resume_goal('g',operator=True)
    tasks.approve(t.id)
    rt.resume_goal('g',operator=True)
    assert rs.state('g')==GoalState.ACTIVE


def test_duplicate_event_not_replayed(setup):
    _,_,_,_,_,rs,rt=setup
    eid,_=rs.submit('g',EventKind.RESOURCE_CHANGED,'event-42')
    assert go(rt.step('g'))==Action.ASK_HUMAN
    assert rs.pending('g') is None
    assert rs.submit('g',EventKind.RESOURCE_CHANGED,'event-42')==(eid,False)


def test_probe_name_and_kind_validation(setup):
    _,_,_,_,_,rs,_=setup
    monitor=EnvironmentMonitor(rs)
    monitor.add('probe',EventKind.PERIODIC,CallbackProbe(lambda:1))
    with pytest.raises(ValueError): monitor.add('probe',EventKind.PERIODIC,CallbackProbe(lambda:2))
    with pytest.raises(ValueError): rs.submit('g','bad-kind','k')


def test_failed_tick_keeps_event_pending(setup):
    _,_,_,_,ctl,rs,rt=setup
    class Fail:
        async def tick(self): raise RuntimeError('boom')
    ctl.tick=Fail().tick
    rs.submit('g',EventKind.TASK_FAILED,'failure-1')
    with pytest.raises(RuntimeError): go(rt.step('g'))
    assert rs.pending('g') is not None
    assert rs.lease('g','other',1) is not None


def test_budget_marks_exhaustion(setup):
    _,_,adaptive,_,_,rs,rt=setup
    with adaptive.connect() as db:
        db.execute('UPDATE adaptive_goals SET cycle=max_cycles WHERE id=?',('g',))
    assert go(rt.step('g',force=True))==Action.ASK_HUMAN
    assert rs.state('g')==GoalState.EXHAUSTED


def test_periodic_schedule_durable_and_no_catchup_burst(setup):
    path,_,_,_,_,rs,_=setup
    rs.schedule('g','daily',60,immediate=True)
    ids=rs.due_events('g')
    assert len(ids)==1
    assert not rs.due_events('g')
    again=PersistentRuntimeStore(path)
    assert again.pending('g').id==ids[0]
    with sqlite3.connect(path) as db:
        db.execute('UPDATE runtime_schedules SET next_due=? WHERE goal_id=?',
                   (time.time()-100000,'g'))
    assert len(again.due_events('g'))==1
    assert not again.due_events('g')


def test_schedule_rejects_conflicting_parameters(setup):
    _,_,_,_,_,rs,_=setup
    rs.schedule('g','pulse',12)
    rs.schedule('g','pulse',12)
    with pytest.raises(ValueError): rs.schedule('g','pulse',10)
    with pytest.raises(ValueError): rs.schedule('g','fast',.001)


def test_cadence_medium_defers_second_event(setup):
    from biomni_dual import CadencePolicy
    path,_,_,_,_,rs,rt=setup
    rt.cadence=CadencePolicy(fast=0,medium=500,slow=500)
    rs.submit('g',EventKind.DATA_CHANGED,'one')
    assert go(rt.step('g'))==Action.ASK_HUMAN
    # Resume deliberately after the first operator interaction.
    rt.resume_goal('g',operator=True)
    rs.submit('g',EventKind.CAPABILITY_CHANGED,'two')
    assert go(rt.step('g')) is None
    assert rs.pending('g') is not None
    with sqlite3.connect(path) as db:
        db.execute('UPDATE runtime_cadence SET last_tick=0 WHERE goal_id=?',('g',))
    assert go(rt.step('g'))==Action.ASK_HUMAN


def test_webhook_hmac_and_replay(setup):
    from biomni_dual import AuthenticatedWebhookIngress
    import hashlib, hmac
    path,_,_,_,_,rs,_=setup
    ingress=AuthenticatedWebhookIngress(rs,b'x'*32)
    body=b'{"change":"dataset"}'
    nonce='req-abc'
    sig=hmac.new(b'x'*32,nonce.encode()+b'\n'+body,hashlib.sha256).hexdigest()
    eid,created=ingress.ingest('g',EventKind.DATA_CHANGED,body,sig,nonce)
    assert created and ingress.ingest('g',EventKind.DATA_CHANGED,body,sig,nonce)==(eid,False)
    with pytest.raises(PermissionError): ingress.ingest('g',EventKind.DATA_CHANGED,body,'bad',nonce)
    assert b'dataset' not in str(rs.pending('g').detail).encode()


def test_webhook_replay_cannot_change_body(setup):
    from biomni_dual import AuthenticatedWebhookIngress
    import hashlib,hmac
    _,_,_,_,_,rs,_=setup
    ing=AuthenticatedWebhookIngress(rs,b'y'*32)
    def sig(body):return hmac.new(b'y'*32,b'nonce\n'+body,hashlib.sha256).hexdigest()
    ing.ingest('g',EventKind.DATA_CHANGED,b'first',sig(b'first'),'nonce')
    with pytest.raises(ValueError): ing.ingest('g',EventKind.DATA_CHANGED,b'second',sig(b'second'),'nonce')


def test_file_probe_scope(tmp_path):
    from biomni_dual import FileFingerprintProbe
    safe=tmp_path/'safe';safe.mkdir()
    t=safe/'file.txt';t.write_text('one')
    p=FileFingerprintProbe(t,allowed_root=safe)
    assert go(p.sample())['size']==3
    with pytest.raises(PermissionError): FileFingerprintProbe(tmp_path,allowed_root=safe)
