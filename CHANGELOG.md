## v0.9.3

- Three-column professional icon rail, durable history/project sidebar and ChatGPT-style conversation/result canvas. See RELEASE_V093.md.

## v0.9.2

- 统一白底黑字、历史会话/任务索引、完整实时事件日志、任务结果可读化。参见 RELEASE_V092.md。

## v0.8.8
- 移除“已完成结果”侧栏菜单；已完成 DAG 节点点击即加载同一结果正文。
- 保留统一阅读/证据组件与后端原有工作区权限 API，并处理页面刷新、跨工作区与退出登录时的缓存状态。
- 保留三级审核队列入口和原始证据完整性。

## v0.8.7
- 审核结果统一 Unicode/JSON 阅读视图，原始 SHA256 证据不变。
- 新增已完成结果菜单、DAG 直达结果和最小权限 API。
- 增加端到端回归测试。

## v0.8.6
- Added dedicated administrator review queue with complete result, SHA256, evidence note, approve/reject and policy settings.
- Added fail-closed L1 delivery auto-acceptance (trusted capability allowlist), L2 independent validator auto-acceptance and L3 independent + human acceptance.
- Added explicit validator module configuration to ASGI server entrypoint; no Web-provided executable validators.
- Preserved legacy dispatch behavior until an administrator opts in to tiered acceptance.

## 0.8.5
- Connect Web task submission to bounded Claude/Codex SDK/CLI planning and DAG validation.
- Add workspace planning policy, read-only planner roles, trusted-step auto-approval switch, scoped planning status, manual retry, and no replay on uncertain commits.
- Dispatch eligible new DAG steps through existing approved capability and Worker resource policies.
- Add planner configuration controls in Task DAG and five extra security/CLI tests (349 total).

## v0.8.4 — Resource-aware dispatch and verified result handoff

- Added `dispatch_runtime.py` with priority-aware task selection, approved-capability/Worker-fit checks, atomic offers and quota reservation, authenticated Worker claim, report, review, cancellation and quarantine.
- Added `remote_worker.py` and `scripts/remote_worker.py`: explicit local argv allowlist; no remote shell command interface.
- Added optional trusted domain validator, admin attestation, SHA256 result-binding and `reported` reservation state.
- Extended workspaces' FastAPI endpoints, topology dispatch evidence and interactive resource dispatch/review panel.
- Retained unified `biomni_dual/` mainline; no version subpackages.

## v0.8.3 — Runtime Relations and Resource Scheduling Visualization

- Add workspace-scoped, transactional worker resource reservations and authenticated worker lease/receipt tracking.
- Quarantine uncertain/expired leases without releasing quotas; explicit administrative reconciliation required.
- Add evidence-referenced capability lifecycle and repair-task relation records.
- Extend tenant-safe topology with reservation and repair relations, quota charts, admin configuration forms, and real-time refresh.
- Add 6 tests (324 total) and deployment/acceptance docs. Resource accounting is logical reservations, not real utilization.

## [0.8.2] — 双中心执行拓扑

- 新增 `execution_topology.py` 的只读、确定性任务—能力—Worker 关联视图
- 新增工作区隔离的 `/api/v2/{ws}/topology` 接口
- Control Center 新增拓扑导航、搜索、缩放、节点详情及任务轨迹跳转
- 拒绝伪造能力健康与 Worker 活动派发；不泄露工作区外依赖
- 新增权限与正确性测试，保持旧功能兼容

## v0.8.1 — Interactive DAG & Execution Trace

- Added workspace-authorized DAG and task-trace APIs.
- Added SVG graph with search, status filter, zoom and clickable task detail.
- Added explicit, owner-validated chat-to-task conversion and source message linking.
- Added optional authorized task correlation in remote Worker log events.
- Kept role checks, live WebSocket updates, and underlying v0.8.0 interfaces.

## 0.6.7

- Add TrialExecutor connecting approved randomized assignments to pinned Docker skill/MCP candidate validations.
- Add durable atomic execution claims, fail-closed uncertainty and operator reconciliation for DB handoff crashes.
- Add independent check evidence recording and isolated PilotLearningView without promotion to production repair history.
- Add offline integration demonstration and regression/security tests.
- Real Docker/SDK integration not performed in this environment.

# Changelog

## 0.6.6

- Added `active_experiments.py` with approved randomized shadow/sandbox comparisons, budgeted allocations, tamper-evident SQLite evidence, and conservative comparison reports.
- Added historical-uncertainty-based experiment prioritization and failure-driven experiment proposals; production remediation policy remains unchanged.
- Added `examples/active_experiment_demo.py`, test coverage, and `ACTIVE_EXPERIMENTS_V066.md`.
- No external side effects, automatic deployment, real sandbox launch or real scientific verification are claimed.

# v0.6.5

- Added `joint_learning.py`: task/capability/remediation snapshot, immutable outcome store, conservative strategy advice, trusted evidence gate.
- Repair outcomes count only after post-remediation terminal execution and an independent verifier artifact.
- Cross-task learning is scoped by explicit task type, failure reason and capability ID.
- Historical alternatives require explicit approval and cannot override human reconciliation.
- Added regression tests and a synthetic offline example.

## 0.6.4 — Autonomous remediation orchestration

- Added `biomni_dual/remediation.py` with durable, idempotent diagnosis handoff, bounded remediation attempts and explicit repair states.
- New `RuntimeRepairPlanner` accepts Claude/Codex-compatible runtimes but returns declarative, schema-validated DAG proposals rather than executing generated commands.
- Gated execution repair behind explicit human TaskCenter approval, in addition to DAG validation.
- Routed execution repair, task replanning and scientific review to independently executable recovery tasks; original failed task remains dependency-blocked until those steps finish.
- Routed capability repair to existing Skill/MCP autonomous builder and mandatory named human approval before publication.
- Optional `AdaptiveController` wiring, safe restart reconciliation, goal isolation, audit transitions and regression tests.
- Added deterministic `examples/remediation_demo.py`, `REMEDIATION_V064.md`.
- Live remote integration remains pending; demo is intentionally offline.

## v0.6.3

- Added `FailureDiagnosisEngine`: persistent, idempotent fault routing and remediation budgets.
- Added `EvidenceAwareValidator`: independent rule checks and verifiable artifact hashes.
- Added blocked handling for uncertain remote side effects.
- Integrated diagnosis into `AdaptiveController`, preserving previous default behavior when not configured.
- Added failure-injection, security, recovery and integration tests, plus a demo and documentation.

# v0.6.2 - Online value learning

- Add `online_learning.py`: idempotent SQLite prediction/outcome records with auditable fingerprints and exact-context performance estimates.
- Add calibrated ValuePolicy with risk/budget gates, cold-start fallback and bounded exploration.
- Add LearnedValueGuidedPlanner, optional AdaptiveController quality-learning feedback, regression tests, and offline demonstration.
- No versioned source directories. Real Claude/Codex/Docker live integration remains unverified.

## v0.6.0 — Rolling task/capability co-evolution

- Added `AdaptiveStore` with SQLite world-state, goal-task membership, observations and bounded decision history.
- Added `AgentRollingPlanner` / `CallbackPlanner` for short-horizon exploration and graph patch proposals without direct planner DB mutations.
- Added `AdaptiveController` for task/capability matching, gap-only build requests, execution, independent result validation, feedback and event-driven replanning.
- Added optional transactionally committed one-time recovery dependencies for failed tasks in DAGVersionManager.
- Added offline multi-round demonstration, new failure-injection tests and `ADAPTIVE_V06.md`.
- Preserved all existing modules in one main package. Real Agent SDK, Docker and MCP endpoints were not used in offline acceptance.

## v0.5.2 - Task-first capability matching

- Reviewed explicit task requirements with schema, cost, license, permission, risk and quality constraints.
- Minimum-cost sufficient internal capability set with dependency closure and fail-closed feasibility.
- Gap-only marketplace request gateway, preserving v0.5.1 catalog adapters and approval workflow.
- Added task-fit demo, documentation and regression tests.

# v0.5.1

- Added P0 read-only marketplace adapters: Official MCP Registry, Glama, skills.sh, Anthropic Skills.
- Normalized source records, isolated provider failures, deduplicated by distinct capability identity.
- Added safe bounded Skill file snapshot staging; MCP marketplace entries remain metadata-only.
- Persisted discovery and explicit candidate selection in SQLite + audit chain.
- Connected selection to v0.5.0 SandboxBuilder validation and approval/publish workflow.
- Added marketplace CLI, documentation and offline fixture coverage.
- Live provider, Docker and SDK integration not verified in this authoring environment.

---

# Changelog

## 0.4.1 — Reliability upgrade

- In-place update on the unified v0.4.0 codebase; no version-specific source subpackages.
- Persistent `SessionStore` with SDK thread/session checkpointing, cached results, and fail-closed UNCERTAIN crash recovery.
- Atomic idempotency-key registration for task submissions and capability build proposals.
- Atomic task claim/finish under SQLite transactions; idempotent completion and published-build replay.
- Error taxonomy with retryability decisions wired into the parallel scheduler.
- SQLite-backed tamper-evident hash-chain audit for task and build state transitions.
- Offline integration and failure-injection tests; safety and recovery limitations documented in `RELIABILITY.md`.

## 0.4.0 — Unified codebase

- Real SDK/CLI adapter interfaces, LLM planning, Docker sandbox builder, approvals, capability registry and WorkerPool integration.

## v0.5.0 — controlled autonomous Skill/MCP lifecycle

- Added `TrustedCatalog`: operator-managed indexed Skill/MCP search, exact capability matching, SHA256-pinned local artifacts, and allowlisted HTTPS catalogs/archives.
- Added `AutonomousBuildCoordinator`: persisted idempotent request lifecycle with catalog-first discovery, Claude/Codex candidate generation fallback, bounded LLM repair, Docker validation, human approval, content-addressed publication, audit trail, and task wake-up.
- Added crash fail-closed recovery for interrupted autonomous build jobs and integration hooks in `DualCenterWorkflow`.
- Added `examples/autonomy_demo.py`, tests and `AUTONOMY.md`.
- Kept **one** `biomni_dual/` code package; no version-named source directories.

## v0.6.1 — Evidence & Value Feedback

- Added `value_feedback.py` with evidence-based scientific quality gates, measurable uncertainty delta, operational utility proxies, and action value ranking under hard resource/risk constraints.
- Added `ValueGuidedPlanner` for selecting bounded rolling planning proposals, without bypassing DAG validation.
- Added immutable SQLite `ExperienceStore` with idempotent assessment writes and audit integration.
- `AdaptiveController` can optionally require independent `QualityEvidence` before marking execution `COMPLETED`.
- Added `tests/test_value_feedback.py`, `examples/value_feedback_demo.py`, and `VALUE_FEEDBACK.md`.

## v0.7.0 — Persistent Autonomous Runtime

- Added persistent event queue, idempotent ingestion, periodic schedules,
  cooperative per-goal leases, heartbeat renewal and goal lifecycle states.
- Added allowlisted environment probes and HMAC-authenticated webhook adapter.
- Added fast/medium/slow event cadence and idle-time suppression.
- Added conservative startup quarantine for orphaned running tasks and
  operator-only resumption after reconciliation.
- Added trusted daemon bootstrap, offline example and dedicated reliability tests.
- No new version directory; single `biomni_dual` package preserved.

## v0.7.3 — Biomni Control Center

- Added responsive SPA with 7 workspaces: overview, DAG, capabilities, Workers, remediation/experiments, approvals and chat.
- Added authenticated, loopback-only control API (`control_center.py`), SQLite conversation records and explicit idempotent TaskCenter submission.
- Added optional, server-configured text-only OpenAI-compatible advisor; no default claims of model availability.
- Added guarded task and capability approval, runtime goal pause/resume, and read-only private state projections.
- Added optional `control_center` to the existing `serve_observed` wrapper.
- Added `scripts/control_center_server.py`, `CONTROL_CENTER_V073.md` and control-center regression tests.
- Preserved unified `biomni_dual/` package and existing v0.7.2 APIs.

## 0.8.0 — Multiuser Collaboration Control Plane

- Added salted scrypt accounts, scoped RBAC/memberships, hashed expiring sessions, member revocation and login lockout.
- Added FastAPI team control-plane endpoints and responsive multiuser Web workspace.
- Added single-use WebSocket tickets, tenant-isolated persisted events and remote Worker logs.
- Added restricted remote Worker command queue with atomic claim and manual reconciliation of unknown outcomes.
- Added OpenAI-compatible model discovery and chat through server-only credentials; fixed missing current user message in advisor requests.
- Added Docker/Caddy TLS deployment template, local bootstrap CLI, safe reference Worker client and integration tests.
- **Single-host reference implementation; HA/SSO/real remote executor production integration remain open.**


## v0.9.0

- Optional Claude/Codex SDK or CLI task complexity classifier with safe fallback and workspace admin UI.
- Preserve automatic Worker result delivery from the prior v0.8.9 auto-delivery build.
