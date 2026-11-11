import asyncio
import pytest
from biomni_dual import (
    QualityEvidence, QualityPolicy, ValuePolicy, ActionOption, ExperienceStore,
    AdaptiveController, AdaptiveStore, CallbackPlanner, PlanProposal, Action,
    TaskCenter, TaskStatus, AuditLog,
)


def run(c): return asyncio.run(c)


def evidence(**overrides):
    base=dict(scientific_quality=.9,reproducibility=.9,goal_progress=.7,
              evidence_ids=('sha256:reproducible-evidence',), uncertainty_before=.8,
              uncertainty_after=.3,cost=2,risk=.1)
    base.update(overrides)
    return QualityEvidence(**base)


@pytest.mark.parametrize('key,bad',[
    ('scientific_quality',float('nan')),('reproducibility',1.1),
    ('goal_progress',-.1),('risk',float('inf')),('cost',-1),
    ('uncertainty_before',1.3),
])
def test_reject_invalid_evidence(key,bad):
    with pytest.raises(ValueError): evidence(**{key:bad})


def test_uncertainty_must_have_paired_measurements():
    with pytest.raises(ValueError): evidence(uncertainty_after=None)
    assert evidence(uncertainty_before=None,uncertainty_after=None).measured_information_gain is None
    assert evidence().measured_information_gain == pytest.approx(.5)
    assert evidence(uncertainty_before=.2,uncertainty_after=.6).measured_information_gain == pytest.approx(-.4)


def test_policy_hard_gates_are_not_overridden_by_utility():
    policy=QualityPolicy(min_quality=.8,min_reproducibility=.8,max_risk=.3)
    result=policy.evaluate(evidence(scientific_quality=.75,goal_progress=1))
    assert not result.accepted
    assert 'scientific_quality_below_threshold' in result.reason
    assert policy.evaluate(evidence()).accepted
    assert not policy.evaluate(evidence(risk=.7)).accepted


def option(action, identifier, progress, information, reuse, cost, risk, **extra):
    return ActionOption(action, identifier, progress, information, reuse,cost,risk,evidence_ids=('plan:1',),**extra)


def test_action_policy_prefers_information_when_valuable():
    policy=ValuePolicy(progress=1,information=1,reuse=0,cost=.1,risk=.5)
    candidates=[option('execute','run',.4,.1,0,1,.1),
                option('explore','inspect',.2,.8,0,1,.1)]
    assert policy.rank(candidates,remaining_cost=5)[0].option.action=='explore'


def test_action_policy_enforces_budget_risk_and_feasibility():
    policy=ValuePolicy()
    candidates=[option('execute','expensive',1,1,1,30,.1),
                option('build_capability','risky',1,1,1,1,.8),
                option('explore','unavailable',1,1,1,1,.1,feasible=False),
                option('validate','safe',.2,.4,0,1,.1)]
    assert [v.option.identifier for v in policy.rank(candidates,remaining_cost=2,max_risk=.3)] == ['safe']
    assert policy.rank(candidates,remaining_cost=0)==[]


def test_invalid_action_estimates_rejected():
    with pytest.raises(ValueError): option('execute','x',.5,.5,.5,1,float('nan'))
    with pytest.raises(ValueError): ValuePolicy(information=-1)
    with pytest.raises(ValueError): QualityPolicy(quality_weight=.8)


def test_experience_store_idempotent_tamper_and_isolation(tmp_path):
    db=str(tmp_path/'eval.sqlite')
    store=ExperienceStore(db)
    assessment=QualityPolicy().evaluate(evidence())
    assert store.save(goal_id='a',task_id='t',attempt=1,assessment=assessment)
    assert not ExperienceStore(db).save(goal_id='a',task_id='t',attempt=1,assessment=assessment)
    assert ExperienceStore(db).list('a')[0]['accepted']
    assert ExperienceStore(db).list('b')==[]
    with pytest.raises(ValueError,match='Conflicting'):
        store.save(goal_id='a',task_id='t',attempt=1,
                   assessment=QualityPolicy().evaluate(evidence(goal_progress=.2)))
    assert AuditLog(db).verify()


class Worker:
    async def run(self,task,capabilities): return 'result with domain data'


class Validator:
    async def validate(self,task,output): return True,'syntactic_pass'


class Evaluator:
    def __init__(self, result): self.result=result
    async def evaluate(self,task,output): return self.result


def setup(tmp_path, evaluator):
    db=str(tmp_path/'research.sqlite')
    tasks=TaskCenter(db);store=AdaptiveStore(db)
    store.create_goal('study',goal_id='g')
    task=tasks.submit('complex analysis',max_attempts=1)
    store.bind_tasks('g',[task.id],tasks)
    ctl=AdaptiveController(goal_id='g',store=store,tasks=tasks,
                           planner=CallbackPlanner(lambda s:PlanProposal(Action.ASK_HUMAN)),
                           worker=Worker(),validator=Validator(),
                           outcome_evaluator=evaluator,quality_policy=QualityPolicy(min_quality=.8),
                           experience_store=ExperienceStore(db))
    return ctl,tasks,task,db


def test_domain_quality_failure_blocks_completed(tmp_path):
    ctl,tasks,task,db=setup(tmp_path,Evaluator(evidence(scientific_quality=.5)))
    assert run(ctl.tick()).action==Action.VALIDATE
    assert tasks.get(task.id).status==TaskStatus.FAILED
    outcomes=ExperienceStore(db).list('g')
    assert len(outcomes)==1 and not outcomes[0]['accepted']
    observations=ctl.store.observations('g')
    assert observations[0].evidence['quality_evaluated']


def test_high_quality_passes_and_survives_restart(tmp_path):
    ctl,tasks,task,db=setup(tmp_path,Evaluator(evidence()))
    assert run(ctl.tick()).action==Action.EXECUTE
    assert tasks.get(task.id).status==TaskStatus.COMPLETED
    outcomes=ExperienceStore(db).list('g')
    assert outcomes[0]['information_gain']==pytest.approx(.5)
    assert outcomes[0]['operational_utility']>0
    assert AuditLog(db).verify()


def test_domain_evaluator_cannot_return_unstructured_scores(tmp_path):
    ctl,tasks,task,db=setup(tmp_path,Evaluator({'quality':1}))
    assert run(ctl.tick()).action==Action.EXECUTE
    assert tasks.get(task.id).status!=TaskStatus.COMPLETED


def test_quality_is_opt_in_backwards_compatible(tmp_path):
    db=str(tmp_path/'db.sqlite'); tasks=TaskCenter(db); store=AdaptiveStore(db)
    store.create_goal('study',goal_id='g'); t=tasks.submit('test')
    store.bind_tasks('g',[t.id],tasks)
    ctl=AdaptiveController(goal_id='g',store=store,tasks=tasks,
                           planner=CallbackPlanner(lambda s:PlanProposal(Action.ASK_HUMAN)),
                           worker=Worker(),validator=Validator())
    assert run(ctl.tick()).action==Action.EXECUTE
    assert tasks.get(t.id).status==TaskStatus.COMPLETED


def test_quality_config_requires_both_components(tmp_path):
    db=str(tmp_path/'db.sqlite'); tasks=TaskCenter(db); store=AdaptiveStore(db)
    store.create_goal('study',goal_id='g')
    with pytest.raises(ValueError):
        AdaptiveController(goal_id='g',store=store,tasks=tasks,
                           planner=CallbackPlanner(lambda s:PlanProposal(Action.ASK_HUMAN)),
                           worker=Worker(),validator=Validator(),quality_policy=QualityPolicy())


def test_value_guided_planner_changes_next_plan(tmp_path):
    from biomni_dual import ValueGuidedPlanner, DAGPatch
    db=str(tmp_path/'guide.sqlite'); tasks=TaskCenter(db); store=AdaptiveStore(db)
    store.create_goal('unfamiliar task',goal_id='g')
    def candidates(snapshot):
        return [
            (option('explore','inspect',.3,.9,.1,1,.1),
             PlanProposal(Action.EXPLORE,DAGPatch(additions=({'key':'inspect','prompt':'inspect unknown samples'},)),'reduce uncertainty')),
            (option('replan','guess',.5,.05,0,1,.2),
             PlanProposal(Action.REPLAN,DAGPatch(additions=({'key':'model','prompt':'build model now'},)),'skip inspection')),
        ]
    planner=ValueGuidedPlanner(candidates,policy=ValuePolicy(progress=.5,information=1,cost=.05))
    ctl=AdaptiveController(goal_id='g',store=store,tasks=tasks,planner=planner,
                           worker=Worker(),validator=Validator())
    choice=run(ctl.tick())
    assert choice.action==Action.EXPLORE
    assert ctl.snapshot().tasks[0].prompt=='inspect unknown samples'


def test_value_guided_planner_fails_closed_on_budget(tmp_path):
    from biomni_dual import ValueGuidedPlanner, DAGPatch
    db=str(tmp_path/'guide.sqlite'); tasks=TaskCenter(db); store=AdaptiveStore(db)
    store.create_goal('task',goal_id='g')
    planner=ValueGuidedPlanner(lambda s:[
        (option('explore','expensive',1,1,1,15,.2),
         PlanProposal(Action.EXPLORE,DAGPatch(additions=({'key':'a','prompt':'a'},))))],remaining_cost=1)
    ctl=AdaptiveController(goal_id='g',store=store,tasks=tasks,planner=planner,
                           worker=Worker(),validator=Validator())
    assert run(ctl.tick()).action==Action.ASK_HUMAN
    assert not ctl.snapshot().tasks
