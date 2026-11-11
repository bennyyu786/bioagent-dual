"""A deterministic, tenant-safe read-only projection of both orchestration centers.

This graph shows only directly evidenced relationships:
- task dependencies from workspace-owned tasks,
- declared requirements and selected capabilities from workspace-owned TaskCenter rows,
- worker-to-task telemetry only from authenticated workspace log events that
  explicitly cite the task ID.
Unknown capability health and worker scheduling are never guessed.
"""
from __future__ import annotations
from collections import defaultdict
from .dag_view import task_graph


def execution_topology(visible_tasks: list[dict], task_center, workers: list[dict],
                       events: list[dict], *, workspace: str, runtime: dict | None = None, dispatches: list[dict] | None = None) -> dict:
    task_ids={str(t['id']) for t in visible_tasks}
    dag=task_graph(visible_tasks)
    nodes=[]; edges=[]
    for task in dag['nodes']:
        nodes.append({'id':'task:'+task['id'],'ref':task['id'],'kind':'task',
                      'label':task['title'],'status':task['status'],
                      'rank':task['rank'],'attempts':task['attempts'],
                      'hidden_dependencies':task['hidden_dependencies']})
    edges.extend({'source':'task:'+e['source'],'target':'task:'+e['target'],
                  'kind':'depends_on','evidence':'task_dag'} for e in dag['edges'])
    capabilities:dict[str,dict]={}
    for tid in sorted(task_ids):
        original=task_center.get(tid)
        if original is None or original.tenant != workspace:
            # A mismatched task is not a valid source of topology evidence.
            continue
        for cid in sorted(set(str(x) for x in original.required_capabilities if x)):
            capabilities.setdefault(cid,{'id':'cap:'+cid,'ref':cid,'kind':'capability',
                                         'label':cid,'status':'unknown','source':'declared_requirement'})
            edges.append({'source':'task:'+tid,'target':'cap:'+cid,
                          'kind':'requires','evidence':'task_requirement'})
        if original.selected_capability:
            cid=str(original.selected_capability)
            capabilities.setdefault(cid,{'id':'cap:'+cid,'ref':cid,'kind':'capability',
                                         'label':cid,'status':'unknown','source':'selected_capability'})
            edges.append({'source':'task:'+tid,'target':'cap:'+cid,
                          'kind':'selected','evidence':'task_selection'})
    nodes.extend(capabilities[k] for k in sorted(capabilities))
    worker_ids={str(w['id']) for w in workers}
    nodes.extend({'id':'worker:'+str(w['id']),'ref':str(w['id']),'kind':'worker',
                  'label':str(w['id']),'status':str(w['state']),
                  'worker_kind':str(w.get('kind','unknown')),'age_seconds':w.get('age_seconds')}
                 for w in sorted(workers,key=lambda x:str(x['id'])))
    # Telemetry evidence links a worker and task, not necessarily an active
    # assignment: the graph deliberately names the edge "reported_on".
    latest:dict[tuple[str,str],int]={}
    for ev in events:
        if ev.get('kind')!='worker_log': continue
        worker=str(ev.get('subject',''))
        payload=ev.get('payload') or {}
        if not isinstance(payload,dict):continue
        tid=str(payload.get('task_id',''))
        if worker in worker_ids and tid in task_ids:
            latest[(worker,tid)]=max(latest.get((worker,tid),0),int(ev['id']))
    for (worker,tid),event_id in sorted(latest.items()):
        edges.append({'source':'worker:'+worker,'target':'task:'+tid,
                      'kind':'reported_on','evidence':'authenticated_worker_log',
                      'event_id':event_id})
    # Trust only the server-controlled workspace-scoped ledger, never user logs,
    # for dispatch intent, authenticated claims, quotas and lifecycle evidence.
    if runtime is not None:
        for cap in runtime['capabilities']:
            cid=cap['id']
            node_id='cap:'+cid
            target=next((n for n in nodes if n['id']==node_id),None)
            if target is None:
                target={'id':node_id,'ref':cid,'kind':'capability','label':cid,
                        'status':'unknown','source':'runtime_lifecycle'}
                nodes.append(target)
            target['status']=cap['lifecycle']
            target['lifecycle_evidence']=cap['evidence_ref']
        worker_capacity={c['worker']:c for c in runtime['capacity']}
        for n in nodes:
            if n['kind']=='worker' and n['ref'] in worker_capacity:
                n['resources']=worker_capacity[n['ref']]
        for a in runtime['assignments']:
            if a['task'] not in task_ids or a['worker'] not in worker_ids:
                continue
            # Historical terminal reservations are shown in the detail list,
            # not mistaken for current assignments.
            if a['state'] in ('reserved','claimed','reported','uncertain'):
                edges.append({'source':'task:'+a['task'],'target':'worker:'+a['worker'],
                              'kind':'reservation','evidence':'runtime_ledger',
                              'reservation_id':a['id'],'state':a['state']})
        for r in runtime['repairs']:
            if r['task'] in task_ids and r['repair_task'] in task_ids:
                edges.append({'source':'task:'+r['task'],'target':'task:'+r['repair_task'],
                              'kind':'repair_dependency','evidence':'runtime_repair_record',
                              'state':r['state']})
    for job in dispatches or []:
        if job['task_id'] not in task_ids or job['worker'] not in worker_ids:continue
        if job['state'] in ('offered','running','review_required','reviewing','uncertain'):
            edges.append({'source':'task:'+job['task_id'],'target':'worker:'+job['worker'],
                          'kind':'dispatch','evidence':'dispatch_ledger',
                          'dispatch_id':job['id'],'state':job['state']})
    edges.sort(key=lambda e:(e['kind'],e['source'],e['target']))
    counts={'tasks':len(dag['nodes']),'capabilities':sum(n['kind']=='capability' for n in nodes),
            'workers':len(worker_ids),'running_tasks':sum(n['status']=='running' for n in dag['nodes']),
            'waiting_approval':sum(n['status']=='waiting_approval' for n in dag['nodes']),
            'failed_or_blocked':sum(n['status'] in ('failed','blocked') for n in dag['nodes'])}
    return {'nodes':nodes,'edges':edges,'counts':counts,
            'hidden_dependency_count':dag['hidden_dependency_count'],
            'has_cycles':dag['has_cycles'],
            'scope':'workspace','semantics':{
                'requires':'task declares a capability requirement; availability unverified',
                'selected':'TaskCenter records selected capability; health unverified',
                'reported_on':'worker log referenced task; NOT proof of active assignment'},
            'runtime':runtime,'dispatches':dispatches or [], 'version':3 if dispatches else (2 if runtime is not None else 1)}
