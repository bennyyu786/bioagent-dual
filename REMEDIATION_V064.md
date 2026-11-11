# v0.6.4 — Autonomous Remediation Orchestrator

This is a **bounded orchestration and recovery handoff**, not unconditional autonomous repair of arbitrary third-party code.

## Components

- `FailureDiagnosisEngine`: provides a deterministic, audited diagnosis per task attempt.
- `AutonomousRemediationOrchestrator`: persistent SQLite job state, per-task remediation budget, routing, approval wait, crash reconciliation and auditable transitions.
- `RuntimeRepairPlanner`: optional Claude/Codex AgentRuntime that proposes JSON `DAGPatch` only. It has no write-capable tools.
- `DAGVersionManager`: validates and commits recovery tasks and dependencies; original failed work cannot resume before independent repair task completion.
- `AutonomousBuildCoordinator`: existing Skill/MCP discovery, candidate generation, sandbox testing and **human approval**; this orchestrator does not bypass it.
- `AdaptiveController(remediation_orchestrator=...)`: opt-in automatic handoff from task diagnosis to the durable remediation job.

## Routing contract

| Diagnosis | Action | Boundary |
| --- | --- | --- |
| `retry_worker` | Recognize a TaskCenter `PENDING` retry | TaskCenter owns attempt count; no duplicate call |
| `repair_execution` | Request bounded, declarative repair task | Revision requires valid DAG, **human approval of repair tasks**, and subsequent result validation |
| `replan_task` | Request new recovery task and recovery dependency | No direct change to completed/running task |
| `review_result` | Request explicit independent review task | Outcome assessor / validator must enforce scientific acceptance |
| `repair_capability` | Request a Skill/MCP candidate using existing autonomous builder | Sandbox validation + named human publication approval |
| `ask_human` | Block remediation | No blind retry with unknown external effects |

A remediation job is keyed by `(task_id, attempt)` and carries `PROPOSED`, `WAITING_CAPABILITY`, `WAITING_APPROVAL`, `APPLIED`, `BLOCKED`, or `REJECTED` state. The coordinator blocks repairs when the shared TaskCenter task is quarantined. Task recovery is not guaranteed if upstream builder state is ambiguous.

## Integrating into the rolling controller

```python
from biomni_dual import (
    AdaptiveController, AutonomousRemediationOrchestrator,
    RuntimeRepairPlanner, FailureDiagnosisEngine,
)

# Use the *same SQLite path* as TaskCenter for consistent audit and recovery edges.
remediation = AutonomousRemediationOrchestrator(
    tasks,
    database='research.sqlite',
    repair_planner=RuntimeRepairPlanner(claude_or_codex_runtime),
    capability_builder=existing_autonomous_build_coordinator,
    adaptive_store=adaptive_store,
    goal_id=goal_id,
    max_per_task=3,
)
controller = AdaptiveController(
    goal_id=goal_id, store=adaptive_store, tasks=tasks,
    planner=rolling_planner, worker=worker, validator=independent_validator,
    failure_diagnosis=FailureDiagnosisEngine('research.sqlite'),
    remediation_orchestrator=remediation,
)
```

`repair_execution` automatically marks generated repair steps as `WAITING_APPROVAL`; an operator must use `TaskCenter.approve(recovery_task_id)` before execution. Only enable model-driven repair after configuring a real independent result validator and an isolated executor. The SDK/CLI integration and Docker/MCP live acceptance remain deployment-dependent.

To advance a capability job, call the **builder's** existing human approval API; then `await remediation.reconcile(job_id)`. This does not grant the model publishing permission. `WAITING_APPROVAL` never auto-approves.

## Reliability and safety

- One persisted remediation job per task attempt; conflicting replay rejected.
- Task attempts and remediation counts have separate, explicit bounds.
- Remote effects with unknown status require operator reconciliation.
- Task patches are validated by `DAGVersionManager`; goal scoping prevents cross-goal recovery dependencies.
- A crash after DAG commit can be reconciled from the persisted `replanned` event, and missing goal binding is repaired without a duplicate DAG node. A crash after task diagnosis but before remediation submission is also reconciled on the next `AdaptiveController.tick()`.
- Planner exceptions and invalid proposals block the remediation; they do not cause uncontrolled retry.
- Existing audit hash chain records routing state transitions, without embedding raw prompts or secrets.
- A repairing task's own outcome must pass independent result validation; a successful planning action does not prove the scientific repair worked.

## Test and demo

```bash
python -m pytest -q
python examples/remediation_demo.py
```

The demo is deterministic and uses fabricated outputs; it does **not** establish a live Claude/Codex, Docker, MCP, or Biomni A1 security/quality acceptance.
