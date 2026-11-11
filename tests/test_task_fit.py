import pytest
from biomni_dual import (Capability, CapabilityKind, CapabilityRegistry,
    Requirement, TaskRequirements, select_for_task, TaskFitCoordinator)


def cap(name, provides=(), **kwargs):
    meta = kwargs.pop('metadata', {})
    return Capability(name, name, CapabilityKind.SKILL,
        metadata={'provides': list(provides), **meta}, **kwargs)


def registry(*caps):
    r = CapabilityRegistry()
    for c in caps: r.register(c)
    return r


def req(*ids, **kw):
    return TaskRequirements('t', tuple(Requirement(x) for x in ids), **kw)


def test_existing_sufficient_bypasses_external():
    r=registry(cap('a',('qc','normalize'),cost=1))
    d=TaskFitCoordinator(r).request_gaps(req('qc','normalize'),kinds={},search=True)
    assert d['selection'].selected == ('a',)
    assert not d['selection'].search_external
    assert d['requests']==()


def test_minimal_combination_prefers_low_cost():
    r=registry(cap('both',('qc','stats'),cost=2), cap('qc',cost=.4), cap('stats',cost=.4),cap('noisy',('qc','stats'),cost=10))
    d=select_for_task(r,req('qc','stats'))
    assert d.sufficient and d.selected==('qc','stats') and d.total_cost==pytest.approx(.8)


def test_schema_is_hard_not_soft_constraint():
    r=registry(cap('wrong',('qc',),quality=1,metadata={'input_schema':'text'}),
               cap('right',('qc',),quality=.7,metadata={'input_schema':'vcf'}))
    d=select_for_task(r,req('qc',input_schema='vcf'))
    assert d.selected==('right',)
    assert 'input_schema_mismatch' in d.evidences[1].reasons or 'input_schema_mismatch' in d.evidences[0].reasons


def test_permission_and_license_filters_fail_closed():
    r=registry(cap('bad',('qc',),metadata={'permissions':['network'],'license':'unknown'}),
               cap('safe',('qc',),metadata={'permissions':[],'license':'MIT'}))
    d=select_for_task(r,req('qc',allowed_licenses=('MIT',)))
    assert d.selected==('safe',)


def test_dependencies_included_once():
    r=registry(cap('base',cost=.3),cap('upper',('qc',),requires=('base',),cost=.7))
    d=select_for_task(r,req('qc'))
    assert d.selected==('base','upper') and d.total_cost == pytest.approx(1)


def test_cycle_or_missing_dependency_rejected():
    r=registry(cap('one',('qc',),requires=('two',)),cap('two',requires=('one',)))
    assert select_for_task(r,req('qc')).gaps==('qc',)


def test_budget_exhaustion_no_false_success():
    d=select_for_task(registry(cap('a',('qc',),cost=50)),req('qc',max_cost=2))
    assert not d.sufficient and d.selected==()


def test_weighted_partial_coverage():
    r=registry(cap('a',('qc',),cost=1))
    t=TaskRequirements('t',(Requirement('qc', weight=9),Requirement('optional', weight=1)),min_coverage=.9)
    d=select_for_task(r,t)
    assert d.sufficient and d.selected==('a',) and d.gaps==('optional',)
    assert not d.search_external


def test_internal_needs_explicit():
    from biomni_dual import Task
    with pytest.raises(ValueError): TaskRequirements.from_task(Task('t','some vague biology'))


class FakeAutonomy:
    def __init__(self): self.calls=[];self.searches=[]
    def request(self, cap, kind, requirement, *, request_key):
        self.calls.append((cap,kind,request_key));return {'id':cap}
    def discover_marketplaces(self,id,*,query,limit):
        self.searches.append((id,query));return {'candidates':[]}


def test_only_missing_need_triggers_market():
    a=FakeAutonomy()
    d=TaskFitCoordinator(registry(cap('existing',('qc',))),a).request_gaps(
        req('qc','causal'),kinds={'causal':'skill'},search=True)
    assert [r['id'] for r in d['requests']]==['causal']
    assert a.calls[0][0]=='causal' and len(a.searches)==1


def test_unknown_missing_kind_fails_before_any_request():
    a=FakeAutonomy()
    with pytest.raises(ValueError): TaskFitCoordinator(registry(),a).request_gaps(req('qc'),kinds={},search=True)
    assert not a.calls


def test_unhealthy_capability_not_used():
    assert select_for_task(registry(cap('a',('qc',),healthy=False)),req('qc')).gaps==('qc',)


def test_greedy_many_candidates():
    caps=[cap(f'c{i}',(f'n{i}',)) for i in range(20)]
    t=req(*(f'n{i}' for i in range(20)))
    d=select_for_task(registry(*caps),t)
    assert d.sufficient and len(d.selected)==20


def test_market_not_searched_when_budget_blocks_complete_set():
    a=FakeAutonomy()
    r=registry(cap('a',('qc',),cost=3),cap('b',('stats',),cost=3))
    decision=TaskFitCoordinator(r,a).request_gaps(req('qc','stats',max_cost=4),kinds={},search=True)
    assert decision['selection'].blocker=='insufficient_budget_or_combination'
    assert decision['requests']==() and not a.searches


def test_unauthorized_approval_capability_is_infeasible():
    r=registry(cap('privileged',('qc',),approval_required=True))
    d=select_for_task(r,req('qc'))
    assert not d.sufficient
    assert 'approval_required' in d.evidences[0].reasons


def test_scheduler_opt_in_uses_minimal_bundle(tmp_path):
    from biomni_dual import ParallelScheduler, ResourcePool, WorkerPool, TaskCenter
    class Worker:
        def __init__(self): self.received=[]
        def run(self, task, capabilities):
            self.received.append(tuple(c.id for c in capabilities))
            return 'ok'
    r=registry(cap('expensive',('qc',),cost=100),cap('cheap',('qc',),cost=1))
    tasks=TaskCenter(tmp_path/'tasks.sqlite')
    task=tasks.submit('run qc',task_id='t',required_capabilities=['qc'])
    worker=Worker()
    scheduler=ParallelScheduler(tasks,r,WorkerPool(worker),ResourcePool(cpu=2),
        task_fit_factory=lambda t: TaskRequirements.from_task(t,max_cost=10))
    done=scheduler.run_until_idle()
    assert done and worker.received==[('cheap',)]


def test_scheduler_opt_in_blocks_missing_without_running(tmp_path):
    from biomni_dual import ParallelScheduler, ResourcePool, WorkerPool, TaskCenter, TaskStatus
    class Worker:
        def run(self,*args): raise AssertionError('should not run')
    tasks=TaskCenter(tmp_path/'tasks.sqlite')
    tasks.submit('missing',task_id='t',required_capabilities=['qc'])
    s=ParallelScheduler(tasks,registry(),WorkerPool(Worker()),ResourcePool(cpu=1),
        task_fit_factory=lambda t: TaskRequirements.from_task(t))
    s.run_until_idle()
    assert tasks.get('t').status==TaskStatus.WAITING_APPROVAL
