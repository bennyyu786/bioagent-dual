"""Deterministic, conservative capability feasibility and coverage evaluation."""
from __future__ import annotations
from dataclasses import dataclass
from .models import Capability
from .task_requirements import TaskRequirements

@dataclass(frozen=True)
class FitEvidence:
    capability_id: str
    feasible: bool
    covered: tuple[str, ...]
    reasons: tuple[str, ...]
    quality: float
    cost: float
    risk: float


def evaluate(cap: Capability, task: TaskRequirements) -> FitEvidence:
    meta = cap.metadata or {}
    reasons = []
    if not cap.healthy: reasons.append('unhealthy')
    if cap.approval_required and not meta.get('approved', False):
        reasons.append('approval_required')
    lic = str(meta.get('license') or '')
    if task.allowed_licenses and lic not in task.allowed_licenses:
        reasons.append('license_mismatch')
    permissions = meta.get('permissions', [])
    if not isinstance(permissions, (list, tuple)) or not all(isinstance(x, str) for x in permissions):
        reasons.append('invalid_permissions')
    elif not set(permissions).issubset(set(task.allowed_permissions)):
        reasons.append('permission_mismatch')
    for field in ('input_schema', 'output_schema'):
        need = getattr(task, field)
        provided = meta.get(field)
        # Unknown compatibility fails closed when the task specifies a schema.
        if need and provided != need:
            reasons.append(field + '_mismatch')
    risk = meta.get('risk', 0.0)
    if not isinstance(risk, (float, int)) or not 0 <= risk <= 1:
        reasons.append('invalid_risk')
        risk = 1.0
    if risk > task.max_risk: reasons.append('risk_exceeded')
    if not 0 <= cap.quality <= 1 or cap.quality < task.min_quality:
        reasons.append('quality_below_minimum')
    if cap.cost < 0: reasons.append('invalid_cost')
    provides = meta.get('provides', ())
    if not isinstance(provides, (list, tuple, set)) or not all(isinstance(x, str) for x in provides):
        provides = ()
        reasons.append('invalid_provides')
    # Capability ID itself is an exact capability, and aliases must be explicit.
    present = set(provides) | {cap.id}
    covered = tuple(n.id for n in task.needs if n.id in present)
    return FitEvidence(cap.id, not bool(reasons), covered if not reasons else (),
                       tuple(sorted(set(reasons))), cap.quality, cap.cost, float(risk))
