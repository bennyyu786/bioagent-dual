import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from biomni_dual import (
    ActiveExperimentStore, ExperimentPlan, RepairContext, FailureRoute,
    TrialMode, TrialExecutor, TrialExecutionLedger, TrialExecutionState,
    PilotLearningView, DockerSandbox, AuditLog,
)


def make(tmp_path, *, approve=True, checker=None, sandbox_runner=None):
    trusted=tmp_path/'evidence'; trusted.mkdir(exist_ok=True)
    store=ActiveExperimentStore(tmp_path/'experiments.db',evidence_root=trusted,
                                trusted_verifiers=('trusted-checker',))
    plan=ExperimentPlan('exp-1',RepairContext('omics','bad_tools','skill.x'),
                        FailureRoute.REPAIR_CAPABILITY,FailureRoute.REPLAN_TASK,
                        mode=TrialMode.SANDBOX,max_units=8,max_total_cost=12,max_cost_per_trial=1)
    store.propose(plan)
    if approve: store.transition(plan.id,approve=True,approver='human')
    candidates={}
    for route in [plan.control,plan.treatment]:
        directory=tmp_path/route.value
        directory.mkdir()
        (directory/'SKILL.md').write_text('---\nname: fake\ndescription: demo\n---\n')
        (directory/'tests').mkdir()
        (directory/'tests'/'test_dummy.py').write_text('def test_dummy(): assert True\n')
        candidates[route]=directory
    ledger=TrialExecutionLedger(tmp_path/'execution.db')
    async def runner(args,stdin):
        if args[1]=='info':return 0,'["name=rootless"]',''
        if args[1]=='run':return 0,'1 passed',''
        if args[1]=='rm':return 0,'',''
        return 1,'','unexpected'
    async def independent(assignment,validation): return validation.passed
    sandbox=DockerSandbox(runner=sandbox_runner or runner)
    executor=TrialExecutor(experiments=store,ledger=ledger,sandbox=sandbox,
                           candidates=candidates,verifier_id='trusted-checker',
                           independent_checker=checker or independent)
    return store,plan,ledger,executor


def run(coro): return asyncio.run(coro)


def test_real_execution_path_with_injected_docker_transport(tmp_path):
    store,plan,ledger,executor=make(tmp_path)
    receipt=run(executor.execute(plan.id,'unit-1'))
    assert receipt.state==TrialExecutionState.RECORDED
    assert Path(receipt.evidence_path).exists()
    assert hashlib.sha256(Path(receipt.evidence_path).read_bytes()).hexdigest()==receipt.evidence_sha256
    report=store.report(plan.id)
    assert report.control.completed+report.treatment.completed==1
    assert report.production_rollout_authorized is False
    assert AuditLog(tmp_path/'execution.db').verify()
    assert AuditLog(tmp_path/'experiments.db').verify()


def test_idempotent_execution_claim_and_no_automatic_replay(tmp_path):
    store,plan,ledger,executor=make(tmp_path)
    run(executor.execute(plan.id,'unit-1'))
    with pytest.raises(RuntimeError, match='already claimed'):
        run(executor.execute(plan.id,'unit-1'))


def test_unapproved_protocol_never_executes(tmp_path):
    store,plan,ledger,executor=make(tmp_path,approve=False)
    with pytest.raises(PermissionError,match='not approved'):
        run(executor.execute(plan.id,'unit-1'))
    assert ledger.status(plan.id,'unit-1') is None


def test_failure_becomes_uncertain_not_scientific_failure(tmp_path):
    async def broken(args,stdin):
        if args[1]=='info':return 0,'["rootless"]',''
        if args[1]=='run':raise RuntimeError('Docker transport failure')
        return 0,'',''
    store,plan,ledger,executor=make(tmp_path,sandbox_runner=broken)
    with pytest.raises(RuntimeError,match='transport failure'):
        run(executor.execute(plan.id,'unit-1'))
    assert ledger.status(plan.id,'unit-1').state==TrialExecutionState.UNCERTAIN
    assert store.report(plan.id).control.completed+store.report(plan.id).treatment.completed==0
    with pytest.raises(RuntimeError,match='already claimed'):
        run(executor.execute(plan.id,'unit-1'))
    with pytest.raises(PermissionError):ledger.reconcile(plan.id,'unit-1',operator='agent:claude')
    assert ledger.reconcile(plan.id,'unit-1',operator='human').state==TrialExecutionState.ABORTED


def test_checker_cannot_mark_failed_sandbox_accepted(tmp_path):
    async def fail_run(args,stdin):
        if args[1]=='info':return 0,'["rootless"]',''
        if args[1]=='run':return 1,'','test failed'
        return 0,'',''
    async def checker(a,v):return True
    store,plan,ledger,executor=make(tmp_path,sandbox_runner=fail_run,checker=checker)
    run(executor.execute(plan.id,'bad-unit'))
    r=store.report(plan.id)
    assert r.control.accepted+r.treatment.accepted==0


def test_checker_error_is_uncertain(tmp_path):
    async def checker(a,v):raise RuntimeError('verifier unavailable')
    store,plan,ledger,executor=make(tmp_path,checker=checker)
    with pytest.raises(RuntimeError):run(executor.execute(plan.id,'unit-x'))
    assert ledger.status(plan.id,'unit-x').state==TrialExecutionState.UNCERTAIN


def test_pilot_learning_view_is_separate_from_production(tmp_path):
    store,plan,ledger,executor=make(tmp_path)
    run(executor.execute(plan.id,'unit-1'))
    control,treatment=PilotLearningView(store).estimates(plan.id)
    assert control.samples+treatment.samples==1
    assert control.source==treatment.source=='isolated_pilot_not_production'
    assert 0 < control.posterior_mean < 1
    assert 0 < treatment.posterior_mean < 1


def test_crash_after_record_before_ledger_update_reconciles_verified(tmp_path):
    store,plan,ledger,executor=make(tmp_path)
    original=ledger.recorded
    def crash(*args,**kwargs):raise RuntimeError('simulated crash after durable result')
    ledger.recorded=crash
    with pytest.raises(RuntimeError,match='simulated crash'):
        run(executor.execute(plan.id,'unit-1'))
    ledger.recorded=original
    assert ledger.status(plan.id,'unit-1').state==TrialExecutionState.UNCERTAIN
    with pytest.raises(PermissionError):
        ledger.reconcile_recorded(store,plan.id,'unit-1',operator='agent:claude')
    receipt=ledger.reconcile_recorded(store,plan.id,'unit-1',operator='human')
    assert receipt.state==TrialExecutionState.RECORDED


def test_reconcile_requires_independent_record(tmp_path):
    store,plan,ledger,executor=make(tmp_path)
    assignment=store.assign(plan.id,'unit-2')
    ledger.claim(assignment)
    with pytest.raises(ValueError,match='No independently recorded'):
        ledger.reconcile_recorded(store,plan.id,'unit-2',operator='human')


def test_fails_closed_when_no_checker(tmp_path):
    store,plan,ledger,executor=make(tmp_path)
    with pytest.raises(ValueError,match='independent checker'):
        TrialExecutor(experiments=store,ledger=ledger,sandbox=executor.sandbox,
                      candidates=executor.candidates,verifier_id='trusted-checker')


def test_candidate_change_rejected_before_docker(tmp_path):
    store,plan,ledger,executor=make(tmp_path)
    for path in executor.candidates.values():
        (path/'SKILL.md').write_text('changed after approval')
    with pytest.raises(PermissionError,match='candidate artifact changed'):
        run(executor.execute(plan.id,'unit-1'))
    assert ledger.status(plan.id,'unit-1') is None
