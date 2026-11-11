"""Minimal sufficient set selection with hard constraints and dependency closure."""
from __future__ import annotations
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable
from .models import Capability
from .registry import CapabilityRegistry
from .task_requirements import TaskRequirements
from .capability_matcher import FitEvidence, evaluate

@dataclass(frozen=True)
class SelectionResult:
    task_id: str
    selected: tuple[str, ...]
    gaps: tuple[str, ...]
    coverage: float
    sufficient: bool
    total_cost: float
    evidences: tuple[FitEvidence, ...]
    blocker: str = ""

    @property
    def search_external(self) -> bool:
        return not self.sufficient


def select_for_task(registry: CapabilityRegistry, task: TaskRequirements,
                    *, candidate_ids: Iterable[str] | None = None) -> SelectionResult:
    all_caps = {c.id: c for c in registry.list()}
    ids = sorted(set(candidate_ids)) if candidate_ids is not None else sorted(all_caps)
    base = {cid: evaluate(all_caps[cid], task) for cid in ids if cid in all_caps}
    feasible: dict[str, set[str]] = {}
    closures: dict[str, set[str]] = {}

    def closure(cid: str, trail: set[str]) -> set[str] | None:
        if cid in trail: return None
        cap = all_caps.get(cid)
        if cap is None: return None
        ev = evaluate(cap, task)
        if not ev.feasible: return None
        out = {cid}
        for dep in cap.requires:
            result = closure(dep, trail | {cid})
            if result is None: return None
            out |= result
        return out

    for cid, ev in base.items():
        if not ev.feasible or not ev.covered: continue
        deps = closure(cid, set())
        if deps is not None:
            feasible[cid] = set(ev.covered)
            closures[cid] = deps

    weights = {n.id: n.weight for n in task.needs}
    total_weight = sum(weights.values())
    ids = sorted(feasible)
    def score(selection):
        covered = set().union(*(feasible[i] for i in selection)) if selection else set()
        actual = set().union(*(closures[i] for i in selection)) if selection else set()
        cost = sum(all_caps[i].cost for i in actual)
        fraction = sum(weights[i] for i in covered) / total_weight
        # Minimize installed capabilities and duplication after cost; prefer quality.
        quality = sum(all_caps[i].quality for i in actual)
        return fraction, cost, len(actual), -quality, tuple(sorted(actual)), covered

    best = None
    if len(ids) <= 15:
        for count in range(len(ids) + 1):
            for combo in combinations(ids, count):
                fraction, cost, size, nq, actual, covered = score(combo)
                if fraction + 1e-10 < task.min_coverage or cost > task.max_cost:
                    continue
                rank = (cost, size, nq, actual)
                if best is None or rank < best[0]: best = (rank, actual, covered, fraction, cost)
    else:
        # Deterministic greedy upper-bound selection for larger catalogs.
        chosen = set()
        remaining = set(ids)
        while remaining:
            current_fraction = score(chosen)[0]
            options = []
            for item in remaining:
                proposed = chosen | {item}
                frac, cost, _, _, _, _ = score(proposed)
                if cost <= task.max_cost and frac > current_fraction:
                    options.append((-(frac-current_fraction)/max(cost-score(chosen)[1], 1e-9), item))
            if not options: break
            _, item = min(options)
            chosen.add(item); remaining.remove(item)
            frac, cost, size, nq, actual, covered = score(chosen)
            if frac >= task.min_coverage:
                best = ((cost,size,nq,actual), actual,covered,frac,cost)
                break
    if best:
        _, actual, covered, fraction, cost = best
    else:
        # Partial best effort for gap reporting; do not activate it as a complete solution.
        available = set().union(*feasible.values()) if feasible else set()
        # Distinguish available functional coverage from an affordable complete set.
        # A capability whose dependency closure already exceeds the budget cannot
        # satisfy even part of this task. Do not search for covered needs twice.
        affordable = [i for i in ids if sum(all_caps[x].cost for x in closures[i]) <= task.max_cost]
        covered = set().union(*(feasible[i] for i in affordable)) if affordable else set()
        if sum(weights[i] for i in covered) / total_weight >= task.min_coverage:
            # Global resource contention: all individually feasible needs exist,
            # but no joint set fits. This is a budget gap, not a market gap.
            covered = set(weights)
        fraction = sum(weights[i] for i in covered) / total_weight
        actual = ()
        cost = 0.0
    gaps = tuple(n.id for n in task.needs if n.id not in covered)
    blocker = '' if best else ('insufficient_budget_or_combination' if not gaps else 'capability_gap')
    return SelectionResult(task.task_id, tuple(actual), gaps, fraction,
                           best is not None, cost, tuple(base[k] for k in sorted(base)), blocker)
