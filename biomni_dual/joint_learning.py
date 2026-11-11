"""Joint task-capability state and evidence-based remediation strategy learning.

A strategy recommendation is NOT execution authorization. Only independently
verified post-remediation outcomes contribute to learning. The module stores
immutable, idempotent facts and never edits the DAG or grants tool permissions.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .failure_diagnosis import Diagnosis, FailureRoute
from .models import TaskStatus
from .reliability import AuditLog


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class RepairContext:
    """Exact domain/context matching prevents unrelated failures polluting policies."""
    task_kind: str
    error_reason: str
    capability_id: str = 'none'

    def __post_init__(self):
        for field in (self.task_kind, self.error_reason, self.capability_id):
            if not isinstance(field, str) or not field.strip() or len(field) > 160:
                raise ValueError('Invalid repair context')

    @property
    def key(self):
        return _json(asdict(self))


@dataclass(frozen=True)
class RepairOutcome:
    """Independent post-repair evaluation, not an agent's self-assessment."""
    remediation_id: str
    context: RepairContext
    route: FailureRoute
    accepted: bool
    verifier_id: str
    evidence_sha256: str
    measured_cost: float
    measured_duration_s: float
    evidence_reference: str

    def __post_init__(self):
        if not self.remediation_id or len(self.remediation_id) > 240:
            raise ValueError('Invalid remediation identity')
        if not isinstance(self.route, FailureRoute) or not isinstance(self.accepted, bool):
            raise ValueError('Invalid verified outcome')
        if (not self.verifier_id.strip() or self.verifier_id.startswith('agent:') or
                len(self.verifier_id) > 160):
            raise ValueError('Independent verifier identity required')
        if not isinstance(self.evidence_sha256, str) or len(self.evidence_sha256) != 64 or any(
                x not in '0123456789abcdef' for x in self.evidence_sha256):
            raise ValueError('Valid evidence SHA256 required')
        if not self.evidence_reference.strip() or len(self.evidence_reference) > 500:
            raise ValueError('Evidence reference required')
        for value in (self.measured_cost, self.measured_duration_s):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('Invalid observed resource cost')


@dataclass(frozen=True)
class RepairEstimate:
    route: FailureRoute
    samples: int
    posterior_success: float
    mean_cost: float | None
    mean_duration_s: float | None


@dataclass(frozen=True)
class StrategyAdvice:
    recommended: FailureRoute
    original: FailureRoute
    reason: str
    evidence: tuple[RepairEstimate, ...]
    requires_approval: bool = False


@dataclass(frozen=True)
class JointSnapshot:
    task_id: str
    status: str
    attempts: int
    required_capabilities: tuple[str, ...]
    capability_health: tuple[tuple[str, bool | None], ...]
    remediation_states: tuple[str, ...]
    known_outcomes: int
    graph_version: int | None = None


class JointExperienceStore:
    """SQLite immutable outcome facts and audit chain in the same transaction."""
    def __init__(self, database: str | Path):
        self.database = str(database)
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS joint_repair_outcomes (
                remediation_id TEXT PRIMARY KEY, context TEXT NOT NULL,
                route TEXT NOT NULL, accepted INTEGER NOT NULL,
                payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                created REAL NOT NULL)''')
            AuditLog.install(db)

    def _connect(self):
        return sqlite3.connect(self.database, timeout=20)

    def record(self, outcome: RepairOutcome) -> bool:
        payload = _json({**asdict(outcome), 'route': outcome.route.value})
        if len(payload) > 8000:
            raise ValueError('Oversized outcome')
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT fingerprint FROM joint_repair_outcomes WHERE remediation_id=?',
                                  (outcome.remediation_id,)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise ValueError('Conflicting verified outcome')
                return False
            db.execute('''INSERT INTO joint_repair_outcomes VALUES (?,?,?,?,?,?,?)''',
                       (outcome.remediation_id, outcome.context.key, outcome.route.value,
                        int(outcome.accepted), payload, fingerprint, time.time()))
            AuditLog.append_in_transaction(db, actor='joint_learning', action='outcome_verified',
                subject=outcome.remediation_id,
                details={'context_sha256': _sha(asdict(outcome.context)),
                         'route': outcome.route.value, 'accepted': outcome.accepted,
                         'evidence_sha256': outcome.evidence_sha256})
        return True

    def outcomes(self, context: RepairContext, route: FailureRoute | None = None):
        with self._connect() as db:
            if route is None:
                rows = db.execute('SELECT payload FROM joint_repair_outcomes WHERE context=? ORDER BY remediation_id',
                                  (context.key,)).fetchall()
            else:
                rows = db.execute('SELECT payload FROM joint_repair_outcomes WHERE context=? AND route=? ORDER BY remediation_id',
                                  (context.key, route.value)).fetchall()
        return [json.loads(x[0]) for x in rows]

    def estimate(self, context: RepairContext, route: FailureRoute) -> RepairEstimate:
        rows = self.outcomes(context, route)
        n = len(rows)
        if not n:
            return RepairEstimate(route, 0, .5, None, None)
        return RepairEstimate(route, n, (sum(x['accepted'] for x in rows)+1)/(n+2),
                              sum(x['measured_cost'] for x in rows)/n,
                              sum(x['measured_duration_s'] for x in rows)/n)

    def count(self):
        with self._connect() as db:
            return db.execute('SELECT COUNT(*) FROM joint_repair_outcomes').fetchone()[0]


class RepairStrategyPolicy:
    """Conservative strategy selection, only among explicitly approved routes.

    This is observational history, not causal inference. No choice is applied
    until the caller explicitly dispatches it through the normal orchestrator.
    """
    ALTERNATIVES = {
        FailureRoute.REPAIR_CAPABILITY: (FailureRoute.REPAIR_CAPABILITY, FailureRoute.REPLAN_TASK),
        FailureRoute.REPAIR_EXECUTION: (FailureRoute.REPAIR_EXECUTION, FailureRoute.REPLAN_TASK),
    }

    def __init__(self, store: JointExperienceStore, *, min_samples: int = 4,
                 improvement_margin: float = .15, max_mean_cost: float | None = None):
        if min_samples < 1 or min_samples > 10000:
            raise ValueError('Invalid sample threshold')
        if not 0 <= improvement_margin <= 1:
            raise ValueError('Invalid margin')
        if max_mean_cost is not None and (not math.isfinite(max_mean_cost) or max_mean_cost < 0):
            raise ValueError('Invalid cost gate')
        self.store, self.min_samples, self.improvement_margin = store, min_samples, improvement_margin
        self.max_mean_cost = max_mean_cost

    def advise(self, diagnosis: Diagnosis, context: RepairContext, *,
               side_effect_uncertain: bool = False,
               approved_routes: Iterable[FailureRoute] = ()) -> StrategyAdvice:
        if side_effect_uncertain or diagnosis.route == FailureRoute.ASK_HUMAN:
            return StrategyAdvice(FailureRoute.ASK_HUMAN, diagnosis.route, 'human_reconciliation_required', ())
        candidates = self.ALTERNATIVES.get(diagnosis.route)
        if not candidates:
            return StrategyAdvice(diagnosis.route, diagnosis.route, 'deterministic_route_preserved', ())
        stats = tuple(self.store.estimate(context, route) for route in candidates)
        baseline = stats[0]
        allowed = set(approved_routes)
        viable = [s for s in stats[1:] if s.route in allowed and s.samples >= self.min_samples and
                  (self.max_mean_cost is None or s.mean_cost <= self.max_mean_cost)]
        if baseline.samples < self.min_samples or not viable:
            return StrategyAdvice(diagnosis.route, diagnosis.route, 'insufficient_or_unauthorized_evidence', stats)
        best = max(viable, key=lambda s: (s.posterior_success, -(s.mean_cost or 0)))
        if best.posterior_success < baseline.posterior_success + self.improvement_margin:
            return StrategyAdvice(diagnosis.route, diagnosis.route, 'no_measurable_improvement', stats)
        return StrategyAdvice(best.route, diagnosis.route, 'validated_history_supports_alternative', stats,
                              requires_approval=True)


class JointRemediationController:
    """Bridge existing task/remediation stores to observed repair learning.

    Proposals require explicit approval before route substitution. Outcomes
    can only be recorded for *applied* remediations after the original task is
    completed and the trusted verifier supplies artifact-backed evidence.
    """
    def __init__(self, tasks, orchestrator, experiences: JointExperienceStore, *,
                 capabilities=None, policy: RepairStrategyPolicy | None = None,
                 evidence_root: str | Path | None = None,
                 trusted_verifiers: tuple[str, ...] = ()):
        self.tasks, self.orchestrator, self.experiences = tasks, orchestrator, experiences
        self.capabilities = capabilities
        self.policy = policy or RepairStrategyPolicy(experiences)
        self.evidence_root = Path(evidence_root).resolve() if evidence_root is not None else None
        self.trusted_verifiers = frozenset(trusted_verifiers)

    def snapshot(self, task_id: str) -> JointSnapshot:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError('Unknown task')
        jobs = self.orchestrator.for_task(task_id)
        health = tuple((cid, None if self.capabilities is None or self.capabilities.get(cid) is None
                        else bool(self.capabilities.get(cid).healthy))
                       for cid in task.required_capabilities)
        versions = self.orchestrator.dag.versions() if hasattr(self.orchestrator, 'dag') else []
        return JointSnapshot(task.id, task.status.value, task.attempts,
                             tuple(task.required_capabilities), health,
                             tuple(j.state.value for j in jobs),
                             sum(1 for j in jobs if self._has_outcome(j.id)),
                             versions[-1]['version'] if versions else None)

    def _has_outcome(self, remediation_id: str) -> bool:
        with self.experiences._connect() as db:
            return db.execute('SELECT 1 FROM joint_repair_outcomes WHERE remediation_id=?',
                              (remediation_id,)).fetchone() is not None

    def recommend(self, diagnosis: Diagnosis, *, task_kind: str,
                  approved_routes: Iterable[FailureRoute] = (),
                  side_effect_uncertain: bool = False) -> StrategyAdvice:
        task = self.tasks.get(diagnosis.task_id)
        if task is None:
            raise KeyError('Unknown task')
        context = RepairContext(task_kind, diagnosis.reason,
                  task.required_capabilities[0] if task.required_capabilities else 'none')
        return self.policy.advise(diagnosis, context, approved_routes=approved_routes,
                                  side_effect_uncertain=side_effect_uncertain or task.status == TaskStatus.BLOCKED)

    def submit(self, diagnosis: Diagnosis, *, task_kind: str,
               approved_alternative: FailureRoute | None = None):
        """Explicit approval of a recommended alternative is required; never implied."""
        task = self.tasks.get(diagnosis.task_id)
        if task is None:
            raise KeyError('Unknown task')
        if approved_alternative is not None:
            advice = self.recommend(diagnosis, task_kind=task_kind,
                                    approved_routes=(approved_alternative,))
            if advice.recommended != approved_alternative or not advice.requires_approval:
                raise PermissionError('Alternative is not evidenced and authorized')
            diagnosis = Diagnosis(approved_alternative, diagnosis.reason, False,
                                  diagnosis.task_id, diagnosis.attempt, diagnosis.evidence_refs)
        return self.orchestrator.submit(diagnosis)

    def record_verified(self, outcome: RepairOutcome):
        job = self.orchestrator.get(outcome.remediation_id)
        task = self.tasks.get(job.task_id)
        history = self.tasks.events(job.task_id) if task is not None else []
        # DAG recovery resets attempts. Match the exact recovery edge belonging
        # to this remediation, then require a subsequent start and terminal event.
        revised = False
        recovery_id = getattr(job, 'recovery_task_id', None)
        if recovery_id:
            for idx, (_, event, detail) in enumerate(history):
                if event == 'replanned' and detail == recovery_id:
                    later = [name for _, name, _ in history[idx + 1:]]
                    revised = 'started' in later and any(
                        e in ('completed', 'failed', 'blocked') for e in later[later.index('started') + 1:])
                    break
        if job.state.value != 'applied' or task is None or (task.attempts <= job.attempt and not revised):
            raise ValueError('Post-repair execution has not occurred')
        if outcome.accepted and task.status != TaskStatus.COMPLETED:
            raise ValueError('Accepted outcome must belong to a completed task')
        if not outcome.accepted and task.status not in (TaskStatus.FAILED, TaskStatus.BLOCKED):
            raise ValueError('Rejected outcome requires a terminal failed or blocked task')
        if job.route != outcome.route or outcome.context.error_reason != job.reason:
            raise ValueError('Outcome is inconsistent with remediation route or reason')
        if outcome.context.capability_id not in (task.required_capabilities or ['none']):
            raise ValueError('Outcome capability context mismatch')
        if self.evidence_root is None:
            raise ValueError('Independent evidence root required')
        if outcome.verifier_id not in self.trusted_verifiers:
            raise PermissionError('Verifier is not on the trusted allowlist')
        evidence = Path(outcome.evidence_reference).resolve(strict=True)
        if not evidence.is_file() or not evidence.is_relative_to(self.evidence_root):
            raise ValueError('Evidence must be a file inside the trusted validation directory')
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != outcome.evidence_sha256:
            raise ValueError('Independent evidence digest mismatch')
        return self.experiences.record(outcome)
