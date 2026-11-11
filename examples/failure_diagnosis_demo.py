"""Offline demonstration using independent, deterministic evidence and a fake worker.

This demonstrates routing and artifact integrity, NOT a real scientific assessment.
"""
import asyncio
import hashlib
import tempfile
from pathlib import Path

from biomni_dual import (Action, AdaptiveController, AdaptiveStore, ArtifactEvidence,
    AuditLog, CallbackPlanner, CheckResult, EvidenceAwareValidator,
    FailureDiagnosisEngine, NonEmptyValidator, PlanProposal, TaskCenter, TaskStatus)


class IndependentTest:
    def __init__(self, root):
        self.root=root

    async def assess(self, task, output):
        artifact=self.root/'holdout.json'
        # Deterministic test result is created by the assessor, not by the worker.
        artifact.write_text('{"independent_holdout_check":true}')
        digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
        return (CheckResult('holdout', True, ArtifactEvidence('holdout.json',digest,'holdout')),)


class Worker:
    async def run(self, task, capabilities):
        return 'Analysis completed; independent validation required.'


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        db=str(root/'state.db')
        tasks=TaskCenter(db)
        store=AdaptiveStore(db)
        diagnosis=FailureDiagnosisEngine(db)
        store.create_goal('demonstrate evidence-based validation',goal_id='demo')
        task=tasks.submit('test a hypothetical biomarker',max_attempts=2)
        store.bind_tasks('demo',[task.id],tasks)
        validator=EvidenceAwareValidator(root,IndependentTest(root),required_checks=('holdout',))
        controller=AdaptiveController(goal_id='demo',store=store,tasks=tasks,
            planner=CallbackPlanner(lambda snapshot:PlanProposal(Action.ASK_HUMAN)),
            worker=Worker(),validator=validator,failure_diagnosis=diagnosis)
        decision=await controller.tick()
        print('decision:',decision.action.value)
        print('task status:',tasks.get(task.id).status.value)
        print('validation evidence:',store.observations('demo')[0].summary)
        print('audit valid:',AuditLog(db).verify())


if __name__=='__main__':
    asyncio.run(main())
