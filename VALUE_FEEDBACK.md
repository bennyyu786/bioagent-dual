# v0.6.1: Evidence-grounded value feedback

The new subsystem does not equate successful execution with scientific success.

## Decision pipeline

1. The task Planner supplies a *bounded list* of possible next planning actions and estimated progress, information gain, reuse value, cost, risk, and a reference to its reasoning/evidence.
2. `ValuePolicy.rank()` filters out infeasible, over-budget and over-risk actions **before** sorting; `ValueGuidedPlanner` selects one proposed `explore`, `replan` or `ask_human` action. Existing `AdaptiveController` DAG checks still control commit.
3. The Worker runs the task and a structural/domain `ResultValidator` checks the output.
4. If configured, a **separate** `outcome_evaluator` supplies typed `QualityEvidence` from domain-specific scientific checks and reproducible artifacts, then `QualityPolicy` applies hard quality, reproducibility, progress and risk gates. Validation failures prevent `COMPLETED`.
5. `ExperienceStore` persists accepted/rejected assessments, evidence references, proxy utility and measured information gain in SQLite, with idempotent writes and audit trail. Existing `AdaptiveStore` also records the evaluation observation.

## Example setup

```python
from biomni_dual import QualityPolicy, ExperienceStore, AdaptiveController

controller = AdaptiveController(
    goal_id=goal_id, store=adaptive_store, tasks=task_center,
    planner=planner, worker=worker, validator=result_validator,
    quality_policy=QualityPolicy(min_quality=.8, min_reproducibility=.8),
    outcome_evaluator=domain_evaluator,
    experience_store=ExperienceStore(database_path),
)
```

`domain_evaluator` must implement `async evaluate(task, output) -> QualityEvidence`.
`evidence_ids` must reference durable evidence stored elsewhere; their validity cannot be proven from their presence alone. An evaluator error fails closed (no task completion). Uncertainty reduction is only computed if both measurements were supplied; negative information gain is permitted. Numeric scores are **operational proxies**: they are not calibrated causal estimates and do not prove research findings.

## Important limits

- Action estimates and relative utility values are heuristics, not learned optimal policies. Information gain forecasts cannot be verified until new observations arrive.
- The demonstration uses explicit artificial QC fixtures, not real biomedical measurements.
- Controller is still single-coordinator SQLite. Execution-side effects remain uncertain if worker crashes; retain manual reconciliation boundary.
- Evaluation recording and task completion occur in separate database transactions. A crash between them can leave a running task and a recorded evaluation. A reconciliation operator must inspect the state; do **not** blindly rerun a side-effecting Worker.
- Avoid exposing private biomedical data or raw credentials in `evidence_ids` or action metadata.
- Enable the quality gates with a genuine domain evaluator. The baseline `NonEmptyValidator` alone is NOT sufficient to validate scientific findings.
