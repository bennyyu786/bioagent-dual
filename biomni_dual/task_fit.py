"""Task-first gateway for the existing autonomous builder and marketplace search.

Discovery does not install or activate any external code. Only exact uncovered
needs can create autonomous build requests. A sufficient internal set bypasses
marketplaces entirely.
"""
from __future__ import annotations
from dataclasses import dataclass
from .capability_selector import SelectionResult, select_for_task
from .task_requirements import TaskRequirements
from .registry import CapabilityRegistry

@dataclass
class TaskFitCoordinator:
    registry: CapabilityRegistry
    autonomy: object | None = None

    def evaluate(self, requirements: TaskRequirements) -> SelectionResult:
        return select_for_task(self.registry, requirements)

    def request_gaps(self, requirements: TaskRequirements, *, kinds: dict[str, str],
                     search: bool = False, limit: int = 10) -> dict:
        """Create requests for exact missing needs; optional discovery is read-only.

        No market candidate is auto-selected. Invalid/unknown kind is rejected,
        so a caller cannot silently convert an unknown tool into a Skill.
        """
        decision = self.evaluate(requirements)
        if decision.sufficient:
            return {'selection': decision, 'requests': (), 'discoveries': ()}
        # A budget/combination problem is not a reason to download more tools.
        if not decision.gaps or self.autonomy is None:
            return {'selection': decision, 'requests': (), 'discoveries': ()}
        requests = []
        found = []
        for need in decision.gaps:
            kind = kinds.get(need)
            if kind not in ('skill', 'mcp'):
                raise ValueError(f'Missing explicit Skill/MCP kind for {need}')
        for need in decision.gaps:
            kind = kinds[need]
            req = self.autonomy.request(need, kind, f'Task {requirements.task_id} needs {need}',
                                        request_key=f'task-fit:{requirements.task_id}:{kind}:{need}')
            requests.append(req)
            if search:
                found.append(self.autonomy.discover_marketplaces(req['id'], query=need, limit=limit))
        return {'selection': decision, 'requests': tuple(requests), 'discoveries': tuple(found)}
