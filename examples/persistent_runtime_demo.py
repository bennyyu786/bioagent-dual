"""Offline, deterministic end-to-end persistent runtime demonstration."""
from __future__ import annotations
import asyncio
import tempfile
from pathlib import Path
from biomni_dual import (TaskCenter,AdaptiveStore,AdaptiveController,CallbackPlanner,
    PlanProposal,Action,PersistentRuntimeStore,PersistentAutonomousRuntime,
    EnvironmentMonitor,CallbackProbe,EventKind,GoalState,AuditLog)


class Worker:
    async def run(self,task,capability_ids):
        return 'verified synthetic data - no real biomedical analysis'


class Validator:
    async def validate(self,task,output):
        return bool(output),'independent_demo_check'


async def main():
    with tempfile.TemporaryDirectory() as directory:
        db=str(Path(directory)/'state.sqlite')
        tasks=TaskCenter(db); adaptive=AdaptiveStore(db)
        adaptive.create_goal('demonstrate ongoing autonomous execution',goal_id='demo')
        task=tasks.submit('inspect the data snapshot')
        adaptive.bind_tasks('demo',[task.id],tasks)
        ctl=AdaptiveController(goal_id='demo',store=adaptive,tasks=tasks,
            planner=CallbackPlanner(lambda state:PlanProposal(Action.ASK_HUMAN,reason='await further research instructions')),
            worker=Worker(),validator=Validator())
        runtime_store=PersistentRuntimeStore(db)
        monitor=EnvironmentMonitor(runtime_store)
        data={'snapshot_version':1}
        monitor.add('data_snapshot',EventKind.DATA_CHANGED,CallbackProbe(lambda:data['snapshot_version']))
        runtime=PersistentAutonomousRuntime(runtime_store,{'demo':ctl},monitor,
            idle_interval=3600,poll_seconds=.01)
        print('first:',(await runtime.step('demo',force=True)).value)
        print('task:',tasks.get(task.id).status.value)
        data['snapshot_version']=2
        print('event:',await monitor.poll('demo'))
        print('next:',(await runtime.step('demo')).value)
        print('state:',runtime_store.state('demo').value)
        print('observations:',len(adaptive.observations('demo')))
        print('audit_valid:',AuditLog(db).verify())
        print('NOTE: synthetic worker, no Docker/SDK network integration')


if __name__=='__main__':
    asyncio.run(main())
