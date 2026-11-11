import time
from threading import Lock
import pytest
from biomni_dual import (TaskCenter, CapabilityRegistry, Capability, CapabilityKind, CapabilityCenter,
    DAGPatch, DAGVersionManager, ResourcePool, ResourceRequest, ParallelScheduler, WorkerPool,
    TaskStatus, DiscoveryAgent, BuilderAgent, CapabilityBuildFlow)

class Recorder:
    def __init__(self, delay=0):
        self.lock = Lock(); self.running = 0; self.peak = 0; self.delay = delay
    def run(self, task, capabilities):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(self.delay)
        with self.lock:
            self.running -= 1
        return task.prompt

def test_dag_versions_transaction(tmp_path):
    tasks = TaskCenter(tmp_path/'t.db'); graph = DAGVersionManager(tasks)
    ids = graph.commit(DAGPatch(additions=({'key':'a','prompt':'QC'}, {'key':'b','prompt':'fit','depends_on':['a']})))
    assert graph.snapshot()[ids['b']]['depends_on'] == [ids['a']]
    first = graph.versions()[0]['version']
    with pytest.raises(ValueError):
        graph.commit(DAGPatch(dependencies=((ids['a'], ids['b']),)))
    assert len(graph.versions()) == 1
    assert graph.snapshot(first)[ids['a']]['depends_on'] == []
    assert tasks.get(ids['a']).depends_on == []

def test_dag_cannot_modify_running(tmp_path):
    tasks=TaskCenter(tmp_path/'t.db'); one=tasks.submit('one'); two=tasks.submit('two')
    tasks.claim(one.id)
    with pytest.raises(ValueError):
        DAGVersionManager(tasks).commit(DAGPatch(dependencies=((one.id,two.id),)))

def test_resource_capacity_and_release():
    p=ResourcePool(cpu=2,gpu=1,memory_mb=1024)
    assert p.reserve('a',ResourceRequest(cpu=1,gpu=1,memory_mb=512))
    assert not p.reserve('b',ResourceRequest(cpu=2,gpu=1,memory_mb=512))
    assert not p.reserve('a',ResourceRequest())
    p.release('a')
    assert p.used.cpu == 0
    assert p.reserve('b',ResourceRequest(cpu=2,gpu=1,memory_mb=1024))

def test_parallel_dag_and_fair_tenants(tmp_path):
    tasks=TaskCenter(tmp_path/'t.db'); reg=CapabilityRegistry(); worker=Recorder(.025)
    one=tasks.submit('a',tenant='alpha'); two=tasks.submit('b',tenant='beta')
    tail=tasks.submit('c',depends_on=[one.id,two.id])
    pool=ResourcePool(cpu=2); s=ParallelScheduler(tasks,reg,WorkerPool(worker),pool,max_workers=2)
    completed=s.run_until_idle()
    assert len(completed) == 3
    assert worker.peak == 2
    assert tasks.get(tail.id).status == TaskStatus.COMPLETED
    assert pool.used.cpu == 0

def test_gap_approval(tmp_path):
    tasks=TaskCenter(tmp_path/'t.db'); reg=CapabilityRegistry(); caps=CapabilityCenter(reg,tmp_path/'c.db')
    task=tasks.submit('do',required_capabilities=['skill.x'])
    scheduler=ParallelScheduler(tasks,reg,WorkerPool(Recorder()),ResourcePool(cpu=1),capability_center=caps)
    assert scheduler.run_until_idle() == []
    assert tasks.get(task.id).status == TaskStatus.WAITING_APPROVAL
    assert caps.proposal('skill.x').status == 'proposed'
    flow=CapabilityBuildFlow(caps,DiscoveryAgent([]),BuilderAgent(tmp_path/'candidates'))
    proposal=flow.request('skill.x','needed')
    assert proposal.exists()
    with pytest.raises(PermissionError):
        caps.publish(Capability('skill.x','x',CapabilityKind.SKILL),verified=True)
    caps.review('skill.x',approve=True)
    caps.publish(Capability('skill.x','x',CapabilityKind.SKILL),verified=True)
    tasks.approve(task.id)
    assert scheduler.run_until_idle()[0].status == TaskStatus.COMPLETED

def test_worker_failure_retries_and_releases(tmp_path):
    tasks=TaskCenter(tmp_path/'t.db'); reg=CapabilityRegistry()
    class Broken:
        def run(self, task, capabilities): raise RuntimeError('fail')
    task=tasks.submit('bad',max_attempts=2)
    pool=ResourcePool(cpu=1)
    s=ParallelScheduler(tasks,reg,WorkerPool(Broken()),pool,max_workers=1)
    output=s.run_until_idle()
    assert len(output)==2
    assert tasks.get(task.id).status == TaskStatus.FAILED
    assert pool.used.cpu == 0

def test_oversize_capacity_does_not_claim(tmp_path):
    tasks=TaskCenter(tmp_path/'t.db'); reg=CapabilityRegistry()
    reg.register(Capability('tool.big','big',CapabilityKind.TOOL, metadata={'resources':{'cpu':4}}))
    task=tasks.submit('big',required_capabilities=['tool.big'])
    scheduler=ParallelScheduler(tasks,reg,WorkerPool(Recorder()),ResourcePool(cpu=2))
    assert scheduler.run_until_idle() == []
    assert tasks.get(task.id).status == TaskStatus.PENDING
    assert tasks.get(task.id).attempts == 0

def test_build_flow_candidate_digest(tmp_path):
    reg=CapabilityRegistry(); caps=CapabilityCenter(reg,tmp_path/'caps.db')
    c=Capability('skill.alpha','alpha',CapabilityKind.SKILL)
    candidate=CapabilityBuildFlow.candidate_from_text(c, 'safe instructions', 'catalog:local')
    flow=CapabilityBuildFlow(caps,DiscoveryAgent([candidate]),BuilderAgent(tmp_path/'out'))
    assert flow.request('skill.alpha','missing') == candidate
    with pytest.raises(PermissionError):
        flow.publish_candidate(candidate,verified_digest='bad',operator_approved=True)
    assert caps.proposal('skill.alpha').status == 'proposed'
    flow.publish_candidate(candidate,verified_digest=candidate.sha256,operator_approved=True)
    assert reg.get('skill.alpha') == c

def test_replanning_agent_from_event(tmp_path):
    from biomni_dual import ReplanningAgent, Event
    tasks=TaskCenter(tmp_path/'t.db'); base=tasks.submit('original')
    v=DAGVersionManager(tasks)
    def backend(event, snapshot):
        assert event.name == 'environment.changed'
        assert snapshot[0]['id'] == base.id
        return DAGPatch(additions=({'key':'new','prompt':'Investigate change','depends_on':[base.id]},),reason='external event')
    mapping=ReplanningAgent(v,backend).on_event(Event('environment.changed',{}))
    assert tasks.get(mapping['new']).depends_on == [base.id]
    assert len(v.versions()) == 1

def test_cli_requires_explicit_optin():
    from biomni_dual import CLIWorker
    worker=CLIWorker('/usr/bin/false')
    with pytest.raises(PermissionError):
        worker.run(__import__('biomni_dual').Task(id='x',prompt='hi'), [])
