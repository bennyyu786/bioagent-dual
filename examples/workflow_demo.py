"""Full offline integration demo. Fake SDK and Docker runner, never production services."""
from __future__ import annotations
import asyncio
import json
import tempfile
from pathlib import Path
from biomni_dual.models import TaskStatus
from biomni_dual.registry import CapabilityRegistry
from biomni_dual.scheduler import TaskCenter
from biomni_dual.dag import DAGVersionManager
from biomni_dual.runtime import ParallelScheduler, ResourcePool, WorkerPool
from biomni_dual import *

class OfflinePlanner:
    async def run(self, req):
        return AgentResult(True,json.dumps({'additions':[
            {'key':'qc','prompt':'Perform QC','required_capabilities':[]},
            {'key':'integrate','prompt':'Integrate data','depends_on':['qc'],
             'required_capabilities':['skill.integration']}],
             'reason':'offline demonstration'}))

class OfflineDockerRunner:
    async def __call__(self,argv,stdin):
        if argv[1]=='info': return (0,'["name=rootless"]','')
        if argv[1]=='run': return (0,'1 passed','')
        return (0,'','')

class EchoWorker:
    def run(self,task,caps): return 'complete '+task.prompt

async def main():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        tasks=TaskCenter(root/'tasks.db')
        registry=CapabilityRegistry()
        dag=DAGVersionManager(tasks)
        planner=LLMPlanner(OfflinePlanner(),dag)
        sandbox=DockerSandbox(runner=OfflineDockerRunner())
        builder=SandboxBuilder(BuildStore(root/'builds.db'),sandbox,registry)
        scheduler=ParallelScheduler(tasks,registry,WorkerPool(EchoWorker()),ResourcePool(cpu=2),max_workers=2)
        system=DualCenterWorkflow(tasks,planner,scheduler,builder)
        plan=await system.submit_goal('Perform QC and integrate data')
        print('Plan:',plan)
        system.dispatch()
        waiting=[t for t in tasks.list() if t.status==TaskStatus.WAITING_APPROVAL]
        assert len(waiting)==1
        print('Capability gap:',waiting[0].required_capabilities)
        build=await system.fill_gap(task_id=waiting[0].id,cap_id='skill.integration',
                    kind='skill',path=Path(__file__).parent/'skill_candidate',approver='demo-operator')
        print('Build:',build['status'])
        system.dispatch()
        print('Tasks:',[(t.prompt,t.status.value) for t in tasks.list()])
        assert all(t.status==TaskStatus.COMPLETED for t in tasks.list())
        print('OFFLINE FLOW PASS (SDK and Docker mocked)')

if __name__=='__main__': asyncio.run(main())
