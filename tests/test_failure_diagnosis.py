import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest

from biomni_dual import (
    Action, AdaptiveController, AdaptiveStore, ArtifactEvidence, AuditLog,
    CallbackPlanner, CheckResult, EvidenceAwareValidator, FailureDiagnosisEngine,
    FailureOrigin, FailureRoute, FailureSignal, NonEmptyValidator, PlanProposal,
    TaskCenter, TaskStatus, UncertainSideEffectError, ErrorKind,
)


def go(v): return asyncio.run(v)


@pytest.fixture
def store(tmp_path):
    path=str(tmp_path/'db.sqlite')
    return TaskCenter(path),AdaptiveStore(path),FailureDiagnosisEngine(path,max_remediations=2)


@pytest.mark.parametrize('kind,route,can_retry', [
    (ErrorKind.TIMEOUT, FailureRoute.RETRY_WORKER, True),
    (ErrorKind.TRANSIENT, FailureRoute.RETRY_WORKER, True),
    (ErrorKind.RESOURCE, FailureRoute.RETRY_WORKER, True),
    (ErrorKind.DEPENDENCY, FailureRoute.REPAIR_CAPABILITY, False),
    (ErrorKind.PERMISSION, FailureRoute.ASK_HUMAN, False),
    (ErrorKind.INVALID_INPUT, FailureRoute.REPLAN_TASK, False),
    (ErrorKind.UNKNOWN, FailureRoute.ASK_HUMAN, False),
])
def test_failure_routing(tmp_path,kind,route,can_retry):
    engine=FailureDiagnosisEngine(tmp_path/'f.sqlite')
    signal=FailureSignal('task',1,FailureOrigin.EXECUTION,'error',kind,True)
    actual=engine.diagnose(signal)
    assert actual.route==route and actual.retryable==can_retry
    assert engine.diagnose(signal)==actual
    assert AuditLog(engine.database).verify()


def test_scientific_and_uncertain_are_not_retried(tmp_path):
    engine=FailureDiagnosisEngine(tmp_path/'f.sqlite')
    science=engine.diagnose(FailureSignal('science',1,FailureOrigin.SCIENTIFIC_QUALITY,'fdr',evidence_refs=('lab:run1',)))
    uncertain=engine.diagnose(FailureSignal('effect',1,FailureOrigin.UNCERTAIN,'unknown',side_effect_uncertain=True))
    assert science.route==FailureRoute.REVIEW_RESULT
    assert science.evidence_refs==('lab:run1',)
    assert uncertain.route==FailureRoute.ASK_HUMAN
    assert not science.retryable and not uncertain.retryable


def test_remediation_budget_and_conflicting_idempotency(tmp_path):
    engine=FailureDiagnosisEngine(tmp_path/'f.sqlite',max_remediations=2)
    for n in (1,2):
        assert engine.diagnose(FailureSignal('x',n,FailureOrigin.EXECUTION,'timeout',ErrorKind.TIMEOUT,True)).retryable
    assert engine.diagnose(FailureSignal('x',3,FailureOrigin.EXECUTION,'timeout',ErrorKind.TIMEOUT,True)).route==FailureRoute.ASK_HUMAN
    assert len(FailureDiagnosisEngine(tmp_path/'f.sqlite').list('x'))==3
    with pytest.raises(ValueError,match='Conflicting'):
        engine.diagnose(FailureSignal('x',1,FailureOrigin.EXECUTION,'changed',ErrorKind.TIMEOUT,True))


class Assessor:
    def __init__(self,values): self.values=values
    async def assess(self,task,output): return tuple(self.values)


def check(root,name='quality.json',good=True,check_id='holdout'):
    path=root/name
    path.write_text('{"metrics":{"fdr":0.03}}')
    return CheckResult(check_id,good,ArtifactEvidence(name,hashlib.sha256(path.read_bytes()).hexdigest(),check_id))


def task(store):
    center,ledger,diag=store
    t=center.submit('validate an experiment',max_attempts=3)
    ledger.create_goal('study',goal_id='research')
    ledger.bind_tasks('research',[t.id],center)
    return t


def controller(store,worker,validator):
    center,ledger,diag=store
    return AdaptiveController(goal_id='research',store=ledger,tasks=center,
        planner=CallbackPlanner(lambda snapshot:PlanProposal(Action.ASK_HUMAN)),
        worker=worker,validator=validator,failure_diagnosis=diag)


class Worker:
    def __init__(self,reply='ok',error=None): self.reply,self.error=reply,error
    async def run(self,task,capabilities):
        if self.error: raise self.error
        return self.reply


def test_evidence_manifest_verified(tmp_path,store):
    t=task(store)
    val=EvidenceAwareValidator(tmp_path,Assessor([check(tmp_path)]),required_checks=('holdout',))
    assert go(val.validate(t,'generated output')) == (True,'all_independent_checks_passed')
    assert go(controller(store,Worker(),val).tick()).action==Action.EXECUTE
    assert store[0].get(t.id).status==TaskStatus.COMPLETED


def test_failed_evidence_blocks_completion(tmp_path,store):
    t=task(store)
    val=EvidenceAwareValidator(tmp_path,Assessor([check(tmp_path,good=False)]),required_checks=('holdout',))
    decision=go(controller(store,Worker(),val).tick())
    assert decision.action==Action.VALIDATE
    assert decision.evidence['route']==FailureRoute.REVIEW_RESULT.value
    assert store[0].get(t.id).status==TaskStatus.FAILED


@pytest.mark.parametrize('case', ['hash','missing','duplicate','traversal','symlink','absent','extra'])
def test_evidence_rejects_invalid_or_incomplete(tmp_path,store,case):
    t=task(store)
    c=check(tmp_path)
    vals=[c]
    checks=('holdout',)
    if case=='hash':
        (tmp_path/'quality.json').write_text('tampered')
    elif case=='missing':
        checks=('holdout','reproducibility')
    elif case=='duplicate':
        vals=[c,c]
    elif case=='traversal':
        vals=[CheckResult('holdout',True,ArtifactEvidence('../hidden','0'*64,'holdout'))]
    elif case=='symlink':
        dest=tmp_path.parent/'external'
        dest.write_text('sensitive')
        (tmp_path/'link').symlink_to(dest)
        vals=[CheckResult('holdout',True,ArtifactEvidence('link',hashlib.sha256(dest.read_bytes()).hexdigest(),'holdout'))]
    elif case=='absent':
        vals=[]
    elif case=='extra':
        vals=[c,CheckResult('unexpected',True,ArtifactEvidence('quality.json',c.evidence.sha256,'unexpected'))]
    val=EvidenceAwareValidator(tmp_path,Assessor(vals),required_checks=checks)
    ok,_=go(val.validate(t,'output'))
    assert not ok


def test_uncertain_side_effect_quarantines_task(store):
    t=task(store)
    ctl=controller(store,Worker(error=UncertainSideEffectError('remote result unknown')),NonEmptyValidator())
    d=go(ctl.tick())
    assert d.action==Action.ASK_HUMAN
    assert d.evidence['route']==FailureRoute.ASK_HUMAN.value
    assert store[0].get(t.id).status==TaskStatus.BLOCKED
    assert go(ctl.tick()).reason=='active_or_uncertain_tasks_require_reconciliation'
    assert AuditLog(store[1].path).verify()


def test_timeout_retried_then_escalated_by_budget(store):
    t=task(store)
    ctl=controller(store,Worker(error=TimeoutError('slow')),NonEmptyValidator())
    assert go(ctl.tick()).evidence['route']==FailureRoute.RETRY_WORKER.value
    assert store[0].get(t.id).status==TaskStatus.PENDING
    assert go(ctl.tick()).evidence['route']==FailureRoute.RETRY_WORKER.value
    assert store[0].get(t.id).status==TaskStatus.PENDING
    assert go(ctl.tick()).evidence['route']==FailureRoute.ASK_HUMAN.value
    assert store[0].get(t.id).status==TaskStatus.FAILED


def test_missing_dependency_not_blindly_retried(store):
    t=task(store)
    ctl=controller(store,Worker(error=ModuleNotFoundError('bio-tool')),NonEmptyValidator())
    d=go(ctl.tick())
    assert d.evidence['route']==FailureRoute.REPAIR_CAPABILITY.value
    assert store[0].get(t.id).status==TaskStatus.FAILED


def test_invalid_evidence_object_fails_closed(tmp_path,store):
    t=task(store)
    malformed=CheckResult('holdout',True,None)  # type: ignore[arg-type]
    val=EvidenceAwareValidator(tmp_path,Assessor([malformed]),required_checks=('holdout',))
    assert go(val.validate(t,'output'))==(False,'unverified_artifact:holdout')
