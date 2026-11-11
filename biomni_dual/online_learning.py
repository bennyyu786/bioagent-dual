"""Evidence-aware online calibration of future task/capability choices.

This is an observational, small-data estimator, NOT reinforcement learning or
proof of scientific validity. Only independent validated outcomes are learned.
Never relaxes feasibility, risk or budget gates; does not execute actions.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from typing import Iterable

from .reliability import AuditLog
from .value_feedback import ActionOption, ActionRanking, OutcomeAssessment, ValuePolicy


def _number(value, name, *, minimum=0., maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'invalid {name}')
    return float(value)


@dataclass(frozen=True)
class LearningContext:
    """Explicit, stable context: do not infer context from untrusted LLM output."""
    task_kind: str
    capability_ids: tuple[str, ...] = ()
    worker_id: str = 'unspecified'

    def __post_init__(self):
        for text in (self.task_kind, self.worker_id, *self.capability_ids):
            if not isinstance(text, str) or not text.strip() or len(text) > 160:
                raise ValueError('context fields must be short nonempty strings')
        if tuple(sorted(set(self.capability_ids))) != self.capability_ids:
            raise ValueError('capability IDs must be sorted and unique')

    @property
    def key(self):
        return json.dumps(asdict(self),sort_keys=True,separators=(',',':'))


@dataclass(frozen=True)
class PredictionRecord:
    """Forecast captured *before* outcome is known, for prediction-error analysis."""
    key: str
    context: LearningContext
    option: ActionOption

    def __post_init__(self):
        if not self.key or len(self.key)>240:
            raise ValueError('invalid prediction key')


@dataclass(frozen=True)
class LearnedEstimate:
    context: LearningContext
    samples: int
    success_probability: float  # Beta(1,1) posterior mean
    mean_progress: float | None
    mean_information_gain: float | None
    mean_cost: float | None
    mean_quality: float | None
    mean_prediction_error: float | None


class OnlineLearningStore:
    """SQLite idempotent records; atomic audit append; restart-safe.

    Outcome verification is enforced by the calling boundary: observations
    require OutcomeAssessment, which contains QualityEvidence. The independent
    evaluator providing QualityEvidence must be trusted by deployment policy.
    """
    def __init__(self, database: str):
        self.database=str(database)
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS learning_predictions (
                key TEXT PRIMARY KEY, context TEXT NOT NULL, forecast TEXT NOT NULL,
                fingerprint TEXT NOT NULL, created REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS learning_outcomes (
                key TEXT PRIMARY KEY, context TEXT NOT NULL, observed TEXT NOT NULL,
                fingerprint TEXT NOT NULL, created REAL NOT NULL)''')
            AuditLog.install(db)

    def _connect(self):
        return sqlite3.connect(self.database,timeout=20)

    @staticmethod
    def _dump(obj):
        return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

    def _write(self,table: str,key: str,context: LearningContext,payload: dict,action: str) -> bool:
        if table not in ('learning_predictions','learning_outcomes'):
            raise ValueError('unknown table')
        if not isinstance(key,str) or not key.strip() or len(key)>240:
            raise ValueError('invalid observation identity')
        serialized=self._dump(payload)
        if len(serialized)>60000: raise ValueError('oversize learning record')
        fingerprint=hashlib.sha256(serialized.encode()).hexdigest()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute(f'SELECT context,fingerprint FROM {table} WHERE key=?',(key,)).fetchone()
            if old:
                if old != (context.key,fingerprint):
                    raise ValueError('Conflicting idempotency key')
                return False
            db.execute(f'INSERT INTO {table} VALUES (?,?,?,?,?)',(key,context.key,serialized,fingerprint,time.time()))
            AuditLog.append_in_transaction(db,actor='online_learning',action=action,subject=key,
                                           details={'context_sha256':hashlib.sha256(context.key.encode()).hexdigest(),
                                                    'record_sha256':fingerprint})
        return True

    def predict(self, record: PredictionRecord) -> bool:
        return self._write('learning_predictions',record.key,record.context,asdict(record.option),'prediction_recorded')

    def observe(self, key: str, context: LearningContext, assessment: OutcomeAssessment, *,
                elapsed_ms: float | None = None) -> bool:
        if not isinstance(assessment, OutcomeAssessment):
            raise TypeError('Only independently assessed outcomes can be learned')
        if elapsed_ms is not None: _number(elapsed_ms,'elapsed_ms')
        ev=assessment.evidence
        payload={'accepted':assessment.accepted,'progress':ev.goal_progress,
                 'quality':ev.scientific_quality,'information_gain':assessment.information_gain,
                 'cost':float(ev.cost),'elapsed_ms':elapsed_ms,
                 'evidence_ids':ev.evidence_ids,'reason':assessment.reason}
        return self._write('learning_outcomes',key,context,payload,'validated_outcome_learned')

    def estimate(self,context: LearningContext) -> LearnedEstimate:
        # Exact-context learning prevents one scientific domain from silently
        # contaminating another. No fuzzy fallback across task kinds.
        with self._connect() as db:
            rows=db.execute('''SELECT o.observed,p.forecast FROM learning_outcomes o
                LEFT JOIN learning_predictions p ON p.key=o.key AND p.context=o.context
                WHERE o.context=? ORDER BY o.key''',(context.key,)).fetchall()
        if not rows:
            return LearnedEstimate(context,0,.5,None,None,None,None,None)
        values=[json.loads(row[0]) for row in rows]
        n=len(values)
        successes=sum(bool(x['accepted']) for x in values)
        def mean(field):
            x=[float(v[field]) for v in values if v.get(field) is not None]
            return sum(x)/len(x) if x else None
        paired_errors=[]
        for observed,forecast in rows:
            if forecast is None: continue
            o=json.loads(observed); p=json.loads(forecast)
            paired_errors.append(abs(float(o['progress'])-float(p['expected_progress'])))
        return LearnedEstimate(context,n,(successes+1)/(n+2),mean('progress'),
                               mean('information_gain'),mean('cost'),mean('quality'),
                               sum(paired_errors)/len(paired_errors) if paired_errors else None)

    def count(self):
        with self._connect() as db:
            return (db.execute('SELECT COUNT(*) FROM learning_predictions').fetchone()[0],
                    db.execute('SELECT COUNT(*) FROM learning_outcomes').fetchone()[0])


class CalibratedValuePolicy:
    """Shrink uncertain model forecasts toward observed, verified outcomes.

    Fallback is the original forecast on cold start. A bounded exploratory
    bonus is allowed only among choices that pass the original hard gates.
    """
    def __init__(self,store: OnlineLearningStore, *, base: ValuePolicy | None = None,
                 prior_strength: float = 4., exploration_bonus: float = 0.0):
        self.store=store
        self.base=base or ValuePolicy()
        self.prior_strength=_number(prior_strength,'prior_strength',minimum=.01)
        self.exploration_bonus=_number(exploration_bonus,'exploration_bonus',maximum=.25)

    def calibrate(self, option: ActionOption, context: LearningContext) -> ActionOption:
        estimate=self.store.estimate(context)
        if estimate.samples == 0:
            return option
        weight=estimate.samples/(estimate.samples+self.prior_strength)
        def blend(previous, measured):
            return previous if measured is None else (1-weight)*previous+weight*measured
        # Rejection probability discounts anticipated progress. Calibrated
        # expected information gain can be negative, but ActionOption's current
        # schema is [0,1], so clamp conservatively at zero for ranking.
        progress=blend(option.expected_progress,estimate.mean_progress)
        progress*=((1-weight)+weight*estimate.success_probability)
        information=max(0.,min(1.,blend(option.expected_information_gain,
                                       estimate.mean_information_gain)))
        cost=blend(option.estimated_cost,estimate.mean_cost)
        return replace(option, expected_progress=max(0.,min(1.,progress)),
                       expected_information_gain=information,
                       estimated_cost=max(0.,cost),
                       # Never lower the declared risk from historic observations.
                       estimated_risk=option.estimated_risk)

    def rank(self, options: Iterable[tuple[ActionOption, LearningContext]], *,
             remaining_cost: float, max_risk: float = .4) -> list[ActionRanking]:
        _number(remaining_cost,'remaining_cost')
        _number(max_risk,'max_risk',maximum=1.)
        ranked=[]
        for option,context in options:
            # Apply ORIGINAL hard constraints before any calibration, preventing
            # historical averages from legitimizing an over-budget option.
            if not option.feasible or option.estimated_cost>remaining_cost or option.estimated_risk>max_risk:
                continue
            calibrated=self.calibrate(option,context)
            if calibrated.estimated_cost>remaining_cost:
                continue
            item=self.base.rank([calibrated],remaining_cost=remaining_cost,max_risk=max_risk)
            if not item: continue
            n=self.store.estimate(context).samples
            bonus=self.exploration_bonus/math.sqrt(n+1)
            ranked.append(ActionRanking(option,round(item[0].score+bonus,8)))
        return sorted(ranked,key=lambda x:(-x.score,x.option.estimated_cost,x.option.identifier))


class LearnedValueGuidedPlanner:
    """Backward-compatible ValueGuidedPlanner candidate source with contexts.

    Candidate callback returns (ActionOption, PlanProposal, LearningContext).
    Proposal still requires AdaptiveController DAG validation and commit.
    """
    def __init__(self,candidates,learner: CalibratedValuePolicy, *,remaining_cost=10.,max_risk=.4):
        self.candidates=candidates;self.learner=learner
        self.remaining_cost=remaining_cost;self.max_risk=max_risk

    async def propose(self,snapshot):
        import inspect
        from .adaptive import Action, PlanProposal
        options=self.candidates(snapshot)
        if inspect.isawaitable(options): options=await options
        items=list(options);by_id={}
        for option,proposal,context in items:
            if option.action not in ('explore','replan','ask_human'):
                raise ValueError('Planner may only propose planning decisions')
            if not isinstance(proposal,PlanProposal) or proposal.action.value != option.action:
                raise ValueError('Action/proposal mismatch')
            if not isinstance(context,LearningContext) or option.identifier in by_id:
                raise ValueError('Invalid or duplicate candidate context')
            by_id[option.identifier]=proposal
        ranking=self.learner.rank(((o,c) for o,_,c in items),remaining_cost=self.remaining_cost,max_risk=self.max_risk)
        if not ranking: return PlanProposal(Action.ASK_HUMAN,reason='no_feasible_calibrated_plan')
        return by_id[ranking[0].option.identifier]
