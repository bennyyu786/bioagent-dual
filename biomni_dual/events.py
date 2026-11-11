"""In-process event notifications; SQLite TaskCenter remains source of truth."""
from __future__ import annotations
from dataclasses import dataclass, field
import time
from collections.abc import Callable

@dataclass(frozen=True)
class Event:
    name: str
    payload: dict
    at: float = field(default_factory=time.time)

class EventBus:
    def __init__(self):
        self._listeners: dict[str, list[Callable[[Event], None]]] = {}

    def subscribe(self, name: str, handler: Callable[[Event], None]):
        self._listeners.setdefault(name, []).append(handler)

    def emit(self, name: str, **payload):
        evt = Event(name, payload)
        for handler in self._listeners.get(name, []):
            handler(evt)
        return evt
