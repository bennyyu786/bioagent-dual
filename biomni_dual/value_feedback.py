"""Evidence-grounded outcome evaluation and bounded next-action prioritization.

Numeric scores are explicit *operational proxies*, NOT proof of scientific truth.
All claims of information gain require user-supplied before/after uncertainty
measurements and traceable evidence identifiers. No unrestricted agent output is
allowed to modify scheduling or quality thresholds directly.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable, Mapping

from .reliability import AuditLog


def _unit(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f'{name} must be a finite number in [0,1]')
    return float(value)


@dataclass(frozen=True)
class QualityEvidence:
    """Scores must come from independent, domain-defined checks.

    No uncertainty estimates -> information gain is unknown (not zero).
    evidence_ids must point to retained reproducible evidence; a nonempty
    string alone is not evidence of scientific validity.
    """
    scientific_quality: float
    reproducibility: float
    goal_progress: float
    evidence_ids: tuple[str, ...]
    uncertainty_before: float | None = None
    uncertainty_after: float | None = None
    cost: float = 0.0
    risk: float = 0.0
    methodology: str = 'domain_validator'

    def __post_init__(self):
        for name in ('scientific_quality','reproducibility','goal_progress','risk'):
            _unit(getattr(self,name),name)
        if (self.uncertainty_before is None) != (self.uncertainty_after is None):
            raise ValueError('Both uncertainty measurements must be supplied together')
        if self.uncertainty_before is not None:
            _unit(self.uncertainty_before,'uncertainty_before')
            _unit(self.uncertainty_after,'uncertainty_after')
        if isinstance(self.cost,bool) or not isinstance(self.cost,(int,float)) or not math.isfinite(self.cost) or self.cost < 0:
            raise ValueError('cost must be a finite nonnegative number')
        if not self.evidence_ids or any(not isinstance(x,str) or not x.strip() or len(x)>300 for x in self.evidence_ids):
            raise ValueError('Traceable evidence_ids required')
        if not isinstance(self.methodology,str) or not self.methodology.strip():
            raise ValueError('Evaluation methodology required')

    @property
    def measured_information_gain(self) -> float | None:
        if self.uncertainty_before is None:
            return None
        return self.uncertainty_before - self.uncertainty_after


@dataclass(frozen=True)
class OutcomeAssessment:
    accepted: bool
    reason: str
    evidence: QualityEvidence
    operational_utility: float
    information_gain: float | None


@dataclass(frozen=True)
class QualityPolicy:
    min_quality: float = 0.7
    min_reproducibility: float = 0.7
    max_risk: float = 0.4
    min_goal_progress: float = 0.0
    quality_weight: float = 0.35
    progress_weight: float = 0.4
    reproducibility_weight: float = 0.25
    cost_penalty: float = 0.01

    def __post_init__(self):
        for key in ('min_quality','min_reproducibility','max_risk','min_goal_progress','quality_weight','progress_weight','reproducibility_weight'):
            _unit(getattr(self,key),key)
        if self.cost_penalty < 0 or not math.isfinite(self.cost_penalty):
            raise ValueError('Invalid cost_penalty')
        if abs(self.quality_weight+self.progress_weight+self.reproducibility_weight - 1) > 1e-8:
            raise ValueError('Quality weights must sum to 1')

    def evaluate(self, evidence: QualityEvidence) -> OutcomeAssessment:
        reasons=[]
        if evidence.scientific_quality < self.min_quality: reasons.append('scientific_quality_below_threshold')
        if evidence.reproducibility < self.min_reproducibility: reasons.append('reproducibility_below_threshold')
        if evidence.goal_progress < self.min_goal_progress: reasons.append('goal_progress_below_threshold')
        if evidence.risk > self.max_risk: reasons.append('risk_above_threshold')
        utility=(self.quality_weight*evidence.scientific_quality+
                 self.progress_weight*evidence.goal_progress+
                 self.reproducibility_weight*evidence.reproducibility-
                 self.cost_penalty*evidence.cost)
        return OutcomeAssessment(not reasons, '|'.join(reasons) if reasons else 'quality_gates_passed',
                                 evidence,round(utility,8),evidence.measured_information_gain)


@dataclass(frozen=True)
class ActionOption:
    action: str
    identifier: str
    expected_progress: float
    expected_information_gain: float
    future_reuse_value: float
    estimated_cost: float
    estimated_risk: float
    feasible: bool = True
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if self.action not in ('execute','explore','build_capability','validate','replan','ask_human'):
            raise ValueError('Unknown action')
        if not self.identifier or not self.evidence_ids:
            raise ValueError('Action needs identifier and supporting evidence')
        for name in ('expected_progress','expected_information_gain','future_reuse_value','estimated_risk'):
            _unit(getattr(self,name),name)
        if not isinstance(self.estimated_cost,(int,float)) or not math.isfinite(self.estimated_cost) or self.estimated_cost<0:
            raise ValueError('Invalid estimated_cost')


@dataclass(frozen=True)
class ActionRanking:
    option: ActionOption
    score: float


class ValuePolicy:
    """Deterministic bounded heuristic. Never executes actions or bypasses approvals."""
    def __init__(self, *, progress=1.0, information=0.5, reuse=0.25, cost=0.1, risk=1.0):
        self.weights=(progress,information,reuse,cost,risk)
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in self.weights):
            raise ValueError('Invalid action weights')

    def rank(self, options: Iterable[ActionOption], *, remaining_cost: float, max_risk: float = 0.4) -> list[ActionRanking]:
        if remaining_cost < 0 or not math.isfinite(remaining_cost): raise ValueError('Invalid remaining cost')
        _unit(max_risk,'max_risk')
        p,i,f,c,r=self.weights
        candidates=[]
        for item in options:
            if not item.feasible or item.estimated_cost > remaining_cost or item.estimated_risk > max_risk:
                continue
            score=p*item.expected_progress+i*item.expected_information_gain+f*item.future_reuse_value-c*item.estimated_cost-r*item.estimated_risk
            candidates.append(ActionRanking(item,round(score,8)))
        return sorted(candidates,key=lambda item:(-item.score,item.option.estimated_cost,item.option.identifier))


class ExperienceStore:
    """Immutable, idempotent evaluation ledger with task/goal isolation and audit events."""
    def __init__(self, database: str):
        self.database=str(database)
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS value_assessments (
                key TEXT PRIMARY KEY, goal_id TEXT NOT NULL, task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL, ts REAL NOT NULL)''')
            AuditLog.install(db)

    def _connect(self):
        return sqlite3.connect(self.database,timeout=20)

    def save(self, *, goal_id: str, task_id: str, attempt: int, assessment: OutcomeAssessment) -> bool:
        if not goal_id or not task_id or attempt < 1: raise ValueError('Invalid outcome identity')
        key=f'{goal_id}:{task_id}:{attempt}'
        payload=json.dumps({'accepted':assessment.accepted,'reason':assessment.reason,
                            'operational_utility':assessment.operational_utility,
                            'information_gain':assessment.information_gain,
                            'evidence':asdict(assessment.evidence)},sort_keys=True,ensure_ascii=False,allow_nan=False)
        fingerprint=hashlib.sha256(payload.encode()).hexdigest()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT fingerprint FROM value_assessments WHERE key=?',(key,)).fetchone()
            if old:
                if old[0] != fingerprint: raise ValueError('Conflicting evaluation for completed attempt')
                return False
            db.execute('INSERT INTO value_assessments VALUES (?,?,?,?,?,?,?)',
                       (key,goal_id,task_id,attempt,payload,fingerprint,time.time()))
            AuditLog.append_in_transaction(db,actor='value_feedback',action='outcome_assessed',subject=task_id,
                                           details={'goal_id':goal_id,'attempt':attempt,'accepted':assessment.accepted,
                                                    'assessment_hash':fingerprint})
        return True

    def list(self,goal_id: str) -> list[dict]:
        with self._connect() as db:
            rows=db.execute('SELECT task_id,attempt,payload FROM value_assessments WHERE goal_id=? ORDER BY ts,key',(goal_id,)).fetchall()
        return [{'task_id':task_id,'attempt':attempt,**json.loads(payload)} for task_id,attempt,payload in rows]


class ValueGuidedPlanner:
    """Score bounded candidate rolling-plan proposals, not arbitrary tool execution.

    The candidate source may be an LLM, but its returned PlanProposal remains
    subject to AdaptiveController's DAG validator. Estimates are explicitly
    forecasts, not measured rewards.
    """
    def __init__(self, candidates, *, policy: ValuePolicy | None = None,
                 remaining_cost: float = 10.0, max_risk: float = 0.4):
        self.candidates=candidates
        self.policy=policy or ValuePolicy()
        self.remaining_cost=remaining_cost
        self.max_risk=max_risk

    async def propose(self, snapshot):
        import inspect
        from .adaptive import Action, PlanProposal
        items=self.candidates(snapshot)
        if inspect.isawaitable(items): items=await items
        items=list(items)
        by_id={}
        for option, proposal in items:
            if option.action not in ('explore','replan','ask_human'):
                raise ValueError('ValueGuidedPlanner candidates must be planning decisions')
            if not isinstance(proposal,PlanProposal) or proposal.action.value != option.action:
                raise ValueError('Action/proposal mismatch')
            if option.identifier in by_id: raise ValueError('Duplicate decision option')
            by_id[option.identifier]=proposal
        ranking=self.policy.rank((option for option,_ in items),
                                 remaining_cost=self.remaining_cost,max_risk=self.max_risk)
        if not ranking:
            return PlanProposal(Action.ASK_HUMAN,reason='no_feasible_value_guided_plan')
        return by_id[ranking[0].option.identifier]
