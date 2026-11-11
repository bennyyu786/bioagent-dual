"""Offline unit/contract tests: mocks do not attest external SDK or Docker behavior."""
import asyncio
import json
import sys
import types
import time
from pathlib import Path
import pytest
from biomni_dual.models import Capability,CapabilityKind,TaskStatus
from biomni_dual.registry import CapabilityRegistry
from biomni_dual.scheduler import TaskCenter
from biomni_dual.dag import DAGVersionManager
from biomni_dual.runtime import ParallelScheduler,WorkerPool,ResourcePool
from biomni_dual import *

class FakeLLM:
    def __init__(self,text=None):
        self.text=text or json.dumps({'additions':[
            {'key':'a','prompt':'QC'}, {'key':'b','prompt':'integrate','depends_on':['a']} ]})
    async def run(self,req): return AgentResult(True,self.text,'thread-1')

class FakeDocker:
    def __init__(self,pass_run=True):
        self.history=[];self.pass_run=pass_run
    async def __call__(self,argv,stdin):
        self.history.append(argv)
        if argv[1]=='info': return (0,'["name=rootless"]','')
        if argv[1]=='run': return (0 if self.pass_run else 1,'1 passed' if self.pass_run else 'test failed','')
        return (0,'','')

class SlowWorker:
    def run(self,task,caps):
        time.sleep(.06)
        return 'OK'

class FailWorker:
    def run(self,task,caps): raise RuntimeError('failed')

@pytest.fixture
def skill(tmp_path):
    p=tmp_path/'skill'; (p/'tests').mkdir(parents=True)
    (p/'SKILL.md').write_text('---\nname: sample\n---\n')
    (p/'tests'/'test_skill.py').write_text('def test_ok(): assert True\n')
    return p

@pytest.fixture
def setup_system(tmp_path):
    tasks=TaskCenter(tmp_path/'tasks.db'); reg=CapabilityRegistry()
    dag=DAGVersionManager(tasks)
    return tasks,reg,dag

def test_01_llm_planner_structured_dag(setup_system):
    tasks,_,dag=setup_system
    output=asyncio.run(LLMPlanner(FakeLLM(),dag).submit_goal('QC and integrate'))
    a,b=output['task_ids']['a'],output['task_ids']['b']
    assert tasks.get(b).depends_on==[a]
    assert dag.versions()[-1]['version']==output['version']

def test_02_invalid_llm_plan_never_committed(setup_system):
    tasks,_,dag=setup_system
    bad=FakeLLM('{"additions":[{"key":"a","prompt":"a","depends_on":["b"]},'+
                '{"key":"b","prompt":"b","depends_on":["a"]}]}')
    with pytest.raises(PlanError):
        asyncio.run(LLMPlanner(bad,dag,max_repair=0).submit_goal('make cycle'))
    assert tasks.list()==[]

def test_03_sdk_contract_mock_codex(monkeypatch,tmp_path):
    class Sandbox: read_only='read_only';workspace_write='workspace_write'
    class Thread:
        id='s-1'
        async def run(self,prompt,**kwargs): return types.SimpleNamespace(final_response='codex: '+prompt)
    class AsyncCodex:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def thread_start(self,**kwargs):
            assert kwargs['sandbox']=='read_only'; return Thread()
        async def thread_resume(self,id): assert id=='s-1';return Thread()
    monkeypatch.setitem(sys.modules,'openai_codex',types.SimpleNamespace(AsyncCodex=AsyncCodex,Sandbox=Sandbox))
    rt=CodexSDKRuntime()
    a=asyncio.run(rt.run(AgentRequest('t1','planner','goal',str(tmp_path))))
    assert a.text=='codex: goal' and a.session_id=='s-1'
    b=asyncio.run(rt.run(AgentRequest('t2','planner','again',str(tmp_path),session_id=a.session_id)))
    assert b.text=='codex: again'

def test_04_sdk_contract_mock_claude(monkeypatch,tmp_path):
    class ResultMessage:
        def __init__(self): self.result='claude-response';self.session_id='ss';self.is_error=False
    class ClaudeAgentOptions:
        def __init__(self,**kwargs):
            assert kwargs['tools']==[] and kwargs['setting_sources']==[]
    async def query(prompt,options):yield ResultMessage()
    monkeypatch.setitem(sys.modules,'claude_agent_sdk',types.SimpleNamespace(
        query=query,ClaudeAgentOptions=ClaudeAgentOptions,ResultMessage=ResultMessage))
    a=asyncio.run(ClaudeSDKRuntime().run(AgentRequest('t','planner','goal',str(tmp_path))))
    assert a.text=='claude-response'

def test_05_parallel_tasks_and_dependencies(setup_system):
    tasks,reg,_=setup_system
    t1=tasks.submit('first');t2=tasks.submit('second');t3=tasks.submit('final',depends_on=[t1.id,t2.id])
    s=ParallelScheduler(tasks,reg,WorkerPool(SlowWorker()),ResourcePool(cpu=2),max_workers=2)
    result=s.run_until_idle()
    assert len(result)==3 and all(tasks.get(x.id).status==TaskStatus.COMPLETED for x in [t1,t2,t3])
    events1=tasks.events(t1.id);events2=tasks.events(t2.id)
    # Two tasks start before either finished (timestamp evidence).
    starts=[next(e[0] for e in ev if e[1]=='started') for ev in [events1,events2]]
    finishes=[next(e[0] for e in ev if e[1]=='completed') for ev in [events1,events2]]
    assert max(starts)<min(finishes)

def test_06_missing_skill_waits_for_approval(setup_system):
    tasks,reg,_=setup_system
    t=tasks.submit('integration',required_capabilities=['skill.integration'])
    s=ParallelScheduler(tasks,reg,WorkerPool(SlowWorker()),ResourcePool(cpu=2))
    s.run_until_idle()
    assert tasks.get(t.id).status==TaskStatus.WAITING_APPROVAL

def test_07_docker_uses_restrictions_and_cleanup(skill):
    fake=FakeDocker(); sandbox=DockerSandbox(runner=fake)
    outcome=asyncio.run(sandbox.validate(skill,kind='skill'))
    assert outcome.passed and outcome.digest==sandbox.digest_tree(skill)
    cmd=next(x for x in fake.history if x[1]=='run')
    for flag in ['--network','none','--read-only','--cap-drop','ALL','--pids-limit','--memory','--cpus','--user','--mount']:
        assert flag in cmd
    assert any(x[1:4]==['rm','-f',outcome.container_id] for x in fake.history)

def test_08_mcp_manifest_validation(tmp_path):
    folder=tmp_path/'mcp';folder.mkdir()
    (folder/'mcp_manifest.json').write_text(json.dumps({'argv':['python','server.py'],
        'expected_tools':['echo'],'test_calls':[{'name':'echo','arguments':{}}]}))
    fake=FakeDocker(); sandbox=DockerSandbox(runner=fake)
    result=asyncio.run(sandbox.validate(folder,kind='mcp'))
    assert result.passed
    assert '/harness/mcp_probe.py' in next(x for x in fake.history if x[1]=='run')

def test_09_no_publish_without_approval_and_hash_match(skill,tmp_path):
    registry=CapabilityRegistry();store=BuildStore(tmp_path/'builds.db')
    builder=SandboxBuilder(store,DockerSandbox(runner=FakeDocker()),registry)
    job=builder.propose('skill.integration','skill',skill)
    with pytest.raises(PermissionError):builder.publish(job)
    asyncio.run(builder.validate(job))
    with pytest.raises(PermissionError):builder.publish(job)
    builder.approve(job,'operator',approved=True)
    (skill/'SKILL.md').write_text('changed')
    with pytest.raises(BuildError):builder.publish(job)
    assert registry.get('skill.integration') is None

def test_10_build_idempotency_and_restart_restore(skill,tmp_path):
    store=BuildStore(tmp_path/'build.db');reg=CapabilityRegistry()
    builder=SandboxBuilder(store,DockerSandbox(runner=FakeDocker()),reg)
    job=builder.propose('skill.integration','skill',skill)
    asyncio.run(builder.validate(job));builder.approve(job,'human',approved=True)
    cap=builder.publish(job)
    assert cap.kind==CapabilityKind.SKILL
    assert builder.publish(job).metadata['digest']==cap.metadata['digest']  # idempotent publish
    new_builder=SandboxBuilder(BuildStore(tmp_path/'build.db'),DockerSandbox(runner=FakeDocker()),CapabilityRegistry())
    assert new_builder.restore_published()==1
    assert new_builder.registry.get('skill.integration') is not None

def test_11_end_to_end_resume_after_publish(skill,tmp_path):
    tasks=TaskCenter(tmp_path/'tasks.db');reg=CapabilityRegistry();dag=DAGVersionManager(tasks)
    json_plan=json.dumps({'additions':[{'key':'qc','prompt':'QC'},
        {'key':'integration','prompt':'Integrate','depends_on':['qc'],
         'required_capabilities':['skill.integration']}]})
    planner=LLMPlanner(FakeLLM(json_plan),dag)
    scheduler=ParallelScheduler(tasks,reg,WorkerPool(SlowWorker()),ResourcePool(cpu=2))
    builder=SandboxBuilder(BuildStore(tmp_path/'builds.db'),DockerSandbox(runner=FakeDocker()),reg)
    app=DualCenterWorkflow(tasks,planner,scheduler,builder)
    asyncio.run(app.submit_goal('Analyze'))
    app.dispatch()
    target=[t for t in tasks.list() if t.status==TaskStatus.WAITING_APPROVAL][0]
    result=asyncio.run(app.fill_gap(task_id=target.id,cap_id='skill.integration',kind='skill',path=skill,approver='operator'))
    assert result['status']=='PUBLISHED'
    app.dispatch()
    assert all(t.status==TaskStatus.COMPLETED for t in tasks.list())

def test_12_recover_interrupted_task_no_double_publish(skill,tmp_path):
    tasks=TaskCenter(tmp_path/'t.db');t=tasks.submit('job',max_attempts=2)
    tasks.claim(t.id)
    restarted=TaskCenter(tmp_path/'t.db')
    assert restarted.recover_interrupted()==1
    assert restarted.get(t.id).status==TaskStatus.PENDING
    assert restarted.recover_interrupted()==0

def test_13_failure_retry_bounded(setup_system):
    tasks,reg,_=setup_system
    t=tasks.submit('failure',max_attempts=2)
    scheduler=ParallelScheduler(tasks,reg,WorkerPool(FailWorker()),ResourcePool(cpu=1))
    scheduler.run_until_idle()
    assert tasks.get(t.id).attempts==2 and tasks.get(t.id).status==TaskStatus.FAILED

def test_14_symlink_candidate_rejected(skill,tmp_path):
    (skill/'escape').symlink_to(tmp_path)
    with pytest.raises(BuildError):DockerSandbox.digest_tree(skill)

def test_15_docker_requires_rootless(skill):
    class NonRootless(FakeDocker):
        async def __call__(self,argv,stdin):
            if argv[1]=='info':return (0,'["name=seccomp"]','')
            return await super().__call__(argv,stdin)
    with pytest.raises(BuildError,match='rootless'):
        asyncio.run(DockerSandbox(runner=NonRootless()).validate(skill,kind='skill'))

def test_16_no_cli_execution_without_opt_in():
    a=CLIServiceRuntime('codex')
    with pytest.raises(PermissionError):
        asyncio.run(a.run(AgentRequest('t','worker','prompt')))

def test_17_capability_agent_generates_nonexecuted_candidate(tmp_path):
    payload={'kind':'skill','files':[{'path':'SKILL.md','content':'# sample'},
             {'path':'tests/test_a.py','content':'def test_a(): assert True'}]}
    agent=CapabilityAgent(FakeLLM(json.dumps(payload)),tmp_path/'staging')
    folder=asyncio.run(agent.propose('skill.demo','skill','test skill'))
    assert (folder/'SKILL.md').exists()
    assert (folder/'tests/test_a.py').exists()

@pytest.mark.parametrize('bad', ['../escape.py','/etc/secret','./foo','a\\b.py','.hidden'])
def test_18_builder_rejects_path_traversal(bad):
    payload={'kind':'skill','files':[{'path':'SKILL.md','content':'ok'},
             {'path':'tests/test_a.py','content':'pass'}, {'path':bad,'content':'evil'}]}
    with pytest.raises(BuildError):CapabilityAgent._parse(json.dumps(payload),'skill')

def test_19_event_replan_is_versioned_and_preserves_completed(setup_system):
    tasks,_,dag=setup_system
    completed=tasks.submit('old');tasks.claim(completed.id);tasks.finish(completed.id,result='done')
    replanner=LLMPlanner(FakeLLM(json.dumps({'additions':[
        {'key':'new','prompt':'recheck','depends_on':[completed.id]}],'reason':'event: retrigger'})),dag)
    result=asyncio.run(replanner.replan(event='new data',task_state=[{'id':completed.id,'status':'completed'}]))
    assert result['version']==1
    assert tasks.get(completed.id).status==TaskStatus.COMPLETED
    assert len(tasks.list())==2

def test_20_mcp_manifest_rejects_unsafe_commands(tmp_path):
    folder=tmp_path/'mcp';folder.mkdir()
    (folder/'mcp_manifest.json').write_text(json.dumps({'argv':['/bin/sh','-c','run']}))
    with pytest.raises(BuildError):
        asyncio.run(DockerSandbox(runner=FakeDocker()).validate(folder,kind='mcp'))

def test_21_three_worker_types_dispatched_in_one_scheduler(tmp_path):
    from biomni_dual.engine import BiomniWorker
    class FakeBiomni:
        def go(self,prompt): return (None,'biomni-result')
    class MockAgent:
        async def run(self,req):return AgentResult(True,req.role+'-result')
    tasks=TaskCenter(tmp_path/'three.db');reg=CapabilityRegistry()
    for label in ['biomni','claude','codex']:
        reg.register(Capability('worker.'+label,'test',CapabilityKind.TOOL))
        tasks.submit('job-'+label,required_capabilities=['worker.'+label])
    workers=WorkerPool(BiomniWorker(FakeBiomni),{
        'worker.claude':SDKWorker(MockAgent(),role='claude'),
        'worker.codex':SDKWorker(MockAgent(),role='codex'),
        'worker.biomni':BiomniWorker(FakeBiomni),
    })
    ParallelScheduler(tasks,reg,workers,ResourcePool(cpu=3),max_workers=3).run_until_idle()
    results={t.prompt:t.result for t in tasks.list()}
    assert results=={'job-biomni':'biomni-result','job-claude':'claude-result','job-codex':'codex-result'}

def test_22_published_snapshot_survives_staging_edits(skill,tmp_path):
    reg=CapabilityRegistry();builder=SandboxBuilder(BuildStore(tmp_path/'build.db'),DockerSandbox(runner=FakeDocker()),reg)
    job=builder.propose('skill.snapshot','skill',skill)
    asyncio.run(builder.validate(job));builder.approve(job,'human',approved=True)
    cap=builder.publish(job)
    stored=Path(cap.metadata['artifact_path'])
    assert stored.exists()
    (skill/'SKILL.md').write_text('modified staging after publication')
    assert builder.restore_published()==1
    assert DockerSandbox.digest_tree(stored)==cap.metadata['digest']

def test_23_interrupted_validation_fails_closed(skill,tmp_path):
    store=BuildStore(tmp_path/'state.db');sandbox=DockerSandbox(runner=FakeDocker())
    job=SandboxBuilder(store,sandbox,CapabilityRegistry()).propose('skill.failed','skill',skill)
    store.transition(job,'PROPOSED','TESTING')
    restarted=BuildStore(tmp_path/'state.db')
    assert restarted.recover_interrupted()==1
    assert restarted.get(job)['status']=='FAILED'
    assert restarted.recover_interrupted()==0

def test_24_docker_timeout_always_attempts_cleanup(skill):
    class TimeoutDocker(FakeDocker):
        async def __call__(self,argv,stdin):
            self.history.append(argv)
            if argv[1]=='info':return (0,'["name=rootless"]','')
            if argv[1]=='run':raise asyncio.TimeoutError()
            return (0,'','')
    runner=TimeoutDocker()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(DockerSandbox(runner=runner).validate(skill,kind='skill'))
    assert any(a[1:3]==['rm','-f'] for a in runner.history)

def test_25_recover_all_reclaims_orphans_and_restores_metadata(skill,tmp_path):
    class Orphans(FakeDocker):
        async def __call__(self,argv,stdin):
            self.history.append(argv)
            if argv[1]=='info':return (0,'["name=rootless"]','')
            if argv[1]=='ps':return (0,'0123456789ab\n','')
            return (0,'','')
    tasks=TaskCenter(tmp_path/'tasks.db');reg=CapabilityRegistry()
    store=BuildStore(tmp_path/'builds.db');sandbox=DockerSandbox(runner=Orphans())
    builder=SandboxBuilder(store,sandbox,reg)
    t=tasks.submit('job');tasks.claim(t.id)
    app=DualCenterWorkflow(tasks,LLMPlanner(FakeLLM(),DAGVersionManager(tasks)),
         ParallelScheduler(tasks,reg,WorkerPool(SlowWorker()),ResourcePool(cpu=1)),builder)
    out=asyncio.run(app.recover_all())
    assert out['tasks']==1 and out['orphan_containers_removed']==1
    assert tasks.get(t.id).status==TaskStatus.PENDING

def test_26_writable_codex_rejects_untrusted_planner_request():
    with pytest.raises(PermissionError):
        asyncio.run(CodexSDKRuntime(writable=True).run(AgentRequest('x','planner','do dangerous changes')))

def test_27_terminal_failure_triggers_llm_replan_and_recovery(tmp_path):
    class OnceFailed:
        failed_once=False
        def run(self,task,caps):
            if task.prompt=='fragile' and not self.failed_once:
                self.failed_once=True
                raise RuntimeError('transient')
            return 'done'
    class ReplanLLM:
        async def run(self,request):
            return AgentResult(True,json.dumps({'additions':[
                {'key':'recovery','prompt':'repair environment'}],
                'reason':'failure recovery'}))
    tasks=TaskCenter(tmp_path/'t.db');reg=CapabilityRegistry()
    first=tasks.submit('fragile',max_attempts=1)
    app=DualCenterWorkflow(tasks,LLMPlanner(ReplanLLM(),DAGVersionManager(tasks)),
        ParallelScheduler(tasks,reg,WorkerPool(OnceFailed()),ResourcePool(cpu=1)),
        SandboxBuilder(BuildStore(tmp_path/'b.db'),DockerSandbox(runner=FakeDocker()),reg))
    asyncio.run(app.dispatch_with_replanning(max_rounds=3))
    task=tasks.get(first.id)
    assert task.status==TaskStatus.COMPLETED
    assert len(task.depends_on)==1
    assert tasks.get(task.depends_on[0]).status==TaskStatus.COMPLETED
    assert len([e for e in tasks.events(first.id) if e[1]=='llm_replan_requested'])==1

def test_28_claude_code_tools_cannot_be_used_by_planner():
    with pytest.raises(PermissionError):
        asyncio.run(ClaudeSDKRuntime(allow_code_tools=True).run(
            AgentRequest('unsafe','planner','write files')))
