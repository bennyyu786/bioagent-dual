from fastapi.testclient import TestClient
from biomni_dual.execution_topology import execution_topology
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.collaboration_api import create_app
from biomni_dual.scheduler import TaskCenter
from biomni_dual.observability import OperationsStore
from biomni_dual.models import Task


def test_deterministic_and_no_cross_tenant_leaks(tmp_path):
    tasks=TaskCenter(tmp_path/'tasks.sqlite')
    first=tasks.submit('A', tenant='alpha',idempotency_key='a1',required_capabilities=['omics.skill'])
    second=tasks.submit('B', tenant='beta',idempotency_key='b1')
    first.selected_capability='omics.skill'
    tasks._save(first,'test_selection')
    visible=[{'id':first.id,'prompt':'A','status':'pending','depends_on':[second.id],'attempts':0}]
    g=execution_topology(visible,tasks,[{'id':'w1','state':'online','kind':'codex','age_seconds':2}],
       [{'id':1,'kind':'worker_log','subject':'w1','payload':{'task_id':first.id}},
        {'id':2,'kind':'worker_log','subject':'w1','payload':{'task_id':second.id}}],workspace='alpha')
    assert any(n['kind']=='capability' and n['ref']=='omics.skill' for n in g['nodes'])
    assert g['hidden_dependency_count']==1
    assert second.id not in str(g)
    assert g['counts']['workers']==1
    assert [e['kind'] for e in g['edges']].count('reported_on')==1
    assert g==execution_topology(visible,tasks,[{'id':'w1','state':'online','kind':'codex','age_seconds':2}],
       [{'id':2,'kind':'worker_log','subject':'w1','payload':{'task_id':second.id}},
        {'id':1,'kind':'worker_log','subject':'w1','payload':{'task_id':first.id}}],workspace='alpha')


def test_unknown_capability_not_claimed_healthy(tmp_path):
    tasks=TaskCenter(tmp_path/'task.db')
    task=tasks.submit('test',tenant='w',idempotency_key='tt',required_capabilities=['uninstalled.skill'])
    g=execution_topology([{'id':task.id,'prompt':'test','status':'pending'}],tasks,[],[],workspace='w')
    cap=next(n for n in g['nodes'] if n['kind']=='capability')
    assert cap['status']=='unknown'
    assert any(e['kind']=='requires' for e in g['edges'])
    assert not any(n['kind']=='worker' for n in g['nodes'])


def test_api_auth_and_workspace_scoping(tmp_path):
    c=CollaborationStore(tmp_path/'team.db')
    owner=c.bootstrap('admin@example.com','LongSecurePassword123')
    c.workspace_create(owner,'other','Other')
    tasks=TaskCenter(tmp_path/'tasks.db')
    operations=OperationsStore(tmp_path/'ops.db')
    client=TestClient(create_app(c,tasks,operations))
    token=client.post('/api/v2/login',json={'email':'admin@example.com','password':'LongSecurePassword123'}).json()['token']
    headers={'Authorization':'Bearer '+token}
    t1=client.post('/api/v2/main/tasks',headers=headers,json={'prompt':'Main 1','request_id':'request-0001'}).json()['id']
    t2=client.post('/api/v2/other/tasks',headers=headers,json={'prompt':'Secret 2','request_id':'request-0002'}).json()['id']
    assert client.get('/api/v2/main/topology').status_code==401
    assert client.get('/api/v2/ghost/topology',headers=headers).status_code==403
    g=client.get('/api/v2/main/topology',headers=headers)
    assert g.status_code==200
    assert any(n['id']=='task:'+t1 for n in g.json()['nodes'])
    assert t2 not in g.text and 'Secret 2' not in g.text
    assert g.json()['scope']=='workspace'


def test_worker_telemetry_is_evidence_not_assignment(tmp_path):
    tasks=TaskCenter(tmp_path/'tasks.db')
    t=tasks.submit('task',tenant='main',idempotency_key='t1')
    v=[{'id':t.id,'prompt':'task','status':'running'}]
    g=execution_topology(v,tasks,[{'id':'w1','state':'online'}],[],workspace='main')
    assert not any(e['kind']=='reported_on' for e in g['edges'])
    log={'id':4,'kind':'worker_log','subject':'w1','payload':{'task_id':t.id,'text':'untrusted log secret'}}
    g=execution_topology(v,tasks,[{'id':'w1','state':'online'}],[log],workspace='main')
    assert any(e['kind']=='reported_on' for e in g['edges'])
    assert 'untrusted log secret' not in str(g)


def test_frontend_has_live_topology():
    from pathlib import Path
    html=(Path(__file__).parents[1]/'biomni_dual/web/collaboration.html').read_text()
    for marker in ('data-page="topology"','renderTopology(target)','/topology','topo-to-task','topo-search','topo-refresh'):
        assert marker in html
