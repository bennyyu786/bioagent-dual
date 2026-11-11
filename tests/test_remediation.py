import asyncio
import sqlite3
from pathlib import Path

import pytest

from biomni_dual import (TaskCenter, TaskStatus, DAGPatch, AdaptiveStore,
    FailureRoute, Diagnosis, AutonomousRemediationOrchestrator, RemediationState,
    RuntimeRepairPlanner, AgentResult)


def run(coro):
    return asyncio.run(coro)


def task_failed(center, *, capabilities=(), max_attempts=1):
    t=center.submit('Analyze scientific data',required_capabilities=list(capabilities),max_attempts=max_attempts)
    center.claim(t.id)
    center.finish(t.id,error='synthetic failure',retryable=False)
    return center.get(t.id)


def diagnosis(task,route):
    return Diagnosis(route,'test_diagnostic',False,task.id,task.attempts)


def proposal(task, d, job_id):
    return DAGPatch(additions=({'key':'review',
        'prompt':'Independently review and validate the scientific result',
        'depends_on':[],'required_capabilities':[]},),
        reason='remediation:'+job_id,recovery=(task.id,'review'))


def setup(tmp_path, *, planner=None, capabilities=(), max_per_task=3, goal=False):
    path=tmp_path/'tasks.sqlite'
    center=TaskCenter(path)
    store=None;gid=None
    t=task_failed(center,capabilities=capabilities)
    if goal:
        store=AdaptiveStore(path)
        gid=store.create_goal('A scientific problem')
        store.bind_tasks(gid,[t.id],center)
    orch=AutonomousRemediationOrchestrator(center,database=path,
        repair_planner=planner, max_per_task=max_per_task,
        adaptive_store=store, goal_id=gid)
    return center,t,orch,store,gid


@pytest.mark.parametrize('route',[FailureRoute.REPLAN_TASK,FailureRoute.REPAIR_EXECUTION,FailureRoute.REVIEW_RESULT])
def test_repair_creates_single_recovery_step(tmp_path,route):
    center,t,o,_,_=setup(tmp_path,planner=proposal)
    j=o.submit(diagnosis(t,route))
    assert o.submit(diagnosis(t,route)).id==j.id
    a=run(o.advance(j.id))
    assert a.state==RemediationState.APPLIED
    assert center.get(t.id).status==TaskStatus.PENDING
    assert a.recovery_task_id in center.get(t.id).depends_on
    assert run(o.advance(j.id))==a
    assert len([x for x in center.list() if x.id!=t.id])==1


def test_invalid_review_plan_never_requeues_task(tmp_path):
    def wrong(task,d,j):
        return DAGPatch(additions=({'key':'fix','prompt':'Just retry','depends_on':[]},),
                        recovery=(task.id,'fix'))
    center,t,o,_,_=setup(tmp_path,planner=wrong)
    a=run(o.advance(o.submit(diagnosis(t,FailureRoute.REVIEW_RESULT)).id))
    assert a.state==RemediationState.BLOCKED
    assert center.get(t.id).status==TaskStatus.FAILED


def test_no_automatic_replay_of_unknown_effects(tmp_path):
    center=TaskCenter(tmp_path/'tasks.sqlite')
    task=center.submit('remote mutation')
    center.claim(task.id)
    center.block_running(task.id)
    o=AutonomousRemediationOrchestrator(center,database=tmp_path/'tasks.sqlite',repair_planner=proposal)
    d=Diagnosis(FailureRoute.ASK_HUMAN,'uncertain',False,task.id,1)
    j=o.submit(d)
    assert j.state==RemediationState.BLOCKED
    assert run(o.advance(j.id)).state==RemediationState.BLOCKED
    assert center.get(task.id).status==TaskStatus.BLOCKED


def test_retry_requires_ready_pending_task(tmp_path):
    center=TaskCenter(tmp_path/'tasks.sqlite')
    task=center.submit('retryable task',max_attempts=2)
    center.claim(task.id)
    center.finish(task.id,error='temporary',retryable=True)
    o=AutonomousRemediationOrchestrator(center,database=tmp_path/'tasks.sqlite')
    j=o.submit(diagnosis(center.get(task.id),FailureRoute.RETRY_WORKER))
    assert run(o.advance(j.id)).state==RemediationState.APPLIED
    assert center.get(task.id).attempts==1


def test_idempotent_conflict(tmp_path):
    _,t,o,_,_=setup(tmp_path)
    o.submit(diagnosis(t,FailureRoute.REPLAN_TASK))
    with pytest.raises(ValueError,match='Conflicting'):
        o.submit(diagnosis(t,FailureRoute.REVIEW_RESULT))


def test_budget_is_cumulative(tmp_path):
    center,t,o,_,_=setup(tmp_path,max_per_task=1)
    j=o.submit(diagnosis(t,FailureRoute.REPLAN_TASK))
    assert j.state==RemediationState.PROPOSED
    # Simulate a further, distinct failed attempt in the same job lineage.
    with center._connect() as db:
        import json
        obj=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(t.id,)).fetchone()[0])
        obj['attempts']=2
        db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(obj),t.id))
    next_attempt=o.submit(diagnosis(center.get(t.id),FailureRoute.REPLAN_TASK))
    assert next_attempt.state==RemediationState.BLOCKED


def test_cross_goal_planner_proposal_rejected(tmp_path):
    center,t,o,store,gid=setup(tmp_path,goal=True)
    foreign=center.submit('Other goal')
    def outside(task,d,j):
        return DAGPatch(additions=({'key':'repair','prompt':'Review',
            'depends_on':[foreign.id]},),recovery=(task.id,'repair'))
    o.repair_planner=outside
    job=o.submit(diagnosis(t,FailureRoute.REPLAN_TASK))
    r=run(o.advance(job.id))
    assert r.state==RemediationState.BLOCKED
    assert center.get(t.id).status==TaskStatus.FAILED
    assert len(store.task_ids(gid))==1


def test_goal_recovery_registration(tmp_path):
    center,t,o,store,gid=setup(tmp_path,goal=True,planner=proposal)
    j=run(o.advance(o.submit(diagnosis(t,FailureRoute.REPLAN_TASK)).id))
    assert j.recovery_task_id in store.task_ids(gid)


def test_commit_crash_reconciled_without_duplicate_dag(tmp_path):
    center,t,o,_,_=setup(tmp_path,planner=proposal)
    j=o.submit(diagnosis(t,FailureRoute.REPLAN_TASK))
    o.dag.commit(proposal(t,diagnosis(t,FailureRoute.REPLAN_TASK),j.id))
    restarted=AutonomousRemediationOrchestrator(center,database=tmp_path/'tasks.sqlite',repair_planner=proposal)
    result=run(restarted.advance(j.id))
    assert result.state==RemediationState.APPLIED
    assert len(center.list())==2


class FakeBuilder:
    def __init__(self): self.records={};self.n=0
    def request(self,cid,kind,requirement,*,request_key,task_id):
        self.n+=1
        self.records['build-1']={'id':'build-1','state':'REQUESTED'}
        return self.records['build-1']
    def _get(self,request_id): return dict(self.records[request_id])
    async def prepare(self,request_id):
        self.records[request_id]['state']='AWAITING_APPROVAL'
        return dict(self.records[request_id])


def test_capability_build_waits_for_human_then_recovery(tmp_path):
    center,t,o,_,_=setup(tmp_path,capabilities=['skill.variant_qc'])
    builder=FakeBuilder();o.capability_builder=builder
    j=o.submit(diagnosis(t,FailureRoute.REPAIR_CAPABILITY))
    j=run(o.advance(j.id))
    assert j.state==RemediationState.WAITING_APPROVAL
    assert builder.n==1 and center.get(t.id).status==TaskStatus.FAILED
    assert run(o.advance(j.id)).state==RemediationState.WAITING_APPROVAL
    builder.records['build-1']['state']='PUBLISHED' # mimics EXTERNAL human approval
    done=run(o.reconcile(j.id))
    assert done.state==RemediationState.APPLIED
    assert center.get(t.id).status==TaskStatus.PENDING
    assert center.get(done.recovery_task_id).required_capabilities==['skill.variant_qc']


def test_builder_rejection_blocks(tmp_path):
    center,t,o,_,_=setup(tmp_path,capabilities=['skill.variant_qc'])
    b=FakeBuilder();o.capability_builder=b
    j=run(o.advance(o.submit(diagnosis(t,FailureRoute.REPAIR_CAPABILITY)).id))
    b.records['build-1']['state']='REJECTED'
    assert run(o.reconcile(j.id)).state==RemediationState.BLOCKED
    assert center.get(t.id).status==TaskStatus.FAILED


def test_missing_builder_blocked(tmp_path):
    _,t,o,_,_=setup(tmp_path,capabilities=['skill.variant_qc'])
    j=run(o.advance(o.submit(diagnosis(t,FailureRoute.REPAIR_CAPABILITY)).id))
    assert j.state==RemediationState.BLOCKED


def test_audit_log(tmp_path):
    from biomni_dual import AuditLog
    center,t,o,_,_=setup(tmp_path,planner=proposal)
    run(o.advance(o.submit(diagnosis(t,FailureRoute.REPLAN_TASK)).id))
    assert AuditLog(tmp_path/'tasks.sqlite').verify()


def test_runtime_planner_rejects_untrusted_schema(tmp_path):
    class Runtime:
        async def run(self,req):
            return AgentResult(success=True,text='{"hello":"world"}')
    _,t,o,_,_=setup(tmp_path)
    p=RuntimeRepairPlanner(Runtime())
    with pytest.raises(Exception):
        run(p(t,diagnosis(t,FailureRoute.REPLAN_TASK),'job'))


def test_adaptive_controller_dispatches_actual_remediation(tmp_path):
    from biomni_dual import (AdaptiveController, CallbackPlanner, PlanProposal,
                             Action, NonEmptyValidator, FailureDiagnosisEngine)
    path=tmp_path/'tasks.sqlite'
    center=TaskCenter(path)
    store=AdaptiveStore(path)
    gid=store.create_goal('Recover a broken analysis',goal_id='research')
    t=center.submit('Analyze data',max_attempts=1)
    store.bind_tasks(gid,[t.id],center)
    diagnosis_engine=FailureDiagnosisEngine(path)
    remediation=AutonomousRemediationOrchestrator(center,database=path,
        repair_planner=proposal,adaptive_store=store,goal_id=gid)

    class BrokenWorker:
        async def run(self, task, caps):
            raise ValueError('input schema invalid')

    controller=AdaptiveController(goal_id=gid,store=store,tasks=center,
        planner=CallbackPlanner(lambda snap: PlanProposal(Action.ASK_HUMAN)),
        worker=BrokenWorker(),validator=NonEmptyValidator(),
        failure_diagnosis=diagnosis_engine, remediation_orchestrator=remediation)
    result=run(controller.tick())
    assert result.evidence['route']==FailureRoute.REPLAN_TASK.value
    assert result.evidence['remediation_state']==RemediationState.APPLIED.value
    job=remediation.for_task(t.id)[0]
    assert job.recovery_task_id in store.task_ids(gid)
    assert center.get(t.id).status==TaskStatus.PENDING
    assert job.recovery_task_id in center.get(t.id).depends_on


def test_crash_after_dag_before_goal_binding_is_reconciled(tmp_path):
    center,t,o,store,gid=setup(tmp_path,goal=True,planner=proposal)
    j=o.submit(diagnosis(t,FailureRoute.REPLAN_TASK))
    assigned=o.dag.commit(proposal(t,diagnosis(t,FailureRoute.REPLAN_TASK),j.id))
    new_id=assigned['review']
    assert new_id not in store.task_ids(gid)
    restarted=AutonomousRemediationOrchestrator(center,database=tmp_path/'tasks.sqlite',
        repair_planner=proposal,adaptive_store=store,goal_id=gid)
    restored=run(restarted.advance(j.id))
    assert restored.state==RemediationState.APPLIED
    assert new_id in store.task_ids(gid)
    assert len(center.list())==2


def test_adaptive_restart_reconstructs_missing_remediation_handoff(tmp_path):
    from biomni_dual import (AdaptiveController, CallbackPlanner, PlanProposal,
                             Action, NonEmptyValidator, FailureDiagnosisEngine,
                             FailureSignal, FailureOrigin, ErrorKind)
    path=tmp_path/'tasks.sqlite'
    center,t,orch,store,gid=setup(tmp_path,goal=True,planner=proposal)
    diagnosis_engine=FailureDiagnosisEngine(path)
    diagnosis_engine.diagnose(FailureSignal(t.id,t.attempts,FailureOrigin.EXECUTION,
        'bad_input',ErrorKind.INVALID_INPUT))
    class Worker:
        async def run(self,task,capabilities): return 'validated'
    ctl=AdaptiveController(goal_id=gid,store=store,tasks=center,
        planner=CallbackPlanner(lambda s:PlanProposal(Action.ASK_HUMAN)),
        worker=Worker(),validator=NonEmptyValidator(),
        failure_diagnosis=diagnosis_engine,remediation_orchestrator=orch)
    decision=run(ctl.tick())
    assert decision.reason=='remediation_reconciled'
    assert center.get(t.id).status==TaskStatus.PENDING
    assert len(orch.for_task(t.id))==1
    assert len(center.list())==2


def test_execution_repair_requires_explicit_human_approval(tmp_path):
    center,t,o,_,_=setup(tmp_path,planner=proposal)
    rem=run(o.advance(o.submit(diagnosis(t,FailureRoute.REPAIR_EXECUTION)).id))
    assert center.get(rem.recovery_task_id).status == TaskStatus.WAITING_APPROVAL
    assert rem.recovery_task_id not in [x.id for x in center.ready()]
    center.approve(rem.recovery_task_id)
    assert rem.recovery_task_id in [x.id for x in center.ready()]
