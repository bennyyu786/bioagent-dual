"""Validated, backend-independent goal planning and bounded failure replanning.

A planner backend may be an LLM, but its JSON proposal is untrusted and validated.
No code or tool execution occurs during planning.
"""
from dataclasses import dataclass, field
from collections.abc import Callable
import re
from .scheduler import TaskCenter
from .models import Task, TaskStatus

@dataclass(frozen=True)
class PlanStep:
    key: str
    prompt: str
    depends_on: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    priority: int = 0

@dataclass(frozen=True)
class GoalPlan:
    goal: str
    steps: tuple[PlanStep, ...]

class PlanValidator:
    @staticmethod
    def validate(plan: GoalPlan, max_steps: int = 30) -> None:
        if not plan.steps or len(plan.steps) > max_steps:
            raise ValueError('Plan must contain 1..%s steps' % max_steps)
        known = set()
        for step in plan.steps:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', step.key):
                raise ValueError('Invalid step key')
            if step.key in known or len(step.prompt) > 16000 or not step.prompt.strip():
                raise ValueError('Duplicate key or invalid prompt')
            if any(dep not in known for dep in step.depends_on):
                raise ValueError('Dependencies must reference earlier steps')
            if len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError('Duplicate dependency')
            known.add(step.key)

class GoalPlanner:
    """Backend returns JSON-like dict. Human review optional at the task level."""
    def __init__(self, backend: Callable[[str], dict], max_steps: int = 30):
        self.backend, self.max_steps = backend, max_steps

    def propose(self, goal: str) -> GoalPlan:
        data = self.backend(goal)
        if not isinstance(data, dict) or not isinstance(data.get('steps'), list):
            raise ValueError('Planner must return mapping with steps list')
        steps = []
        for obj in data['steps']:
            if not isinstance(obj, dict):
                raise ValueError('Invalid step')
            steps.append(PlanStep(
                key=obj['key'], prompt=obj['prompt'],
                depends_on=tuple(obj.get('depends_on', [])),
                required_capabilities=tuple(obj.get('required_capabilities', [])),
                priority=int(obj.get('priority', 0))))
        plan = GoalPlan(goal, tuple(steps))
        PlanValidator.validate(plan, self.max_steps)
        return plan

    def submit(self, center: TaskCenter, goal: str, *, tenant='default') -> list[Task]:
        plan = self.propose(goal)
        # Deterministic submission order from validated topological order.
        created = {}
        for step in plan.steps:
            created[step.key] = center.submit(
                step.prompt, depends_on=[created[d].id for d in step.depends_on],
                required_capabilities=list(step.required_capabilities),
                priority=step.priority, tenant=tenant)
        return list(created.values())

class FailureReplanner:
    """Once-per-task recovery task insertion, bounded and idempotent.

    Recovery creates a new dependency on the original task. It is intentionally
    opt-in and only invoked after a terminal worker failure.
    """
    def __init__(self, center: TaskCenter, recovery: Callable[[Task], PlanStep | None]):
        self.center, self.recovery = center, recovery

    def on_failure(self, task: Task) -> Task | None:
        if task.status != TaskStatus.FAILED:
            return None
        if any(e[1] == 'replanned' for e in self.center.events(task.id)):
            return None
        step = self.recovery(task)
        if step is None:
            return None
        if step.depends_on:
            raise ValueError('Recovery step cannot reference external step keys')
        new = self.center.submit(step.prompt,
                                 required_capabilities=list(step.required_capabilities),
                                 priority=step.priority, tenant=task.tenant)
        self.center.reset_failed_with_dependency(task.id, new.id)
        return new
