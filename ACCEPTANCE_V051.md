# v0.5.1 acceptance matrix

| Requirement | Evidence | Status |
|---|---|---|
| Official MCP Registry read-only search with `version=latest` | `test_official_mcp_parse` | Offline pass |
| Glama authenticated search and mandatory attribution metadata | `test_glama_auth_and_attribution`, `test_glama_missing_key` | Offline pass |
| skills.sh search and detail snapshot retrieval | `test_skills_sh_resolve_and_stage` | Offline pass |
| Anthropic Skills commit-pinned discovery and blob retrieval | `test_anthropic_pinned_commit_and_resolve` | Offline pass |
| Normalize and deduplicate with per-provider errors | `test_all_four_federated`, `test_partial_failure_no_fallback_to_install` | Offline pass |
| Filesystem traversal and unbounded content rejected | `test_bad_path_and_missing_skill`, source limits | Offline pass |
| Federated search linked to v0.5 coordinator | `test_marketplace_coordinator_live_path_simulated` | Offline pass |
| Candidate must exist in durable discovery log | `test_selection_requires_discovery_record` | Offline pass |
| Skills require sandbox evidence and human approval | `test_marketplace_coordinator_live_path_simulated` | Offline pass with fake sandbox |
| MCP registry results cannot auto-install/publish | `test_mcp_market_remains_metadata_only` | Offline pass |
| No regression of older code | whole `python -m pytest -q` | Offline pass |
| Live four-market HTTPS integration | `scripts/marketplace_search.py --live` | **Not run: no DNS/network in authoring container** |
| Real rootless Docker and real MCP handshake | `scripts/live_smoke.py` on target host | **Not run in authoring container** |

Offline fixtures test documented API shapes but cannot guarantee a service has not changed. Re-run live smoke during deployment and use a test key with least privilege.
