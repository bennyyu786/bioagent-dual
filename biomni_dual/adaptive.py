"""Bounded, observable, task/capability co-evolution controller (v0.6.0).

The controller chooses one locally justified action per tick. It never grants
an LLM direct database, marketplace install or production execution rights.
Planner proposals are validated by DAGVersionManager; builds remain behind
existing approval and sandbox interfaces. No claim of global optimality.
"""
from __future__ import annotations
import asyncio
import hashlib
import inspect
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .dag import DAGPatch, DAGVersionManager
from .contracts import AgentRequest
from .models import Task, TaskStatus
from .reliability import AuditLog, classify_error
from .scheduler import TaskCenter
from .task_requirements import TaskRequirements
from .task_fit import TaskFitCoordinator


class Action(str, Enum):
    EXPLORE = 'explore'
    EXECUTE = 'execute'
    BUILD = 'build_capability'
    REPLAN = 'replan'
    VALIDATE = 'validate'
    ASK_HUMAN = 'ask_human'
    FINISH = 'finish'


@dataclass(frozen=True)
class Observation:
    key: str
    task_id: str
    kind: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanProposal:
    action: Action
    patch: DAGPatch | None = None
    reason: str = ''


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    task_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorldSnapshot:
    goal_id: str
    goal: str
    cycle: int
    tasks: tuple[Task, ...]
    observations: tuple[Observation, ...]
    budget_left: int


class RollingPlanner(Protocol):
    async def propose(self, snapshot: WorldSnapshot) -> PlanProposal: ...


class ResultValidator(Protocol):
    async def validate(self, task: Task, output: str) -> tuple[bool, str]: ...


class Worker(Protocol):
    async def run(self, task: Task, capability_ids: tuple[str, ...]) -> str: ...


class NonEmptyValidator:
    """Only syntactic baseline; domain-specific scientific validation is required."""
    async def validate(self, task: Task, output: str) -> tuple[bool, str]:
        ok = isinstance(output, str) and bool(output.strip())
        return ok, 'nonempty_output' if ok else 'missing_output'


class CallbackPlanner:
    """Adapter for deterministic planners or LLM-proposal functions (no DB writes)."""
    def __init__(self, callback):
        self.callback = callback

    async def propose(self, snapshot: WorldSnapshot) -> PlanProposal:
        value = self.callback(snapshot)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, PlanProposal):
            raise ValueError('Planner must return a typed PlanProposal')
        return value


class AgentRollingPlanner:
    """Uses existing Claude/Codex AgentRuntime, returning proposals only.

    It does not call LLMPlanner.submit_goal/replan, because those commit the DAG
    themselves. All writes must go through AdaptiveController validation.
    """
    def __init__(self, runtime, *, max_steps_per_patch: int = 5):
        self.runtime=runtime
        if not 1 <= max_steps_per_patch <= 20:
            raise ValueError('Invalid planning horizon')
        self.horizon=max_steps_per_patch

    async def propose(self, snapshot: WorldSnapshot) -> PlanProposal:
        state={
            'goal':snapshot.goal,'cycle':snapshot.cycle,
            'tasks':[{'id':t.id,'prompt':t.prompt,'status':t.status.value,
                      'error':(t.error or '')[:350], 'depends_on':t.depends_on}
                     for t in snapshot.tasks[:60]],
            'observations':[{'kind':o.kind,'summary':o.summary[:500]}
                            for o in snapshot.observations[-15:]],
            'remaining_cycles':snapshot.budget_left,
        }
        prompt=('You plan a previously unknown long-horizon scientific goal. '
                'Propose ONLY the next small, verifiable set of steps, not a full fixed plan. '
                'Output strict JSON {"action":"explore|replan|ask_human|finish",'
                '"reason":"...","patch":{"additions":[{"key":"...",'
                '"prompt":"...","depends_on":[],"required_capabilities":[]}],'
                '"dependencies":[],"reason":"...","recovery":["failed_task_id","new_step_key"]}}. '
                'For ask_human or finish, omit patch. Prefer explore when the evidence is '
                'insufficient; never propose modifying completed/running tasks. '
                f'Max new steps: {self.horizon}. No tools or code execution. State: '
                +json.dumps(state,ensure_ascii=False)[:18000])
        response=await self.runtime.run(AgentRequest(
            task_id='rolling-'+snapshot.goal_id+'-'+str(snapshot.cycle),role='planner',
            prompt=prompt,allowed_capabilities=()))
        if not response.success:
            raise ValueError('Planner failed: '+response.text[:500])
        try:
            raw=response.text.strip()
            if raw.startswith('```'):
                from .planner import LLMPlanner
                import re
                raw=re.sub(r'^```(?:json)?\s*|\s*```$', '', raw).strip()
            obj=json.loads(raw)
            if not isinstance(obj,dict) or set(obj)-{'action','reason','patch'}:
                raise ValueError('Unknown planner fields')
            action=Action(obj['action'])
            patch=None
            if action in (Action.EXPLORE,Action.REPLAN):
                from .planner import LLMPlanner
                patch=LLMPlanner.parse(json.dumps(obj['patch']))
                if len(patch.additions)>self.horizon:
                    raise ValueError('Exceeded rolling planning horizon')
            elif 'patch' in obj:
                raise ValueError('Unexpected patch for non-planning action')
            return PlanProposal(action,patch,str(obj.get('reason',''))[:2000])
        except (ValueError,TypeError,KeyError) as exc:
            raise ValueError('Invalid rolling planner proposal') from exc


class AdaptiveStore:
    """SQLite world-state / observation ledger, with idempotent evidence writes."""
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS adaptive_goals (
                id TEXT PRIMARY KEY, description TEXT NOT NULL, cycle INTEGER NOT NULL DEFAULT 0,
                max_cycles INTEGER NOT NULL, created REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS adaptive_observations (
                key TEXT PRIMARY KEY, goal_id TEXT NOT NULL, task_id TEXT NOT NULL,
                kind TEXT NOT NULL, summary TEXT NOT NULL, evidence TEXT NOT NULL, ts REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS adaptive_goal_tasks (
                goal_id TEXT NOT NULL, task_id TEXT NOT NULL,
                PRIMARY KEY(goal_id,task_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS adaptive_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL,
                cycle INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
                task_id TEXT, ts REAL NOT NULL, UNIQUE(goal_id,cycle))''')
            AuditLog.install(db)

    def connect(self):
        return sqlite3.connect(self.path, timeout=20)

    def create_goal(self, goal: str, *, goal_id: str | None = None, max_cycles: int = 30) -> str:
        if not goal.strip() or not 1 <= max_cycles <= 10000:
            raise ValueError('Invalid goal or cycle budget')
        gid = goal_id or uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT description,max_cycles FROM adaptive_goals WHERE id=?',(gid,)).fetchone()
            if old:
                if old != (goal,max_cycles):
                    raise ValueError('Goal ID already used with different parameters')
                return gid
            db.execute('INSERT INTO adaptive_goals VALUES (?,?,?,?,?)',(gid,goal,0,max_cycles,time.time()))
            AuditLog.append_in_transaction(db,actor='adaptive',action='goal_created',subject=gid,
                                           details={'max_cycles':max_cycles})
        return gid

    def bind_tasks(self, goal_id: str, task_ids: list[str], center: TaskCenter) -> None:
        """Register existing task DAG roots explicitly; prevent cross-goal contamination."""
        self.get_goal(goal_id)
        with self.connect() as db:
            bound={r[0] for r in db.execute('SELECT task_id FROM adaptive_goal_tasks WHERE goal_id=?',(goal_id,))}
        prospective=bound | set(task_ids)
        for tid in task_ids:
            task=center.get(tid)
            if task is None or not set(task.depends_on).issubset(prospective):
                raise ValueError('Goal task missing or depends on an unbound task')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for tid in task_ids:
                db.execute('INSERT OR IGNORE INTO adaptive_goal_tasks VALUES (?,?)',(goal_id,tid))

    def task_ids(self, goal_id: str) -> set[str]:
        with self.connect() as db:
            return {r[0] for r in db.execute(
                'SELECT task_id FROM adaptive_goal_tasks WHERE goal_id=?',(goal_id,))}

    def get_goal(self, goal_id: str) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT description,cycle,max_cycles FROM adaptive_goals WHERE id=?',(goal_id,)).fetchone()
        if row is None: raise KeyError('Unknown goal')
        return {'goal':row[0],'cycle':row[1],'max_cycles':row[2]}

    def remember(self, goal_id: str, observation: Observation) -> bool:
        if not observation.key or not observation.kind or len(observation.summary)>2000:
            raise ValueError('Invalid observation')
        payload = json.dumps(observation.evidence,sort_keys=True,ensure_ascii=False)
        if len(payload)>16000: raise ValueError('Evidence too large')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT goal_id,task_id,kind,summary,evidence FROM adaptive_observations WHERE key=?',(observation.key,)).fetchone()
            new=(goal_id,observation.task_id,observation.kind,observation.summary,payload)
            if old:
                if old != new: raise ValueError('Observation key reused with different content')
                return False
            db.execute('INSERT INTO adaptive_observations VALUES (?,?,?,?,?,?,?)',
                       (observation.key,*new,time.time()))
            AuditLog.append_in_transaction(db,actor='adaptive',action='observed',subject=observation.task_id,
                                           details={'key_sha256':hashlib.sha256(observation.key.encode()).hexdigest(),
                                                    'kind':observation.kind,'goal_id':goal_id})
        return True

    def observations(self, goal_id: str) -> tuple[Observation,...]:
        with self.connect() as db:
            rows=db.execute('SELECT key,task_id,kind,summary,evidence FROM adaptive_observations WHERE goal_id=? ORDER BY ts,key',(goal_id,)).fetchall()
        return tuple(Observation(a,b,c,d,json.loads(e)) for a,b,c,d,e in rows)

    def record_decision(self, goal_id: str, decision: Decision) -> int:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT cycle,max_cycles FROM adaptive_goals WHERE id=?',(goal_id,)).fetchone()
            if not row: raise KeyError('Unknown goal')
            cycle,limit=row
            if cycle >= limit: raise RuntimeError('Goal cycle budget exhausted')
            db.execute('INSERT INTO adaptive_decisions(goal_id,cycle,action,reason,task_id,ts) VALUES (?,?,?,?,?,?)',
                       (goal_id,cycle+1,decision.action.value,decision.reason[:2000],decision.task_id,time.time()))
            db.execute('UPDATE adaptive_goals SET cycle=? WHERE id=?',(cycle+1,goal_id))
            AuditLog.append_in_transaction(db,actor='adaptive',action='decision',subject=goal_id,
                                           details={'cycle':cycle+1,'type':decision.action.value,'task_id':decision.task_id})
            return cycle+1

    def decisions(self,goal_id: str) -> list[dict]:
        with self.connect() as db:
            rows=db.execute('SELECT cycle,action,reason,task_id FROM adaptive_decisions WHERE goal_id=? ORDER BY cycle',(goal_id,)).fetchall()
        return [dict(cycle=a,action=b,reason=c,task_id=d) for a,b,c,d in rows]


class AdaptiveController:
    """Single-coordinator rolling controller; do not run simultaneous ticks for a goal.

    Crash after an external Worker side effect is intentionally NOT automatically
    replayed: TaskCenter/SessionStore reconciliation remains the safety boundary.
    """
    def __init__(self, *, goal_id: str, store: AdaptiveStore, tasks: TaskCenter,
                 planner: RollingPlanner, worker: Worker, validator: ResultValidator,
                 fit: TaskFitCoordinator | None = None, gap_kinds: dict[str,str] | None = None,
                 capability_feedback=None, max_plans: int = 10,
                 quality_policy=None, outcome_evaluator=None, experience_store=None,
                 online_learning_store=None, learning_context=None, failure_diagnosis=None,
                 remediation_orchestrator=None):
        self.goal_id,self.store,self.tasks=goal_id,store,tasks
        self.planner,self.worker,self.validator=planner,worker,validator
        self.fit,self.gap_kinds=fit,gap_kinds or {}
        self.capability_feedback=capability_feedback
        self.max_plans=max_plans
        self.failure_diagnosis=failure_diagnosis
        self.remediation_orchestrator=remediation_orchestrator
        self.quality_policy,self.outcome_evaluator,self.experience_store=quality_policy,outcome_evaluator,experience_store
        self.online_learning_store,self.learning_context=online_learning_store,learning_context
        if (online_learning_store is None) != (learning_context is None):
            raise ValueError("Online learning store and context resolver must be configured together")
        if online_learning_store is not None and quality_policy is None:
            raise ValueError("Online learning requires independent quality assessment")
        if (quality_policy is None) != (outcome_evaluator is None):
            raise ValueError("Quality policy and outcome evaluator must be configured together")
        self.dag=DAGVersionManager(tasks)
        self._lock=asyncio.Lock()

    def snapshot(self) -> WorldSnapshot:
        g=self.store.get_goal(self.goal_id)
        ids=self.store.task_ids(self.goal_id)
        return WorldSnapshot(self.goal_id,g['goal'],g['cycle'],tuple(t for t in self.tasks.list() if t.id in ids),
                             self.store.observations(self.goal_id),g['max_cycles']-g['cycle'])

    async def _plan(self, snapshot: WorldSnapshot) -> Decision:
        if sum(1 for x in self.store.decisions(self.goal_id) if x['action'] in (Action.REPLAN.value,Action.EXPLORE.value)) >= self.max_plans:
            return Decision(Action.ASK_HUMAN,'planning_budget_exhausted')
        proposal=await self.planner.propose(snapshot)
        if proposal.action not in (Action.EXPLORE,Action.REPLAN,Action.ASK_HUMAN,Action.FINISH):
            raise ValueError('Planner may only propose explore/replan/ask_human/finish')
        if proposal.action in (Action.REPLAN,Action.EXPLORE):
            if proposal.patch is None:
                raise ValueError('Exploration/replanning requires a DAG patch')
            bound=self.store.task_ids(self.goal_id)
            declared=set()
            for entry in proposal.patch.additions:
                if not isinstance(entry.get('key'),str) or entry['key'] in declared:
                    raise ValueError('Invalid/duplicate proposed step')
                if any(d not in bound and d not in declared for d in entry.get('depends_on',())):
                    raise ValueError('Cross-goal or forward task dependency')
                declared.add(entry['key'])
            for tid,dep in proposal.patch.dependencies:
                if tid not in bound and tid not in declared:
                    raise ValueError('Cross-goal DAG modification')
                if dep not in bound and dep not in declared:
                    raise ValueError('Cross-goal DAG dependency')
            if proposal.patch.recovery is not None:
                failed, newkey = proposal.patch.recovery
                if failed not in bound or newkey not in declared:
                    raise ValueError('Recovery cannot target a foreign task or unknown step')
            # atomic graph validation and commit; membership attaches to same goal
            assigned=self.dag.commit(proposal.patch)
            self.store.bind_tasks(self.goal_id,list(assigned.values()),self.tasks)
        elif proposal.patch is not None:
            raise ValueError('Unexpected DAG patch')
        return Decision(proposal.action,proposal.reason or 'planner_proposal')

    async def tick(self) -> Decision:
        async with self._lock:
            snapshot=self.snapshot()
            if snapshot.budget_left <= 0:
                return Decision(Action.ASK_HUMAN,'cycle_budget_exhausted')
            # Recover diagnosis -> remediation handoff after coordinator crash.
            # Only inspect failures belonging to this goal; completed/blocked
            # tasks are never automatically replayed.
            if self.remediation_orchestrator is not None and self.failure_diagnosis is not None:
                from .remediation import RemediationState
                for failed in snapshot.tasks:
                    if failed.status != TaskStatus.FAILED:
                        continue
                    diagnoses=self.failure_diagnosis.list(failed.id)
                    current=next((d for d in diagnoses if d.attempt==failed.attempts),None)
                    if current is None:
                        continue
                    job=self.remediation_orchestrator.submit(current)
                    if job.state in (RemediationState.PROPOSED,RemediationState.WAITING_CAPABILITY):
                        job=await self.remediation_orchestrator.advance(job.id)
                    elif job.state==RemediationState.WAITING_APPROVAL:
                        job=await self.remediation_orchestrator.reconcile(job.id)
                    if job.state in (RemediationState.PROPOSED,RemediationState.WAITING_CAPABILITY,
                                     RemediationState.WAITING_APPROVAL):
                        decision=Decision(Action.ASK_HUMAN,'remediation_pending_external_work',
                                          failed.id,{'remediation_id':job.id,'state':job.state.value})
                        self.store.record_decision(self.goal_id,decision)
                        return decision
                    if job.state==RemediationState.APPLIED and self.tasks.get(failed.id).status==TaskStatus.PENDING:
                        decision=Decision(Action.REPLAN,'remediation_reconciled',failed.id,
                                          {'remediation_id':job.id})
                        self.store.record_decision(self.goal_id,decision)
                        return decision
            if any(t.status in (TaskStatus.RUNNING,TaskStatus.BLOCKED) for t in snapshot.tasks):
                decision=Decision(Action.ASK_HUMAN,'active_or_uncertain_tasks_require_reconciliation')
            elif [t for t in self.tasks.ready() if t.id in self.store.task_ids(self.goal_id)]:
                task=next(t for t in self.tasks.ready() if t.id in self.store.task_ids(self.goal_id))
                selected=tuple(task.required_capabilities)
                if self.fit and task.required_capabilities:
                    req=TaskRequirements.from_task(task)
                    fit=self.fit.evaluate(req)
                    if not fit.sufficient:
                        if fit.gaps:
                            # Exactly the uncovered needs are requested; request keys are idempotent.
                            response=self.fit.request_gaps(req,kinds=self.gap_kinds)
                            decision=Decision(Action.BUILD,'missing_capability',task.id,
                                              {'gaps':list(fit.gaps),'requests':[r['id'] for r in response['requests']]})
                        else:
                            decision=Decision(Action.ASK_HUMAN,'budget_or_combination_blocker',task.id)
                        self.store.record_decision(self.goal_id,decision)
                        return decision
                    selected=fit.selected
                claimed=self.tasks.claim(task.id)
                start=time.monotonic()
                try:
                    output=await self.worker.run(claimed,selected)
                    if not isinstance(output,str):
                        raise TypeError('Worker must return a string')
                    valid,reason=await self.validator.validate(claimed,output)
                    if not isinstance(valid,bool) or not isinstance(reason,str):
                        raise TypeError('Validator must return (bool,str)')
                    assessment=None
                    if valid and self.quality_policy is not None:
                        # The domain evaluator must provide real check outputs and
                        # evidence references; we never synthesize quality scores.
                        from .value_feedback import QualityEvidence
                        evidence=await self.outcome_evaluator.evaluate(claimed,output)
                        if not isinstance(evidence,QualityEvidence):
                            raise TypeError('Outcome evaluator must return QualityEvidence')
                        assessment=self.quality_policy.evaluate(evidence)
                        if self.experience_store is not None:
                            self.experience_store.save(goal_id=self.goal_id,task_id=claimed.id,
                                                       attempt=claimed.attempts,assessment=assessment)
                        if self.online_learning_store is not None:
                            from .online_learning import LearningContext
                            context=self.learning_context(claimed,selected)
                            if not isinstance(context,LearningContext):
                                raise TypeError("Learning context resolver must return LearningContext")
                            self.online_learning_store.observe(
                                key=f"{self.goal_id}:{claimed.id}:{claimed.attempts}",
                                context=context,assessment=assessment,
                                elapsed_ms=(time.monotonic()-start)*1000)
                        valid=assessment.accepted
                        reason=assessment.reason
                    self.store.remember(self.goal_id,Observation(
                        key=f'validation:{claimed.id}:{claimed.attempts}',task_id=claimed.id,
                        kind='validation',summary=reason[:2000],evidence={'accepted':valid,
                            'quality_evaluated':assessment is not None,
                            'operational_utility':assessment.operational_utility if assessment else None,
                            'measured_information_gain':assessment.information_gain if assessment else None}))
                    if valid:
                        self.tasks.finish(claimed.id,result=output)
                        decision=Decision(Action.EXECUTE,'executed_and_validated',claimed.id)
                    else:
                        self.tasks.finish(claimed.id,error='validation_failed: '+reason[:500],retryable=False)
                        extra={}
                        if self.failure_diagnosis is not None:
                            from .failure_diagnosis import FailureSignal,FailureOrigin
                            signal=FailureSignal(claimed.id,claimed.attempts,
                                FailureOrigin.SCIENTIFIC_QUALITY if assessment is not None else FailureOrigin.VALIDATION,
                                reason[:500],evidence_refs=tuple(assessment.evidence.evidence_ids) if assessment else ())
                            diagnosis=self.failure_diagnosis.diagnose(signal)
                            extra={'route':diagnosis.route.value,'reason':diagnosis.reason}
                        decision=Decision(Action.VALIDATE,'result_validation_failed',claimed.id,extra)
                except Exception as exc:
                    info=classify_error(exc)
                    retryable=info.retryable
                    extra={'error_kind':info.kind.value}
                    uncertain=False
                    if self.failure_diagnosis is not None:
                        from .failure_diagnosis import (FailureSignal,FailureOrigin,
                                                        FailureRoute,UncertainSideEffectError)
                        uncertain=isinstance(exc,UncertainSideEffectError)
                        diagnosis=self.failure_diagnosis.diagnose(FailureSignal(
                            claimed.id,claimed.attempts,
                            FailureOrigin.UNCERTAIN if uncertain else FailureOrigin.EXECUTION,
                            info.code,info.kind,info.retryable,uncertain))
                        retryable=diagnosis.retryable and diagnosis.route==FailureRoute.RETRY_WORKER
                        extra.update({'route':diagnosis.route.value,'reason':diagnosis.reason})
                    if uncertain:
                        self.tasks.block_running(claimed.id,reason='uncertain_external_effect')
                    else:
                        self.tasks.finish(claimed.id,error=f'{info.kind.value}/{info.code}: {info.message}',retryable=retryable)
                    decision=Decision(Action.ASK_HUMAN if uncertain else Action.EXECUTE,
                                      'worker_failed',claimed.id,extra)
                # The deterministic failure classifier proposes a route; the
                # remediation orchestrator owns the durable, bounded handoff.
                # Never run this path after an unclassified or successful result.
                if (self.remediation_orchestrator is not None and self.failure_diagnosis is not None
                        and 'route' in decision.evidence):
                    diagnosed=self.failure_diagnosis.list(claimed.id)
                    current=next((d for d in diagnosed if d.attempt==claimed.attempts),None)
                    if current is not None:
                        job=self.remediation_orchestrator.submit(current)
                        job=await self.remediation_orchestrator.advance(job.id)
                        decision=Decision(decision.action,decision.reason,claimed.id,
                            {**decision.evidence,'remediation_id':job.id,
                             'remediation_state':job.state.value})
                if self.capability_feedback and selected:
                    self.capability_feedback.observe(claimed.id,[self.fit.registry.get(i) for i in selected if self.fit.registry.get(i)],
                                                     success=self.tasks.get(claimed.id).status == TaskStatus.COMPLETED,elapsed_ms=(time.monotonic()-start)*1000)
            elif snapshot.tasks and all(t.status == TaskStatus.COMPLETED for t in snapshot.tasks):
                with self.store.connect() as db:
                    recent=db.execute('''SELECT COUNT(*) FROM adaptive_observations
                      WHERE goal_id=? AND kind='environment_change' AND ts > COALESCE(
                      (SELECT MAX(ts) FROM adaptive_decisions WHERE goal_id=?),0)''',
                      (self.goal_id,self.goal_id)).fetchone()[0]
                decision=await self._plan(snapshot) if recent else Decision(Action.FINISH,'all_known_tasks_validated')
            elif any(t.status == TaskStatus.WAITING_APPROVAL for t in snapshot.tasks):
                decision=Decision(Action.ASK_HUMAN,'waiting_for_approval')
            else:
                decision=await self._plan(snapshot)
            self.store.record_decision(self.goal_id,decision)
            return decision

    async def run(self, max_ticks: int = 10) -> list[Decision]:
        if not 1 <= max_ticks <= 1000: raise ValueError('Invalid tick count')
        out=[]
        for _ in range(max_ticks):
            d=await self.tick();out.append(d)
            if d.action in (Action.ASK_HUMAN,Action.FINISH,Action.BUILD):
                break
        return out
