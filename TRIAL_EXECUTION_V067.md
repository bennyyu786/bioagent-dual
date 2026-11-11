# v0.6.7 — Experiment execution and pilot learning

## Purpose

This is a direct mainline upgrade of v0.6.6, with no version-specific source packages.
The previously planning-only `ActiveExperimentStore` is now connected to a
`TrialExecutor` backed by the existing `DockerSandbox` runtime, using approved
and pinned candidate directories. The experiment records a trial only after a
trusted independent verifier approves the sandbox outcome.

## Components

- `TrialExecutor`: approved assignment → pinned skill/MCP artifact → Rootless Docker validation → independent checker → immutable JSON evidence → `ActiveExperimentStore.record`.
- `TrialExecutionLedger`: SQLite atomic claim before invoking an external sandbox; exact-once **claim**, not exactly-once external execution. Unresolved executions remain blocked.
- `PilotLearningView`: read-only Beta(1,1) estimates from trial data. Segregated from `JointExperienceStore` production repair outcomes.
- `TrialExecutionReceipt`: evidence and SHA256 tracking.

## Key guarantees and boundaries

- No trial starts before explicit human experiment approval.
- No generated shell commands are executed by the orchestration controller. Docker receives only fixed test commands and read-only candidate mounts.
- A candidate's file digest is pinned; modifying it requires new review and a newly constructed executor.
- Rootless Docker (a separate dedicated daemon), no-network, non-root UID, no-new-privileges, read-only root, and CPU/memory/PID quotas are expected for real execution.
- The independent checker is supplied by the trusted operator, not by the Agent. It cannot declare success when the sandbox validation fails.
- An unresolved Docker error or coordinator crash is marked **uncertain**, never silently treated as a failed experiment or retried.
- If the trial's independent result was persisted before the coordinator crashed, a human can reconcile that record using `reconcile_recorded` after reviewing the state.
- Zero *monetary* cost is entered as a placeholder for the current sandbox run; elapsed time is measured. Do not interpret zero as a measured market price. Budget reservations are managed by `ActiveExperimentStore`.
- The current Docker validation is a **candidate conformance trial**, not full paired evaluation on distinct real scientific datasets. Repeating the same fixture does not give statistically independent observations. Reports remain pilot-only and never authorize production rollout.
- A Docker worker can run a candidate authored with Claude Agent SDK or Codex SDK via the existing `CapabilityAgent.propose()`, but generation must happen before candidate approval and hash pinning. Running writable Claude/Codex processes within this controller is not supported.
- Containerized checks and signed trusted verification are still insufficient alone to establish scientific correctness. Independent domain validators remain necessary.

## Tests

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python examples/trial_execution_demo.py
```

`trial_execution_demo.py` uses an injected fake Docker transport (no daemon).
For live sandbox validation, install the sandbox image and connect to a dedicated
Rootless Docker daemon, then configure `TrialExecutor` without a fake runner.
No real Docker daemon, authenticated Claude/Codex session, or scientific data
were available in the authoring environment. **Live integration is unverified.**

## Next implementation targets

1. Unit-specific immutable test fixtures and paired experimental assignments.
2. Separate worker service for authenticated Claude/Codex candidate generation, with per-candidate approval.
3. Docker completion receipts from an external trusted worker for crash recovery.
4. Calibrated monetary and energy accounting; scientific domain validation.
