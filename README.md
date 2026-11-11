# v0.8.6 — Result Acceptance Center and Tiered Verification

See [ACCEPTANCE_V086.md](ACCEPTANCE_V086.md) for the manual-review UI, L1/L2/L3 verification policy and migration steps.

# v0.8.5 — Web auto-planning

The Control Center now optionally calls a configured Claude/Codex planning runtime when a user submits a goal and commits a scoped, validated task DAG. Admins may allow safe steps using a *preapproved* execution capability to proceed automatically; high-risk steps and new capabilities remain gated. **See [`WEB_AUTO_PLANNING_V085.md`](WEB_AUTO_PLANNING_V085.md)** for setup and limitations.

**v0.8.4.post1 bug fix:** See [SUBMISSION_FIX_POST1.md](SUBMISSION_FIX_POST1.md) for the Web task submission → approved capability → automatic dispatch bridge.

## v0.8.4 Resource-aware Dispatch + Remote Worker

管理员可按任务优先级、已批准能力、在线 Worker 和资源配额自动生成派发工单；Worker 使用本地固定适配器领取任务、运行并提交待验证结果。`reported` 持续占用配额直到独立验证；超时与跨数据库不确定状态不可自动重放。详情见 [DISPATCH_V084.md](DISPATCH_V084.md)、[ACCEPTANCE_V084.md](ACCEPTANCE_V084.md)。

离线演示：`python examples/dispatch_runtime_demo.py`（需先 `pip install -e '.[server,test]'`）。

## v0.8.3 Runtime Relations / Resource Topology

Control Center 的双中心拓扑现在可显示可信运行时关系：工作区 Worker 的 CPU/内存/GPU **预留配额**、任务资源预留与领取状态、证据引用的能力生命周期，以及任务修复依赖。管理员可直接在拓扑面板配置 Worker 配额和提交显式资源预留，仍不会自动执行远程作业。

详情请阅 [RUNTIME_RELATIONS_V083.md](RUNTIME_RELATIONS_V083.md) 和 [ACCEPTANCE_V083.md](ACCEPTANCE_V083.md)。

## v0.8.2 — 双中心执行拓扑

新增任务—能力—Worker 三列交互式 SVG 拓扑；API `/api/v2/{workspace}/topology` 保持工作区权限隔离，只展示经过记录的关系。详见 [TOPOLOGY_V082.md](TOPOLOGY_V082.md)。

# Biomni Dual Center v0.8.1

**New:** interactive workspace-scoped task DAG, task detail and state trace, explicit chat-to-task provenance, and optional Worker task-linked logs. See [INTERACTIVE_DAG_V081.md](INTERACTIVE_DAG_V081.md).

# Biomni Dual Center v0.7.1 — Deployment & Observability

Direct upgrade of the unified v0.7.0 mainline. Added local health/readiness/Prometheus metrics, Worker heartbeat telemetry, persistent alerts, deployment templates and a live acceptance checker. No source version subpackages are introduced.

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python examples/persistent_runtime_demo.py
```

For real setup use [DEPLOYMENT_V071.md](DEPLOYMENT_V071.md) and [ACCEPTANCE_V071.md](ACCEPTANCE_V071.md). **You must provide your own trusted runtime bootstrap and credentials.** Docker Compose/systemd and the live Claude/Codex/remote MCP integrations have not been exercised on this authoring host.

---

# Biomni Dual Center v0.6.7

**New: isolated experiment execution and separate pilot learning.** This direct mainline upgrade of v0.6.6 connects approved trial assignments to pinned candidate artifacts and Rootless Docker validation, with an independently supplied verification function, durable execution claims and crash reconciliation. See [TRIAL_EXECUTION_V067.md](TRIAL_EXECUTION_V067.md).

```bash
python -m pip install -e ".[test]"
python -m pytest -q
python examples/trial_execution_demo.py
```

**Verification boundary:** The added tests/demo inject a simulated Docker transport. Real Rootless Docker, authenticated Claude/Codex SDKs, MCP servers and biomedical datasets were not available here; live execution remains unverified. Pilot trials never automatically change production policy.

---

# Biomni Dual Center v0.6.6

**New: audited active experiments and bounded shadow/sandbox repair-strategy comparisons.** Directly upgrades the unified v0.6.5 source; no versioned Python directories. See [ACTIVE_EXPERIMENTS_V066.md](ACTIVE_EXPERIMENTS_V066.md) and [ACCEPTANCE_V066.md](ACCEPTANCE_V066.md).

```bash
python -m pip install -e ".[test]"
python -m pytest -q
python examples/active_experiment_demo.py
```

All experimental outcomes in the included demo are **synthetic**; experiments only create proposals, assignments and verified evidence records, not live tool execution. Real Docker/MCP/Claude/Codex/Biomni integration remains unverified. The experiment report **never grants production permission**.

---

# Biomni Dual Center v0.6.5

**New:** joint task-capability snapshots and audited, evidence-constrained repair strategy learning. This is a direct mainline upgrade of v0.6.4, with no versioned Python subpackages. See [JOINT_LEARNING_V065.md](JOINT_LEARNING_V065.md).

```bash
python -m pip install -e ".[test]"
python -m pytest -q
python examples/joint_learning_demo.py
```

The demo uses synthetic independently labelled results, not real biomedical results. No live SDK/Docker/MCP verification has been performed.

---

# Biomni Dual Center v0.6.4

**New: bounded autonomous remediation orchestration** — diagnose → route → propose a recovery task or capability build → validate/approve → resume. Direct upgrade of the unified v0.6.3 source tree, with no versioned source subpackages. See [REMEDIATION_V064.md](REMEDIATION_V064.md) and [ACCEPTANCE_V064.md](ACCEPTANCE_V064.md).

```bash
python -m pip install -e ".[test]"
python -m pytest -q
python examples/remediation_demo.py
```

Offline integration tests exercise recovery and approvals using fakes; **live Claude/Codex SDK, Docker, MCP and Biomni A1 tests have not been run in this environment**.

---

# Biomni Dual Center v0.6.3

New in this version: persistent failure diagnosis and evidence-aware result validation. See [FAILURE_VALIDATION.md](FAILURE_VALIDATION.md).

# Biomni Dual Center v0.6.0

**Dynamic task/capability co-evolution on the single unified codebase.** Unknown long-horizon goals are tackled through bounded rolling plans: observe, explore, select sufficient capabilities, execute, validate, and replan or build capability when needed. New: `biomni_dual/adaptive.py`, `examples/adaptive_demo.py`, and [ADAPTIVE_V06.md](ADAPTIVE_V06.md).

```bash
python -m pip install -e ".[test]"
python -m pytest -q
python examples/adaptive_demo.py
```

This offline demonstration uses a simulated Worker, result validator and explicitly simulated approval; actual Claude/Codex, Biomni, Rootless Docker and MCP live integration remains unverified in the authoring environment. There are no `v03/` / `v04/` / `v05/` / `v06/` source subpackages.

---

## v0.5.2 Task-fit-first optimization

Internal validated capability coverage is checked before market discovery. New APIs: `TaskRequirements`, `select_for_task`, `TaskFitCoordinator`, and opt-in `ParallelScheduler(task_fit_factory=...)`. See [TASK_FIT.md](TASK_FIT.md) and `examples/task_fit_demo.py`. External discovery remains opt-in and cannot install or publish code.

# Previous v0.5.2 notes

> Direct upgrade from unified v0.5.0 source; no `v05/` or `v051/` directory.

## New in 0.5.2 — P0 federated discovery

Added *read-only* adapters for the Official MCP Registry, Glama, skills.sh, and Anthropic Skills in `biomni_dual/marketplaces.py`. Normalized results feed `AutonomousBuildCoordinator.discover_marketplaces()` and a persisted selection workflow. Selected Skill text can be staged for the existing Docker validation + human approval. MCP listings are **metadata only**; no external MCP service is automatically installed.

```bash
python -m pytest -q
python scripts/marketplace_search.py 'variant' --kind skill --live
python scripts/marketplace_search.py 'biomedical' --kind mcp --live
```

See [P0_MARKETS.md](P0_MARKETS.md) and [ACCEPTANCE_V051.md](ACCEPTANCE_V051.md). Live P0 APIs and rootless Docker require an external deployment environment; this authoring environment cannot reach the internet.

---


## New in 0.5

A persistent **Skill/MCP discovery → generation → Docker testing → review → publication** coordinator, built on the previous reliable task/worker/approval core. See [AUTONOMY.md](AUTONOMY.md) and [ACCEPTANCE_V05.md](ACCEPTANCE_V05.md) for setup and explicit security limits.

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest -q
python examples/autonomy_demo.py  # simulated LLM and Docker, clearly labeled
```

Live autonomous building requires an authenticated Claude/Codex runtime, a dedicated rootless Docker host with `biomni-sandbox:py311`, and a human publication approver. **Do not interpret offline tests as live sandbox acceptance.**

---

# Biomni Dual Center v0.5.0

**Unified mainline source package**, upgraded directly from v0.4.0, no version subdirectories.

See [RELIABILITY.md](RELIABILITY.md) for sessions, idempotency, error classes, audit verification and limitations.

## Quick check

```bash
python -m pip install -e ".[test]"
python -m pytest -q
python examples/reliability_demo.py
python examples/workflow_demo.py
```

## Existing v0.4.0 architecture

## What shipped

- `biomni_dual.LLMPlanner`: Claude or Codex runtime -> strict JSON -> DAG validation -> atomic SQLite DAG patch. Bounded repair and event-driven replan.
- `biomni_dual.CapabilityAgent`: LLM creates a JSON list of candidate source files. Trusted controller validates relative paths, total size, and stage location; it does not execute code.
- `ClaudeSDKRuntime`, `CodexSDKRuntime`, `CLIServiceRuntime`: concrete optional SDK/CLI calls with read-only planning defaults, explicit opt-in code-writing workers, timeouts and best-effort cancellation. SDK adapters must be run in an isolated worker process/VM for untrusted execution. CLI is disabled by default.
- `SDKWorker`, original `BiomniWorker`, `WorkerPool` and `ParallelScheduler`: route Biomni/Claude/Codex work through a shared TaskCenter and resource scheduler.
- `DockerSandbox`: fail-closed Rootless Docker prerequisite, no-network read-only containers with CPU, memory and PID quotas, no extra caps, non-root UID, timeout and best-effort forced cleanup. `Skill` runs pytest; `MCP` runs actual stdio `initialize`, `tools/list`, and `tools/call` probe inside image.
- `BuildStore`/`SandboxBuilder`: separate SQLite build states (PROPOSED -> TESTING -> REVIEW -> APPROVED -> PUBLISHED); SHA-256 content-addressed snapshot; manual approval; on restart, interrupting a TESTING build causes FAILED and published artifact metadata can be restored.
- `DualCenterWorkflow`: orchestrates plan, dispatch, capability gap, approval and resume. `dispatch_with_replanning` reacts to terminal failures and supports one bounded recovery DAG per failure.

## Requirements

- Python 3.11+
- For **live Claude SDK**: installed `claude-agent-sdk`, working Claude Code credentials.
- For **live Codex SDK**: installed `openai-codex`, working Codex credentials.
- For **live Docker validation**: a separate Linux host with a **Rootless Docker daemon** (ordinary Docker Desktop or a rootful daemon does not meet the default policy), and the trusted image built by the operator.
- For **live Biomni**: separately install snap-stanford/Biomni and its biomedical runtime dependencies; the A1 factory is provided by the integrator.

Do not expose real clinical/patient data to a third-party SDK or generated tool without appropriate access controls.

## Install and offline tests

```bash
unzip biomni-dual-center-v0.4.0.zip
cd biomni-dual-center-v0.4.0
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
pytest -q
python examples/workflow_demo.py
```

The demo uses mocked LLM and Docker clients. It tests the workflow, **not** whether a real LLM, a real Docker daemon, or a real MCP server is working.

## Live integration tests (opt-in)

On the separate Rootless Docker host, build the **trusted** verification image. The build requires access to approved package repositories and may require pin review. Do NOT give a model access to the Docker socket.

```bash
docker build -t biomni-sandbox:py311 sandbox/
python scripts/live_smoke.py --check docker
```

This launches a real no-network test container for the included Skill and a real test container for the included echo MCP server. It fails if the daemon is not Rootless, if tools cannot initialize, or if tests fail. No Docker binary is present in the authoring environment, so these tests were **not run here**.

Optional live SDK checks, which require your own authentication and may incur API usage:

```bash
pip install -e '.[agents]'
python scripts/live_smoke.py --check claude
python scripts/live_smoke.py --check codex
```

To run the entire live suite (requires all prerequisites):

```bash
python scripts/live_smoke.py --check all
```

Live SDK integrations are deliberately **not included** in the offline 60-test result.

## API usage

```python
from biomni_dual import CodexSDKRuntime, LLMPlanner
from biomni_dual.scheduler import TaskCenter
from biomni_dual.dag import DAGVersionManager

store = TaskCenter('tasks.db')
planner = LLMPlanner(CodexSDKRuntime(), DAGVersionManager(store))
# result = asyncio.run(planner.submit_goal('Perform quality control then integrate omics'))
```

To build a capability without executing model-created scripts on the host:

```python
# generated files become *staged candidate data* only
from biomni_dual import CapabilityAgent, ClaudeSDKRuntime
agent = CapabilityAgent(ClaudeSDKRuntime(), './staging')
# folder = asyncio.run(agent.propose('skill.integration', 'skill', 'Integrate matrices'))
# submit folder to SandboxBuilder; never auto-publish
```

## Security boundaries and limitations

1. Agent SDK tool restrictions are **not a security sandbox**. Claude/Codex execution that can edit or execute code must be launched by a separate isolation service and constrained workspace.
2. The Docker builder validates candidate source code, but the image itself must be operator-built from a trusted Dockerfile. Candidate dependencies are not automatically downloaded.
3. Docker Rootless is not a strong security boundary against kernel vulnerabilities. For hostile third-party code, use gVisor or a disposable VM/microVM plus external network policy.
4. Both SQLite TaskCenter and resource reservations assume **one coordinator process**, not a distributed lease system; in-flight work may be replayed after a crash. Only submit idempotent actions or add transactional external action deduplication.
5. Task result, planner prompt and build logs may contain sensitive content. Provide encryption/redaction/retention policies before production use.
6. Human approval is **explicit** but implemented as a local method call, not authenticated RBAC or multi-party sign-off. Put an identity-aware approval service in front of it in production.
7. The sandbox's `pytest` tests validate **specified behavior**, not absolute safety. The MCP probe executes only the explicit test cases in the manifest.
8. Capability metadata is restored on restart from a digest-verified local artifact snapshot. No remote signed registry is shipped.
9. Session IDs returned from SDKs are surfaced, but sessions are not yet durably persisted in TaskCenter. SDK cancellation is best-effort; production needs worker-level process supervision.
10. Dedicated integration tests on actual Claude, Codex, Docker, MCP, and Biomni installs remain required for full external signoff.

See `ACCEPTANCE.md` for the ten-item acceptance matrix and the actual evidence level.

## v0.6.2: Scientific result quality and value feedback

Use an independent domain outcome evaluator, `QualityPolicy`, and `ExperienceStore`
to gate task completion on quality/reproducibility/risk, and persist measured
uncertainty changes when available. `ValueGuidedPlanner` chooses bounded
exploration/replanning proposals under risk and cost constraints, retaining
the existing DAG validator and approval boundaries.

```bash
python examples/value_feedback_demo.py
python -m pytest -q
```

See `VALUE_FEEDBACK.md` for evidence requirements, limitations and the API.

## v0.6.2 Online value learning

See [ONLINE_LEARNING.md](ONLINE_LEARNING.md) for the audit-backed online learning store, exact task/capability/Worker context estimates, prediction error, cold-start policy, and restrictions. Run `python examples/online_learning_demo.py` for an offline demonstration.

## v0.7.0: persistent autonomous operation

The new `PersistentAutonomousRuntime` wraps `AdaptiveController` with durable
environment events, three decision cadences, authenticated webhook ingestion,
periodic schedules, cooperative leases and explicit goal lifecycle controls.
It runs only in the authorized goal scope and does not bypass approvals.
Read `PERSISTENT_RUNTIME_V070.md` for the operational safety contract.

```bash
python examples/persistent_runtime_demo.py
```

Use `scripts/runtime_daemon.py` with a trusted user-provided bootstrap factory
for a long-running service. See the deployment limitations in the new manual.

## v0.7.2 Frontend dashboard

An embedded read-only frontend is available at `/dashboard` on the observability port. To run the dashboard standalone: `python scripts/dashboard_server.py --ops-db operations.sqlite --runtime-db runtime.sqlite`. See `FRONTEND_V072.md` for limitations.

## v0.7.3: Biomni Control Center and conversational task entry

The new authenticated workbench is available at `/control`. It provides a responsive operator interface for real task DAGs, capabilities, Worker telemetry, remediation, experiments and approvals. The Chat panel persists messages and allows an explicit, idempotent conversion of user text into a TaskCenter task. Optional OpenAI-compatible advisory model calls are configured server-side and never gain execution privileges.

Start it with `BIOMNI_CONTROL_TOKEN` and `scripts/control_center_server.py`; see [CONTROL_CENTER_V073.md](CONTROL_CENTER_V073.md) for full configuration and security boundaries. Previous monitoring APIs and `/dashboard` remain available; the Control Center does **not** automatically run the orchestrator.

## v0.8.0 — Team Control Center

Multiuser ASGI service, workspace RBAC, real-time WebSockets, remote Worker management, model integration, and responsive team UI are described in [TEAM_CONTROL_V080.md](TEAM_CONTROL_V080.md).

```bash
pip install -e '.[server,test]'
export BIOMNI_BOOTSTRAP_PASSWORD='use-a-real-random-secret'
python scripts/team_server.py --bootstrap-email admin@example.com
unset BIOMNI_BOOTSTRAP_PASSWORD
python scripts/team_server.py
```

Open `http://127.0.0.1:8766/control` locally. v0.7.3's old loopback-only Control Center remains available; it does **not** provide per-user RBAC. v0.8.0 does not claim production HA or end-to-end real remote Worker runtime acceptance.

## v0.8.7：统一结果阅读

前端「结果审核」可切换 Unicode/JSON 阅读模式与原始证据；新增「已完成结果」历史菜单及 DAG 结果入口。详见 `RESULT_PRESENTATION_V087.md`。


## v0.8.8：在任务 DAG 查看最终结果

点击已完成的 DAG 节点，直接在右侧任务详情查看完整结果、原始证据并复制。独立“已完成结果”菜单已移除，待审核工作仍通过“结果审核”菜单完成。详见 `DAG_RESULT_V088.md`。
