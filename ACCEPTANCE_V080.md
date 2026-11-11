# v0.8.0 acceptance

| Area | Acceptance | Status |
|---|---|---|
| 0.7.3 regression | 295 existing tests | PASS (local) |
| New multiuser controls | User login, logout, role/tenant isolation, revocation, event scope, audit | PASS (local) |
| WebSocket | One-time ticket, workspace authorization, live event received | PASS (TestClient) |
| Worker controls | Registered Worker, authenticated atomic claim, receipt, no unauthorized commands | PASS (local) |
| Logs | Bounded payload, workspace event stream, terminal control stripping | PASS (local) |
| Model | GET /models and POST /chat/completions against real local HTTP stub | PASS (local stub) |
| Front-end | JavaScript syntax check, HTML served via ASGI | PASS (syntax/API), headless Chromium navigation blocked by sandbox policy; visual QA not certified |
| Full suite | pytest | PASS: 305 tests |
| Production Docker + Caddy TLS | Real deployment to public DNS | NOT TESTED (no Docker daemon/credentials) |
| Remote Claude/Codex/Biomni/A1 | Real vendor integration and long-running load | NOT TESTED |
| Multi-node HA, corporate SSO | PostgreSQL/event bus/OIDC | NOT IMPLEMENTED |

Editable installation verified offline using `pip install --no-build-isolation --no-deps -e .`; ordinary pip dependency installation could not contact the package index from this environment.

This is a production-oriented single-node control-plane baseline, not a certified production deployment.
