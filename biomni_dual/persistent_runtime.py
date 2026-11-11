"""Durable, bounded event-driven orchestration for the existing AdaptiveController.

The runtime owns scheduling / lifecycle decisions, not unrestricted external tools.
Events have at-least-once delivery: replay is safe only because the controller's
TaskCenter detects RUNNING tasks and quarantines uncertain external effects.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol
from uuid import uuid4

from .adaptive import Action, AdaptiveController, Observation
from .models import TaskStatus
from .reliability import AuditLog


class GoalState(str, Enum):
    ACTIVE = 'active'
    PAUSED = 'paused'
    WAITING = 'waiting_approval'
    COMPLETED = 'completed'
    EXHAUSTED = 'budget_exhausted'
    STOPPED = 'stopped'


class EventKind(str, Enum):
    DATA_CHANGED = 'data_changed'
    RESOURCE_CHANGED = 'resource_changed'
    CAPABILITY_CHANGED = 'capability_changed'
    WORKER_FINISHED = 'worker_finished'
    TASK_FAILED = 'task_failed'
    HUMAN_MESSAGE = 'human_message'
    HEALTH_CHANGED = 'health_changed'
    PERIODIC = 'periodic'


@dataclass(frozen=True)
class RuntimeEvent:
    id: int
    goal_id: str
    kind: str
    key: str
    detail: dict[str, Any]
    attempts: int


class PersistentRuntimeStore:
    """SQLite event queue + leases + lifecycle, with audit events in one transaction.

    Intended for a single shared SQLite DB on one host; not distributed fencing.
    Never put sensitive input payloads or secrets in event.detail.
    """
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        with self.connect() as db:
            db.execute('PRAGMA busy_timeout=10000')
            db.execute('''CREATE TABLE IF NOT EXISTS runtime_goals (
              goal_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL,
              next_due REAL NOT NULL DEFAULT 0, last_reason TEXT NOT NULL DEFAULT '')''')
            db.execute('''CREATE TABLE IF NOT EXISTS runtime_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL, kind TEXT NOT NULL,
              event_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
              created REAL NOT NULL, updated REAL NOT NULL)''')
            db.execute('''CREATE INDEX IF NOT EXISTS runtime_event_pending
                          ON runtime_events(goal_id,status,id)''')
            db.execute('''CREATE TABLE IF NOT EXISTS runtime_leases (
              goal_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
              token TEXT NOT NULL, until REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS runtime_probe_state (
              goal_id TEXT NOT NULL, probe TEXT NOT NULL, digest TEXT NOT NULL,
              checked REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(goal_id,probe))''')
            db.execute('''CREATE TABLE IF NOT EXISTS runtime_schedules (
              goal_id TEXT NOT NULL, schedule_id TEXT NOT NULL, period REAL NOT NULL,
              next_due REAL NOT NULL, kind TEXT NOT NULL,
              PRIMARY KEY(goal_id,schedule_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS runtime_cadence (
              goal_id TEXT NOT NULL, lane TEXT NOT NULL, last_tick REAL NOT NULL,
              PRIMARY KEY(goal_id,lane))''')
            db.execute('''CREATE TABLE IF NOT EXISTS runtime_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL, event_id INTEGER,
              action TEXT NOT NULL, reason TEXT NOT NULL, ts REAL NOT NULL)''')
            AuditLog.install(db)

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.execute('PRAGMA busy_timeout=10000')
        return db

    def register_goal(self, goal_id: str) -> None:
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO runtime_goals VALUES (?,?,?,?,?)',
                       (goal_id, GoalState.ACTIVE.value, time.time(), 0, ''))

    def state(self, goal_id: str) -> GoalState:
        with self.connect() as db:
            row = db.execute('SELECT state FROM runtime_goals WHERE goal_id=?', (goal_id,)).fetchone()
        if row is None: raise KeyError('Goal not registered')
        return GoalState(row[0])

    def set_state(self, goal_id: str, state: GoalState, reason: str = '', *, operator: bool = False):
        if not isinstance(state, GoalState): raise ValueError('Invalid goal state')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM runtime_goals WHERE goal_id=?', (goal_id,)).fetchone()
            if not row: raise KeyError('Goal not registered')
            previous = GoalState(row[0])
            # Automated control must never silently undo explicit pause or stop.
            if not operator and previous in (GoalState.PAUSED, GoalState.STOPPED) and state != previous:
                raise PermissionError('Goal paused/stopped by operator')
            db.execute('UPDATE runtime_goals SET state=?,updated=?,last_reason=? WHERE goal_id=?',
                       (state.value,time.time(),reason[:300],goal_id))
            AuditLog.append_in_transaction(db,actor='operator' if operator else 'runtime',
                                           action='goal_state',subject=goal_id,
                                           details={'from':previous.value,'to':state.value,'reason':reason[:120]})

    def submit(self, goal_id: str, kind: EventKind, key: str,
               detail: dict[str, Any] | None = None) -> tuple[int, bool]:
        if not isinstance(kind, EventKind) or not key or len(key) > 250:
            raise ValueError('Invalid event type/key')
        payload=json.dumps(detail or {},sort_keys=True,ensure_ascii=False)
        if len(payload)>4096: raise ValueError('Event metadata too large')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM runtime_goals WHERE goal_id=?',(goal_id,)).fetchone():
                raise KeyError('Goal not registered')
            old=db.execute('SELECT id,goal_id,kind,payload FROM runtime_events WHERE event_key=?',(key,)).fetchone()
            if old:
                if old[1:] != (goal_id,kind.value,payload):
                    raise ValueError('Event idempotency key reused with different payload')
                return old[0],False
            now=time.time()
            cursor=db.execute('''INSERT INTO runtime_events(goal_id,kind,event_key,payload,created,updated)
                                 VALUES (?,?,?,?,?,?)''',(goal_id,kind.value,key,payload,now,now))
            eid=cursor.lastrowid
            AuditLog.append_in_transaction(db,actor='environment',action='event_queued',subject=goal_id,
                                           details={'event_id':eid,'kind':kind.value,'key_sha256':hashlib.sha256(key.encode()).hexdigest()})
            return eid,True

    def pending(self, goal_id: str) -> RuntimeEvent | None:
        with self.connect() as db:
            row=db.execute('''SELECT id,kind,event_key,payload,attempts FROM runtime_events
                              WHERE goal_id=? AND status='pending' ORDER BY id LIMIT 1''',(goal_id,)).fetchone()
        return RuntimeEvent(row[0],goal_id,row[1],row[2],json.loads(row[3]),row[4]) if row else None

    def mark_event(self, event: RuntimeEvent, status: str):
        if status not in ('done','pending','blocked'): raise ValueError('Invalid event state')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''UPDATE runtime_events SET status=?,updated=? WHERE id=? AND status='pending' ''',
                       (status,time.time(),event.id))

    def lease(self, goal_id: str, owner: str, ttl: float) -> str | None:
        if ttl<=0 or not owner: raise ValueError('Invalid lease')
        token=uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now=time.time()
            prior=db.execute('SELECT owner,until FROM runtime_leases WHERE goal_id=?',(goal_id,)).fetchone()
            if prior and prior[1]>now: return None
            db.execute('REPLACE INTO runtime_leases VALUES (?,?,?,?)',(goal_id,owner,token,now+ttl))
        return token

    def renew(self, goal_id: str, token: str, ttl: float) -> bool:
        if ttl<=0: raise ValueError('Invalid lease')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            updated=db.execute('''UPDATE runtime_leases SET until=? WHERE goal_id=? AND token=?
                                  AND until>?''',(time.time()+ttl,goal_id,token,time.time())).rowcount
        return bool(updated)

    def release(self, goal_id: str, token: str):
        with self.connect() as db:
            db.execute('DELETE FROM runtime_leases WHERE goal_id=? AND token=?',(goal_id,token))

    def schedule(self, goal_id: str, schedule_id: str, period_seconds: float,
                 *, kind: EventKind = EventKind.PERIODIC, immediate: bool = False):
        if not schedule_id or len(schedule_id)>80 or period_seconds<1 or not isinstance(kind,EventKind):
            raise ValueError('Invalid periodic schedule')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT period,kind FROM runtime_schedules WHERE goal_id=? AND schedule_id=?',
                           (goal_id,schedule_id)).fetchone()
            if old:
                if old != (period_seconds,kind.value):
                    raise ValueError('Schedule already exists with different parameters')
                return
            db.execute('INSERT INTO runtime_schedules VALUES (?,?,?,?,?)',
                (goal_id,schedule_id,period_seconds,
                 time.time() if immediate else time.time()+period_seconds,kind.value))

    def due_events(self, goal_id: str) -> list[int]:
        # At most one due event per schedule; skip missed periods after outage.
        created=[]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now=time.time()
            rows=db.execute('SELECT schedule_id,period,next_due,kind FROM runtime_schedules WHERE goal_id=? AND next_due<=?',
                            (goal_id,now)).fetchall()
            for sid,period,due,kind in rows:
                db.execute('UPDATE runtime_schedules SET next_due=? WHERE goal_id=? AND schedule_id=?',
                           (now+period,goal_id,sid))
                key=f'schedule:{goal_id}:{sid}:{int(due*1000000)}'
                cursor=db.execute('INSERT OR IGNORE INTO runtime_events (goal_id,kind,event_key,payload,created,updated) VALUES (?,?,?,?,?,?)',
                    (goal_id,kind,key,'{}',now,now))
                if cursor.rowcount:
                    created.append(cursor.lastrowid)
                    AuditLog.append_in_transaction(db,actor='timer',action='event_queued',subject=goal_id,
                        details={'event_id':cursor.lastrowid,'kind':kind,'schedule':sid})
        return created

    def lane_due(self, goal_id: str, lane: str, interval: float) -> bool:
        with self.connect() as db:
            row=db.execute('SELECT last_tick FROM runtime_cadence WHERE goal_id=? AND lane=?',
                           (goal_id,lane)).fetchone()
        return row is None or time.time()-row[0]>=interval

    def mark_lane(self, goal_id: str, lane: str):
        with self.connect() as db:
            db.execute('REPLACE INTO runtime_cadence VALUES (?,?,?)',
                       (goal_id,lane,time.time()))

    def record_run(self, goal_id: str, event_id: int | None, action: Action, reason: str):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO runtime_runs(goal_id,event_id,action,reason,ts) VALUES (?,?,?,?,?)',
                       (goal_id,event_id,action.value,reason[:300],time.time()))
            AuditLog.append_in_transaction(db,actor='runtime',action='tick',subject=goal_id,
                                           details={'event_id':event_id,'action':action.value,'reason':reason[:100]})

    def probe_change(self, goal_id: str, probe: str, digest: str) -> int | None:
        """Return increasing version on transition; None for baseline/unchanged."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT digest,revision FROM runtime_probe_state WHERE goal_id=? AND probe=?',
                           (goal_id,probe)).fetchone()
            if row and row[0] == digest:
                db.execute('UPDATE runtime_probe_state SET checked=? WHERE goal_id=? AND probe=?',
                           (time.time(),goal_id,probe))
                return None
            version=(row[1]+1) if row else 0
            db.execute('REPLACE INTO runtime_probe_state VALUES (?,?,?,?,?)',
                       (goal_id,probe,digest,time.time(),version))
        return version if row else None



class Probe(Protocol):
    async def sample(self) -> Any: ...


class CallbackProbe:
    def __init__(self, callback: Callable[[], Any]):
        self.callback=callback
    async def sample(self):
        value=self.callback()
        return await value if inspect.isawaitable(value) else value


class EnvironmentMonitor:
    """Poll allowlisted, side-effect-free probes; emit only on a state transition."""
    def __init__(self, store: PersistentRuntimeStore):
        self.store=store
        self.probes: dict[str,tuple[EventKind,Probe]]={}

    def add(self, name: str, kind: EventKind, probe: Probe):
        if not name or len(name)>80 or not isinstance(kind,EventKind) or name in self.probes:
            raise ValueError('Invalid/duplicate probe')
        self.probes[name]=(kind,probe)

    async def poll(self, goal_id: str) -> list[int]:
        added=[]
        for name,(kind,probe) in self.probes.items():
            try:
                sample=await probe.sample()
                encoded=json.dumps(sample,sort_keys=True,default=str,ensure_ascii=False)
                if len(encoded)>100000: raise ValueError('Probe payload too large')
                digest=hashlib.sha256(encoded.encode()).hexdigest()
                revision=self.store.probe_change(goal_id,name,digest)
                if revision is not None:
                    event_id,inserted=self.store.submit(goal_id,kind,f'probe:{goal_id}:{name}:{revision}:{digest}',
                       {'probe':name,'digest':digest})
                    if inserted: added.append(event_id)
            except Exception as exc:
                # Store error class only; never leak probe secrets into audit/events.
                digest=hashlib.sha256(('error:'+type(exc).__name__).encode()).hexdigest()
                revision=self.store.probe_change(goal_id,name+':health',digest)
                if revision is not None:
                    eid,inserted=self.store.submit(goal_id,EventKind.HEALTH_CHANGED,
                        f'probe-health:{goal_id}:{name}:{revision}:{digest}', {'probe':name,'error_type':type(exc).__name__})
                    if inserted: added.append(eid)
        return added


@dataclass(frozen=True)
class CadencePolicy:
    """Logical control lanes; urgent failures preempt slower planning cycles."""
    fast: float = 0
    medium: float = 5
    slow: float = 300

    def __post_init__(self):
        if min(self.fast,self.medium,self.slow)<0:
            raise ValueError('Invalid cadence')

    def lane(self, kind: str) -> tuple[str,float]:
        if kind in (EventKind.TASK_FAILED.value,EventKind.WORKER_FINISHED.value,
                    EventKind.HEALTH_CHANGED.value):
            return ('fast',self.fast)
        if kind == EventKind.PERIODIC.value:
            return ('slow',self.slow)
        return ('medium',self.medium)


class PersistentAutonomousRuntime:
    """Per-goal lease, event-triggered rolling decisions, bounded polling.

    One tick may run a real worker. Leases protect cooperative coordinators,
    not against stale workers after lease loss; external tool replay is blocked
    by TaskCenter RUNNING/BLOCKED states and requires operator reconciliation.
    """
    def __init__(self, store: PersistentRuntimeStore, controllers: dict[str,AdaptiveController],
                 monitor: EnvironmentMonitor | None = None, *, owner: str | None = None,
                 lease_seconds: float = 60, poll_seconds: float = 5, idle_interval: float = 60,
                 cadence: CadencePolicy | None = None):
        if min(lease_seconds,poll_seconds,idle_interval)<=0: raise ValueError('Invalid runtime intervals')
        self.store,self.controllers,self.monitor=store,controllers,monitor
        self.owner=owner or uuid4().hex
        self.lease_seconds,self.poll_seconds,self.idle_interval=lease_seconds,poll_seconds,idle_interval
        self.cadence=cadence or CadencePolicy()
        self._stopping=asyncio.Event()
        self._last_tick:dict[str,float]={}
        for goal_id,controller in controllers.items():
            if controller.goal_id != goal_id: raise ValueError('Controller goal mismatch')
            controller.store.get_goal(goal_id)
            store.register_goal(goal_id)

    def stop(self):
        self._stopping.set()

    def reconcile_startup(self, goal_id: str) -> int:
        """Quarantine orphaned executions; never blindly retry external effects.

        Must be called after prior lease expiry. The caller may need to reconcile
        with remote worker/session records before allowing any replay.
        """
        if goal_id not in self.controllers: raise KeyError('Unknown controller goal')
        with self.store.connect() as db:
            lease=db.execute('SELECT until FROM runtime_leases WHERE goal_id=?',(goal_id,)).fetchone()
        if lease and lease[0]>time.time():
            raise RuntimeError('Cannot recover while another coordinator holds lease')
        controller=self.controllers[goal_id]
        count=0
        for task in controller.snapshot().tasks:
            if task.status==TaskStatus.RUNNING:
                controller.tasks.block_running(task.id,reason='startup_external_effect_uncertain')
                count+=1
        if count:
            self.store.set_state(goal_id,GoalState.WAITING,'startup_reconciliation_required')
        return count

    def resume_goal(self, goal_id: str, *, operator: bool = False):
        """Explicitly resume after approval/reconciliation; no automatic bypass."""
        if not operator: raise PermissionError('Operator authorization required')
        controller=self.controllers[goal_id]
        if any(t.status in (TaskStatus.BLOCKED,TaskStatus.RUNNING,TaskStatus.WAITING_APPROVAL)
               for t in controller.snapshot().tasks):
            raise RuntimeError('Unreconciled or unapproved task remains')
        self.store.set_state(goal_id,GoalState.ACTIVE,'operator_resumed',operator=True)

    async def _heartbeat(self, goal_id: str, token: str):
        while not self._stopping.is_set():
            await asyncio.sleep(self.lease_seconds/3)
            if not self.store.renew(goal_id,token,self.lease_seconds):
                return

    def _update_goal(self, goal_id: str, action: Action, reason: str):
        if reason in ('cycle_budget_exhausted','planning_budget_exhausted'):
            self.store.set_state(goal_id,GoalState.EXHAUSTED,reason)
        elif action==Action.FINISH:
            self.store.set_state(goal_id,GoalState.COMPLETED,reason)
        elif action in (Action.ASK_HUMAN,Action.BUILD):
            self.store.set_state(goal_id,GoalState.WAITING,reason)

    async def step(self, goal_id: str, *, force: bool = False) -> Action | None:
        if goal_id not in self.controllers: raise KeyError('Unknown controller goal')
        if self.store.state(goal_id)!=GoalState.ACTIVE: return None
        ctl=self.controllers[goal_id]
        self.store.due_events(goal_id)
        if self.monitor is not None:
            await self.monitor.poll(goal_id)
        event=self.store.pending(goal_id)
        now=time.monotonic()
        lane='idle'
        if event is not None:
            lane,interval=self.cadence.lane(event.kind)
            if not force and not self.store.lane_due(goal_id,lane,interval):
                return None
        if event is None and not force and now-self._last_tick.get(goal_id,float('-inf'))<self.idle_interval:
            return None
        token=self.store.lease(goal_id,self.owner,self.lease_seconds)
        if token is None: return None
        heartbeat=asyncio.create_task(self._heartbeat(goal_id,token))
        try:
            # An event signals a new observation, not a free-form instruction.
            if event is not None and event.kind in (EventKind.DATA_CHANGED.value,
                    EventKind.CAPABILITY_CHANGED.value,EventKind.HUMAN_MESSAGE.value,
                    EventKind.RESOURCE_CHANGED.value):
                ctl.store.remember(goal_id,Observation(
                    key=f'runtime-event:{event.id}',task_id='',kind='environment_change',
                    summary=f'{event.kind}: {event.key[:100]}',
                    evidence={'runtime_event_id':event.id,'kind':event.kind}))
            decision=await ctl.tick()
            # No silent continuation after lease ownership changed.
            if not self.store.renew(goal_id,token,self.lease_seconds):
                raise RuntimeError('Runtime lease lost; reconcile before further actions')
            self.store.record_run(goal_id,event.id if event else None,decision.action,decision.reason)
            if event is not None: self.store.mark_lane(goal_id,lane)
            if event is not None: self.store.mark_event(event,'done')
            self._update_goal(goal_id,decision.action,decision.reason)
            self._last_tick[goal_id]=time.monotonic()
            return decision.action
        finally:
            heartbeat.cancel()
            try: await heartbeat
            except asyncio.CancelledError: pass
            self.store.release(goal_id,token)

    async def serve(self, *, max_loops: int | None = None):
        if max_loops is not None and max_loops<1: raise ValueError('Invalid loop limit')
        loops=0
        while not self._stopping.is_set():
            for gid in self.controllers:
                if self._stopping.is_set(): break
                try:
                    await self.step(gid)
                except Exception:
                    # Do not swallow operational failures or mark events complete.
                    # A production supervisor should log and restart the service.
                    raise
            loops+=1
            if max_loops is not None and loops>=max_loops: break
            try: await asyncio.wait_for(self._stopping.wait(),timeout=self.poll_seconds)
            except asyncio.TimeoutError: pass
