"""Offline reproducible demo; synthetic validated evidence, no external APIs."""
from tempfile import TemporaryDirectory
from pathlib import Path

from biomni_dual import (
    ActionOption, AuditLog, CalibratedValuePolicy, LearningContext,
    OnlineLearningStore, PredictionRecord, QualityEvidence, QualityPolicy,
)


def option(name, progress):
    return ActionOption('explore',name,progress,.5,0,1,.1,
                        evidence_ids=('demo:forecast',))


def observed(progress, quality):
    return QualityPolicy(min_quality=.7).evaluate(QualityEvidence(
        scientific_quality=quality, reproducibility=.9, goal_progress=progress,
        evidence_ids=('demo:synthetic-check',), uncertainty_before=.8,
        uncertainty_after=.3, cost=1, risk=.1))


def main():
    with TemporaryDirectory() as tmp:
        path=str(Path(tmp)/'experience.sqlite')
        store=OnlineLearningStore(path)
        fast=LearningContext('longitudinal_causal',('fast_tool',),'codex')
        careful=LearningContext('longitudinal_causal',('validated_tool',),'biomni')
        policy=CalibratedValuePolicy(store,prior_strength=2)
        options=[(option('fast',.95),fast),(option('validated',.75),careful)]
        before=[r.option.identifier for r in policy.rank(options,remaining_cost=5)]
        for context,score,quality in [(fast,.1,.2),(careful,.9,.95)]:
            for i in range(6):
                key=f'{context.worker_id}:{i}'
                store.predict(PredictionRecord(key,context,option(key,.9)))
                store.observe(key,context,observed(score,quality))
        restored=OnlineLearningStore(path)
        after=[r.option.identifier for r in CalibratedValuePolicy(
            restored,prior_strength=2).rank(options,remaining_cost=5)]
        print('before learning:',before)
        print('after learning:',after)
        print('fast success posterior:',round(restored.estimate(fast).success_probability,3))
        print('validated success posterior:',round(restored.estimate(careful).success_probability,3))
        print('records (predictions,outcomes):',restored.count())
        print('audit verified:',AuditLog(path).verify())
        assert before==['fast','validated']
        assert after==['validated','fast']


if __name__=='__main__':
    main()
