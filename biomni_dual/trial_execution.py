"""v0.6.7: isolated trial execution and separate pilot-learning evidence.

The trusted controller creates a durable RUNNING claim before entering Docker.
A claim left RUNNING after a crash is UNCERTAIN and must be reconciled by an
operator; this intentionally sacrifices availability to avoid duplicate effects.
Trial outcomes are NOT automatically converted into production repair outcomes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

from .active_experiments import ActiveExperimentStore, ExperimentState, TrialAssignment, TrialResult
from .failure_diagnosis import FailureRoute
from .reliability import AuditLog
from .sandbox import DockerSandbox


class TrialExecutionState(str, Enum):
    RUNNING = 'running'
    RECORDED = 'recorded'
    UNCERTAIN = 'uncertain'
    ABORTED = 'aborted'


@dataclass(frozen=True)
class TrialExecutionReceipt:
    experiment_id: str
    unit_id: str
    route: FailureRoute
    state: TrialExecutionState
    evidence_path: str | None = None
    evidence_sha256: str | None = None


@dataclass(frozen=True)
class PilotEstimate:
    experiment_id: str
    route: FailureRoute
    samples: int
    accepted: int
    posterior_mean: float
    mean_observed_cost: float | None
    source: str = 'isolated_pilot_not_production'


class TrialExecutionLedger:
    """Durable single-writer claim; intentionally no automatic expired-lease stealing."""
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS trial_execution_claims (
                experiment_id TEXT NOT NULL, unit_id TEXT NOT NULL,
                route TEXT NOT NULL, state TEXT NOT NULL,
                evidence_path TEXT, evidence_sha256 TEXT, created REAL NOT NULL,
                PRIMARY KEY (experiment_id,unit_id))''')
            AuditLog.install(db)

    def connect(self):
        return sqlite3.connect(self.db_path, timeout=20)

    def status(self, experiment_id: str, unit_id: str):
        with self.connect() as db:
            row = db.execute('SELECT route,state,evidence_path,evidence_sha256 FROM trial_execution_claims WHERE experiment_id=? AND unit_id=?',
                (experiment_id,unit_id)).fetchone()
        if row is None:
            return None
        return TrialExecutionReceipt(experiment_id,unit_id,FailureRoute(row[0]),TrialExecutionState(row[1]),row[2],row[3])

    def claim(self, assignment: TrialAssignment) -> TrialExecutionReceipt:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT route,state FROM trial_execution_claims WHERE experiment_id=? AND unit_id=?',
                (assignment.experiment_id,assignment.unit_id)).fetchone()
            if old:
                if old[0] != assignment.route.value:
                    raise ValueError('Conflicting trial claim route')
                raise RuntimeError('Trial already claimed; reconcile before any replay')
            db.execute('INSERT INTO trial_execution_claims VALUES (?,?,?,?,?,?,?)',
                (assignment.experiment_id,assignment.unit_id,assignment.route.value,TrialExecutionState.RUNNING.value,None,None,time.time()))
            AuditLog.append_in_transaction(db,actor='trial-executor',action='execution_claimed',
                subject=assignment.experiment_id,details={'unit_sha256':hashlib.sha256(assignment.unit_id.encode()).hexdigest(),
                'route':assignment.route.value})
        return self.status(assignment.experiment_id,assignment.unit_id)

    def _transition(self, assignment: TrialAssignment, state: TrialExecutionState, evidence_path=None, evidence_sha256=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT route,state FROM trial_execution_claims WHERE experiment_id=? AND unit_id=?',
                (assignment.experiment_id,assignment.unit_id)).fetchone()
            if not row or row[0] != assignment.route.value:
                raise ValueError('Missing or conflicting trial claim')
            if row[1] == state.value:
                return self.status(assignment.experiment_id,assignment.unit_id)
            if row[1] != TrialExecutionState.RUNNING.value:
                raise RuntimeError('Invalid execution transition')
            db.execute('UPDATE trial_execution_claims SET state=?,evidence_path=?,evidence_sha256=? WHERE experiment_id=? AND unit_id=?',
                (state.value,evidence_path,evidence_sha256,assignment.experiment_id,assignment.unit_id))
            AuditLog.append_in_transaction(db,actor='trial-executor',action='execution_'+state.value,
                subject=assignment.experiment_id,details={'unit_sha256':hashlib.sha256(assignment.unit_id.encode()).hexdigest(),
                'evidence_sha256':evidence_sha256})
        return self.status(assignment.experiment_id,assignment.unit_id)

    def uncertain(self, assignment: TrialAssignment):
        return self._transition(assignment,TrialExecutionState.UNCERTAIN)

    def recorded(self, assignment: TrialAssignment, evidence_path: str, evidence_sha256: str):
        return self._transition(assignment,TrialExecutionState.RECORDED,evidence_path,evidence_sha256)

    def reconcile_recorded(self, experiments: ActiveExperimentStore,
                           experiment_id: str, unit_id: str, *, operator: str):
        """Finish a crashed DB handoff only when the independent record exists."""
        if not operator or operator.startswith('agent:'):
            raise PermissionError('Independent operator required')
        current = self.status(experiment_id,unit_id)
        if not current or current.state not in (TrialExecutionState.RUNNING, TrialExecutionState.UNCERTAIN):
            raise ValueError('No unresolved claim')
        with experiments._connect() as db:
            row=db.execute('SELECT payload FROM active_results WHERE experiment_id=? AND unit_id=?',
                           (experiment_id,unit_id)).fetchone()
        if row is None:
            raise ValueError('No independently recorded outcome')
        result=json.loads(row[0])
        path=Path(result['evidence_path']).resolve(strict=True)
        if not path.is_relative_to(experiments.evidence_root) or not path.is_file():
            raise ValueError('Untrusted evidence location')
        if hashlib.sha256(path.read_bytes()).hexdigest() != result['evidence_sha256']:
            raise ValueError('Evidence digest mismatch')
        assignment=TrialAssignment(experiment_id,unit_id,current.route,experiments.get(experiment_id)[0].mode,0)
        if current.state == TrialExecutionState.UNCERTAIN:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE trial_execution_claims SET state=? WHERE experiment_id=? AND unit_id=?',
                           (TrialExecutionState.RUNNING.value,experiment_id,unit_id))
                AuditLog.append_in_transaction(db,actor=operator,action='verified_record_reconciliation',
                    subject=experiment_id,details={'unit_sha256':hashlib.sha256(unit_id.encode()).hexdigest()})
        return self.recorded(assignment,str(path),result['evidence_sha256'])

    def reconcile(self, experiment_id: str, unit_id: str, *, operator: str,
                  outcome: str = 'aborted'):
        if not operator or operator.startswith('agent:') or outcome != 'aborted':
            raise PermissionError('Human must independently reconcile uncertain trials; no automatic retry')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT state FROM trial_execution_claims WHERE experiment_id=? AND unit_id=?',
                (experiment_id,unit_id)).fetchone()
            if not old or old[0] not in (TrialExecutionState.UNCERTAIN.value,TrialExecutionState.RUNNING.value):
                raise ValueError('Only unresolved attempts can be reconciled')
            db.execute('UPDATE trial_execution_claims SET state=? WHERE experiment_id=? AND unit_id=?',
                       (TrialExecutionState.ABORTED.value,experiment_id,unit_id))
            AuditLog.append_in_transaction(db,actor=operator,action='execution_manually_aborted',subject=experiment_id,
                details={'unit_sha256':hashlib.sha256(unit_id.encode()).hexdigest()})
        return self.status(experiment_id,unit_id)


class TrialExecutor:
    """Runs fixed, previously approved and randomized trial candidates in Docker.

    The CLI/SDK may *prepare* candidate files prior to this boundary, but
    neither the agent nor the candidate controls trial result recording.
    Independent checker is a trusted function injected by the operator.
    """
    def __init__(self, *, experiments: ActiveExperimentStore, ledger: TrialExecutionLedger,
                 sandbox: DockerSandbox, candidates: Mapping[FailureRoute, Path | str],
                 verifier_id: str, independent_checker=None):
        self.experiments, self.ledger, self.sandbox = experiments, ledger, sandbox
        self.candidates = {route: Path(path).resolve(strict=True) for route,path in candidates.items()}
        # Candidate contents are pinned at approval/selection time. A later file
        # modification must not silently change the trial under test.
        self.pinned_digests = {route: DockerSandbox.digest_tree(path)
                               for route,path in self.candidates.items()}
        if not verifier_id or verifier_id not in experiments.trusted_verifiers:
            raise PermissionError('Independent verifier must be allowlisted')
        if independent_checker is None or not callable(independent_checker):
            raise ValueError('Explicit independent checker function is required')
        self.verifier_id, self.independent_checker = verifier_id, independent_checker
        for route, path in self.candidates.items():
            if route not in FailureRoute or not path.is_dir():
                raise ValueError('Invalid trial candidate directory')

    async def execute(self, experiment_id: str, unit_id: str, *, kind: str = 'skill') -> TrialExecutionReceipt:
        # Fail closed: no docker or quota usage before approved protocol.
        plan, state = self.experiments.get(experiment_id)
        if state != ExperimentState.APPROVED:
            raise PermissionError('Trial protocol is not approved/open')
        if kind not in ('skill','mcp'):
            raise ValueError('Unknown sandbox trial kind')
        assignment = self.experiments.assign(experiment_id,unit_id)
        candidate = self.candidates.get(assignment.route)
        if candidate is None:
            raise ValueError('No candidate artifact for assigned arm')
        # Execute only fixed artifacts, not dynamic agent-supplied shell commands.
        if DockerSandbox.digest_tree(candidate) != self.pinned_digests[assignment.route]:
            raise PermissionError('Approved candidate artifact changed; new approval required')
        self.ledger.claim(assignment)
        start=time.monotonic()
        try:
            validation=await self.sandbox.validate(candidate,kind=kind)
            if validation.digest != self.pinned_digests[assignment.route]:
                raise PermissionError('Sandbox validated unexpected candidate digest')
            # Checker returns a trustworthy external assessment, not Agent self-report.
            assessment = await self.independent_checker(assignment,validation)
            if type(assessment) is not bool:
                raise ValueError('Independent checker must return boolean')
            accepted = bool(validation.passed and assessment)
            elapsed=max(0.0,time.monotonic()-start)
            # The Docker limits are the actual resource boundary. Monetary cost is
            # unknown here; zero is an accounting placeholder, not claimed spend.
            cost=0.0
            evidence={
                'experiment_id':experiment_id,'unit_id':unit_id,
                'route':assignment.route.value,'protocol_version':plan.protocol_version,
                'verifier_id':self.verifier_id,'accepted':accepted,
                'sandbox_image':validation.image,'candidate_digest':validation.digest,
                'sandbox_passed':validation.passed,'container_id':validation.container_id,
                'elapsed_s':elapsed,'cost_measured':False,
                'replication_note':'Repeated fixture executions are not independent research samples',
            }
            root=self.experiments.evidence_root
            # Exclusive create prevents overwriting independent evidence.
            path=root/('trial-'+uuid.uuid4().hex+'.json')
            data=json.dumps(evidence,sort_keys=True,allow_nan=False).encode('utf-8')
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'wb') as f:
                f.write(data);f.flush();os.fsync(f.fileno())
            digest=hashlib.sha256(data).hexdigest()
            result=TrialResult(experiment_id,unit_id,assignment.route,accepted,cost,elapsed,
                self.verifier_id,str(path),digest,plan.protocol_version)
            self.experiments.record(result)
            return self.ledger.recorded(assignment,str(path),digest)
        except BaseException:
            # Crucial: never classify an infrastructure crash as experimental failure.
            self.ledger.uncertain(assignment)
            raise


class PilotLearningView:
    """Read-only experimental learning view, separate from production outcomes."""
    def __init__(self, experiments: ActiveExperimentStore):
        self.experiments = experiments

    def estimates(self, experiment_id: str) -> tuple[PilotEstimate,PilotEstimate]:
        report=self.experiments.report(experiment_id)
        def estimate(arm):
            return PilotEstimate(experiment_id,arm.route,arm.completed,arm.accepted,
                (arm.accepted+1)/(arm.completed+2),arm.mean_cost)
        return estimate(report.control),estimate(report.treatment)
