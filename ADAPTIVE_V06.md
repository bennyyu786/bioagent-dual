# v0.6.0 — Open-ended adaptive task/capability co-evolution

## What is implemented

The new `biomni_dual/adaptive.py` implements a **bounded rolling-control loop** on top of the existing v0.5.2 TaskCenter, DAGVersionManager, TaskFitCoordinator, capability market/builder, and reliability infrastructure. The planner is invoked with the currently observed world state and proposes a small graph delta instead of a one-shot full-task plan.

- **World state & experience memory:** `AdaptiveStore` persists goals, goal-to-task membership, idempotent observation evidence, cycle budgets, and decisions in SQLite; the existing AuditLog keeps a hash chain of mutation events.
- **Rolling planner:** `CallbackPlanner` for deterministic tests, `AgentRollingPlanner` for actual authenticated Claude/Codex `AgentRuntime.run()` adapters. The Agent receives a limited snapshot and returns a typed `PlanProposal`. It **cannot directly write tasks** or install Skills.
- **Task evolution:** `AdaptiveController` checks proposals, `DAGVersionManager` commits versioned patches transactionally. New tasks may depend on old task IDs or earlier new keys. A recovery patch may atomically insert a repair step and reset one failed task with a new prerequisite. Existing running/completed tasks cannot have their dependencies rewritten.
- **Capability evolution:** the controller delegates to the existing `TaskFitCoordinator`. Internal task-fit is attempted first; if explicit needs are uncovered, idempotent requests go to `AutonomousBuildCoordinator`. Docker testing, human approval, and publication still occur in the *existing* v0.5.x builder, not inside the planner. A cost/combination problem blocks, instead of triggering extra marketplace searches.
- **Task/capability mapping:** the controller uses the selected minimal sufficient capability set, not all registered tools. It supplies only that set to the Worker. This is a deterministic mapping policy, not an ML-learned global optimization.
- **Result validation:** a pluggable `ResultValidator` runs **before** `TaskCenter.finish(... success ...)`. Rejected results become failed task states and evidence; errors are routed through the existing retry classifier.
- **Feedback and bounded action:** validation observations are persisted, and optional `CapabilityCenter.observe()` receives success and elapsed time. The next planning step can react to `environment_change` observations. `max_cycles`, `max_plans`, and one-recovery-per-failed-task provide finite stopping limits.

`NonEmptyValidator` exists for smoke tests only. Production biomedical quality requires dedicated tests for cohort alignment, data leakage, reproducibility, statistical assumptions, causal identification, etc.

## Actions

`explore` and `replan` propose a DAG patch. `execute` performs one ready task and validates its result. `build_capability` requests only uncovered Skill/MCP needs; it stops the running tick loop pending independent validation/approval. `validate` reports a failed result check, and `ask_human` blocks uncertain state/budget/approval. `finish` means **all currently known tasks** passed validation; it does **not** prove that an open-ended research objective has been scientifically achieved.

## Usage

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python examples/adaptive_demo.py
```

To use a real LLM planner, provide an authenticated `ClaudeSDKRuntime` or `CodexSDKRuntime` to `AgentRollingPlanner(runtime)`. For real execution, provide a Worker adapter exposing `async run(task, selected_capability_ids) -> str`; the existing SDK/CLI workers may need a thin wrapper. For each goal use `AdaptiveStore.create_goal` and `AdaptiveController`; bind any existing tasks with `AdaptiveStore.bind_tasks`, otherwise let the planner propose new tasks. For real capability creation, inject `TaskFitCoordinator(registry, autonomy=AutonomousBuildCoordinator(...))` and configure its Docker/Supervisor pipeline. Do not use the demo's simulated manual registry update in production.

## Limitations and safety

- This is a **single-coordinator SQLite engine**, not distributed HA. Do not run two controllers concurrently on the same goal. An external crashed Worker with uncertain side effects must be reconciled using the prior SessionStore/TaskCenter recovery mechanisms; there is no exactly-once guarantee.
- `AgentRollingPlanner` is an integration adapter; this environment's tests use fake runtimes. Live authenticated SDK calls, real Biomni A1, Docker, and real MCP services require a separate deployment acceptance run.
- Dynamic goal completion remains based on known task acceptance plus policy; domain-level research completion should be an independent human/scientific gate.
- Validator checks need to be supplied for each scientific workflow. Never assume nonempty output is true or causally correct.
- The external marketplace remains discovery-only; unverified candidates cannot be executed/published automatically.
- Project-wide budgets for cost/tokens and multi-coordinator distributed coordination are not implemented. `max_cycles`, `max_plans`, and existing resource constraints are partial safeguards.
- Keep sensitive raw research data out of planner prompts and observation summaries. Audit details intentionally omit large raw data.

## Offline acceptance coverage

See `tests/test_adaptive.py`: rolling exploration/replanning, environment-triggered planning, scientific-validation rejection, retry taxonomy, capability gap-only build requests, no external discovery for sufficient internal capabilities, goal isolation, cross-goal DAG rejection, observation idempotency/audit, restart state, uncertain active task blocking, cycle/planning budgets, LLM proposal schemas, atomic failed-task recovery, and rollback on a cyclic recovery patch.
