# v0.4.0 acceptance evidence (2026-11-09)

**Offline:** `pytest -q` => **60 passed** using Python 3.13 in the provided authoring container. `PYTHONPATH=. python examples/workflow_demo.py` => `OFFLINE FLOW PASS`. No actual Docker/Claude/Codex binary was available, and no live SDK credentials were exercised.

| # | Requested acceptance case | Implemented | Evidence in this environment | To finish live signoff |
|---|---|---|---|---|
| 1 | Natural language objective -> JSON-schema-compatible task DAG | Yes | LLMPlanner parser, SQLite DAG validation and mock LLM tests | Run authenticated real LLM with structured output and verify DAG quality on representative goals |
| 2 | Biomni, Claude, Codex workers under unified scheduling | Yes, adapter-level | Mock SDK contracts + fake Biomni Agent routing test | Install real Biomni, authenticate both SDKs and run worker jobs |
| 3 | Two independent jobs execute in parallel | Yes | Real Python ThreadPool scheduling with staggered worker timings | Load test on target machine |
| 4 | Failed job triggers new DAG version | Yes, via dispatch_with_replanning | LLMPlanner.replan + immutable completed jobs test | Run real LLM and verify recovery policy on representative failures |
| 5 | Missing Skill/MCP creates build proposal | Yes | WAITING_APPROVAL, CapabilityAgent candidate generation | Run live agent discovery/generation |
| 6 | Skill passes tests inside separate Docker container | Yes, invocation coded | Mock Docker command, flags, cleanup | `python scripts/live_smoke.py --check docker` against Rootless host |
| 7 | MCP handshake/list/call | Yes, Python MCP harness | Manifest validation + mocked Docker invocation | Real echo MCP verification in same `--check docker` |
| 8 | Unapproved artifact cannot enter Registry | Yes | Negative tests for lack of approval and altered digest | Identity-aware external approval review |
| 9 | Failure/timeout/restart avoid double publish/resource leak | Partial | Idempotence, fail-closed interrupted validation, TaskCenter recovery, Docker cleanup command tests | Inject real container/network failures; add distributed leases and external side-effect idempotency |
| 10 | Published capability unblocks original task | Yes | Offline end-to-end scenario and demo | Run after true Docker and agent integration |

**Summary:** all ten cases have corresponding code paths and offline validation where feasible; **the full ten-case live acceptance has NOT been completed**. Cases 2, 4, 6, 7 and 9 need additional production-environment checks or integrations before claiming full acceptance.
