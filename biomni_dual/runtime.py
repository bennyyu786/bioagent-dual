"""Bounded parallel scheduler for one coordinator process.

Reservations are in-memory and protected by a lock. SQLite claim is performed
under this coordinator's dispatch lock: do not run two coordinators against the
same DB. This is not a distributed lease system.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from threading import RLock
from time import perf_counter
from collections import deque
from typing import Protocol
from .models import Task, Capability, TaskStatus
from .registry import CapabilityRegistry
from .scheduler import TaskCenter
from .capabilities import CapabilityCenter
from .events import EventBus
from .reliability import classify_error

@dataclass(frozen=True)
class ResourceRequest:
    cpu: int = 1
    gpu: int = 0
    memory_mb: int = 256

    def __post_init__(self):
        if self.cpu < 0 or self.gpu < 0 or self.memory_mb < 0:
            raise ValueError('Resources cannot be negative')

class ResourcePool:
    def __init__(self, cpu: int, gpu: int = 0, memory_mb: int = 4096):
        self.capacity = ResourceRequest(cpu, gpu, memory_mb)
        self.used = ResourceRequest(0, 0, 0)
        self._reservations: dict[str, ResourceRequest] = {}
        self._lock = RLock()

    def reserve(self, task_id: str, amount: ResourceRequest) -> bool:
        with self._lock:
            if task_id in self._reservations:
                return False
            if (self.used.cpu + amount.cpu > self.capacity.cpu or
                self.used.gpu + amount.gpu > self.capacity.gpu or
                self.used.memory_mb + amount.memory_mb > self.capacity.memory_mb):
                return False
            self._reservations[task_id] = amount
            self.used = ResourceRequest(self.used.cpu + amount.cpu,
                                        self.used.gpu + amount.gpu,
                                        self.used.memory_mb + amount.memory_mb)
            return True

    def release(self, task_id: str):
        with self._lock:
            amount = self._reservations.pop(task_id, None)
            if amount is None:
                return
            self.used = ResourceRequest(self.used.cpu - amount.cpu,
                                        self.used.gpu - amount.gpu,
                                        self.used.memory_mb - amount.memory_mb)

class Worker(Protocol):
    def run(self, task: Task, capabilities: list[Capability]) -> str: ...

class WorkerPool:
    def __init__(self, default: Worker, by_kind: dict[str, Worker] | None = None):
        self.default = default
        self.by_kind = by_kind or {}

    def choose(self, task: Task) -> Worker:
        # Explicit tags allow multiple adapters without invoking external CLIs by default.
        return self.by_kind.get(task.selected_capability or '', self.default)

class ParallelScheduler:
    """Runnable parallel scheduler, fair across tenants in round-robin order.

    Call run_until_idle from exactly one coordinator process. Worker adapters
    must be thread-safe or create isolated agents per job (BiomniWorker does).
    """
    def __init__(self, tasks: TaskCenter, registry: CapabilityRegistry, workers: WorkerPool,
                 resources: ResourcePool, *, max_workers: int = 4,
                 capability_center: CapabilityCenter | None = None, event_bus: EventBus | None = None,
                 task_fit_factory=None):
        if max_workers < 1:
            raise ValueError('max_workers must be positive')
        self.tasks, self.registry, self.workers = tasks, registry, workers
        self.resources, self.max_workers = resources, max_workers
        self.capability_center = capability_center
        self.task_fit_factory = task_fit_factory
        self.events = event_bus or EventBus()
        self._lock = RLock()
        self._last_tenant: str | None = None

    def _fair_order(self, candidates: list[Task]) -> list[Task]:
        buckets: dict[str, deque[Task]] = {}
        for task in candidates:  # ready() is already priority sorted
            buckets.setdefault(task.tenant, deque()).append(task)
        tenants = sorted(buckets)
        if self._last_tenant in tenants:
            idx = (tenants.index(self._last_tenant) + 1) % len(tenants)
            tenants = tenants[idx:] + tenants[:idx]
        out: list[Task] = []
        while any(buckets.values()):
            for tenant in tenants:
                if buckets[tenant]:
                    out.append(buckets[tenant].popleft())
        return out

    def _request_for(self, capabilities: list[Capability]) -> ResourceRequest:
        cpu, gpu, memory = 1, 0, 256
        for cap in capabilities:
            spec = cap.metadata.get('resources', {})
            if not isinstance(spec, dict):
                raise ValueError('Capability resources must be a mapping')
            cpu = max(cpu, int(spec.get('cpu', 1)))
            gpu = max(gpu, int(spec.get('gpu', 0)))
            memory = max(memory, int(spec.get('memory_mb', 256)))
        return ResourceRequest(cpu, gpu, memory)

    def _execute(self, task: Task, caps: list[Capability]) -> Task:
        started = perf_counter()
        success, result, error = False, None, None
        try:
            result = str(self.workers.choose(task).run(task, caps))
            success = True
        except Exception as exc:
            classified = classify_error(exc)
            error = f'{classified.kind.value}/{classified.code}: {classified.message}'
            retryable = classified.retryable
        elapsed_ms = (perf_counter() - started) * 1000
        with self._lock:
            try:
                if self.capability_center is not None:
                    self.capability_center.observe(task.id, caps, success=success, elapsed_ms=elapsed_ms)
                finished = self.tasks.finish(task.id, result=result, error=error, retryable=retryable if error else False)
                self.events.emit('task.finished', task_id=task.id, success=success,
                                 elapsed_ms=elapsed_ms, status=finished.status.value)
                return finished
            finally:
                self.resources.release(task.id)

    def run_until_idle(self, max_dispatch: int = 100) -> list[Task]:
        """Run until no pending work can be scheduled. Blocking until in-flight jobs finish.

        Handles retries and unlocks follow-on DAG tasks. Capacity impossible
        requests remain pending and emit resource.blocked, not marked failed.
        """
        output: list[Task] = []
        in_flight: dict[Future, str] = {}
        dispatched = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while True:
                progress = False
                with self._lock:
                    if dispatched < max_dispatch:
                        order = self._fair_order(self.tasks.ready())
                        for candidate in order:
                            if len(in_flight) >= self.max_workers or dispatched >= max_dispatch:
                                break
                            if self.task_fit_factory is not None:
                                # Opt-in task-fit path: hard constraints and cheapest
                                # sufficient internal bundle. Factory supplies reviewed
                                # requirements, never guesses from natural language.
                                from .capability_selector import select_for_task
                                requirements = self.task_fit_factory(candidate)
                                if requirements.task_id != candidate.id:
                                    raise ValueError('Task-fit requirements belong to a different task')
                                decision = select_for_task(self.registry, requirements)
                                if not decision.sufficient:
                                    if decision.gaps:
                                        self.tasks.require_approval(candidate.id)
                                        self.events.emit('capability.gap', task_id=candidate.id, missing=list(decision.gaps))
                                        progress = True
                                    else:
                                        self.events.emit('capability.budget_blocked', task_id=candidate.id)
                                    continue
                                caps = [self.registry.get(cap_id) for cap_id in decision.selected]
                            else:
                                gaps = (self.capability_center.gaps_for(candidate.required_capabilities)
                                        if self.capability_center else self.registry.missing(candidate.required_capabilities))
                                if gaps:
                                    if self.capability_center:
                                        self.capability_center.propose_gaps(candidate.required_capabilities,
                                            reason=f'Task {candidate.id}: {candidate.prompt}')
                                    self.tasks.require_approval(candidate.id)
                                    self.events.emit('capability.gap', task_id=candidate.id, missing=gaps)
                                    progress = True
                                    continue
                                # Required capabilities form a bundle, not a ranked single candidate.
                                if candidate.required_capabilities:
                                    selected: dict[str, Capability] = {}
                                    def add_cap(cap_id: str):
                                        if cap_id in selected:
                                            return
                                        cap = self.registry.get(cap_id)
                                        if cap is None or not cap.healthy:
                                            raise ValueError(f'Unavailable capability: {cap_id}')
                                        selected[cap_id] = cap
                                        for dependency in cap.requires:
                                            add_cap(dependency)
                                    for cap_id in candidate.required_capabilities:
                                        add_cap(cap_id)
                                    caps = list(selected.values())
                                else:
                                    caps = self.registry.select(candidate.prompt)
                            if any(cap.approval_required for cap in caps) and not candidate.approved:
                                self.tasks.require_approval(candidate.id)
                                self.events.emit('task.approval_required', task_id=candidate.id)
                                progress = True
                                continue
                            try:
                                request = self._request_for(caps)
                            except ValueError as exc:
                                self.tasks.require_approval(candidate.id)
                                self.events.emit('resource.invalid', task_id=candidate.id, error=str(exc))
                                progress = True
                                continue
                            if not self.resources.reserve(candidate.id, request):
                                self.events.emit('resource.blocked', task_id=candidate.id)
                                continue
                            try:
                                running = self.tasks.claim(candidate.id)
                                running.selected_capability = ','.join(c.id for c in caps)
                                self.tasks._save(running, 'capabilities_selected', running.selected_capability)
                                future = executor.submit(self._execute, running, caps)
                                in_flight[future] = candidate.id
                                self._last_tenant = candidate.tenant
                                dispatched += 1
                                progress = True
                                self.events.emit('task.dispatched', task_id=candidate.id, tenant=candidate.tenant)
                            except Exception:
                                self.resources.release(candidate.id)
                                raise
                if in_flight:
                    done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for future in done:
                        del in_flight[future]
                        output.append(future.result())
                        progress = True
                if not progress:
                    break
        return output
