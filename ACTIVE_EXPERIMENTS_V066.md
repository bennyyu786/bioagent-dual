# v0.6.6: Active experiments and controlled strategy comparisons

This release is a **direct upgrade of the unified v0.6.5 source package**. There are no versioned Python subpackages.

## What is implemented

- `ActiveExperimentCoordinator.propose_for_failure` turns a precisely identified capability/execution repair diagnosis into a **proposal** to compare the default route with task replanning. Uncertain side effects, permission failures, and scientific review routes are ineligible.
- `ExperimentPriorityPolicy` consults the previous `JointExperienceStore` for an **uncertainty-based pilot priority**, without writing to history or changing the production remediation route. It refuses experiments when their budget is insufficient, permissions are missing, or external effects are uncertain.
- `ActiveExperimentStore` persists protocol-fixed proposals, explicit human approval, bounded randomly balanced arm assignments, strict trial cost reservation and independently attested outcomes in SQLite with tamper-evident audit entries. Assignments survive process restarts and duplicate requests are idempotent.
- `ComparisonReport` computes counts, observed rates, Wilson 95% intervals, observed risk difference, mean costs and a deliberately conservative, exploratory pilot conclusion. **It never authorizes deployment or changes `RepairStrategyPolicy`.**

## Architecture

```text
FailureDiagnosisEngine -> JointRemediationController (existing production path)
                       -> ActiveExperimentCoordinator (optional proposal only)
                            -> ExperimentPriorityPolicy (history + gates)
                            -> ActiveExperimentStore (SQLite + audit)
                            -> HUMAN REVIEW
                            -> shadow/sandbox randomized allocations
                            -> independently generated test evidence
                            -> pilot comparison report (NO automatic rollout)
```

The store **does not execute** sandbox trials, create Docker containers, issue SDK commands, install Skills/MCP, modify DAGs, or grant any capability. A separate, trusted runtime must run the allocated candidate in a properly isolated, permission-scoped environment. Mock fixtures are used for automated tests and examples. Do not connect real clinical data to this example.

## Safety controls

1. Only `SHADOW` and `SANDBOX` experiments are accepted. External side effects and sensitive data are forbidden. A real isolated environment must enforce these rules; metadata is not a security boundary.
2. Each protocol fixes the task/capability context, strategy arms, version, maximum sample count, per-trial reserve and total cost. Changes require a **new experiment ID**.
3. Human approval is required. `agent:*` identities cannot be used as approvers or verifiers. Trusted verifier identities must be explicitly allowlisted.
4. One unit gets one durable arm. Blocked randomization balances arms while randomizing ties. Unit IDs must denote comparable independent test cases; repeated task copies or protocol drift destroy validity.
5. Verification JSON and file SHA256 must agree with experiment ID, unit ID, assigned route, protocol version, verifier identity and acceptance result. Evidence must reside under the trusted evidence root.
6. No auto-switch of a production strategy, no automatic task retries or permissions, and no automatic model/Skill/MCP deployment based on a pilot finding.
7. Tests do not establish causal superiority of one method in real biomedical deployments. Real randomized comparisons still depend on comparable cases, allocation integrity, noninterference and unbiased outcome assessment; confidence intervals are descriptive and can be misleading with repeated looks or small samples.
8. SQLite audit integrity is not an off-host immutable ledger; high-risk code requires VM/microVM isolation beyond ordinary Docker.

## Example

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python examples/active_experiment_demo.py
```

The demo uses **synthetic results**, not real experiments. For a real run, create a trusted executor that receives `TrialAssignment`, enforces a clean shadow/isolated environment, executes the chosen protocol without live-world side effects, and emits independent structured validation evidence. Verify its isolation and credentials in a dedicated deployment environment before enabling external SDK/Docker runs.

## Remaining work

- Trusted remotely isolated trial-executor integration (Docker Rootless / gVisor / microVM) and live evaluation under real Claude/Codex/Biomni workers.
- Pre-registered endpoints and sequential-testing/multiplicity control for long-running experiments.
- Bias/confounding controls for non-randomized retrospective experience; privacy policy for case IDs and sensitive biomedical results.
- External approval workflow and policy-controlled promotion gate (pilot results are **not** a release decision).
