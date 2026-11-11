# v0.5.2 Task-first Capability Matching

The **primary objective is task fit, not catalog size**. This release adds deterministic, testable matching before the v0.5.1 federated marketplace and autonomy pipeline.

## Policy and workflow

1. Extract explicit requirements into `TaskRequirements` from a reviewed task. An LLM can propose requirements, but they **must be validated and reviewed**; this package does not guess safety-sensitive requirements from free text.
2. Evaluate **internal registered, healthy** capabilities first. Each capability may declare `metadata.provides`, `metadata.input_schema`, `metadata.output_schema`, `metadata.permissions`, `metadata.license`, and `metadata.risk`.
3. Reject incompatible schemas, licenses, permissions, inadequate quality, excessive risk, unapproved capabilities and broken dependency chains *before* optimization.
4. Choose a feasible **minimum-cost sufficient set**, counting transitive dependencies only once. For up to 15 candidate providers, perform deterministic exhaustive search; for more, use a deterministic greedy heuristic. It is not a global optimum guarantee in the greedy case.
5. Only uncovered requirements create `AutonomousBuildCoordinator.request()` entries via `TaskFitCoordinator.request_gaps()`. Marketplace calls require `search=True`; search only returns untrusted candidates. **No automatic installation, execution, activation or publication.**
6. If needs are individually covered but their combination exceeds budget, return `blocker=insufficient_budget_or_combination`, not redundant marketplace requests. The caller should consider budget changes or task decomposition.
7. After testing and approval through the existing builder, re-run matching against the updated registry. Only disclose the selected capabilities to the Worker.

## Metadata contract

```python
Capability('qc','QC tool',CapabilityKind.SKILL,metadata={
    'provides':['variant.qc'],
    'input_schema':'vcf.v1',
    'output_schema':'qc.v1',
    'permissions':[],
    'license':'MIT',
    'risk':0.1,
})
```

If a task specifies a schema or license restriction, **missing metadata is not assumed compatible**. Capability quality, cost, permissions, provenance and risk are declarations; they must come from trusted validation rather than external market marketing text.

## API

```python
from biomni_dual import TaskRequirements, Requirement, TaskFitCoordinator

requirements = TaskRequirements('task-1',(Requirement('variant.qc'),),
    input_schema='vcf.v1', allowed_licenses=('MIT',), max_cost=10)
coordinator = TaskFitCoordinator(registry, autonomy)
result = coordinator.evaluate(requirements)
if not result.sufficient:
    response = coordinator.request_gaps(requirements,
        kinds={'variant.qc': 'skill'}, search=True)
    # Review discovered options; build in sandbox and approve before publication.
```

This is a controlled orchestration API, **not yet an autonomous free-text semantic requirements extractor**, and the main scheduler keeps its existing path for backward compatibility. To use task-first matching, call the new gateway or `DualCenterWorkflow.assess_task_capabilities()` explicitly.

## Limits

- Cross-capability input/output **wiring** and scientific correctness are not validated by static schema equality.
- `min_coverage < 1` can designate optional needs; unmet optional needs remain in `gaps` but do not cause marketplace search when coverage threshold is met.
- Market candidates cannot satisfy a requirement until independently validated and registered.
- Docker and live Claude/Codex/MCP integrations remain separately deployable and were not exercised here.

## Opt-in integration with ParallelScheduler

```python
scheduler = ParallelScheduler(
    tasks, registry, workers, resources,
    task_fit_factory=lambda task: TaskRequirements.from_task(
        task, max_cost=5, allowed_licenses=('MIT',),
        allowed_permissions=(), min_coverage=1.0,
    ),
)
```

The scheduler uses the chosen minimal bundle. A missing need goes to the existing task-approval path, where the operator can initiate `TaskFitCoordinator.request_gaps()`. Budget/combination infeasibility emits `capability.budget_blocked` and leaves the task pending; it does not start gratuitous market discovery. This is **opt-in** for compatibility with existing v0.5.1 scheduling semantics.
