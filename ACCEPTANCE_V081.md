# v0.8.1 acceptance

- [x] Backward compatibility: all v0.8.0 tests still pass.
- [x] Deterministic DAG layout contract for visible nodes and edges.
- [x] Filter foreign/missing dependency identifiers from API output.
- [x] Reject traces for tasks not owned by the requesting workspace.
- [x] Capture task-center state transitions with explicit evidence provenance.
- [x] Persist chat-to-task provenance after validating actor, workspace and exact source text.
- [x] Reject unauthorized Worker log-to-task links across workspaces.
- [x] Client-side graph interaction, search, status filter, detail panel, zoom and keyboard activation present.
- [x] Python tests pass; JS syntax passes.
- [ ] Browser visual/navigation verification: unavailable due to browser administrator policy.
- [ ] True distributed Worker / model / Docker production integration: requires external deployment.
- [ ] Cross-host high availability / SSO / database migrations: not provided.
