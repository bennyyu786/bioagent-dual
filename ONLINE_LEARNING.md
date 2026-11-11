# Biomni Dual Center v0.6.2: online value calibration

## Purpose

Use **independently assessed real execution outcomes** to calibrate later action choices. This is a constrained statistical update, not reinforcement learning, and is not a guarantee of causal benefit, scientific accuracy or generalization.

## Data flow

1. A Planner proposes an `ActionOption` with a fixed evidence reference and initial forecasts.
2. `OnlineLearningStore.predict(PredictionRecord(...))` optionally records the forecast **before** execution. This permits later prediction-error comparisons; callers must ensure the forecast precedes execution.
3. The execution passes independent syntax and domain-quality validation. A trusted domain evaluator constructs `QualityEvidence` and the configured `QualityPolicy` returns `OutcomeAssessment`.
4. `OnlineLearningStore.observe(key, context, assessment)` records validated results. A unique idempotency key binds one attempt; conflicting replays fail.
5. `OnlineLearningStore.estimate(context)` computes a Beta(1,1) smoothed success rate and empirical progress, quality, cost, measured information gain (if available), and forecast error (only if a prior prediction was recorded).
6. `CalibratedValuePolicy` shrinks prior action forecasts toward observations with `n/(n+prior_strength)`, discounts expected progress by the smoothed success rate and optionally adds a strictly bounded exploration bonus.
7. `LearnedValueGuidedPlanner` passes its chosen `PlanProposal` to the existing `AdaptiveController` for normal DAG validation and transaction commit.

## Context and leakage

`LearningContext(task_kind, capability_ids, worker_id)` requires stable taxonomy identifiers. Exact-context lookup prevents accidental transfer between scientific tasks and between competing tools/Workers. Learning across goals is deliberate; use separate DBs/tenant-scoped task kinds if cross-project evidence sharing is not appropriate. Do not store raw protected datasets or secrets in evidence identifiers.

## Minimal usage

```python
from biomni_dual import (
    OnlineLearningStore, LearningContext, PredictionRecord,
    CalibratedValuePolicy, ActionOption,
)

store = OnlineLearningStore('experience.sqlite')
ctx = LearningContext('variant_qc', ('clinvar_lookup',), 'biomni')
option = ActionOption('explore', 'inspect', 0.8, 0.4, 0.0, 1.0, 0.1,
                      evidence_ids=('planner:reference-1',))
store.predict(PredictionRecord('goal:task:attempt-1', ctx, option))
# After independent domain assessment:
# store.observe('goal:task:attempt-1', ctx, assessment)
policy = CalibratedValuePolicy(store, prior_strength=4, exploration_bonus=0.02)
ranked = policy.rank([(option, ctx)], remaining_cost=10, max_risk=0.3)
```

## AdaptiveController integration

Supply `online_learning_store=OnlineLearningStore(db)` and `learning_context=lambda task, ids: LearningContext('variant_qc',tuple(sorted(ids)),'biomni')`, **together with** `quality_policy` and `outcome_evaluator`. The controller records a validated outcome under `goal_id:task_id:attempt`; it does not infer an outcome for a worker exception or simple nonempty text. Thus, API errors and unverified executions are not automatically treated as failed scientific trials in the learned success rate. This is deliberate but introduces selection bias; add independently assessed failure outcomes at the appropriate layer before treating these rates as global success probabilities.

## Safety and limitations

- No new agent execution rights. Original budget, approval, feasibility, permission and risk gates remain authoritative; calibration never lowers declared risk.
- Missing uncertainty measurements remain `None`. Negative observed information gain is stored as negative; action ranking clamps its calibrated predictor at zero because the existing `ActionOption` schema requires [0,1].
- Historical observed cost is used conservatively: a candidate failing the **original** cost bound remains excluded. Higher calibrated expected costs may also exclude it.
- Historical signals are observational, can drift, and may be confounded by task difficulty and selection. Do not interpret ranking improvements as causal efficacy.
- Beta-smoothed empirical acceptance and mean progress are heuristics. They are not calibrated confidence intervals, contextual bandits, or distribution-shift detection.
- Requires honest, independent domain evaluation and durable evidence references. Passing fabricated `QualityEvidence` would poison learning; source authentication is a deployment concern.
- SQLite works for one coordinator. Distributed writers, tamper-resistant external audit storage and production Worker isolation remain future tasks.

## Validate

```bash
python -m pytest -q
python examples/online_learning_demo.py
```
