"""Deterministic offline demonstration; no real scientific or Docker validation."""
import asyncio
import tempfile
from pathlib import Path
from biomni_dual import (TaskCenter, TaskStatus, AdaptiveStore,
    FailureRoute, Diagnosis, DAGPatch, AutonomousRemediationOrchestrator,
    AuditLog)


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'research.sqlite'
        center=TaskCenter(path)
        memory=AdaptiveStore(path)
        gid=memory.create_goal('Novel biomedical study')
        original=center.submit('Analyze disease progression',max_attempts=1)
        memory.bind_tasks(gid,[original.id],center)
        center.claim(original.id)
        center.finish(original.id,error='invalid_input',retryable=False)
        async def repair(task, diagnosis, job_id):
            return DAGPatch(additions=({'key':'diagnose',
                'prompt':'Independently review inputs and validate the corrected data schema',
                'depends_on':[]},),
                recovery=(task.id,'diagnose'),reason=f'remediation:{job_id}')
        orchestrator=AutonomousRemediationOrchestrator(
            center,database=path,repair_planner=repair,
            adaptive_store=memory,goal_id=gid)
        fault=Diagnosis(FailureRoute.REPLAN_TASK,'invalid_input',False,original.id,1)
        created=orchestrator.submit(fault)
        fixed=await orchestrator.advance(created.id)
        print('remediation:', fixed.state.value)
        print('original task after patch:', center.get(original.id).status.value)
        print('repair task in goal:', fixed.recovery_task_id in memory.task_ids(gid))
        recovery=center.claim(fixed.recovery_task_id)
        center.finish(recovery.id,result='Independent fixture check passed')
        assert center.get(original.id).id in [t.id for t in center.ready()]
        print('original is ready after independent repair:', True)
        print('audit chain valid:', AuditLog(path).verify())

if __name__=='__main__':
    asyncio.run(main())
