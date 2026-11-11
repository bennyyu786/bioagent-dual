import pytest
from biomni_dual import (Capability, CapabilityKind, CapabilityRegistry, TaskCenter,
                         DualCenter, CapabilityCenter, PlanStep, GoalPlanner, FailureReplanner,
                         TaskStatus)

@pytest.fixture
def setup(tmp_path):
    registry = CapabilityRegistry()
    registry.register(Capability('tool.qc', 'QC', CapabilityKind.TOOL))
    return registry, TaskCenter(tmp_path / 'tasks.db'), CapabilityCenter(registry, tmp_path / 'caps.db')

def test_planner_topological_submission(setup):
    _, tasks, _ = setup
    planner = GoalPlanner(lambda goal: {'steps': [
        {'key':'a','prompt':'QC'}, {'key':'b','prompt':'Analyze','depends_on':['a']} ]})
    first, second = planner.submit(tasks, 'go')
    assert second.depends_on == [first.id]
    assert [t.id for t in tasks.ready()] == [first.id]

def test_planner_invalid_cycle_or_forward_reference(setup):
    _, tasks, _ = setup
    p = GoalPlanner(lambda goal: {'steps':[{'key':'a','prompt':'x','depends_on':['b']},
                                              {'key':'b','prompt':'y','depends_on':['a']} ]})
    with pytest.raises(ValueError): p.submit(tasks, 'go')
    assert tasks.list() == []

def test_gap_review_publish(setup):
    registry, tasks, caps = setup
    tasks.submit('Need skill', required_capabilities=['skill.analysis'], task_id='gap')
    class Worker:
        def run(self, task, capabilities): return 'done'
    engine = DualCenter(tasks, registry, Worker(), capability_center=caps)
    assert engine.step().status == TaskStatus.WAITING_APPROVAL
    assert caps.proposal('skill.analysis').status == 'proposed'
    with pytest.raises(PermissionError):
        caps.publish(Capability('skill.analysis', 'analysis', CapabilityKind.SKILL), verified=True)
    caps.review('skill.analysis', approve=True)
    with pytest.raises(PermissionError):
        caps.publish(Capability('skill.analysis', 'analysis', CapabilityKind.SKILL), verified=False)
    caps.publish(Capability('skill.analysis', 'analysis', CapabilityKind.SKILL), verified=True)
    tasks.approve('gap')
    assert engine.step().status == TaskStatus.COMPLETED
    assert caps.performance('skill.analysis')['calls'] == 1

def test_transitive_gaps(setup):
    registry, _, caps = setup
    registry.register(Capability('skill.analysis', 'analysis', CapabilityKind.SKILL, requires=('mcp.genes.lookup',)))
    assert caps.gaps_for(['skill.analysis']) == ['mcp.genes.lookup']

def test_one_time_replan(setup):
    registry, tasks, caps = setup
    tasks.submit('failing task', task_id='fail', max_attempts=1)
    class Worker:
        def run(self, task, capabilities):
            if 'failing' in task.prompt: raise ValueError('crash')
            return 'fixed'
    replan = FailureReplanner(tasks, lambda task: PlanStep('recovery', 'Repair input'))
    engine = DualCenter(tasks, registry, Worker(), approval_for_unmatched=False,
                        capability_center=caps, replanner=replan)
    failed = engine.step()
    assert failed.status == TaskStatus.FAILED
    assert tasks.get('fail').status == TaskStatus.PENDING
    new = next(t for t in tasks.list() if t.id != 'fail')
    assert tasks.get('fail').depends_on == [new.id]
    assert engine.step().status == TaskStatus.COMPLETED
    assert engine.step().status == TaskStatus.FAILED
    assert len(tasks.list()) == 2
    assert len([e for e in tasks.events('fail') if e[1]=='replanned']) == 1

def test_gap_rejected_not_published(setup):
    _, _, caps = setup
    caps.propose_gaps(['tool.unknown'], reason='test')
    caps.review('tool.unknown', approve=False)
    with pytest.raises(PermissionError):
        caps.publish(Capability('tool.unknown','x',CapabilityKind.TOOL), verified=True)
