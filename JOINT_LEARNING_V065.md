# v0.6.5 — Joint task–capability state and repair-strategy learning

## Purpose

Move from diagnosing and *applying* a remediation to recording whether its **subsequent execution and independent evaluation** showed that the remediation worked. Do not equate `APPLIED` with scientific success.

The extension is implemented in the existing `biomni_dual/` package as `joint_learning.py`. v0.6.4 orchestration, registries, and approval boundaries remain compatible.

## Components

- `RepairContext`: explicit, exact task kind / failure reason / capability ID scope. There is no cross-domain transfer by default.
- `JointRemediationController.snapshot()`: joins task status, required capability health, remediation states and observed repair count in a read-only view.
- `JointExperienceStore`: immutable repair outcome facts, duplicate-safe keys, SHA256 fingerprint and transactionally appended audit events.
- `RepairStrategyPolicy`: Beta(1,1) smoothed acceptance-rate comparison. Requires minimum observed sample counts, explicit allowed alternative route, and minimum improvement margin. Can also reject candidates on measured cost. Observational estimates are **not causal comparisons**.
- `JointRemediationController`: bridges existing `TaskCenter`, `AutonomousRemediationOrchestrator` and independent evidence. It never executes generated code or grants permissions.

## Safety gates

1. Route `ASK_HUMAN` and uncertain side effects are never overridden with automatic execution.
2. Only `REPAIR_EXECUTION → REPLAN_TASK` and `REPAIR_CAPABILITY → REPLAN_TASK` are eligible alternative recommendations in this version; no other routes are broadened.
3. A recommended alternative still needs explicit authorization (`approved_alternative`) at submission. Otherwise the previous deterministic route is used.
4. Learning is admitted only if remediation has state `APPLIED`, the original task has a **new terminal post-repair execution**, and the outcome agrees with its state.
5. Validation evidence must come from an operator-controlled path beneath `evidence_root`, match the supplied SHA256, and identify a verifier in `trusted_verifiers`. Configure the root as read-only to the Agent/Worker. A SHA256 checksum only checks file consistency, not truthfulness or quality.
6. No unknown or unrelated domain's history is silently reused. Missing observations retain the original route.
7. Existing sandbox, permission, build approval, and remediation budget checks still apply downstream.

## Application example

```python
from biomni_dual import (
    JointExperienceStore, JointRemediationController, RepairStrategyPolicy
)

store = JointExperienceStore("state.sqlite")
policy = RepairStrategyPolicy(store, min_samples=4, improvement_margin=0.15)
controller = JointRemediationController(
    task_center, remediation_orchestrator, store,
    capabilities=capability_registry, policy=policy,
    evidence_root="/separate-validator/reports",
    trusted_verifiers=("independent-lab-validator",),
)

advice = controller.recommend(diagnosis, task_kind="longitudinal_omics")
# No automatic override; human/system policy must grant explicit route approval.
job = controller.submit(diagnosis, task_kind="longitudinal_omics")
# ... existing orchestrator advances job, Worker re-executes task,
# independent validator writes evidence to its trusted directory ...
# controller.record_verified(RepairOutcome(...))
```

### Important operational limitations

- Per-route success estimates are simple observational summaries. Confidence, data drift, sample selection, and workload differences need more evaluation before enabling automatic policy adjustments.
- `RepairOutcome` is a structured assertion backed by an independently protected file and a verifier allowlist, **not a digital signature or remote attestation**. Administrator must isolate the validator filesystem and its credentials from the Worker.
- SQLite is suitable for a single coordinator. Distributed multi-tenant operation requires leases, an external event log and stronger identity/authorization.
- Live Claude/Codex, Docker/MCP and Biomni A1 integrations were not verified here.

## Verify

```bash
python -m pytest -q
python examples/joint_learning_demo.py
```
