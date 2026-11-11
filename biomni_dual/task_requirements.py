"""Task-grounded capability needs. No LLM output is trusted without validation."""
from __future__ import annotations
from dataclasses import dataclass
import re

_KEY = re.compile(r'^[A-Za-z0-9_.:-]{1,120}$')

@dataclass(frozen=True)
class Requirement:
    id: str
    description: str = ''
    weight: float = 1.0

    def __post_init__(self):
        if not _KEY.fullmatch(self.id):
            raise ValueError('Invalid requirement ID')
        if not (0 < self.weight <= 100):
            raise ValueError('Requirement weight must be between 0 and 100')

@dataclass(frozen=True)
class TaskRequirements:
    task_id: str
    needs: tuple[Requirement, ...]
    input_schema: str = ''
    output_schema: str = ''
    allowed_licenses: tuple[str, ...] = ()
    allowed_permissions: tuple[str, ...] = ()
    max_cost: float = float('inf')
    min_quality: float = 0.0
    max_risk: float = 1.0
    min_coverage: float = 1.0

    def __post_init__(self):
        if not self.task_id or not self.needs or len(self.needs) > 128:
            raise ValueError('Task requires an ID and 1-128 explicit needs')
        if len({n.id for n in self.needs}) != len(self.needs):
            raise ValueError('Duplicate requirements')
        if not (0 < self.min_coverage <= 1) or not (0 <= self.min_quality <= 1):
            raise ValueError('Invalid quality/coverage threshold')
        if not (0 <= self.max_risk <= 1) or self.max_cost < 0:
            raise ValueError('Invalid risk/cost budget')

    @classmethod
    def from_task(cls, task, **kwargs):
        """Extract only explicit requirements, never guess from the task prompt."""
        if not task.required_capabilities:
            raise ValueError('Task has no explicit capability requirements; provide reviewed requirements')
        return cls(task_id=task.id, needs=tuple(Requirement(n) for n in dict.fromkeys(task.required_capabilities)), **kwargs)
