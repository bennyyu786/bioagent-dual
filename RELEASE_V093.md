# v0.9.3 — Three-column navigation UI

- Three-column desktop layout: far-left icon rail for professional configuration, second column for durable workspaces/session/task history, third column for conversation and results.
- Sidebar “新聊天” starts a new session without deleting saved histories.
- Existing Task Center, DAG, Capability Center, Worker, WebSocket logs, model settings and permissions remain accessible through the left rail.
- Responsive layout hides history column on narrow screens while retaining the icon rail and chat input.
- Preserves v0.9.2 APIs, state, workspace permissions, task persistence and result decoding.
- No user database, secret or runtime state included in source distribution.

- Sidebar project list reflects the authenticated user’s real accessible workspaces; selecting one reuses the existing workspace switching and history refresh path.
