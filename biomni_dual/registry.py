from __future__ import annotations
"""Versioned in-memory capability registry with deterministic matching."""
import re
from .models import Capability

class CapabilityRegistry:
    def __init__(self):
        self._items: dict[str, Capability] = {}

    def register(self, item: Capability, replace: bool = False) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", item.id):
            raise ValueError("Capability IDs may only contain letters, numbers, _, ., :, -")
        if item.id in self._items and not replace:
            raise ValueError(f"Duplicate capability ID: {item.id}")
        self._items[item.id] = item

    def get(self, id: str) -> Capability | None:
        return self._items.get(id)

    def list(self) -> list[Capability]:
        return sorted(self._items.values(), key=lambda c: c.id)

    def missing(self, ids: list[str]) -> list[str]:
        return [i for i in ids if i not in self._items or not self._items[i].healthy]

    def select(self, prompt: str, required: list[str] | None = None, limit: int = 5) -> list[Capability]:
        """Baseline matching; deliberately deterministic, no unsupported LLM claims."""
        required = required or []
        if self.missing(required):
            return []
        tokens = set(re.findall(r"[\w-]+", prompt.lower()))
        scores = []
        for c in self._items.values():
            if not c.healthy:
                continue
            if any(r not in self._items or not self._items[r].healthy for r in c.requires):
                continue
            if required and c.id not in required and not set(required).issubset(set(c.requires)):
                continue
            words = set(re.findall(r"[\w-]+", (c.id + ' ' + c.description + ' ' + ' '.join(c.tags)).lower()))
            overlap = len(tokens & words)
            score = overlap * 2 + c.quality - c.cost * 0.1
            scores.append((score, c))
        scores.sort(key=lambda p: (-p[0], p[1].id))
        return [c for _, c in scores[:limit]]

    def import_biomni_registry(self, agent) -> int:
        """Read-only discovery of Biomni native tools; does not mutate the A1 instance."""
        from .models import CapabilityKind
        count = 0
        for module, tools in getattr(agent, 'module2api', {}).items():
            for tool in tools:
                if not isinstance(tool, dict) or not tool.get('name'):
                    continue
                item = Capability(id=f"biomni.{module}.{tool['name']}", description=tool.get('description', ''), kind=CapabilityKind.TOOL, metadata={'module': module, 'name': tool['name']})
                self.register(item, replace=True)
                count += 1
        return count
