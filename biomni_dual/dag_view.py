"""Workspace-scoped, deterministic, read-only DAG presentation contracts.

Only use tasks already filtered by CollaborationStore ownership; do not
resolve missing/cross-workspace dependencies from the global TaskCenter.
"""
from __future__ import annotations


def task_graph(visible_tasks: list[dict]) -> dict:
    """Create graph nodes, visible edges and topological ranks without tenant leaks.

    Cycles are signaled instead of looping. Missing dependencies remain counts,
    but their opaque IDs are never returned to unauthorized workspaces.
    """
    tasks = {str(t['id']): t for t in visible_tasks}
    indegree = {tid: 0 for tid in tasks}
    next_of = {tid: [] for tid in tasks}
    hidden = {tid: 0 for tid in tasks}
    edges = []
    for tid, task in tasks.items():
        for raw in task.get('depends_on') or []:
            dep = str(raw)
            if dep not in tasks:
                hidden[tid] += 1
                continue
            edges.append({'source': dep, 'target': tid})
            indegree[tid] += 1
            next_of[dep].append(tid)
    ranks = {tid: 0 for tid in tasks}
    ready = sorted(tid for tid, d in indegree.items() if d == 0)
    seen = set()
    while ready:
        cur = ready.pop(0)
        seen.add(cur)
        for nxt in sorted(next_of[cur]):
            ranks[nxt] = max(ranks[nxt], ranks[cur] + 1)
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    cycles = sorted(set(tasks) - seen)
    # Ranks of cyclic components are undefined. Put those in the last column.
    for tid in cycles:
        ranks[tid] = max(ranks.values(), default=0) + 1
    nodes = []
    for tid, t in sorted(tasks.items(), key=lambda item: (ranks[item[0]], item[0])):
        nodes.append({
            'id': tid, 'title': str(t.get('prompt') or '')[:240],
            'status': str(t.get('status') or 'pending'),
            'rank': ranks[tid], 'attempts': t.get('attempts', 0),
            'approved': bool(t.get('approved')), 'hidden_dependencies': hidden[tid],
            'cycle': tid in cycles,
        })
    edges.sort(key=lambda e: (e['source'], e['target']))
    return {'nodes': nodes, 'edges': edges, 'has_cycles': bool(cycles),
            'hidden_dependency_count': sum(hidden.values())}


def task_trace(task_id: str, workspace: str, visible_tasks: list[dict], events: list[dict], task_center) -> dict:
    """Scope all trace evidence to owned tasks; never expose raw result or logs."""
    tasks = {str(t['id']): t for t in visible_tasks}
    if task_id not in tasks:
        raise ValueError('Task not visible')
    t = tasks[task_id]
    original = task_center.get(task_id)
    # The read was performed after workspace ownership validation. Re-check
    # tenant because databases may be shared with untrusted integrations.
    if original is None or original.tenant != workspace:
        # Ownership is the authorization source; this extra check protects
        # against accidental cross-tenant task ID reassignment.
        raise ValueError('Task state mismatch')
    accepted = []
    for ev in events:
        if ev.get('subject') == task_id or (isinstance(ev.get('payload'), dict) and ev['payload'].get('task_id') == task_id):
            accepted.append({'id': ev['id'], 'kind': ev['kind'], 'created': ev['created'],
                             'subject': ev['subject'], 'source':'collaboration'})
    accepted.extend(task_center.lifecycle_events(task_id))
    accepted.sort(key=lambda ev: (ev['created'],ev['source'],ev['id']))
    return {'task': {'id': t['id'], 'title': t['prompt'], 'status': t['status'],
                     'attempts': t.get('attempts', 0), 'approved': t.get('approved', False),
                     'depends_on': [d for d in (t.get('depends_on') or []) if str(d) in tasks],
                     'required_capabilities': list(original.required_capabilities)[:50],
                     'selected_capability': original.selected_capability,
                     'error': (original.error or '')[:2000],
                     'result_available': original.result is not None,
                     'updated_at': original.updated_at},
            'events': accepted[-100:],
            'scope': 'workspace-owned-events-only'}
