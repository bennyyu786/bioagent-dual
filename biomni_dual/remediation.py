"""v0.6.4: audited, restart-safe handoff from failure diagnosis to remediation.

No generated code is executed here. Model planners return *declarative* DAGPatch
proposals; DAGVersionManager validates and commits them. Capability builds are
sandboxed by AutonomousBuildCoordinator and require named human approval.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Awaitable
from uuid import uuid4

from .contracts import AgentRequest
from .dag import DAGPatch, DAGVersionManager
from .failure_diagnosis import Diagnosis, FailureRoute
from .models import Task, TaskStatus
from .reliability import AuditLog
from .scheduler import TaskCenter


class RemediationState(str, Enum):
    PROPOSED = 'proposed'
    WAITING_CAPABILITY = 'waiting_capability'
    WAITING_APPROVAL = 'waiting_approval'
    READY = 'ready'
    APPLIED = 'applied'
    BLOCKED = 'blocked'
    REJECTED = 'rejected'


@dataclass(frozen=True)
class Remediation:
    id: str
    task_id: str
    attempt: int
    route: FailureRoute
    state: RemediationState
    reason: str
    request_id: str | None = None
    recovery_task_id: str | None = None
    error: str | None = None


class RuntimeRepairPlanner:
    """Claude/Codex-compatible runtime producing proposals, never direct writes.

    Source task and remediation ID are injected by trusted controller; the
    returned recovery edge and new step must be validated independently.
    """
    def __init__(self, runtime, *, max_steps: int = 3):
        if not 1 <= max_steps <= 10:
            raise ValueError('Invalid patch horizon')
        self.runtime, self.max_steps = runtime, max_steps

    async def __call__(self, task: Task, diagnosis: Diagnosis, remediation_id: str) -> DAGPatch:
        from .planner import LLMPlanner
        prompt = (
            'Return ONLY JSON DAGPatch {"additions":[{"key":"repair",'
            '"prompt":"...","depends_on":[],"required_capabilities":[]}],'
            '"dependencies":[],"reason":"...",'
            f'"recovery":["{task.id}","repair"]}}. '
            'The added steps must DIAGNOSE or REPAIR the specified failure; do not '
            'claim tests passed, grant permissions, run commands, or change existing '
            'tasks. Repair steps must be independently validated after execution. '
            f'Task: {task.prompt[:1500]}\nFailure route: {diagnosis.route.value}; '
            f'reason: {diagnosis.reason[:300]}; evidence refs: {list(diagnosis.evidence_refs)[:10]}. '
            f'Max steps: {self.max_steps}.'
        )
        result = await self.runtime.run(AgentRequest(
            task_id=f'remediation-{remediation_id}', role='planner', prompt=prompt,
            allowed_capabilities=()))
        if not result.success:
            raise RuntimeError('Repair planner unavailable')
        patch = LLMPlanner.parse(result.text)
        if len(patch.additions) > self.max_steps:
            raise ValueError('Too many repair steps')
        return patch


class AutonomousRemediationOrchestrator:
    """Durable state machine. One remediation per task attempt.

    A single coordinator should call advance() for each job; transitions are
    SQLite-serialized. Interrupted external operations are intentionally BLOCKED.
    """
    def __init__(self, tasks: TaskCenter, *, database: str | Path,
                 repair_planner=None, capability_builder=None,
                 max_per_task: int = 3, adaptive_store=None, goal_id: str | None = None):
        if not 1 <= max_per_task <= 50:
            raise ValueError('Invalid remediation budget')
        self.tasks = tasks
        self.database = str(database)
        self.repair_planner = repair_planner
        self.capability_builder = capability_builder
        self.max_per_task = max_per_task
        self.adaptive_store, self.goal_id = adaptive_store, goal_id
        if (adaptive_store is None) != (goal_id is None):
            raise ValueError('Adaptive store and goal ID must be configured together')
        self.dag = DAGVersionManager(tasks)
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS remediation_jobs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                route TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
                request_id TEXT, recovery_task_id TEXT, error TEXT,
                created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(task_id,attempt))''')
            AuditLog.install(db)

    def _connect(self):
        return sqlite3.connect(self.database, timeout=20)

    def _row(self, row) -> Remediation:
        return Remediation(row[0],row[1],row[2],FailureRoute(row[3]),
                           RemediationState(row[4]),row[5],row[6],row[7],row[8])

    def get(self, id: str) -> Remediation:
        with self._connect() as db:
            row=db.execute('''SELECT id,task_id,attempt,route,state,reason,request_id,
                recovery_task_id,error FROM remediation_jobs WHERE id=?''',(id,)).fetchone()
        if row is None: raise KeyError('Unknown remediation')
        return self._row(row)

    def for_task(self, task_id: str) -> list[Remediation]:
        with self._connect() as db:
            rows=db.execute('''SELECT id,task_id,attempt,route,state,reason,request_id,
                recovery_task_id,error FROM remediation_jobs WHERE task_id=? ORDER BY attempt''',
                (task_id,)).fetchall()
        return [self._row(x) for x in rows]

    def _change(self, id: str, previous: RemediationState, new: RemediationState,
                *, request_id: str | None = None, recovery_id: str | None = None,
                error: str | None = None) -> Remediation:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count=db.execute('''UPDATE remediation_jobs SET state=?,
                request_id=COALESCE(?,request_id),
                recovery_task_id=COALESCE(?,recovery_task_id),error=?,updated=?
                WHERE id=? AND state=?''',
                (new.value,request_id,recovery_id,error[:500] if error else None,
                 time.time(),id,previous.value)).rowcount
            if count != 1: raise RuntimeError('Concurrent remediation transition')
            AuditLog.append_in_transaction(db,actor='remediation',action=new.value,
                subject=id,details={'previous':previous.value,'request_id':request_id,
                                    'recovery_id':recovery_id,'error':bool(error)})
        return self.get(id)

    def submit(self, diagnosis: Diagnosis) -> Remediation:
        task=self.tasks.get(diagnosis.task_id)
        if task is None or diagnosis.attempt != task.attempts:
            raise ValueError('Diagnosis does not match current task attempt')
        if self.adaptive_store is not None and task.id not in self.adaptive_store.task_ids(self.goal_id):
            raise ValueError('Remediation cannot target a foreign goal')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT id,route,reason FROM remediation_jobs WHERE task_id=? AND attempt=?',
                           (diagnosis.task_id,diagnosis.attempt)).fetchone()
            if old:
                if old[1:] != (diagnosis.route.value,diagnosis.reason):
                    raise ValueError('Conflicting diagnosis for task attempt')
                return self.get(old[0])
            count=db.execute('SELECT COUNT(*) FROM remediation_jobs WHERE task_id=?',
                             (task.id,)).fetchone()[0]
            state=(RemediationState.BLOCKED if count >= self.max_per_task or
                   diagnosis.route == FailureRoute.ASK_HUMAN or
                   task.status == TaskStatus.BLOCKED else RemediationState.PROPOSED)
            id=uuid4().hex; now=time.time()
            db.execute('''INSERT INTO remediation_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                       (id,task.id,diagnosis.attempt,diagnosis.route.value,state.value,
                        diagnosis.reason,None,None,None,now,now))
            AuditLog.append_in_transaction(db,actor='remediation',action='submitted',
                subject=id,details={'task_id':task.id,'attempt':diagnosis.attempt,
                                    'route':diagnosis.route.value,'state':state.value})
        return self.get(id)

    async def advance(self, id: str) -> Remediation:
        job=self.get(id)
        if job.state in (RemediationState.APPLIED,RemediationState.BLOCKED,
                         RemediationState.REJECTED,RemediationState.WAITING_APPROVAL):
            return job
        task=self.tasks.get(job.task_id)
        if task is None: raise KeyError('Task disappeared')
        if task.status == TaskStatus.BLOCKED:
            return self._change(id,job.state,RemediationState.BLOCKED,
                                error='uncertain external effects require reconciliation')
        if job.route == FailureRoute.RETRY_WORKER:
            if task.status == TaskStatus.PENDING:
                return self._change(id,job.state,RemediationState.APPLIED)
            return self._change(id,job.state,RemediationState.BLOCKED,
                                error='Retry requires a pending task')
        if job.route == FailureRoute.ASK_HUMAN:
            return self._change(id,job.state,RemediationState.BLOCKED)
        if job.route == FailureRoute.REPAIR_CAPABILITY:
            if self.capability_builder is None:
                return self._change(id,job.state,RemediationState.BLOCKED,error='Builder not configured')
            if job.state == RemediationState.PROPOSED:
                if not task.required_capabilities:
                    return self._change(id,job.state,RemediationState.BLOCKED,
                                        error='No declared capability to repair')
                capability_id=task.required_capabilities[0]
                kind='mcp' if capability_id.startswith('mcp.') else 'skill'
                request=self.capability_builder.request(
                    capability_id,kind,task.prompt,
                    request_key=f'remediation:{job.id}',task_id=task.id)
                job=self._change(id,job.state,RemediationState.WAITING_CAPABILITY,
                                 request_id=request['id'])
            record=self.capability_builder._get(job.request_id)
            if record['state'] == 'PUBLISHED':
                return await self._commit_recovery(job,task)
            if record['state'] in ('REJECTED','FAILED','BLOCKED'):
                return self._change(id,job.state,RemediationState.BLOCKED,
                                    error='Capability build rejected, failed or ambiguous')
            if record['state'] == 'AWAITING_APPROVAL':
                return self._change(id,job.state,RemediationState.WAITING_APPROVAL)
            # The builder's prepare operation is resumable only through its own
            # explicit reconciliation logic. Never auto-approve or auto-publish.
            if record['state'] in ('REQUESTED','REPAIR_PENDING'):
                try:
                    record=await self.capability_builder.prepare(job.request_id)
                except Exception:
                    return self._change(id,job.state,RemediationState.BLOCKED,
                                        error='Capability build interrupted; reconcile externally')
                if record['state'] == 'AWAITING_APPROVAL':
                    return self._change(id,job.state,RemediationState.WAITING_APPROVAL)
            return job
        # Repair / replan / independent result review: proposals only.
        if job.state != RemediationState.PROPOSED:
            return job
        if task.status != TaskStatus.FAILED:
            # A prior DAG transaction may have succeeded just before a crash.
            # Reconcile the durable recovery edge before deciding to block.
            return await self._commit_recovery(job,task)
        if self.repair_planner is None:
            return self._change(id,job.state,RemediationState.BLOCKED,
                                error='No independent repair planner configured')
        diagnosis=Diagnosis(job.route,job.reason,False,job.task_id,job.attempt)
        try:
            patch=self.repair_planner(task,diagnosis,id)
            if inspect.isawaitable(patch): patch=await patch
            if not isinstance(patch,DAGPatch) or not patch.additions or patch.recovery is None:
                raise ValueError('Repair must include a new recovery step and recovery edge')
            if patch.recovery[0] != task.id or patch.recovery[1] not in {x['key'] for x in patch.additions}:
                raise ValueError('Recovery must target its own task and newly proposed step')
            if job.route == FailureRoute.REVIEW_RESULT and not any('validat' in x['prompt'].lower() or
                        'review' in x['prompt'].lower() for x in patch.additions):
                raise ValueError('Scientific result requires independent review step')
            return await self._commit_recovery(job,task,patch)
        except Exception as exc:
            # A planner error is not grounds to retry dangerous execution.
            return self._change(id,job.state,RemediationState.BLOCKED,
                                error=f'Proposal/commit rejected: {type(exc).__name__}')

    async def _commit_recovery(self, job: Remediation, task: Task,
                               patch: DAGPatch | None = None) -> Remediation:
        # Check for crash between DAG commit and remediation state update.
        for _,event,details in self.tasks.events(task.id):
            if event == 'replanned':
                current=self.tasks.get(task.id)
                if current and details in current.depends_on:
                    if self.adaptive_store is not None and details not in self.adaptive_store.task_ids(self.goal_id):
                        # Crash after DAG transaction, before goal membership was saved.
                        # Validate the new recovery node rather than creating a duplicate.
                        self.adaptive_store.bind_tasks(self.goal_id,[details],self.tasks)
                    return self._change(job.id,job.state,RemediationState.APPLIED,recovery_id=details)
        if task.status != TaskStatus.FAILED:
            return self._change(job.id,job.state,RemediationState.BLOCKED,
                                error='Task state changed before recovery commit')
        if patch is None:
            if not task.required_capabilities:
                return self._change(job.id,job.state,RemediationState.BLOCKED,
                                    error='No ability to verify')
            patch=DAGPatch(additions=({
                'key':'verify_repaired_capability',
                'prompt':'Independently verify restored capability against original task inputs and output contract',
                'required_capabilities':[task.required_capabilities[0]],
                'depends_on':[],
            },),recovery=(task.id,'verify_repaired_capability'),reason=f'remediation:{job.id}')
        if self.adaptive_store is not None:
            bound=self.adaptive_store.task_ids(self.goal_id)
            keys=set()
            for entry in patch.additions:
                k=entry['key']
                if k in keys: raise ValueError('Duplicate step key')
                if any(dep not in bound and dep not in keys for dep in entry.get('depends_on',())):
                    raise ValueError('Cross-goal remediation prerequisite')
                keys.add(k)
            for target, dep in patch.dependencies:
                if target not in bound and target not in keys:
                    raise ValueError('Cross-goal task mutation')
                if dep not in bound and dep not in keys:
                    raise ValueError('Cross-goal task prerequisite')
        if job.route == FailureRoute.REPAIR_EXECUTION:
            # Model-generated changes to code/environment are always staged
            # behind TaskCenter's explicit approval gate.
            from dataclasses import replace
            patch=replace(patch, additions=tuple(
                {**entry, 'requires_approval':True} for entry in patch.additions))
        mapping=self.dag.commit(patch)
        if self.adaptive_store is not None:
            self.adaptive_store.bind_tasks(self.goal_id,list(mapping.values()),self.tasks)
        recovery_id=mapping[patch.recovery[1]]
        return self._change(job.id,job.state,RemediationState.APPLIED,recovery_id=recovery_id)

    async def reconcile(self, id: str) -> Remediation:
        """Explicitly poll a published build or reconcile an interrupted DAG commit."""
        job=self.get(id)
        if job.state == RemediationState.WAITING_APPROVAL and self.capability_builder:
            record=self.capability_builder._get(job.request_id)
            if record['state'] == 'PUBLISHED':
                return await self._commit_recovery(job,self.tasks.get(job.task_id))
            if record['state'] in ('FAILED','REJECTED','BLOCKED'):
                return self._change(id,job.state,RemediationState.BLOCKED,
                                    error='Capability build did not publish')
        return job

    def recover(self) -> int:
        """Fail closed on interrupted in-flight operations; never replay remote work."""
        count=0
        with self._connect() as db:
            rows=db.execute('SELECT id,state FROM remediation_jobs WHERE state=?',
                            (RemediationState.READY.value,)).fetchall()
        for id,state in rows:
            self._change(id,RemediationState(state),RemediationState.BLOCKED,
                         error='Interrupted work requires reconciliation')
            count+=1
        return count
