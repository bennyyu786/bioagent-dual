import asyncio
from dataclasses import dataclass
import pytest
from biomni_dual import (Action, AdaptiveController, AdaptiveStore, CallbackPlanner,
    Capability, CapabilityKind, CapabilityRegistry, DAGPatch, NonEmptyValidator,
    Observation, PlanProposal, TaskCenter, TaskFitCoordinator, TaskStatus)


def go(coro): return asyncio.run(coro)

class Worker:
    def __init__(self, output='ok'):
        self.output=output
        self.calls=[]
    async def run(self, task, capability_ids):
        self.calls.append((task.id,capability_ids))
        return self.output

class Validator:
    def __init__(self, accept=True): self.accept=accept
    async def validate(self, task, output):
        return self.accept,'scientific_holdout_test' if self.accept else 'scientific_validation_rejected'

class Autonomy:
    def __init__(self): self.calls=[]
    def request(self,cid,kind,requirement,request_key,**kwargs):
        self.calls.append(request_key)
        return {'id':request_key}

@pytest.fixture
def env(tmp_path):
    db=str(tmp_path/'adaptive.db')
    return TaskCenter(db),AdaptiveStore(db)

def make(center,store,planner,worker=None,validator=None,goal_id='research',**kw):
    store.create_goal('unknown multi-omics challenge',goal_id=goal_id,max_cycles=30)
    return AdaptiveController(goal_id=goal_id,store=store,tasks=center,
                              planner=CallbackPlanner(planner),worker=worker or Worker(),
                              validator=validator or Validator(),**kw)

def test_bounded_rolling_two_step_decomposition(env):
    tasks,store=env
    worker=Worker()
    def planner(s):
        if not s.tasks:
            return PlanProposal(Action.EXPLORE,DAGPatch(additions=({'key':'inspect','prompt':'inspect data'},)), 'discover')
        if len(s.tasks)==1:
            assert s.tasks[0].status==TaskStatus.COMPLETED
            return PlanProposal(Action.REPLAN,DAGPatch(additions=({'key':'model','prompt':'model after inspection','depends_on':[s.tasks[0].id]},)),'new evidence')
        return PlanProposal(Action.ASK_HUMAN,reason='done')
    ctl=make(tasks,store,planner,worker=worker)
    assert go(ctl.tick()).action==Action.EXPLORE
    assert go(ctl.tick()).action==Action.EXECUTE
    # explicit environment event triggers another rolling plan after successful task
    store.remember('research',Observation('new-data','', 'environment_change','new omics modality'))
    assert go(ctl.tick()).action==Action.REPLAN
    assert go(ctl.tick()).action==Action.EXECUTE
    assert go(ctl.tick()).action==Action.FINISH
    assert len(worker.calls)==2
    assert len(ctl.dag.versions())==2
    assert len(store.observations('research'))==3


def test_failed_scientific_validation_not_completed(env):
    tasks,store=env
    task=tasks.submit('scientific task')
    ctl=make(tasks,store,lambda s:PlanProposal(Action.ASK_HUMAN),validator=Validator(False))
    store.bind_tasks('research',[task.id],tasks)
    assert go(ctl.tick()).action==Action.VALIDATE
    assert tasks.get(task.id).status==TaskStatus.FAILED
    assert store.observations('research')[0].evidence['accepted'] is False


def test_error_classification_retry(env):
    tasks,store=env
    task=tasks.submit('call',max_attempts=2)
    class Broken:
        async def run(self,task,caps): raise TimeoutError('slow')
    ctl=make(tasks,store,lambda s:PlanProposal(Action.ASK_HUMAN),worker=Broken())
    store.bind_tasks('research',[task.id],tasks)
    assert go(ctl.tick()).evidence['error_kind']=='timeout'
    assert tasks.get(task.id).status==TaskStatus.PENDING
    go(ctl.tick())
    assert tasks.get(task.id).status==TaskStatus.FAILED


def test_missing_skill_requests_only_gap_once(env):
    tasks,store=env
    a=Autonomy()
    reg=CapabilityRegistry()
    reg.register(Capability('qc','QC',CapabilityKind.SKILL,metadata={'provides':['qc']}))
    task=tasks.submit('analysis',required_capabilities=['qc','causal'])
    fit=TaskFitCoordinator(reg,a)
    ctl=make(tasks,store,lambda s:PlanProposal(Action.ASK_HUMAN),fit=fit,gap_kinds={'causal':'skill'})
    store.bind_tasks('research',[task.id],tasks)
    d=go(ctl.tick())
    assert d.action==Action.BUILD
    assert d.evidence['gaps']==['causal']
    go(ctl.tick())
    assert a.calls==['task-fit:'+task.id+':skill:causal']*2
    assert tasks.get(task.id).status==TaskStatus.PENDING


def test_no_external_search_if_internal_sufficient(env):
    tasks,store=env
    reg=CapabilityRegistry(); reg.register(Capability('qc','QC',CapabilityKind.SKILL,metadata={'provides':['qc']}))
    task=tasks.submit('quality control',required_capabilities=['qc'])
    worker=Worker()
    ctl=make(tasks,store,lambda s:PlanProposal(Action.ASK_HUMAN),worker=worker,fit=TaskFitCoordinator(reg,Autonomy()))
    store.bind_tasks('research',[task.id],tasks)
    assert go(ctl.tick()).action==Action.EXECUTE
    assert worker.calls[0][1]==('qc',)


def test_goal_isolation(env):
    tasks,store=env
    a=tasks.submit('goal A')
    b=tasks.submit('goal B')
    one=make(tasks,store,lambda s:PlanProposal(Action.ASK_HUMAN),goal_id='a')
    two=make(tasks,store,lambda s:PlanProposal(Action.ASK_HUMAN),goal_id='b')
    store.bind_tasks('a',[a.id],tasks)
    store.bind_tasks('b',[b.id],tasks)
    go(one.tick())
    assert tasks.get(b.id).status==TaskStatus.PENDING
    go(two.tick())
    assert tasks.get(b.id).status==TaskStatus.COMPLETED


def test_reject_cross_goal_dag_change(env):
    tasks,store=env
    t=tasks.submit('another goal')
    ctl=make(tasks,store,lambda s:PlanProposal(Action.REPLAN,DAGPatch(dependencies=((t.id,t.id),))))
    with pytest.raises(ValueError,match='Cross-goal'):
        go(ctl.tick())
    assert tasks.get(t.id).status==TaskStatus.PENDING


def test_observation_idempotency_and_audit(env):
    tasks,store=env
    store.create_goal('goal',goal_id='g')
    obs=Observation('id1','t','input','observed',{'count':2})
    assert store.remember('g',obs)
    assert not store.remember('g',obs)
    with pytest.raises(ValueError,match='reused'):
        store.remember('g',Observation('id1','t','input','different'))
    from biomni_dual import AuditLog
    assert AuditLog(store.path).verify()


def test_restart_preserves_progress(env):
    tasks,store=env
    task=tasks.submit('study')
    ctl=make(tasks,store,lambda s:PlanProposal(Action.ASK_HUMAN))
    store.bind_tasks('research',[task.id],tasks)
    go(ctl.tick())
    store2=AdaptiveStore(store.path)
    ctl2=AdaptiveController(goal_id='research',store=store2,tasks=TaskCenter(store.path),
        planner=CallbackPlanner(lambda s: PlanProposal(Action.ASK_HUMAN)),worker=Worker(),validator=Validator())
    assert ctl2.snapshot().cycle==1
    assert go(ctl2.tick()).action==Action.FINISH


def test_running_task_requires_reconciliation(env):
    tasks,store=env
    t=tasks.submit('external side effect')
    ctl=make(tasks,store,lambda s:PlanProposal(Action.ASK_HUMAN))
    store.bind_tasks('research',[t.id],tasks)
    tasks.claim(t.id)
    assert go(ctl.tick()).action==Action.ASK_HUMAN
    assert tasks.get(t.id).status==TaskStatus.RUNNING


def test_cycle_budget_limits_actions(env):
    tasks,store=env
    store.create_goal('goal',goal_id='g',max_cycles=1)
    ctl=AdaptiveController(goal_id='g',store=store,tasks=tasks,
        planner=CallbackPlanner(lambda s:PlanProposal(Action.ASK_HUMAN)),worker=Worker(),validator=Validator())
    assert go(ctl.tick()).action==Action.ASK_HUMAN
    assert go(ctl.tick()).reason=='cycle_budget_exhausted'
    assert len(store.decisions('g'))==1


def test_planning_budget_limits_graph_growth(env):
    tasks,store=env
    n=0
    def planner(s):
        return PlanProposal(Action.EXPLORE,DAGPatch(additions=({'key':f'step-{len(s.tasks)}','prompt':'explore'},)))
    ctl=make(tasks,store,planner,max_plans=1)
    assert go(ctl.tick()).action==Action.EXPLORE
    go(ctl.tick())
    # After step completes, a new environment event arrives.
    store.remember('research',Observation('event','', 'environment_change','changed'))
    assert go(ctl.tick()).reason=='planning_budget_exhausted'


def test_planner_cannot_overwrite_completed_task(env):
    tasks,store=env
    t=tasks.submit('existing')
    ctl=make(tasks,store,lambda s:PlanProposal(Action.REPLAN,DAGPatch(dependencies=((t.id,t.id),))))
    store.bind_tasks('research',[t.id],tasks)
    go(ctl.tick())
    store.remember('research',Observation('evt','', 'environment_change','new data'))
    with pytest.raises(ValueError): go(ctl.tick())
    assert tasks.get(t.id).status==TaskStatus.COMPLETED


def test_llm_rolling_planner_runtime_contract(env):
    from biomni_dual import AgentRollingPlanner, AgentResult
    class MockRuntime:
        async def run(self,req):
            assert req.role=='planner' and not req.allowed_capabilities
            return AgentResult(True,'{"action":"explore","reason":"inspect first","patch":{"additions":[{"key":"inspect","prompt":"inspect data"}],"dependencies":[]}}')
    tasks,store=env
    store.create_goal('multimodal research',goal_id='g')
    ctl=AdaptiveController(goal_id='g',store=store,tasks=tasks,planner=AgentRollingPlanner(MockRuntime()),worker=Worker(),validator=Validator())
    assert go(ctl.tick()).action==Action.EXPLORE
    assert len(ctl.snapshot().tasks)==1


def test_llm_rolling_planner_invalid_schema(env):
    from biomni_dual import AgentRollingPlanner, AgentResult
    class MockRuntime:
        async def run(self,req):
            return AgentResult(True,'{"action":"execute","patch":{}}')
    tasks,store=env
    ctl=make(tasks,store,lambda s:PlanProposal(Action.ASK_HUMAN))
    planner=AgentRollingPlanner(MockRuntime())
    with pytest.raises(ValueError): go(planner.propose(ctl.snapshot()))


def test_validation_failure_rolls_into_atomic_recovery(env):
    tasks,store=env
    failing=tasks.submit('run scientific analysis',max_attempts=1)
    class Check:
        async def validate(self,task,output):
            return ('repair' in task.prompt or task.attempts > 1),'needs data leakage check' 
    def planner(s):
        if any(t.status == TaskStatus.FAILED for t in s.tasks):
            return PlanProposal(Action.REPLAN,DAGPatch(
                additions=({'key':'fix','prompt':'repair data leakage'},),
                recovery=(failing.id,'fix'),reason='validation') )
        return PlanProposal(Action.ASK_HUMAN)
    ctl=make(tasks,store,planner,validator=Check())
    store.bind_tasks('research',[failing.id],tasks)
    assert go(ctl.tick()).action==Action.VALIDATE
    assert tasks.get(failing.id).status==TaskStatus.FAILED
    assert go(ctl.tick()).action==Action.REPLAN
    assert tasks.get(failing.id).status==TaskStatus.PENDING
    assert len(tasks.get(failing.id).depends_on)==1
    assert go(ctl.tick()).action==Action.EXECUTE  # repair runs first
    assert go(ctl.tick()).action==Action.VALIDATE # original still fails unless genuinely corrected
    assert tasks.get(failing.id).status==TaskStatus.FAILED
    # Same failed task may not be automatically recovered more than once.
    with pytest.raises(ValueError,match='already automatically recovered'):
        go(ctl.tick())


def test_recovery_graph_cycle_rolls_back(env):
    tasks,store=env
    failed=tasks.submit('failed',max_attempts=1)
    tasks.claim(failed.id);tasks.finish(failed.id,error='oops',retryable=False)
    ctl=make(tasks,store,lambda s:PlanProposal(Action.REPLAN,DAGPatch(
        additions=({'key':'fix','prompt':'fix','depends_on':[failed.id]},),recovery=(failed.id,'fix'))))
    store.bind_tasks('research',[failed.id],tasks)
    before=len(tasks.list())
    with pytest.raises(ValueError,match='cycle'):
        go(ctl.tick())
    assert len(tasks.list())==before
    assert tasks.get(failed.id).status==TaskStatus.FAILED
