"""Thin orchestrator for deterministic execution and approval-resume workflow."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from .planner import LLMPlanner
from .builder import SandboxBuilder
from .scheduler import TaskCenter
from .runtime import ParallelScheduler
from .models import TaskStatus
from .reliability import SessionStore

@dataclass
class DualCenterWorkflow:
    tasks: TaskCenter
    planner: LLMPlanner
    scheduler: ParallelScheduler
    builder: SandboxBuilder
    sessions: SessionStore | None = None
    autonomy: object | None = None


    def assess_task_capabilities(self, task_id: str, registry):
        """Task-centric capability assessment without mutating task state."""
        from .task_requirements import TaskRequirements
        from .capability_selector import select_for_task
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return select_for_task(registry, TaskRequirements.from_task(task))

    async def submit_goal(self, goal: str) -> dict:
        return await self.planner.submit_goal(goal)

    def dispatch(self) -> list:
        return self.scheduler.run_until_idle()

    async def fill_gap(self, *, task_id: str, cap_id: str, kind: str,
                       path: str | Path, approver: str | None = None):
        task=self.tasks.get(task_id)
        if not task or task.status != TaskStatus.WAITING_APPROVAL:
            raise ValueError('Task not awaiting capability')
        if cap_id not in task.required_capabilities:
            raise ValueError('Capability not required by task')
        build_id=self.builder.propose(cap_id,kind,path)
        job=await self.builder.validate(build_id)
        if job['status'] != 'REVIEW': return job
        if approver is None: return job
        self.builder.approve(build_id,approver,approved=True)
        self.builder.publish(build_id)
        self.tasks.approve(task_id)
        return self.builder.store.get(build_id)

    async def dispatch_with_replanning(self, *, max_rounds: int = 3) -> list:
        """Dispatch and react to new terminal failures with bounded LLM proposals.

        A recovery proposal with key='recovery' adds a prerequisite recovery job,
        and requeues the terminally failed job exactly once. Other proposals may
        add alternative paths; they never overwrite completed/running jobs.
        """
        finished=[]
        for _ in range(max_rounds):
            batch=await __import__('asyncio').to_thread(self.dispatch)
            finished.extend(batch)
            failed=[t for t in batch if t.status == TaskStatus.FAILED]
            if not failed: break
            for task in failed:
                if any(e[1]=='llm_replan_requested' for e in self.tasks.events(task.id)):
                    continue
                # Record intent first to avoid duplicate model requests after crash.
                # This is at-most-once; operators may manually request a retry.
                self.tasks._save(task,'llm_replan_requested','bounded automatic replan')
                try:
                    proposal=await self.planner.replan(event=f'failed task {task.id}: {task.error}',
                        task_state=[{'id':t.id,'prompt':t.prompt,'status':t.status.value,
                        'depends_on':t.depends_on} for t in self.tasks.list()])
                    recovery=proposal['task_ids'].get('recovery')
                    if recovery:
                        self.tasks.reset_failed_with_dependency(task.id,recovery)
                except Exception:
                    # Fail closed. Leave original task FAILED and preserve events.
                    raise
        return finished

    def recover(self) -> int:
        return self.tasks.recover_interrupted()

    def request_capability(self, capability_id: str, kind: str, requirement: str, *,
                           request_key: str, task_id: str | None = None):
        if self.autonomy is None: raise RuntimeError('Capability autonomy not configured')
        return self.autonomy.request(capability_id, kind, requirement,
                                     request_key=request_key, task_id=task_id)

    async def build_capability(self, request_id: str):
        if self.autonomy is None: raise RuntimeError('Capability autonomy not configured')
        return await self.autonomy.prepare(request_id)

    def publish_capability(self, request_id: str, *, approver: str, approve: bool):
        if self.autonomy is None: raise RuntimeError('Capability autonomy not configured')
        return self.autonomy.approve_and_publish(request_id,approver=approver,approve=approve)

    async def recover_all(self) -> dict:
        """Fail closed on crashed validation and clean labeled Docker orphans."""
        interrupted=self.builder.store.recover_interrupted()
        autonomy_blocked=self.autonomy.recover() if self.autonomy else 0
        # An ambiguous Agent SDK attempt is not automatically safe to replay.
        uncertain = self.sessions.recover() if self.sessions else 0
        tasks = self.tasks.recover_interrupted(self.sessions.uncertain_task_ids() if self.sessions else set())
        orphans=await self.builder.sandbox.cleanup_orphans()
        capabilities=self.builder.restore_published()
        return {'tasks':tasks,'uncertain_sessions':uncertain,'interrupted_builds':interrupted,
                'orphan_containers_removed':orphans,'restored_capabilities':capabilities,'blocked_autonomous_builds':autonomy_blocked}
