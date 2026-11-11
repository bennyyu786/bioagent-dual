"""Evidence-aware fault classification, bounded remediation, and audited routing.

A diagnosis is a proposal, not permission to execute arbitrary tools or modify DAGs.
Uncertain external side effects always require human reconciliation.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .reliability import AuditLog, ErrorKind


class FailureRoute(str, Enum):
    RETRY_WORKER = 'retry_worker'
    REPAIR_EXECUTION = 'repair_execution'
    REPAIR_CAPABILITY = 'repair_capability'
    REPLAN_TASK = 'replan_task'
    REVIEW_RESULT = 'review_result'
    ASK_HUMAN = 'ask_human'


class UncertainSideEffectError(RuntimeError):
    """Worker reports an operation whose external effects cannot be reconciled."""


class FailureOrigin(str, Enum):
    EXECUTION = 'execution'
    VALIDATION = 'validation'
    SCIENTIFIC_QUALITY = 'scientific_quality'
    CAPABILITY = 'capability'
    UNCERTAIN = 'uncertain'


@dataclass(frozen=True)
class FailureSignal:
    task_id: str
    attempt: int
    origin: FailureOrigin
    error_code: str
    error_kind: ErrorKind = ErrorKind.UNKNOWN
    retryable: bool = False
    side_effect_uncertain: bool = False
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.task_id or self.attempt < 1 or not self.error_code:
            raise ValueError('Invalid failure signal')
        if any(not x or len(x) > 300 for x in self.evidence_refs):
            raise ValueError('Invalid evidence reference')


@dataclass(frozen=True)
class Diagnosis:
    route: FailureRoute
    reason: str
    retryable: bool
    task_id: str
    attempt: int
    evidence_refs: tuple[str, ...] = ()


class FailureDiagnosisEngine:
    """Deterministic fail-closed routing with global per-task remediation budget."""
    def __init__(self, database: str | Path, *, max_remediations: int = 3):
        if not 0 <= max_remediations <= 100:
            raise ValueError('Invalid remediation budget')
        self.database = str(database)
        self.max_remediations = max_remediations
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS failure_diagnoses (
                task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                fingerprint TEXT NOT NULL, route TEXT NOT NULL,
                reason TEXT NOT NULL, retryable INTEGER NOT NULL,
                evidence TEXT NOT NULL, ts REAL NOT NULL,
                PRIMARY KEY(task_id, attempt))''')
            AuditLog.install(db)

    def _connect(self):
        return sqlite3.connect(self.database, timeout=20)

    @staticmethod
    def classify(signal: FailureSignal) -> tuple[FailureRoute, str, bool]:
        if signal.side_effect_uncertain or signal.origin == FailureOrigin.UNCERTAIN:
            return FailureRoute.ASK_HUMAN, 'uncertain_external_effect', False
        if signal.origin in (FailureOrigin.VALIDATION, FailureOrigin.SCIENTIFIC_QUALITY):
            return FailureRoute.REVIEW_RESULT, 'independent_validation_failed', False
        if signal.origin == FailureOrigin.CAPABILITY or signal.error_kind == ErrorKind.DEPENDENCY:
            return FailureRoute.REPAIR_CAPABILITY, 'capability_unavailable_or_broken', False
        if signal.error_kind in (ErrorKind.AUTH, ErrorKind.PERMISSION):
            return FailureRoute.ASK_HUMAN, 'authorization_required', False
        if signal.error_kind == ErrorKind.INVALID_INPUT:
            return FailureRoute.REPLAN_TASK, 'input_or_task_specification_invalid', False
        if signal.error_kind == ErrorKind.CANCELLED:
            return FailureRoute.ASK_HUMAN, 'cancelled_needs_review', False
        if signal.retryable and signal.error_kind in (ErrorKind.TIMEOUT, ErrorKind.TRANSIENT,
                                                      ErrorKind.RATE_LIMIT, ErrorKind.RESOURCE):
            return FailureRoute.RETRY_WORKER, 'temporary_execution_problem', True
        if signal.error_kind == ErrorKind.UNKNOWN:
            return FailureRoute.ASK_HUMAN, 'unknown_root_cause', False
        return FailureRoute.REPAIR_EXECUTION, 'execution_fix_needed', False

    def diagnose(self, signal: FailureSignal) -> Diagnosis:
        raw = json.dumps(asdict(signal), sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('''SELECT fingerprint,route,reason,retryable,evidence FROM failure_diagnoses
                                WHERE task_id=? AND attempt=?''', (signal.task_id, signal.attempt)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ValueError('Conflicting failure diagnosis for attempt')
                return Diagnosis(FailureRoute(old[1]), old[2], bool(old[3]), signal.task_id,
                                 signal.attempt, tuple(json.loads(old[4])))
            count = db.execute('SELECT COUNT(*) FROM failure_diagnoses WHERE task_id=?',
                               (signal.task_id,)).fetchone()[0]
            route, reason, retryable = self.classify(signal)
            if count >= self.max_remediations:
                route, reason, retryable = FailureRoute.ASK_HUMAN, 'remediation_budget_exhausted', False
            db.execute('INSERT INTO failure_diagnoses VALUES (?,?,?,?,?,?,?,?)',
                       (signal.task_id, signal.attempt, fingerprint, route.value, reason,
                        int(retryable), json.dumps(signal.evidence_refs), time.time()))
            AuditLog.append_in_transaction(db, actor='failure_diagnosis', action='routed',
                                           subject=signal.task_id,
                                           details={'attempt':signal.attempt,'route':route.value,
                                                    'reason':reason,'evidence_count':len(signal.evidence_refs)})
        return Diagnosis(route, reason, retryable, signal.task_id, signal.attempt, signal.evidence_refs)

    def list(self, task_id: str) -> list[Diagnosis]:
        with self._connect() as db:
            rows=db.execute('SELECT attempt,route,reason,retryable,evidence FROM failure_diagnoses '
                            'WHERE task_id=? ORDER BY attempt',(task_id,)).fetchall()
        return [Diagnosis(FailureRoute(route),reason,bool(retry),task_id,attempt,tuple(json.loads(refs)))
                for attempt,route,reason,retry,refs in rows]
