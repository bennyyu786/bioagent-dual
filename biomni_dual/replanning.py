"""Event-informed, validated planner interface; no implicit model or code execution."""
from __future__ import annotations
from collections.abc import Callable
from .dag import DAGPatch, DAGVersionManager
from .events import Event

class ReplanningAgent:
    """A pluggable model/planner returns a declarative DAGPatch, not executable code.

    The backend receives an event and a read-only snapshot of task state.
    All changes are validated and transactionally committed by DAGVersionManager.
    Existing active/completed tasks cannot have dependencies rewritten.
    """
    def __init__(self, versions: DAGVersionManager,
                 backend: Callable[[Event, list[dict]], DAGPatch | None],
                 *, max_revisions: int = 20):
        self.versions, self.backend, self.max_revisions = versions, backend, max_revisions

    def on_event(self, event: Event) -> dict[str, str] | None:
        if len(self.versions.versions()) >= self.max_revisions:
            raise RuntimeError('Revision limit reached; human review required')
        state = [dict(id=t.id, prompt=t.prompt, status=t.status.value,
                      depends_on=list(t.depends_on), error=t.error)
                 for t in self.versions.center.list()]
        patch = self.backend(event, state)
        if patch is None:
            return None
        if not isinstance(patch, DAGPatch):
            raise TypeError('Replanner must return DAGPatch or None')
        return self.versions.commit(patch)
