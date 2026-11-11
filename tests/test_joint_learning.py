import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from biomni_dual import (
    AuditLog, AutonomousRemediationOrchestrator, Capability, CapabilityKind,
    CapabilityRegistry, Diagnosis, FailureRoute, JointExperienceStore,
    JointRemediationController, RepairContext, RepairOutcome, RepairStrategyPolicy,
    RemediationState, TaskCenter, TaskStatus,
)


def outcome(uid, context, route, accepted, evidence_path):
    return RepairOutcome(uid, context, route, accepted, 'independent-validator',
                         hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                         2.0, 4.0, str(evidence_path))


def diagnostic(task, route=FailureRoute.REPAIR_CAPABILITY):
    return Diagnosis(route, 'capability_unavailable_or_broken', False, task.id, task.attempts)


def setup(tmp_path, required=None):
    path = tmp_path / 'state.sqlite'
    tasks = TaskCenter(path)
    task = tasks.submit('Scientific task', required_capabilities=required or [], max_attempts=1)
    tasks.claim(task.id)
    tasks.finish(task.id, error='broken')
    orch = AutonomousRemediationOrchestrator(tasks, database=path)
    store = JointExperienceStore(path)
    cap = CapabilityRegistry()
    for cid in (required or []):
        cap.register(Capability(cid, 'Capability', CapabilityKind.SKILL, healthy=False))
    trusted = tmp_path / 'trusted'
    trusted.mkdir()
    evidence = trusted / 'report.json'
    evidence.write_text('{"verifier":"independent","passed":true}')
    ctl = JointRemediationController(tasks, orch, store, capabilities=cap, evidence_root=trusted,
                                     trusted_verifiers=('independent-validator',))
    return tasks, task, orch, store, ctl, evidence


def test_cold_start_keeps_original(tmp_path):
    *_, store, ctl, evidence = setup(tmp_path, ['skill.qc'])
    context = RepairContext('genomics', 'broken', 'skill.qc')
    result = RepairStrategyPolicy(store).advise(
        Diagnosis(FailureRoute.REPAIR_CAPABILITY, 'broken', False, 'job', 1),context,
        approved_routes=[FailureRoute.REPLAN_TASK])
    assert result.recommended == FailureRoute.REPAIR_CAPABILITY
    assert result.reason == 'insufficient_or_unauthorized_evidence'


def test_proven_history_changes_recommendation(tmp_path):
    store = JointExperienceStore(tmp_path/'store.sqlite')
    ctx = RepairContext('genomics', 'broken', 'skill.qc')
    evidence = tmp_path/'evidence'; evidence.write_text('proof')
    for i in range(6):
        store.record(outcome(f'cap-{i}', ctx, FailureRoute.REPAIR_CAPABILITY, False, evidence))
        store.record(outcome(f'plan-{i}', ctx, FailureRoute.REPLAN_TASK, True, evidence))
    advice = RepairStrategyPolicy(store).advise(
        Diagnosis(FailureRoute.REPAIR_CAPABILITY, 'broken', False, 'task', 1), ctx,
        approved_routes=[FailureRoute.REPLAN_TASK])
    assert advice.recommended == FailureRoute.REPLAN_TASK
    assert advice.requires_approval
    assert advice.evidence[0].samples == 6
    assert advice.evidence[0].posterior_success == .125
    assert advice.evidence[1].posterior_success == .875


def test_no_auto_route_change_without_authorization(tmp_path):
    store = JointExperienceStore(tmp_path/'history.sqlite')
    ctx = RepairContext('genomics', 'broken')
    evidence = tmp_path/'artifact';evidence.write_text('proof')
    for i in range(4):
        store.record(outcome(f'a{i}',ctx,FailureRoute.REPAIR_CAPABILITY,False,evidence))
        store.record(outcome(f'b{i}',ctx,FailureRoute.REPLAN_TASK,True,evidence))
    diagnosis = Diagnosis(FailureRoute.REPAIR_CAPABILITY,'broken',False,'task',1)
    assert RepairStrategyPolicy(store).advise(diagnosis,ctx).recommended == FailureRoute.REPAIR_CAPABILITY
    assert RepairStrategyPolicy(store).advise(diagnosis,ctx,side_effect_uncertain=True,
        approved_routes=[FailureRoute.REPLAN_TASK]).recommended == FailureRoute.ASK_HUMAN


def test_restricted_routes_never_overridden(tmp_path):
    store=JointExperienceStore(tmp_path/'history.sqlite')
    p=RepairStrategyPolicy(store)
    ctx=RepairContext('research','authorization_required')
    for route in (FailureRoute.ASK_HUMAN,FailureRoute.REVIEW_RESULT,FailureRoute.RETRY_WORKER):
        advice=p.advise(Diagnosis(route,'authorization_required',False,'x',1),ctx,
                        approved_routes=[FailureRoute.REPLAN_TASK])
        assert advice.recommended == route


def test_estimates_exact_context_only(tmp_path):
    store=JointExperienceStore(tmp_path/'history.sqlite')
    evidence=tmp_path/'proof'; evidence.write_text('independent')
    ctx=RepairContext('genomics','broken','skill.qc')
    store.record(outcome('one',ctx,FailureRoute.REPAIR_CAPABILITY,True,evidence))
    assert store.estimate(ctx,FailureRoute.REPAIR_CAPABILITY).samples==1
    assert store.estimate(RepairContext('proteomics','broken','skill.qc'),FailureRoute.REPAIR_CAPABILITY).samples==0
    assert store.estimate(RepairContext('genomics','broken','skill.other'),FailureRoute.REPAIR_CAPABILITY).samples==0


def test_idempotence_and_conflict(tmp_path):
    store=JointExperienceStore(tmp_path/'history.sqlite')
    evidence=tmp_path/'proof';evidence.write_text('independent')
    ctx=RepairContext('genomics','broken')
    entry=outcome('remediation-1',ctx,FailureRoute.REPLAN_TASK,True,evidence)
    assert store.record(entry)
    assert not store.record(entry)
    with pytest.raises(ValueError,match='Conflicting'):
        store.record(replace(entry,accepted=False))
    assert AuditLog(tmp_path/'history.sqlite').verify()


def test_reject_bad_evidence_and_nonindependent_verifier(tmp_path):
    ctx=RepairContext('genomics','broken')
    evidence=tmp_path/'proof'; evidence.write_text('valid')
    valid=outcome('r1',ctx,FailureRoute.REPLAN_TASK,True,evidence)
    with pytest.raises(ValueError,match='SHA256'):
        replace(valid,evidence_sha256='invalid')
    with pytest.raises(ValueError,match='Independent verifier'):
        replace(valid,verifier_id='agent:claude')
    with pytest.raises(ValueError):
        replace(valid,measured_cost=-1)


def test_joint_snapshot_includes_capability_health(tmp_path):
    tasks, task, orch, store, ctl, evidence = setup(tmp_path,['skill.qc'])
    snap=ctl.snapshot(task.id)
    assert snap.status==TaskStatus.FAILED.value
    assert snap.capability_health==(('skill.qc',False),)
    assert snap.known_outcomes==0


def test_record_requires_applied_and_reexecuted(tmp_path):
    tasks, task, orch, store, ctl, evidence = setup(tmp_path,['skill.qc'])
    j=orch.submit(diagnostic(tasks.get(task.id)))
    ctx=RepairContext('genomics',j.reason,'skill.qc')
    with pytest.raises(ValueError,match='Post-repair'):
        ctl.record_verified(outcome(j.id,ctx,j.route,True,evidence))
    assert store.count()==0


def test_record_requires_validated_artifact_inside_root(tmp_path):
    tasks, task, orch, store, ctl, evidence=setup(tmp_path)
    job=SimpleNamespace(id='job1',task_id=task.id,attempt=1,
        route=FailureRoute.REPLAN_TASK,reason='invalid_input',state=RemediationState.APPLIED)
    orch.get=lambda _:job
    # Simulate post-remediation successful reexecution.
    with tasks._connect() as db:
        import json
        obj=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task.id,)).fetchone()[0]);
        obj['status']='completed';obj['attempts']=2
        db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(obj),task.id))
    ctx=RepairContext('genomics',job.reason)
    record=outcome(job.id,ctx,job.route,True,evidence)
    outside=tmp_path/'outside';outside.write_text('outside')
    with pytest.raises(ValueError,match='trusted'):
        ctl.record_verified(outcome(job.id,ctx,job.route,True,outside))
    with pytest.raises(ValueError,match='digest'):
        ctl.record_verified(replace(record,evidence_sha256='0'*64))
    assert ctl.record_verified(record)
    assert not ctl.record_verified(record)
    assert ctl.snapshot(task.id).known_outcomes==0  # mock job is not in remediation table


def test_observed_repair_failure_is_learnable(tmp_path):
    tasks,task,orch,store,ctl,evidence=setup(tmp_path)
    job=SimpleNamespace(id='job2',task_id=task.id,attempt=1,
        route=FailureRoute.REPLAN_TASK,reason='invalid_input',state=RemediationState.APPLIED)
    orch.get=lambda _:job
    with tasks._connect() as db:
        import json
        obj=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task.id,)).fetchone()[0]);obj['attempts']=2
        db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(obj),task.id))
    ctx=RepairContext('genomics',job.reason)
    assert ctl.record_verified(outcome(job.id,ctx,job.route,False,evidence))
    assert store.estimate(ctx,job.route).samples==1
    with pytest.raises(ValueError,match='completed task'):
        ctl.record_verified(outcome('other',ctx,job.route,True,evidence))


def test_no_evidence_root_fails_closed(tmp_path):
    tasks,task,orch,store,ctl,evidence=setup(tmp_path)
    bad=JointRemediationController(tasks,orch,store)
    job=SimpleNamespace(id='job',task_id=task.id,attempt=0,
       route=FailureRoute.REPLAN_TASK,reason='bad',state=RemediationState.APPLIED)
    orch.get=lambda _:job
    with tasks._connect() as db:
        import json
        obj=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task.id,)).fetchone()[0]);obj['attempts']=2
        db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(obj),task.id))
    with pytest.raises(ValueError,match='Independent evidence root'):
        bad.record_verified(outcome('job',RepairContext('x','bad'),job.route,False,evidence))


def test_strategy_respects_cost_gate(tmp_path):
    store=JointExperienceStore(tmp_path/'s.sqlite')
    ctx=RepairContext('omics','broken')
    evidence=tmp_path/'proof'; evidence.write_text('proof')
    for i in range(5):
        store.record(outcome(f'x{i}',ctx,FailureRoute.REPAIR_EXECUTION,False,evidence))
        store.record(outcome(f'y{i}',ctx,FailureRoute.REPLAN_TASK,True,evidence))
    policy=RepairStrategyPolicy(store,max_mean_cost=1)
    advice=policy.advise(Diagnosis(FailureRoute.REPAIR_EXECUTION,'broken',False,'t',1),ctx,
                          approved_routes=[FailureRoute.REPLAN_TASK])
    assert advice.recommended==FailureRoute.REPAIR_EXECUTION


def test_task_status_fail_closed_on_route_switch(tmp_path):
    tasks,task,orch,store,ctl,evidence=setup(tmp_path,['skill.qc'])
    with pytest.raises(PermissionError):
        ctl.submit(diagnostic(task),task_kind='genomics',approved_alternative=FailureRoute.REPLAN_TASK)
    assert len(orch.for_task(task.id))==0


def test_record_conflicting_context_is_rejected(tmp_path):
    tasks,task,orch,store,ctl,evidence=setup(tmp_path,['skill.qc'])
    job=SimpleNamespace(id='r',task_id=task.id,attempt=0,route=FailureRoute.REPAIR_CAPABILITY,
                        reason='missing',state=RemediationState.APPLIED)
    orch.get=lambda _:job
    with tasks._connect() as db:
        import json
        obj=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task.id,)).fetchone()[0]);obj['attempts']=2;obj['status']='completed'
        db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(obj),task.id))
    with pytest.raises(ValueError,match='context mismatch'):
        ctl.record_verified(outcome('r',RepairContext('genomics','missing','other'),job.route,True,evidence))


def test_real_recovery_attempt_reset_still_learns(tmp_path):
    from biomni_dual import DAGPatch
    tasks,task,orch,store,ctl,evidence=setup(tmp_path)
    def planner(task, diagnosis, rid):
        return DAGPatch(additions=({'key':'diagnose','prompt':'Independent data validation',
                                    'depends_on':[]},),
                        recovery=(task.id,'diagnose'),reason='repair')
    orch.repair_planner=planner
    job=orch.submit(Diagnosis(FailureRoute.REPLAN_TASK,'bad_input',False,
                              task.id,tasks.get(task.id).attempts))
    job=asyncio.run(orch.advance(job.id))
    repair=tasks.claim(job.recovery_task_id)
    tasks.finish(repair.id,result='diagnostic report')
    original=tasks.claim(task.id)
    assert original.attempts == 1  # reset by DAG recovery
    tasks.finish(task.id,result='validated')
    recorded=ctl.record_verified(outcome(job.id,RepairContext('genomics','bad_input'),
                                        FailureRoute.REPLAN_TASK,True,evidence))
    assert recorded
    assert ctl.snapshot(task.id).known_outcomes==1


def test_untrusted_verifier_rejected_even_with_valid_hash(tmp_path):
    tasks,task,orch,store,ctl,evidence=setup(tmp_path)
    job=SimpleNamespace(id='j',task_id=task.id,attempt=0,
        route=FailureRoute.REPLAN_TASK,reason='wrong',state=RemediationState.APPLIED)
    orch.get=lambda _:job
    with tasks._connect() as db:
        import json
        obj=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(task.id,)).fetchone()[0])
        obj['attempts']=2;obj['status']='completed'
        db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(obj),task.id))
    record=replace(outcome('j',RepairContext('g','wrong'),job.route,True,evidence),
                   verifier_id='unknown-validator')
    with pytest.raises(PermissionError,match='allowlist'):
        ctl.record_verified(record)


def test_authorized_strategy_is_submitted_to_real_remediation_store(tmp_path):
    tasks,task,orch,store,ctl,evidence=setup(tmp_path,['skill.qc'])
    context=RepairContext('genomics','capability_unavailable_or_broken','skill.qc')
    for i in range(5):
        store.record(outcome(f'cap-{i}',context,FailureRoute.REPAIR_CAPABILITY,False,evidence))
        store.record(outcome(f'plan-{i}',context,FailureRoute.REPLAN_TASK,True,evidence))
    diagnosis=diagnostic(tasks.get(task.id))
    advice=ctl.recommend(diagnosis,task_kind='genomics',approved_routes=(FailureRoute.REPLAN_TASK,))
    assert advice.recommended==FailureRoute.REPLAN_TASK
    job=ctl.submit(diagnosis,task_kind='genomics',approved_alternative=FailureRoute.REPLAN_TASK)
    assert job.route==FailureRoute.REPLAN_TASK
    assert job.state==RemediationState.PROPOSED
    assert AuditLog(tmp_path/'state.sqlite').verify()


def test_joint_snapshot_contains_recovery_dag_version(tmp_path):
    from biomni_dual import DAGPatch
    tasks,task,orch,store,ctl,evidence=setup(tmp_path)
    orch.repair_planner=lambda t,d,r: DAGPatch(
        additions=({'key':'review','prompt':'independent review','depends_on':[]},),
        recovery=(t.id,'review'))
    job=orch.submit(Diagnosis(FailureRoute.REPLAN_TASK,'bad_input',False,
                              task.id,tasks.get(task.id).attempts))
    asyncio.run(orch.advance(job.id))
    snap=ctl.snapshot(task.id)
    assert snap.graph_version==1
    assert snap.remediation_states==('applied',)
