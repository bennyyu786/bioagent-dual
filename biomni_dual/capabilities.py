"""Auditable capability-gap proposals and observed execution performance.

Build proposals are inert: they never download, install, shell-execute, or start
MCP servers. An operator explicitly registers a tested capability after approval.
"""
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from .models import Capability
from .registry import CapabilityRegistry

@dataclass(frozen=True)
class GapProposal:
    capability_id: str
    status: str
    reason: str
    requested_at: float

class CapabilityCenter:
    def __init__(self, registry: CapabilityRegistry, db_path: str | Path):
        self.registry, self.db_path = registry, str(db_path)
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS gap_proposals (
                capability_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                reason TEXT NOT NULL, requested_at REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS capability_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL, capability_id TEXT NOT NULL,
                success INTEGER NOT NULL, elapsed_ms REAL NOT NULL,
                observed_at REAL NOT NULL)''')

    def _db(self):
        return sqlite3.connect(self.db_path, timeout=15)

    def gaps_for(self, required: list[str]) -> list[str]:
        """Find unregistered/unhealthy/transitively missing dependencies."""
        missing, visited = set(), set()
        def visit(cap_id):
            if cap_id in visited:
                return
            visited.add(cap_id)
            cap = self.registry.get(cap_id)
            if cap is None or not cap.healthy:
                missing.add(cap_id)
            else:
                for dep in cap.requires:
                    visit(dep)
        for cap_id in required:
            visit(cap_id)
        return sorted(missing)

    def propose_gaps(self, required: list[str], *, reason: str) -> list[GapProposal]:
        out = []
        for cap_id in self.gaps_for(required):
            with self._db() as db:
                db.execute('''INSERT OR IGNORE INTO gap_proposals
                (capability_id,status,reason,requested_at) VALUES (?,?,?,?)''',
                           (cap_id, 'proposed', reason[:4000], time.time()))
            out.append(self.proposal(cap_id))
        return out

    def proposal(self, cap_id: str) -> GapProposal | None:
        with self._db() as db:
            row = db.execute('SELECT * FROM gap_proposals WHERE capability_id=?', (cap_id,)).fetchone()
        return GapProposal(*row) if row else None

    def review(self, cap_id: str, *, approve: bool) -> GapProposal:
        current = self.proposal(cap_id)
        if not current or current.status != 'proposed':
            raise ValueError('No pending capability proposal')
        with self._db() as db:
            db.execute('UPDATE gap_proposals SET status=? WHERE capability_id=?',
                       ('approved' if approve else 'rejected', cap_id))
        return self.proposal(cap_id)

    def publish(self, cap: Capability, *, verified: bool) -> GapProposal:
        p = self.proposal(cap.id)
        if not p or p.status != 'approved' or not verified:
            raise PermissionError('Approved proposal and independent verification required')
        if cap.requires and self.gaps_for(list(cap.requires)):
            raise ValueError('Capability dependencies are not healthy')
        self.registry.register(cap, replace=True)
        with self._db() as db:
            db.execute('UPDATE gap_proposals SET status=? WHERE capability_id=?', ('published', cap.id))
        return self.proposal(cap.id)

    def observe(self, task_id: str, capabilities: list[Capability], *, success: bool, elapsed_ms: float):
        with self._db() as db:
            for cap in capabilities:
                db.execute('''INSERT INTO capability_observations
                (task_id,capability_id,success,elapsed_ms,observed_at) VALUES (?,?,?,?,?)''',
                           (task_id, cap.id, int(success), max(0, elapsed_ms), time.time()))

    def performance(self, cap_id: str) -> dict:
        with self._db() as db:
            row = db.execute('''SELECT COUNT(*), SUM(success), AVG(elapsed_ms)
                FROM capability_observations WHERE capability_id=?''', (cap_id,)).fetchone()
        n = row[0]
        return {'calls': n, 'success_rate': (row[1] or 0) / n if n else None,
                'mean_elapsed_ms': row[2]}
