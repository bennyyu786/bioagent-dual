"""v0.6.6: controlled, auditable comparisons of safe remediation strategies.

Experiments never run tools, modify DAGs, install MCP/Skills or change the
production remediation policy. The caller executes approved *shadow* or
isolated-sandbox trials independently and supplies independently verified
artifacts. Results are randomized pilot evidence, not causal proof.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from .failure_diagnosis import Diagnosis, FailureRoute
from .joint_learning import RepairContext
from .reliability import AuditLog


def _encode(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


class ExperimentState(str, Enum):
    PROPOSED = 'proposed'
    APPROVED = 'approved'
    CLOSED = 'closed'
    REJECTED = 'rejected'


class TrialMode(str, Enum):
    SHADOW = 'shadow'
    SANDBOX = 'sandbox'


SAFE_ROUTES = frozenset((FailureRoute.REPAIR_CAPABILITY,
                         FailureRoute.REPLAN_TASK,
                         FailureRoute.REPAIR_EXECUTION))


@dataclass(frozen=True)
class ExperimentPlan:
    id: str
    context: RepairContext
    control: FailureRoute
    treatment: FailureRoute
    mode: TrialMode = TrialMode.SHADOW
    max_units: int = 20
    max_total_cost: float = 100.0
    max_cost_per_trial: float = 5.0
    min_samples_per_arm: int = 4
    purpose: str = 'Compare verified repair strategies'
    protocol_version: str = '1'
    side_effects_allowed: bool = False
    sensitive_data_allowed: bool = False

    def __post_init__(self):
        if not self.id or len(self.id) > 120 or not self.purpose or len(self.purpose) > 400:
            raise ValueError('Invalid experiment identity/purpose')
        if self.control not in SAFE_ROUTES or self.treatment not in SAFE_ROUTES or self.control == self.treatment:
            raise ValueError('Only two distinct eligible repair strategies may be compared')
        if not isinstance(self.mode, TrialMode):
            raise ValueError('Invalid trial mode')
        if self.side_effects_allowed or self.sensitive_data_allowed:
            raise PermissionError('Experimental trials cannot use external side effects or sensitive data')
        if not (4 <= self.max_units <= 10000 and 2 <= self.min_samples_per_arm <= self.max_units // 2):
            raise ValueError('Invalid sample budget')
        for value in (self.max_total_cost, self.max_cost_per_trial):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid cost limits')
        if self.max_cost_per_trial > self.max_total_cost:
            raise ValueError('Trial exceeds experiment budget')
        if not self.protocol_version or len(self.protocol_version) > 100:
            raise ValueError('Invalid protocol')


@dataclass(frozen=True)
class TrialAssignment:
    experiment_id: str
    unit_id: str
    route: FailureRoute
    mode: TrialMode
    allocation_index: int


@dataclass(frozen=True)
class TrialResult:
    experiment_id: str
    unit_id: str
    route: FailureRoute
    accepted: bool
    measured_cost: float
    measured_duration_s: float
    verifier_id: str
    evidence_path: str
    evidence_sha256: str
    protocol_version: str = '1'

    def __post_init__(self):
        if not self.experiment_id or not self.unit_id or not isinstance(self.route, FailureRoute):
            raise ValueError('Invalid result identity')
        if not isinstance(self.accepted, bool):
            raise ValueError('Result outcome must be boolean')
        if not self.verifier_id or self.verifier_id.startswith('agent:'):
            raise ValueError('Independent verifier required')
        if not isinstance(self.evidence_sha256, str) or len(self.evidence_sha256) != 64 or any(
                c not in '0123456789abcdef' for c in self.evidence_sha256):
            raise ValueError('Invalid evidence digest')
        if not self.evidence_path or not self.protocol_version:
            raise ValueError('Evidence/protocol required')
        for x in (self.measured_cost, self.measured_duration_s):
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
                raise ValueError('Invalid measured cost or duration')


@dataclass(frozen=True)
class ArmSummary:
    route: FailureRoute
    assigned: int
    completed: int
    accepted: int
    success_rate: float | None
    wilson_low: float | None
    wilson_high: float | None
    mean_cost: float | None


@dataclass(frozen=True)
class ComparisonReport:
    experiment_id: str
    state: ExperimentState
    control: ArmSummary
    treatment: ArmSummary
    observed_difference: float | None
    decision: str
    explanation: str
    protocol_version: str
    total_reserved: float
    total_observed: float
    # Even a positive sandbox pilot requires fresh external authorization.
    production_rollout_authorized: bool = False


def _wilson(successes: int, total: int, z: float = 1.96):
    if not total:
        return None, None
    rate = successes / total
    denom = 1 + z*z/total
    center = (rate + z*z/(2*total))/denom
    half = z*math.sqrt(rate*(1-rate)/total + z*z/(4*total*total))/denom
    return max(0, center-half), min(1, center+half)


class ActiveExperimentStore:
    """Fixed protocols, durable randomized assignment, conservative reporting.

    Experiment execution is intentionally outside this class. The trusted
    controller enforces actual sandbox isolation; this DB cannot do so.
    """
    def __init__(self, database: str | Path, *, evidence_root: str | Path,
                 trusted_verifiers: Iterable[str]):
        self.database = str(database)
        self.evidence_root = Path(evidence_root).resolve(strict=True)
        if not self.evidence_root.is_dir():
            raise ValueError('Evidence root must be directory')
        self.trusted_verifiers = frozenset(trusted_verifiers)
        if not self.trusted_verifiers or any(not x or x.startswith('agent:') for x in self.trusted_verifiers):
            raise ValueError('Trusted external verifier allowlist required')
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS active_experiments (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, plan TEXT NOT NULL,
                state TEXT NOT NULL, approver TEXT, created REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS active_assignments (
                experiment_id TEXT NOT NULL, unit_id TEXT NOT NULL, route TEXT NOT NULL,
                idx INTEGER NOT NULL, reserved REAL NOT NULL,
                PRIMARY KEY(experiment_id,unit_id), UNIQUE(experiment_id,idx))''')
            db.execute('''CREATE TABLE IF NOT EXISTS active_results (
                experiment_id TEXT NOT NULL, unit_id TEXT NOT NULL, payload TEXT NOT NULL,
                fingerprint TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(experiment_id,unit_id))''')
            AuditLog.install(db)

    def _connect(self):
        return sqlite3.connect(self.database, timeout=20)

    @staticmethod
    def _payload(plan: ExperimentPlan) -> str:
        return _encode({**asdict(plan), 'control': plan.control.value,
                        'treatment': plan.treatment.value, 'mode': plan.mode.value})

    def get(self, id: str) -> tuple[ExperimentPlan, ExperimentState]:
        with self._connect() as db:
            row = db.execute('SELECT plan,state FROM active_experiments WHERE id=?', (id,)).fetchone()
        if row is None:
            raise KeyError('Unknown experiment')
        raw = json.loads(row[0]); raw['context'] = RepairContext(**raw['context'])
        raw['control'] = FailureRoute(raw['control']); raw['treatment'] = FailureRoute(raw['treatment'])
        raw['mode'] = TrialMode(raw['mode'])
        return ExperimentPlan(**raw), ExperimentState(row[1])

    def propose(self, plan: ExperimentPlan) -> ExperimentState:
        payload = self._payload(plan)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint,state FROM active_experiments WHERE id=?', (plan.id,)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError('Conflicting experiment id')
                return ExperimentState(old[1])
            db.execute('INSERT INTO active_experiments VALUES (?,?,?,?,?,?)',
                       (plan.id, digest, payload, ExperimentState.PROPOSED.value, None, time.time()))
            AuditLog.append_in_transaction(db, actor='experiment', action='proposed', subject=plan.id,
                details={'protocol_sha256': digest, 'mode':plan.mode.value})
        return ExperimentState.PROPOSED

    def transition(self, id: str, *, approve: bool, approver: str) -> ExperimentState:
        if not approver or approver.startswith('agent:') or len(approver)>120:
            raise ValueError('Independent human approver required')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,approver FROM active_experiments WHERE id=?', (id,)).fetchone()
            if row is None: raise KeyError('Unknown experiment')
            new = ExperimentState.APPROVED if approve else ExperimentState.REJECTED
            if row[0] == new.value and row[1] == approver:
                return new
            if row[0] != ExperimentState.PROPOSED.value:
                raise ValueError('Experiment is no longer awaiting approval')
            db.execute('UPDATE active_experiments SET state=?,approver=? WHERE id=?',
                       (new.value, approver, id))
            AuditLog.append_in_transaction(db, actor='experiment', action=new.value, subject=id,
                                           details={'approver':approver})
        return new

    def close(self, id: str, *, actor: str='operator') -> ExperimentState:
        if not actor or actor.startswith('agent:'):
            raise ValueError('Independent operator required')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM active_experiments WHERE id=?', (id,)).fetchone()
            if row is None: raise KeyError('Unknown experiment')
            if row[0] == ExperimentState.CLOSED.value:
                return ExperimentState.CLOSED
            if row[0] != ExperimentState.APPROVED.value:
                raise ValueError('Only approved experiment can close')
            db.execute('UPDATE active_experiments SET state=? WHERE id=?', (ExperimentState.CLOSED.value, id))
            AuditLog.append_in_transaction(db, actor='experiment', action='closed', subject=id,
                                           details={'operator':actor})
        return ExperimentState.CLOSED

    def assign(self, experiment_id: str, unit_id: str) -> TrialAssignment:
        if not unit_id or len(unit_id) > 180:
            raise ValueError('Invalid unit identity')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT plan,state FROM active_experiments WHERE id=?', (experiment_id,)).fetchone()
            if row is None: raise KeyError('Unknown experiment')
            plan_raw = json.loads(row[0]); state = ExperimentState(row[1])
            old = db.execute('SELECT route,idx FROM active_assignments WHERE experiment_id=? AND unit_id=?',
                             (experiment_id,unit_id)).fetchone()
            if old:
                return TrialAssignment(experiment_id,unit_id,FailureRoute(old[0]),TrialMode(plan_raw['mode']),old[1])
            if state != ExperimentState.APPROVED:
                raise PermissionError('Only approved open experiments may assign units')
            existing = db.execute('SELECT route,reserved FROM active_assignments WHERE experiment_id=?',
                                  (experiment_id,)).fetchall()
            if len(existing) >= plan_raw['max_units']:
                raise RuntimeError('Experiment unit budget exhausted')
            if sum(x[1] for x in existing) + plan_raw['max_cost_per_trial'] > plan_raw['max_total_cost'] + 1e-9:
                raise RuntimeError('Experiment cost budget exhausted')
            # Block randomization: random among arms with the fewest allocations.
            counts = {plan_raw['control']:0,plan_raw['treatment']:0}
            for route,_ in existing: counts[route] += 1
            least = min(counts.values())
            available = [r for r,n in counts.items() if n == least]
            route = available[secrets.randbelow(len(available))]
            idx = len(existing) + 1
            db.execute('INSERT INTO active_assignments VALUES (?,?,?,?,?)',
                       (experiment_id,unit_id,route,idx,plan_raw['max_cost_per_trial']))
            AuditLog.append_in_transaction(db, actor='experiment', action='assigned', subject=experiment_id,
                                           details={'unit_digest':hashlib.sha256(unit_id.encode()).hexdigest(),
                                                    'arm':route,'index':idx})
        return TrialAssignment(experiment_id,unit_id,FailureRoute(route),TrialMode(plan_raw['mode']),idx)

    def record(self, result: TrialResult) -> bool:
        if result.verifier_id not in self.trusted_verifiers:
            raise PermissionError('Verifier not on independent allowlist')
        path = Path(result.evidence_path).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(self.evidence_root):
            raise ValueError('Evidence must be inside trusted root')
        if hashlib.sha256(path.read_bytes()).hexdigest() != result.evidence_sha256:
            raise ValueError('Evidence digest mismatch')
        payload = _encode({**asdict(result),'route':result.route.value})
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT plan,state FROM active_experiments WHERE id=?', (result.experiment_id,)).fetchone()
            if row is None: raise KeyError('Unknown experiment')
            plan = json.loads(row[0])
            old = db.execute('SELECT fingerprint FROM active_results WHERE experiment_id=? AND unit_id=?',
                             (result.experiment_id,result.unit_id)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError('Conflicting trial result')
                return False
            if row[1] != ExperimentState.APPROVED.value:
                raise PermissionError('Only approved open experiments accept results')
            assignment = db.execute('SELECT route,reserved FROM active_assignments WHERE experiment_id=? AND unit_id=?',
                                    (result.experiment_id,result.unit_id)).fetchone()
            if not assignment or assignment[0] != result.route.value:
                raise ValueError('Result does not match assigned strategy')
            if plan['protocol_version'] != result.protocol_version:
                raise ValueError('Protocol mismatch')
            if result.measured_cost > assignment[1] + 1e-9:
                raise ValueError('Per-trial cost exceeded reserved budget')
            # Verify that evidence is specifically bound to assignment / protocol.
            # A plain SHA256 of an arbitrary passing file would be insufficient.
            try:
                evidence = json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, UnicodeError) as exc:
                raise ValueError('Structured independent evidence required') from exc
            claims = {'experiment_id':result.experiment_id, 'unit_id':result.unit_id,
                      'route':result.route.value,'protocol_version':result.protocol_version,
                      'verifier_id':result.verifier_id,'accepted':result.accepted}
            if not isinstance(evidence, dict) or any(evidence.get(k) != v for k,v in claims.items()):
                raise ValueError('Evidence claims do not match randomized assignment')
            db.execute('INSERT INTO active_results VALUES (?,?,?,?,?)',
                       (result.experiment_id,result.unit_id,payload,digest,time.time()))
            AuditLog.append_in_transaction(db, actor='experiment', action='independently_verified',
                                           subject=result.experiment_id,
                                           details={'unit_digest':hashlib.sha256(result.unit_id.encode()).hexdigest(),
                                                    'route':result.route.value,'accepted':result.accepted,
                                                    'evidence_sha256':result.evidence_sha256})
        return True

    def report(self, experiment_id: str) -> ComparisonReport:
        plan,state = self.get(experiment_id)
        with self._connect() as db:
            assignments = db.execute('SELECT route,reserved FROM active_assignments WHERE experiment_id=?',
                                     (experiment_id,)).fetchall()
            results = [json.loads(x[0]) for x in db.execute('SELECT payload FROM active_results WHERE experiment_id=?',
                                                             (experiment_id,)).fetchall()]
        def summarize(route):
            rows = [r for r in results if r['route'] == route.value]
            n = len(rows); wins = sum(r['accepted'] for r in rows)
            lo,hi = _wilson(wins,n)
            return ArmSummary(route, sum(a[0] == route.value for a in assignments), n, wins,
                              wins/n if n else None, lo,hi,
                              sum(r['measured_cost'] for r in rows)/n if n else None)
        control = summarize(plan.control); treatment = summarize(plan.treatment)
        observed = (treatment.success_rate-control.success_rate if control.completed and treatment.completed else None)
        if control.completed < plan.min_samples_per_arm or treatment.completed < plan.min_samples_per_arm:
            decision,reason='insufficient_evidence','Both arms require independent verified samples'
        elif treatment.wilson_low > control.wilson_high:
            decision,reason='treatment_promising','Pilot interval separation; replication and approval required'
        elif control.wilson_low > treatment.wilson_high:
            decision,reason='control_promising','Pilot interval separation; replication and approval required'
        else:
            decision,reason='inconclusive','Confidence intervals overlap; no strategy switch justified'
        return ComparisonReport(experiment_id,state,control,treatment,observed,decision,reason,
                                plan.protocol_version,sum(x[1] for x in assignments),
                                sum(x['measured_cost'] for x in results))


class ActiveExperimentCoordinator:
    """Converts known remediation ambiguity into a safe proposal, never executes it."""
    def __init__(self, store: ActiveExperimentStore):
        self.store = store

    def propose_for_failure(self, diagnosis: Diagnosis, context: RepairContext, *,
                            experiment_id: str, max_units: int = 20,
                            max_total_cost: float = 100.0, max_cost_per_trial: float = 5.0,
                            mode: TrialMode = TrialMode.SHADOW) -> ExperimentPlan:
        if diagnosis.route not in (FailureRoute.REPAIR_CAPABILITY, FailureRoute.REPAIR_EXECUTION):
            raise PermissionError('Only eligible remediations support safe pilot comparison')
        if context.error_reason != diagnosis.reason:
            raise ValueError('Diagnosis context mismatch')
        plan = ExperimentPlan(experiment_id,context,diagnosis.route,FailureRoute.REPLAN_TASK,
                              mode=mode,max_units=max_units,max_total_cost=max_total_cost,
                              max_cost_per_trial=max_cost_per_trial)
        self.store.propose(plan)
        return plan


@dataclass(frozen=True)
class ExperimentOpportunity:
    worth_testing: bool
    reason: str
    baseline_samples: int
    alternative_samples: int
    priority_score: float
    estimated_max_spend: float


class ExperimentPriorityPolicy:
    """Select whether to propose an experiment using production repair history.

    This is an *exploration heuristic*, not an estimated causal treatment
    effect. History only influences whether to propose a pilot, never whether
    its actions are authorized. Unknown contexts have maximal uncertainty.
    """
    def __init__(self, experiences, *, min_spend_for_experiment: float = 0.0,
                 sufficient_samples: int = 30):
        if min_spend_for_experiment < 0 or not math.isfinite(min_spend_for_experiment):
            raise ValueError('Invalid cost policy')
        if sufficient_samples < 2:
            raise ValueError('Invalid evidence threshold')
        self.experiences = experiences
        self.min_spend_for_experiment = min_spend_for_experiment
        self.sufficient_samples = sufficient_samples

    def assess(self, plan: ExperimentPlan, *, remaining_budget: float,
               side_effect_uncertain: bool = False, permissions_ok: bool = True):
        if remaining_budget < 0 or not math.isfinite(remaining_budget):
            raise ValueError('Invalid available budget')
        baseline = self.experiences.estimate(plan.context, plan.control)
        alternative = self.experiences.estimate(plan.context, plan.treatment)
        spend = min(plan.max_total_cost, remaining_budget)
        required = plan.min_samples_per_arm * 2 * plan.max_cost_per_trial
        if side_effect_uncertain or not permissions_ok:
            return ExperimentOpportunity(False, 'unsafe_or_unauthorized', baseline.samples,
                                         alternative.samples, 0.0, spend)
        if spend < max(required, self.min_spend_for_experiment) or plan.max_units < plan.min_samples_per_arm * 2:
            return ExperimentOpportunity(False, 'insufficient_experiment_budget', baseline.samples,
                                         alternative.samples, 0.0, spend)
        # Shrink uncertainty only in the same explicit task/capability context.
        uncertainty = 1 / math.sqrt(baseline.samples + 1) + 1 / math.sqrt(alternative.samples + 1)
        if baseline.samples >= self.sufficient_samples and alternative.samples >= self.sufficient_samples:
            return ExperimentOpportunity(False, 'history_sufficient_for_now', baseline.samples,
                                         alternative.samples, 0.0, spend)
        priority = uncertainty / (1 + required / max(remaining_budget, 1e-9))
        return ExperimentOpportunity(True, 'safe_pilot_may_reduce_uncertainty',baseline.samples,
                                     alternative.samples,priority,spend)
