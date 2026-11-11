from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class CapabilityKind(str, Enum):
    TOOL = "tool"
    MCP = "mcp"
    SKILL = "skill"
    KNOW_HOW = "know_how"

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    WAITING_APPROVAL = "waiting_approval"

@dataclass(frozen=True)
class Capability:
    id: str
    description: str
    kind: CapabilityKind
    version: str = "1.0.0"
    tags: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    cost: float = 1.0
    quality: float = 0.8
    healthy: bool = True
    approval_required: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class Task:
    id: str
    prompt: str
    depends_on: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    priority: int = 0
    tenant: str = "default"
    max_attempts: int = 2
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    result: str | None = None
    error: str | None = None
    selected_capability: str | None = None
    approved: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
