import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from biomni_dual import (ActiveExperimentStore, ActiveExperimentCoordinator, ExperimentPlan,
    TrialMode, ExperimentState, TrialResult, FailureRoute, RepairContext, Diagnosis, AuditLog)


def build(tmp_path, **kwargs):
    root = tmp_path/'trusted'; root.mkdir(exist_ok=True)
    store = ActiveExperimentStore(tmp_path/'exp.sqlite', evidence_root=root,
                                  trusted_verifiers=('external-verifier',))
    plan = ExperimentPlan('pilot-1', RepairContext('genomics','capability_unavailable_or_broken','skill.qc'),
                          FailureRoute.REPAIR_CAPABILITY, FailureRoute.REPLAN_TASK,
                          max_units=12,max_total_cost=30,max_cost_per_trial=2,**kwargs)
    store.propose(plan)
    return store,plan,root


def submit(store,plan,root,unit,accepted=True,cost=1):
    assignment = store.assign(plan.id,unit)
    evidence = {'experiment_id':plan.id, 'unit_id':unit, 'route':assignment.route.value,
                'protocol_version':plan.protocol_version, 'verifier_id':'external-verifier',
                'accepted':accepted}
    path = root/f'{unit}.json'
    path.write_text(json.dumps(evidence))
    result = TrialResult(plan.id,unit,assignment.route,accepted,cost,1.5,
                         'external-verifier',str(path),hashlib.sha256(path.read_bytes()).hexdigest())
    return assignment, result


def test_requires_approval_and_rejects_agent_actor(tmp_path):
    store,plan,root=build(tmp_path)
    with pytest.raises(PermissionError): store.assign(plan.id,'u')
    with pytest.raises(ValueError): store.transition(plan.id,approve=True,approver='agent:claude')
    assert store.transition(plan.id,approve=True,approver='human')==ExperimentState.APPROVED
    assert store.transition(plan.id,approve=True,approver='human')==ExperimentState.APPROVED
    assert AuditLog(tmp_path/'exp.sqlite').verify()


def test_balanced_allocations_stable_across_restart(tmp_path):
    store,plan,root=build(tmp_path)
    store.transition(plan.id,approve=True,approver='reviewer')
    assignments=[store.assign(plan.id,f'u-{i}') for i in range(12)]
    assert abs(sum(x.route == plan.control for x in assignments)-sum(x.route == plan.treatment for x in assignments))<=1
    reopened=ActiveExperimentStore(tmp_path/'exp.sqlite',evidence_root=root,trusted_verifiers=('external-verifier',))
    assert reopened.assign(plan.id,'u-0')==assignments[0]
    with pytest.raises(RuntimeError,match='unit budget'): reopened.assign(plan.id,'overflow')


def test_cost_budget_is_reserved_including_unfinished_trials(tmp_path):
    store,plan,root=build(tmp_path)
    limited=replace(plan,id='limited',max_total_cost=4)
    store.propose(limited)
    store.transition(limited.id,approve=True,approver='reviewer')
    store.assign(limited.id,'one');store.assign(limited.id,'two')
    with pytest.raises(RuntimeError,match='cost budget'):store.assign(limited.id,'three')


def test_evidence_bound_to_assignment_and_deduplicated(tmp_path):
    store,plan,root=build(tmp_path)
    store.transition(plan.id,approve=True,approver='reviewer')
    _, result=submit(store,plan,root,'one')
    assert store.record(result)
    assert store.record(result) is False
    with pytest.raises(ValueError,match='Conflicting'):store.record(replace(result,measured_cost=1.5))
    bad=replace(result,unit_id='other')
    with pytest.raises(ValueError):store.record(bad)
    assert AuditLog(tmp_path/'exp.sqlite').verify()


def test_no_agent_self_verification_and_tamper(tmp_path):
    store,plan,root=build(tmp_path)
    store.transition(plan.id,approve=True,approver='reviewer')
    _,result=submit(store,plan,root,'one')
    with pytest.raises(PermissionError):store.record(replace(result,verifier_id='untrusted'))
    path=Path(result.evidence_path);path.write_text('tampered')
    with pytest.raises(ValueError,match='digest'):store.record(result)
    path.write_text('{}')
    modified=replace(result,evidence_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError,match='claims'):store.record(modified)


def test_path_escape_rejected(tmp_path):
    store,plan,root=build(tmp_path)
    store.transition(plan.id,approve=True,approver='reviewer')
    _,result=submit(store,plan,root,'one')
    outside=tmp_path/'outside.json';outside.write_bytes(Path(result.evidence_path).read_bytes())
    with pytest.raises(ValueError,match='trusted root'):
        store.record(replace(result,evidence_path=str(outside)))


def test_report_needs_minimum_samples_and_never_authorizes_rollout(tmp_path):
    store,plan,root=build(tmp_path)
    store.transition(plan.id,approve=True,approver='reviewer')
    assert store.report(plan.id).decision=='insufficient_evidence'
    for i in range(12):
        assignment,result=submit(store,plan,root,f'u{i}',accepted=False)
        if assignment.route == plan.treatment:
            evidence=json.loads(Path(result.evidence_path).read_text())
            evidence['accepted']=True
            Path(result.evidence_path).write_text(json.dumps(evidence))
            result=replace(result,accepted=True,evidence_sha256=hashlib.sha256(Path(result.evidence_path).read_bytes()).hexdigest())
        assert store.record(result)
    report=store.report(plan.id)
    assert report.control.completed == report.treatment.completed == 6
    assert report.treatment.success_rate==1 and report.control.success_rate==0
    assert report.decision=='treatment_promising'
    assert report.production_rollout_authorized is False
    assert report.total_reserved == 24
    assert report.total_observed == 12


def test_closed_experiment_refuses_new_allocations_but_old_assignment_stable(tmp_path):
    store,plan,root=build(tmp_path)
    store.transition(plan.id,approve=True,approver='human')
    assignment=store.assign(plan.id,'one')
    store.close(plan.id)
    assert store.assign(plan.id,'one')==assignment
    with pytest.raises(PermissionError):store.assign(plan.id,'two')
    _,res=submit_existing_result(store,plan,root,assignment)
    # Already persisted results may be retrieved, but new submissions after close are prohibited.
    with pytest.raises(PermissionError):store.record(res)


def submit_existing_result(store,plan,root,assignment):
    evidence={'experiment_id':plan.id,'unit_id':assignment.unit_id,'route':assignment.route.value,
        'protocol_version':plan.protocol_version,'verifier_id':'external-verifier','accepted':True}
    path=root/'existing.json';path.write_text(json.dumps(evidence))
    return assignment,TrialResult(plan.id,assignment.unit_id,assignment.route,True,1,1,
        'external-verifier',str(path),hashlib.sha256(path.read_bytes()).hexdigest())


def test_reject_high_risk_arms_and_sensitive_data(tmp_path):
    context=RepairContext('biology','broken')
    args=('x',context,FailureRoute.REPAIR_CAPABILITY,FailureRoute.REPLAN_TASK)
    with pytest.raises(PermissionError):ExperimentPlan(*args,side_effects_allowed=True)
    with pytest.raises(PermissionError):ExperimentPlan(*args,sensitive_data_allowed=True)
    with pytest.raises(ValueError):ExperimentPlan('x',context,FailureRoute.ASK_HUMAN,FailureRoute.REPLAN_TASK)
    with pytest.raises(ValueError):ExperimentPlan('x',context,FailureRoute.REPLAN_TASK,FailureRoute.REPLAN_TASK)


def test_coordinator_only_proposes_eligible_failures(tmp_path):
    store,plan,root=build(tmp_path)
    coordinator=ActiveExperimentCoordinator(store)
    diag=Diagnosis(FailureRoute.REPAIR_CAPABILITY,'capability_unavailable_or_broken',False,'task',1)
    result=coordinator.propose_for_failure(diag,plan.context,experiment_id='pilot-2')
    assert result.control==diag.route
    assert store.get('pilot-2')[1] == ExperimentState.PROPOSED
    with pytest.raises(PermissionError):
        coordinator.propose_for_failure(Diagnosis(FailureRoute.ASK_HUMAN,diag.reason,False,'task',1),
                                        plan.context,experiment_id='bad')


def test_reject_wrong_protocol_and_cost_excess(tmp_path):
    store,plan,root=build(tmp_path)
    store.transition(plan.id,approve=True,approver='human')
    _,r=submit(store,plan,root,'one')
    with pytest.raises(ValueError,match='Protocol mismatch'):store.record(replace(r,protocol_version='v2'))
    with pytest.raises(ValueError,match='cost exceeded'):store.record(replace(r,measured_cost=100))


def test_experiment_priority_uses_joint_history_without_relaxing_gates(tmp_path):
    from biomni_dual import ExperimentPriorityPolicy, JointExperienceStore
    store,plan,_=build(tmp_path)
    experience=JointExperienceStore(tmp_path/'history.sqlite')
    policy=ExperimentPriorityPolicy(experience)
    opportunity=policy.assess(plan,remaining_budget=30)
    assert opportunity.worth_testing and opportunity.baseline_samples==0
    assert not policy.assess(plan,remaining_budget=10).worth_testing
    assert not policy.assess(plan,remaining_budget=30,side_effect_uncertain=True).worth_testing
    assert not policy.assess(plan,remaining_budget=30,permissions_ok=False).worth_testing


def test_experiment_policy_does_not_mutate_history(tmp_path):
    from biomni_dual import ExperimentPriorityPolicy, JointExperienceStore
    _,plan,_=build(tmp_path)
    experiences=JointExperienceStore(tmp_path/'history.sqlite')
    before=experiences.count()
    opportunity=ExperimentPriorityPolicy(experiences).assess(plan,remaining_budget=50)
    assert opportunity.worth_testing
    assert experiences.count()==before
