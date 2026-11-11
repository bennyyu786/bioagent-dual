from pathlib import Path
import pytest
from biomni_dual import (
 Capability, CapabilityKind, CapabilityRegistry, SkillManager, MCPManager,
 TaskCenter, TaskStatus, DualCenter, BiomniWorker
)

class Worker:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
    def run(self, task, capabilities):
        self.calls.append(task.id)
        if self.fail:
            raise RuntimeError('bad')
        return f'ok:{task.id}'

@pytest.fixture
def registry():
    r = CapabilityRegistry()
    r.register(Capability('tool.alpha', 'alpha biomedical analysis', CapabilityKind.TOOL))
    return r

@pytest.fixture
def tasks(tmp_path):
    return TaskCenter(tmp_path / 'jobs.db')

def test_unique_registry_and_matching(registry):
    with pytest.raises(ValueError):
        registry.register(Capability('tool.alpha', 'dup', CapabilityKind.TOOL))
    assert registry.select('alpha analysis')[0].id == 'tool.alpha'
    assert registry.missing(['nonexistent']) == ['nonexistent']

def test_task_dag_and_cycle(tasks):
    a = tasks.submit('first', task_id='a')
    b = tasks.submit('next', depends_on=['a'], task_id='b')
    assert [x.id for x in tasks.ready()] == ['a']
    with pytest.raises(ValueError):
        tasks.add_dependency('a', 'b')

def test_run_dependencies(tasks, registry):
    tasks.submit('alpha', task_id='a')
    tasks.submit('alpha again', task_id='b', depends_on=['a'])
    worker = Worker()
    engine = DualCenter(tasks, registry, worker)
    out = engine.run_until_idle()
    assert [t.id for t in out] == ['a', 'b']
    assert all(t.status == TaskStatus.COMPLETED for t in tasks.list())
    assert worker.calls == ['a', 'b']

def test_retries_and_persistence(tasks, registry):
    tasks.submit('alpha', task_id='fail', max_attempts=2)
    engine = DualCenter(tasks, registry, Worker(fail=True))
    engine.step()
    assert tasks.get('fail').status == TaskStatus.PENDING
    engine.step()
    assert tasks.get('fail').status == TaskStatus.FAILED
    reopened = TaskCenter(tasks.db_path)
    assert reopened.get('fail').attempts == 2
    assert [x[1] for x in reopened.events('fail')][-1] == 'failed'

def test_approval(tasks, registry):
    registry.register(Capability('tool.sensitive', 'sensitive operation', CapabilityKind.TOOL, approval_required=True))
    tasks.submit('sensitive operation', required_capabilities=['tool.sensitive'], task_id='approval')
    engine = DualCenter(tasks, registry, Worker())
    assert engine.step().status == TaskStatus.WAITING_APPROVAL
    tasks.approve('approval')
    assert engine.step().status == TaskStatus.COMPLETED

def test_missing_capability_blocked_for_review(tasks, registry):
    tasks.submit('missing', required_capabilities=['tool.notfound'], task_id='gap')
    engine = DualCenter(tasks, registry, Worker())
    assert engine.step().status == TaskStatus.WAITING_APPROVAL
    assert not tasks.ready()

def test_recover_interrupted(tasks):
    tasks.submit('alpha', task_id='restart')
    tasks.claim('restart')
    assert tasks.recover_interrupted() == 1
    assert tasks.get('restart').status == TaskStatus.PENDING

def test_skill_discovery(tmp_path, registry):
    root = tmp_path / 'skills'
    folder = root / 'demo'
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text('---\nname: demo\ndescription: Test skill\nrequires:\n  tools: [tool.alpha]\n---\n# Test\n')
    manager = SkillManager(registry, root)
    found = manager.discover()
    assert found[0].id == 'skill.demo'
    assert found[0].requires == ('tool.alpha',)
    assert '# Test' in manager.load_instructions('skill.demo')

def test_mcp_no_auto_execution(tmp_path, registry):
    config = tmp_path / 'servers.yaml'
    config.write_text('mcp_servers:\n  sample:\n    enabled: true\n    tools:\n      - name: ping\n        description: Test\n')
    manager = MCPManager(registry)
    assert manager.load_config(config) == 1
    assert registry.get('mcp.sample.ping') is not None
    with pytest.raises(PermissionError):
        manager.attach_to_biomni(object())

def test_biomni_adapter():
    class Stub:
        def go(self, prompt):
            assert 'Research task' in prompt
            return ([], 'answer')
    worker = BiomniWorker(Stub)
    assert worker.run(type('T', (), {'prompt': 'hello'})(), []) == 'answer'

def test_skill_injection(tmp_path, registry):
    root = tmp_path / 'skills'; folder = root / 'test'; folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text('---\nname: pipeline\ndescription: pipeline\n---\n# Follow validation step\n')
    manager = SkillManager(registry, root); manager.discover()
    class Stub:
        def go(self, prompt):
            assert '# Follow validation step' in prompt
            return ([], 'ok')
    cap = registry.get('skill.pipeline')
    assert BiomniWorker(Stub, skill_manager=manager).run(type('T', (), {'prompt':'test'})(), [cap]) == 'ok'
