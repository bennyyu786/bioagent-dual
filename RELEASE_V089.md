# Biomni Dual Center v0.8.9 — source-based repair

This complete source package was generated from the user-provided **v0.8.8** ZIP, not from v0.8.4. It is not a copy of the user's Mac installation and does not contain their local `state/`.

## Changes

- `biomni_dual/task_router.py`: deterministic conservative classification: bounded greetings/identity requests -> simple, bounded read-only inspect/query -> single, all uncertain/multi-step/side-effecting input -> complex.
- `biomni_dual/collaboration_api.py`: when automatic planning is enabled, simple/single requests bypass the heavy Planner and become a single normally governed task with approved execution capability; complex requests continue using the existing validated DAG Planner. Existing failed simple `web_goal_plans` can be moved to a safe worker dispatch using the admin-only `POST /api/v2/{ws}/tasks/{task_id}/direct` endpoint; the server checks task state, workspace, approved capability, and dispatch constraints. Model discovery reports sanitized missing-key/network/HTTP/invalid-response errors.
- `biomni_dual/web_planning.py`: identifiable, sanitized Planner error diagnostics and a distinct `direct` planning ledger status.
- `biomni_dual/web/collaboration.html`: legacy activate button only for legacy tasks; failed planned goals get replan and, when eligible, direct dispatch. Model discover UI shows useful sanitized diagnostic.
- `pyproject.toml`: version 0.8.9.
- `tests/test_v089_routing.py` and `tests/test_v089_model_probe.py`: regression coverage.

## Limits

A simple question submitted as a **task** still requires approved execution capability, online Worker, and acceptance policy, exactly like the old governed task flow. It does **not** immediately call the API/chat model directly. For immediate, read-only text answers use the existing Chat page's AI Assistant backed by `/models/chat`. Multi-step planning still depends on a working Claude/Codex backend. Real external SDK/Worker execution was not verified in this packaging environment.

## Tested

- 33 targeted tests passed (routing, recovery, model-probe, existing planning/dispatch/collaboration), and Node JavaScript syntax check passed.
- Full test run reached approximately 98%, then the runner timed out; **full suite is not claimed passed**.

## Mac upgrade without deleting local state

1. Back up your local `state/`, configuration, databases, and `.env` separately.
2. Inspect the changed files and merge them into your local v0.8.8 directory; avoid overwriting user-generated directories.
3. In the existing environment: `python3 -m pip install -e '.[server,test,agents]'` (if the `agents` extra is available in your environment).
4. Stop the correct running server and restart `python scripts/team_server.py` from the project directory.
5. Hard refresh the browser; validate the simple question and failed-plan task panel.
6. Use the model panel with server-side `BIOMNI_MODEL_API_KEY`. Actual API/model availability depends on the provider.

Note: ZIP extraction by itself does not update or restart a running Mac server.
