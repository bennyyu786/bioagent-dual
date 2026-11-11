# v0.6.6 acceptance

## Verified locally

- Prior 227 regression tests remain green.
- Experiment proposal is idempotent and conflicting protocols are rejected.
- Shadow/sandbox-only, human-approval-before-allocation, explicit limits on samples and reserved cost.
- Balanced randomized assignment, restart-stable allocations, deduplication, and conflicting result rejection.
- Independent verifier allowlist, trusted root path enforcement, evidence digest and assignment-bound evidence claims.
- Conservative comparison report with Wilson intervals and no production rollout authorization.
- Cost-aware, uncertainty-aware experiment priority without modifying learned production history.
- SQLite audit chain verification and synthetic end-to-end demo.

## NOT verified locally

- Actual isolated Docker execution or microVM safety properties.
- Live Claude/Codex/Biomni and actual Skill/MCP testing.
- External human approval service integration.
- Biomedical scientific validity or causal advantage between strategies.

These conditions must pass separate live acceptance tests before claiming production readiness.
