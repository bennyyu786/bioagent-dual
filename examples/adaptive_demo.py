"""Offline demonstration of rolling task/capability co-evolution (no real SDK/Docker)."""
import asyncio
import tempfile
from pathlib import Path
from biomni_dual import (
    Action, AdaptiveController, AdaptiveStore, CallbackPlanner, Capability,
    CapabilityKind, CapabilityRegistry, DAGPatch, Observation, PlanProposal,
    TaskCenter, TaskFitCoordinator,
)

class DemoWorker:
    async def run(self, task, capabilities):
        print('  worker:',task.prompt,'using',list(capabilities))
        return 'evidence: '+task.prompt

class DemoValidator:
    async def validate(self, task, result):
        return result.startswith('evidence:'),'independent_mock_validation'

class ApprovedBuilder:
    def request(self, capability_id, kind, requirement, request_key, **kwargs):
        print('  capability proposal (not installed):',capability_id)
        return {'id':request_key}

async def main():
    with tempfile.TemporaryDirectory() as folder:
        db=str(Path(folder)/'research.sqlite')
        center=TaskCenter(db);memory=AdaptiveStore(db)
        registry=CapabilityRegistry()
        registry.register(Capability('qc','quality control',CapabilityKind.TOOL,
                                     metadata={'provides':['qc']}))
        builder=ApprovedBuilder()
        def plan(s):
            if not s.tasks:
                return PlanProposal(Action.EXPLORE,DAGPatch(additions=(
                    {'key':'inspect','prompt':'Inspect available samples','required_capabilities':['qc']},
                ),reason='explore current data'),'initial exploration')
            if len(s.tasks)==1:
                return PlanProposal(Action.REPLAN,DAGPatch(additions=(
                    {'key':'causal','prompt':'Estimate longitudinal causal effects',
                     'depends_on':[s.tasks[0].id], 'required_capabilities':['causal.longitudinal']},
                ),reason='new observation'),'observed longitudinal structure')
            return PlanProposal(Action.ASK_HUMAN,reason='Review findings')
        goal_id=memory.create_goal('Unknown longitudinal multi-omics study',goal_id='research')
        loop=AdaptiveController(goal_id=goal_id,store=memory,tasks=center,
            planner=CallbackPlanner(plan),worker=DemoWorker(),validator=DemoValidator(),
            fit=TaskFitCoordinator(registry,builder),gap_kinds={'causal.longitudinal':'skill'})
        for i in range(2):
            d=await loop.tick();print('cycle:',d.action.value,d.reason)
        memory.remember(goal_id,Observation('new-omics','', 'environment_change',
                                           'Repeated samples at multiple time points'))
        print('cycle:',(await loop.tick()).action.value)
        missing=await loop.tick()
        print('cycle:',missing.action.value,missing.evidence)
        # In production this registration MUST come from SandboxBuilder with verified
        # immutable artifact and explicit Supervisor approval. Here it is simulated.
        print('  simulated external verification + explicit operator approval')
        registry.register(Capability('causal.longitudinal','longitudinal causal effect',
                                     CapabilityKind.SKILL, metadata={'provides':['causal.longitudinal']}))
        for d in await loop.run(max_ticks=4):
            print('cycle:',d.action.value,d.reason)
        print('DAG versions:',len(loop.dag.versions()))
        print('observations:',len(memory.observations(goal_id)))
        print('completed:',[t.status.value for t in loop.snapshot().tasks])

if __name__=='__main__':
    asyncio.run(main())
