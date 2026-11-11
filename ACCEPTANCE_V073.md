# v0.7.3 Control Center — acceptance

- [x] Existing v0.7.2 tests retained.
- [x] Operator-only read of prompts, task DAG, conversation, skills, Workers, repairs, experiments and approval requests.
- [x] Persistent conversation entry and an explicit, idempotent "submit as task" action.
- [x] Optional server-side OpenAI-compatible advisor (no tools or execution).
- [x] Task and capability approval using existing guarded APIs.
- [x] Goal pause/resume via original runtime state rules.
- [x] Tests for authorization, request size, cross-session separation, repeat submissions and state transitions.
- [x] JavaScript syntax check using Node.js.
- [x] Python offline tests and editable install.
- [ ] Browser screenshot QA: local browser automation is blocked by the environment's browser policy (not a code failure).
- [ ] Real LLM endpoint call: provider credentials not configured here.
- [ ] Actual Docker/Claude/Codex/Biomni deployment and long-lived UI: external infrastructure not available here.

Note: Task creation does not auto-bind to an existing goal or invoke an executor. A deployed runtime must be configured to consume tasks.
