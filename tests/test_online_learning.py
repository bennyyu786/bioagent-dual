import asyncio
from dataclasses import replace

import pytest

from biomni_dual import (
    Action, ActionOption, AdaptiveController, AdaptiveStore, AuditLog,
    CalibratedValuePolicy, CallbackPlanner, ExperienceStore, LearningContext,
    LearnedValueGuidedPlanner, OnlineLearningStore, OutcomeAssessment,
    PlanProposal, PredictionRecord, QualityEvidence, QualityPolicy,
    TaskCenter, TaskStatus, ValuePolicy, DAGPatch,
)


def evidence(progress=.8, quality=.9, cost=2, before=.9, after=.4):
    return QualityEvidence(quality,.9,progress,('sha256:trusted_validator_output',),
                           uncertainty_before=before,uncertainty_after=after,
                           cost=cost,risk=.1)


def option(identifier, progress=.8, info=.5, cost=1, risk=.1, action='explore'):
    return ActionOption(action,identifier,progress,info,.1,cost,risk,evidence_ids=('planner:evidence',))


def add(store, context, n, *, progress=.2, quality=.2, cost=1, estimated=.9):
    for i in range(n):
        key=f'{context.task_kind}:{context.worker_id}:{','.join(context.capability_ids)}:{i}'
        store.predict(PredictionRecord(key,context,option(key,progress=estimated)))
        assessment=QualityPolicy(min_quality=.75).evaluate(evidence(progress=progress,quality=quality,cost=cost))
        store.observe(key,context,assessment)


def test_context_checks_and_cold_start(tmp_path):
    with pytest.raises(ValueError): LearningContext('study',('b','a'))
    with pytest.raises(ValueError): LearningContext('')
    st=OnlineLearningStore(str(tmp_path/'x.db'))
    ctx=LearningContext('study',('tool.qc',),'codex')
    original=option('x')
    assert st.estimate(ctx).samples==0
    assert CalibratedValuePolicy(st).calibrate(original,ctx)==original


def test_duplicate_and_conflicting_prediction_and_outcome(tmp_path):
    st=OnlineLearningStore(str(tmp_path/'x.db'))
    ctx=LearningContext('task')
    trial=PredictionRecord('run1',ctx,option('a'))
    assert st.predict(trial)
    assert not st.predict(trial)
    with pytest.raises(ValueError,match='Conflicting'):
        st.predict(replace(trial, option=option('a',progress=.1)))
    score=QualityPolicy().evaluate(evidence())
    assert st.observe('run1',ctx,score)
    assert not st.observe('run1',ctx,score)
    with pytest.raises(ValueError,match='Conflicting'):
        st.observe('run1',ctx,QualityPolicy().evaluate(evidence(progress=.1)))
    assert st.count()==(1,1)
    assert AuditLog(st.database).verify()


def test_unvalidated_records_are_not_learned(tmp_path):
    st=OnlineLearningStore(str(tmp_path/'x.db'))
    with pytest.raises(TypeError): st.observe('a',LearningContext('task'), {'accepted':True})
    assert st.count()==(0,0)


def test_learning_discounts_repeated_failures_and_does_not_cross_domains(tmp_path):
    st=OnlineLearningStore(str(tmp_path/'x.db'))
    bad=LearningContext('variant_qc',('tool.bad',),'workerA')
    good=LearningContext('variant_qc',('tool.good',),'workerA')
    other=LearningContext('causal_analysis',('tool.bad',),'workerA')
    add(st,bad,8,progress=.05,quality=.1)
    add(st,good,8,progress=.9,quality=.95)
    p=CalibratedValuePolicy(st,prior_strength=2)
    candidates=[(option('bad',.95),bad),(option('good',.7),good)]
    ranked=p.rank(candidates,remaining_cost=10)
    assert [x.option.identifier for x in ranked]==['good','bad']
    assert st.estimate(bad).success_probability < st.estimate(good).success_probability
    assert p.calibrate(option('other',.95),other).expected_progress==.95
    assert st.estimate(bad).mean_prediction_error>.5
    assert OnlineLearningStore(st.database).estimate(bad).samples==8


def test_unknown_information_gain_does_not_become_fake_zero(tmp_path):
    st=OnlineLearningStore(str(tmp_path/'x.db'));ctx=LearningContext('study')
    for i in range(5):
        score=QualityPolicy().evaluate(evidence(before=None,after=None))
        st.observe(f't{i}',ctx,score)
    assert st.estimate(ctx).mean_information_gain is None
    old=option('a',info=.77)
    assert CalibratedValuePolicy(st).calibrate(old,ctx).expected_information_gain==.77


def test_hard_constraints_remain_enforced_after_learning(tmp_path):
    st=OnlineLearningStore(str(tmp_path/'x.db'));ctx=LearningContext('study')
    add(st,ctx,10,progress=1,quality=1,cost=.01)
    p=CalibratedValuePolicy(st,exploration_bonus=.25)
    candidates=[(option('overbudget',cost=100),ctx),
                (option('overrisk',risk=.9),ctx),
                (replace(option('disabled'),feasible=False),ctx),
                (option('ok',cost=1),ctx)]
    assert [x.option.identifier for x in p.rank(candidates,remaining_cost=2,max_risk=.4)]==['ok']


def test_negative_information_gain_not_counted_as_positive(tmp_path):
    st=OnlineLearningStore(str(tmp_path/'x.db'));ctx=LearningContext('study')
    st.observe('a',ctx,QualityPolicy().evaluate(evidence(before=.1,after=.8)))
    assert st.estimate(ctx).mean_information_gain==pytest.approx(-.7)
    assert 0 <= CalibratedValuePolicy(st).calibrate(option('a'),ctx).expected_information_gain<.5


def test_planner_uses_persisted_learning_and_preserves_dag_validator(tmp_path):
    db=str(tmp_path/'plan.db');st=OnlineLearningStore(db)
    bad=LearningContext('study',('baseline',)); good=LearningContext('study',('inspection',))
    add(st,bad,7,progress=.05,quality=.1)
    add(st,good,7,progress=.9,quality=.95)
    def candidates(snapshot):
        return [(option('baseline',progress=.95),
                 PlanProposal(Action.EXPLORE,DAGPatch(additions=({'key':'model','prompt':'model prematurely'},))),bad),
                (option('inspection',progress=.7),
                 PlanProposal(Action.EXPLORE,DAGPatch(additions=({'key':'inspect','prompt':'inspect the data'},))),good)]
    store=AdaptiveStore(db);tasks=TaskCenter(db);store.create_goal('study',goal_id='goal')
    planner=LearnedValueGuidedPlanner(candidates,CalibratedValuePolicy(st,prior_strength=2))
    class Worker:
        async def run(self,task,capabilities): return 'ok'
    class Validator:
        async def validate(self,task,output): return True,'ok'
    ctl=AdaptiveController(goal_id='goal',store=store,tasks=tasks,planner=planner,
                           worker=Worker(),validator=Validator())
    assert asyncio.run(ctl.tick()).action==Action.EXPLORE
    assert ctl.snapshot().tasks[0].prompt=='inspect the data'


def test_controller_records_real_quality_feedback_with_provided_context(tmp_path):
    db=str(tmp_path/'feedback.db');tasks=TaskCenter(db);store=AdaptiveStore(db)
    online=OnlineLearningStore(db);store.create_goal('study',goal_id='g')
    task=tasks.submit('quality check',max_attempts=1);store.bind_tasks('g',[task.id],tasks)
    class Worker:
        async def run(self,task,capabilities):return 'real output from test worker'
    class Validator:
        async def validate(self,task,output):return True,'syntactic check'
    class Evaluator:
        async def evaluate(self,task,output):return evidence(progress=.9,quality=.95)
    context=LearningContext('variant_qc',(),'biomni')
    ctl=AdaptiveController(goal_id='g',store=store,tasks=tasks,
        planner=CallbackPlanner(lambda snapshot:PlanProposal(Action.ASK_HUMAN)),
        worker=Worker(),validator=Validator(),outcome_evaluator=Evaluator(),
        quality_policy=QualityPolicy(),experience_store=ExperienceStore(db),
        online_learning_store=online,learning_context=lambda task,capabilities:context)
    assert asyncio.run(ctl.tick()).action==Action.EXECUTE
    assert tasks.get(task.id).status==TaskStatus.COMPLETED
    assert online.estimate(context).samples==1
    assert online.estimate(LearningContext('other')).samples==0
    assert AuditLog(db).verify()


def test_online_learning_requires_quality_policy(tmp_path):
    db=str(tmp_path/'x.db');store=AdaptiveStore(db);tasks=TaskCenter(db)
    store.create_goal('study',goal_id='g')
    with pytest.raises(ValueError,match='Online learning'):
        AdaptiveController(goal_id='g',store=store,tasks=tasks,planner=None,worker=None,validator=None,
                           online_learning_store=OnlineLearningStore(db),
                           learning_context=lambda t,c:LearningContext('study'))


def test_invalid_values_rejected(tmp_path):
    st=OnlineLearningStore(str(tmp_path/'x.db'))
    with pytest.raises(ValueError): CalibratedValuePolicy(st,exploration_bonus=2)
    with pytest.raises(ValueError): CalibratedValuePolicy(st,prior_strength=0)
    with pytest.raises(ValueError): st.observe('x',LearningContext('study'),QualityPolicy().evaluate(evidence()),elapsed_ms=-1)
