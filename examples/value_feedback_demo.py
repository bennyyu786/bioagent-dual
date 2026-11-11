"""Deterministic v0.6.1 demonstration; measurements are illustrative fixtures."""
import asyncio
import tempfile
from pathlib import Path
from biomni_dual import (
    AdaptiveController, AdaptiveStore, TaskCenter, CallbackPlanner, PlanProposal, Action,
    QualityEvidence, QualityPolicy, ValuePolicy, ActionOption, ValueGuidedPlanner,
    ExperienceStore, DAGPatch,
)

class Worker:
    async def run(self, task, ids):
        return 'analysis completed; see independent QC artifact'

class StructuralValidator:
    async def validate(self, task, output):
        return ('analysis completed' in output, 'structural_QC')

class ScientificEvaluator:
    async def evaluate(self, task, output):
        # Explicit fixture values from an imagined external QC harness.
        # These are NOT measurements produced by this demo itself.
        return QualityEvidence(scientific_quality=.92,reproducibility=.88,
            goal_progress=.65,evidence_ids=('fixture:qc-report-001',),
            uncertainty_before=.8,uncertainty_after=.35,cost=2.0,risk=.08)

async def main():
    with tempfile.TemporaryDirectory() as folder:
        db=str(Path(folder)/'research.sqlite')
        tasks=TaskCenter(db);store=AdaptiveStore(db)
        store.create_goal('unfamiliar longitudinal study',goal_id='study')
        def candidates(snapshot):
            return [
                (ActionOption('explore','inspect',.3,.9,.3,1,.1,evidence_ids=('fixture:planner-001',)),
                 PlanProposal(Action.EXPLORE,DAGPatch(additions=({'key':'inspect','prompt':'Inspect dataset structure'},)),'information-first')),
                (ActionOption('replan','model',.5,.05,.05,1,.2,evidence_ids=('fixture:planner-002',)),
                 PlanProposal(Action.REPLAN,DAGPatch(additions=({'key':'model','prompt':'Build model immediately'},)),'model-first')),
            ]
        ctl=AdaptiveController(goal_id='study',store=store,tasks=tasks,
            planner=ValueGuidedPlanner(candidates,policy=ValuePolicy(progress=.5,information=1)),
            worker=Worker(),validator=StructuralValidator(),outcome_evaluator=ScientificEvaluator(),
            quality_policy=QualityPolicy(min_quality=.8,min_reproducibility=.8),
            experience_store=ExperienceStore(db))
        for decision in await ctl.run(max_ticks=5):
            print('action:',decision.action.value,'reason:',decision.reason)
        outcomes=ExperienceStore(db).list('study')
        for outcome in outcomes:
            print('accepted:',outcome['accepted'],'utility:',outcome['operational_utility'],
                  'measured_information_gain:',outcome['information_gain'])
        print('audit records:',len(outcomes))

if __name__=='__main__': asyncio.run(main())
