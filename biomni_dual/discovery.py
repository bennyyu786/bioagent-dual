"""Non-executing capability discovery, candidate preparation and approval.

Catalogs must be explicitly supplied by the operator. No arbitrary network search,
package download, MCP startup, script execution, or trust-on-first-use.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from .models import Capability, CapabilityKind
from .capabilities import CapabilityCenter
from .registry import CapabilityRegistry

@dataclass(frozen=True)
class Candidate:
    capability: Capability
    source: str
    sha256: str
    documentation: str

class DiscoveryAgent:
    def __init__(self, catalog: list[Candidate]):
        self.catalog = {item.capability.id: item for item in catalog}

    def find(self, capability_id: str) -> Candidate | None:
        return self.catalog.get(capability_id)

class BuilderAgent:
    """Writes an inert build proposal. Does not install/execute skill or MCP code."""
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(self, capability_id: str, description: str, *, kind: CapabilityKind = CapabilityKind.SKILL) -> Path:
        if not capability_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-' for c in capability_id):
            raise ValueError('Invalid capability identifier')
        path = self.root / (capability_id + '.proposal.json')
        payload = {'id': capability_id, 'kind': kind.value, 'description': description,
                   'state': 'candidate_only', 'executed': False, 'approved': False}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
        return path

class CapabilityBuildFlow:
    def __init__(self, center: CapabilityCenter, discovery: DiscoveryAgent, builder: BuilderAgent):
        self.center, self.discovery, self.builder = center, discovery, builder

    def request(self, capability_id: str, reason: str) -> Candidate | Path:
        self.center.propose_gaps([capability_id], reason=reason)
        return self.discovery.find(capability_id) or self.builder.prepare(capability_id, reason)

    def publish_candidate(self, candidate: Candidate, *, verified_digest: str, operator_approved: bool) -> None:
        """Caller independently verifies exact artifact digest; approval is explicit.

        This verifies declared digest equality only, not code safety or signature validity.
        """
        if not operator_approved:
            raise PermissionError('Operator approval required')
        if not candidate.sha256 or verified_digest != candidate.sha256:
            raise PermissionError('Independent artifact verification required')
        self.center.review(candidate.capability.id, approve=True)
        self.center.publish(candidate.capability, verified=True)

    @staticmethod
    def candidate_from_text(capability: Capability, documentation: str, source: str) -> Candidate:
        return Candidate(capability, source, hashlib.sha256(documentation.encode()).hexdigest(), documentation)
