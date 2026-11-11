# v0.6.4 Acceptance Matrix

Verification date: 2026-11-09. This report distinguishes **offline verified** from **not live-tested**.

| Acceptance | Evidence | Status |
| --- | --- | --- |
| All v0.6.3 regression tests remain compatible | full `pytest` suite | PASS (offline) |
| Durable per-attempt remediation ID and duplicate handling | `tests/test_remediation.py` | PASS (offline) |
| Worker retry does not override TaskCenter attempt budget | retry transition tests | PASS (offline) |
| Execution and DAG repair add a recovery dependency | route-specific recovery tests | PASS (offline) |
| Code/environment repair is gated by operator approval | `test_execution_repair_requires_explicit_human_approval` | PASS (offline) |
| Independent result review requires a review step | invalid review patch test | PASS (offline) |
| Missing Skill/MCP enters Builder, does not auto approve | fake builder approval and rejection tests | PASS (offline) |
| Human review required for uncertain effects | blocked side-effect test | PASS (offline) |
| Global remediation budget applies per task | budget test | PASS (offline) |
| Cross-goal modifications rejected | scope test | PASS (offline) |
| Crash after DAG commit does not duplicate patch | crash recovery tests | PASS (offline) |
| Crash between failure diagnosis and handoff can resume | controller restart test | PASS (offline) |
| Audit log hash chain remains valid | audit test | PASS (offline) |
| Live Claude Code/Codex generated repair plan | requires authenticated SDK | NOT TESTED |
| Real Skill/MCP Docker sandbox and approval | requires isolated Docker deployment | NOT TESTED |
| Live Biomni A1 and scientific evidence assessor | requires biomedical environment and research data | NOT TESTED |

**Important limits:** This version routes failures into **bounded, declarative repair actions**. It does not guarantee correctness of model-generated repair plans or scientific outcomes. The recovery step itself must be independently validated. Do not give a model direct production Docker, Registry or SQLite privileges. SQLite-based implementation assumes one coordinating process per goal. Published capability availability still depends on the upstream registry and worker infrastructure.
