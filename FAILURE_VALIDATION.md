# v0.6.3: Failure diagnosis and evidence-aware validation

This version extends the **same** `biomni_dual/` source tree. No version-specific package directory is introduced.

## Operational sequence

1. A running task returns a result or an exception.
2. `EvidenceAwareValidator` asks a **separately controlled** `DomainAssessor` for domain-specific checks. It verifies each required check ID, checks the SHA-256 hash of referenced files on disk under a configured evidence root, rejects missing/duplicate checks and path traversal, and refuses completion when any check fails.
3. `AdaptiveController` passes execution or scientific-validation failures to `FailureDiagnosisEngine`, which atomically stores an idempotent diagnosis and appends a linked audit event.
4. The route is surfaced as a `Decision.evidence["route"]` value. The controller automatically retries **only** eligible transient failures, subject to both the task attempt limit and diagnosis budget. Other routes cause a terminal failed state for a planner/capability supervisor to review; **routes themselves do not grant authority** to install an MCP or edit a task DAG.
5. `UncertainSideEffectError` blocks the task (`BLOCKED`) and requires explicit reconciliation before retrying. This is deliberately stricter than ordinary transient failures.

## Routing table

| Failure | Route | Automatic retry? |
|---|---|---|
| Timeout, transient network, resource exhaustion, rate limit | `retry_worker` | Yes, under budgets |
| Missing dependency or broken declared capability | `repair_capability` | No; capability proposal/approval required |
| Invalid input or task specification | `replan_task` | No; planner validates and submits a DAG proposal |
| Failed independent result check or scientific quality gate | `review_result` | No; independent validation and corrective planning |
| Permission/authentication or cancellation | `ask_human` | No |
| Unknown error | `ask_human` | No |
| Uncertain remote side effect | `ask_human` + `BLOCKED` | Never without reconciliation |

## Basic integration

```python
from biomni_dual import FailureDiagnosisEngine, EvidenceAwareValidator

diagnosis = FailureDiagnosisEngine("research.sqlite", max_remediations=3)
validator = EvidenceAwareValidator(
    root="/isolated/validation-artifacts",
    assessor=your_independent_assessor,
    required_checks=("no_split_leakage", "fdr_verified", "reproducible_run"),
)

controller = AdaptiveController(
    ..., validator=validator, failure_diagnosis=diagnosis
)
```

A real assessor must compute its own scientific checks from trusted inputs; the worker cannot certify its own outputs simply by returning `passed=True`. Artifact hashes prove file integrity, **not scientific truth**. For high-risk tasks run the assessor in an isolated process/container with separate credentials and test inputs. The demonstration uses artificial evidence and should not be interpreted as validation of actual biomedical results.

## Persisted state and failure boundaries

- `failure_diagnoses(task_id, attempt)` holds an immutable diagnosis and source fingerprint, with an audit-log event in the same SQLite transaction.
- A conflicting replay of the same attempt is rejected; identical replays return the stored route.
- Per-task remediation count persists across process restarts; the default is three. Additional failures are escalated to human review.
- The existing `TaskCenter` still has its own `max_attempts`; neither budget bypasses the other.
- The system remains a **single-coordinator, SQLite-based** prototype. The diagnosis policy is deterministic; it is not an autonomous root-cause discovery model.
- `repair_capability`, `replan_task`, and `review_result` are **action proposals**, not an automatic end-to-end repair executor. Wiring to the existing approved Capability Builder / DAG replan flow still requires policy-specific handling.
- In-flight crash consistency across the task database and remote systems cannot provide exactly-once side effects. Existing session reconciliation remains mandatory.

## Validation commands

```bash
PYTHONPATH=. python -m pytest -q
PYTHONPATH=. python examples/failure_diagnosis_demo.py
```
