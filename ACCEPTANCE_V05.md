# v0.5.0 Acceptance matrix

| Requirement | Implementation | Validation |
| --- | --- | --- |
| Search Skills from trusted catalog | `TrustedCatalog.from_json`, `search` | Local catalog test PASS |
| Search MCP candidates | Same catalog with kind `mcp` | MCP catalog route PASS |
| Refresh hosted index | `TrustedCatalog.from_https_index`, allowlisted HTTPS | Mocked HTTP PASS; live network not tested |
| Fetch candidate artifact | Local folder or pinned HTTPS tar.gz with safe extraction | Local PASS, rejected-host PASS; live download not tested |
| Build missing Skill | `CapabilityAgent.propose` through pluggable SDK | Fake LLM PASS; authenticated model not tested |
| Build missing MCP | JSON candidate generation, manifest | Fake LLM PASS; authenticated model not tested |
| Isolated Skill validation | Existing DockerSandbox | Mock test PASS; rootless Docker absent |
| Isolated MCP initialization/list/call | Existing `sandbox/harness/mcp_probe.py` | Mock test PASS; live MCP handshake not tested |
| Repair failed candidates | bounded `REPAIR_PENDING`, feedback to LLM | Offline failure injection PASS |
| Approval gate | `approve_and_publish`, named approver | PASS |
| Idempotent, durable requests | SQLite request key and audit log | PASS |
| Content-addressed publish | Existing SandboxBuilder SHA256 verified snapshot | Offline PASS |
| Release waiting task | Registry check + TaskCenter.approve | PASS |
| Crash and publish reconciliation | fail closed on interruption, reconcile partially published artifact | Offline PASS |
| Full runtime integration | Claude/Codex, Biomni A1, real Docker host | NOT TESTED in this environment |

**No production/full live E2E acceptance claim.** For real deployment, run the original `scripts/live_smoke.py` on an isolated rootless-Docker Linux host, with authenticated SDKs, plus a separate trusted MCP Server candidate. Inspect source and validation logs before authorizing production publication.
