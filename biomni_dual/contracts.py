"""Versioned contracts for independent agent runtimes and proposals."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Protocol

@dataclass(frozen=True)
class AgentRequest:
    task_id: str
    role: str
    prompt: str
    workspace: str | None = None
    allowed_capabilities: tuple[str, ...] = ()
    session_id: str | None = None
    timeout: float = 180.0

@dataclass(frozen=True)
class AgentResult:
    success: bool
    text: str
    session_id: str | None = None
    artifacts: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

class AgentRuntime(Protocol):
    async def run(self, request: AgentRequest) -> AgentResult: ...
    async def cancel(self, task_id: str) -> bool: ...

class PlanError(ValueError):
    pass

class BuildError(RuntimeError):
    pass
