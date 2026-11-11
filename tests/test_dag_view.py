from fastapi.testclient import TestClient
from biomni_dual.dag_view import task_graph
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.collaboration_api import create_app
from biomni_dual.scheduler import TaskCenter
from biomni_dual.observability import OperationsStore


def test_deterministic_layout_and_missing_dependencies():
    tasks = [
        {'id': 'b', 'prompt': 'B', 'status': 'running', 'depends_on': ['a', 'invisible']},
        {'id': 'a', 'prompt': 'A', 'status': 'completed', 'depends_on': []},
        {'id': 'c', 'prompt': 'C', 'status': 'pending', 'depends_on': ['b']},
    ]
    graph = task_graph(tasks)
    assert [n['id'] for n in graph['nodes']] == ['a', 'b', 'c']
    assert [n['rank'] for n in graph['nodes']] == [0, 1, 2]
    assert graph['edges'] == [{'source': 'a', 'target': 'b'}, {'source': 'b', 'target': 'c'}]
    assert 'invisible' not in str(graph)
    assert graph['hidden_dependency_count'] == 1
    assert graph == task_graph(list(reversed(tasks)))


def test_cycle_detection_is_bounded():
    g = task_graph([{'id':'a','depends_on':['b']}, {'id':'b','depends_on':['a']}])
    assert g['has_cycles'] is True
    assert len(g['nodes']) == 2
    assert all(n['cycle'] for n in g['nodes'])


def test_graph_and_trace_are_authorized_and_tenant_scoped(tmp_path):
    c=CollaborationStore(tmp_path/'team.db')
    owner=c.bootstrap('owner@example.com','SecurePassword_1234')
    c.workspace_create(owner,'other','Other')
    tasks=TaskCenter(tmp_path/'tasks.db')
    app=TestClient(create_app(c,tasks,OperationsStore(tmp_path/'ops.db')))
    login=app.post('/api/v2/login',json={'email':'owner@example.com','password':'SecurePassword_1234'})
    h={'Authorization':'Bearer '+login.json()['token']}
    one=app.post('/api/v2/main/tasks',headers=h,json={'prompt':'Main task','request_id':'request001'})
    two=app.post('/api/v2/other/tasks',headers=h,json={'prompt':'Other task','request_id':'request002'})
    assert one.status_code==two.status_code==200
    a,b=one.json()['id'],two.json()['id']
    graph=app.get('/api/v2/main/graph',headers=h)
    assert graph.status_code==200 and [n['id'] for n in graph.json()['nodes']]==[a]
    assert b not in graph.text
    assert app.get('/api/v2/main/tasks/'+b+'/trace',headers=h).status_code==404
    trace=app.get('/api/v2/main/tasks/'+a+'/trace',headers=h)
    assert trace.status_code==200 and trace.json()['task']['id']==a
    assert 'result' not in trace.json()['task']
    assert 'Other task' not in trace.text
    # No auth, no workspace permission, or no owned task => deny.
    assert app.get('/api/v2/main/graph').status_code==401
    assert app.get('/api/v2/main/tasks/'+a+'/trace').status_code==401
    assert app.get('/api/v2/unknown/graph',headers=h).status_code==403


def test_trace_event_scope_and_redaction(tmp_path):
    c=CollaborationStore(tmp_path/'team.db')
    uid=c.bootstrap('owner@example.com','SecurePassword_1234')
    t=TaskCenter(tmp_path/'tasks.db')
    app=TestClient(create_app(c,t,OperationsStore(tmp_path/'ops.db')))
    h={'Authorization':'Bearer '+app.post('/api/v2/login',json={'email':'owner@example.com','password':'SecurePassword_1234'}).json()['token']}
    tid=app.post('/api/v2/main/tasks',headers=h,json={'prompt':'test','request_id':'request003'}).json()['id']
    c.emit('main','task_progress',tid,{'secret':'dont echo raw payload'})
    c.emit('main','worker_log','other',{'text':'unrelated'})
    d=app.get('/api/v2/main/tasks/'+tid+'/trace',headers=h).json()
    assert any(e['kind']=='task_progress' for e in d['events'])
    assert 'dont echo' not in str(d)
    assert 'unrelated' not in str(d)


def test_frontend_includes_interactive_canvas(tmp_path):
    from pathlib import Path
    src=Path(__file__).parents[1]/'biomni_dual/web/collaboration.html'
    html=src.read_text()
    for token in ('dagviewport','graphNodes()','renderGraph(target)','zoom-in','jump-task-chat','data-node'):
        assert token in html


def test_chat_to_task_evidence_and_lifecycle(tmp_path):
    c=CollaborationStore(tmp_path/'team.db')
    admin=c.bootstrap('owner@example.com','SecurePassword_1234')
    c.workspace_create(admin,'other','Other')
    t=TaskCenter(tmp_path/'tasks.db')
    client=TestClient(create_app(c,t,OperationsStore(tmp_path/'ops.db')))
    auth=client.post('/api/v2/login',json={'email':'owner@example.com','password':'SecurePassword_1234'}).json()['token']
    h={'Authorization':'Bearer '+auth}
    msg=client.post('/api/v2/main/chat',headers=h,json={'session':'research','body':'Prepare cohort QC'}).json()['id']
    other=client.post('/api/v2/other/chat',headers=h,json={'session':'research','body':'Other confidential'}).json()['id']
    # Source content, authorship, and workspace must match the explicit conversion.
    base={'prompt':'Prepare cohort QC','source_message_id':msg,'request_id':'req_001001'}
    assert client.post('/api/v2/other/tasks',headers=h,json={**base,'request_id':'req_001002'}).status_code==403
    assert client.post('/api/v2/main/tasks',headers=h,json={**base,'source_message_id':other}).status_code==403
    assert client.post('/api/v2/main/tasks',headers=h,json={**base,'prompt':'Different task'}).status_code==400
    submitted=client.post('/api/v2/main/tasks',headers=h,json=base)
    assert submitted.status_code==200
    tid=submitted.json()['id']
    trace=client.get('/api/v2/main/tasks/'+tid+'/trace',headers=h).json()
    assert trace['source_message']=={'id':msg,'session':'research'}
    assert any(e['source']=='task_center' and e['kind']=='task_submitted' for e in trace['events'])
    assert not any('Other confidential' in str(e) for e in trace['events'])


def test_viewer_cannot_convert_another_users_message(tmp_path):
    c=CollaborationStore(tmp_path/'team.db')
    uid=c.bootstrap('owner@example.com','SecurePassword_1234')
    c.invite(uid,'main','operator@example.com','StrongPassword_5678','operator')
    t=TaskCenter(tmp_path/'tasks.db')
    client=TestClient(create_app(c,t,OperationsStore(tmp_path/'ops.db')))
    def h(email,pw):
        return {'Authorization':'Bearer '+client.post('/api/v2/login',json={'email':email,'password':pw}).json()['token']}
    ha=h('owner@example.com','SecurePassword_1234')
    hb=h('operator@example.com','StrongPassword_5678')
    mid=client.post('/api/v2/main/chat',headers=ha,json={'session':'main','body':'Sensitive cohort'}).json()['id']
    assert client.post('/api/v2/main/tasks',headers=hb,json={'prompt':'Sensitive cohort','source_message_id':mid,'request_id':'req_002002'}).status_code==403


def test_worker_log_can_be_correlated_to_owned_task_only(tmp_path):
    c=CollaborationStore(tmp_path/'team.db')
    uid=c.bootstrap('owner@example.com','SecurePassword_1234')
    c.workspace_create(uid,'other','Other')
    t=TaskCenter(tmp_path/'tasks.db')
    client=TestClient(create_app(c,t,OperationsStore(tmp_path/'ops.db')))
    h={'Authorization':'Bearer '+client.post('/api/v2/login',json={'email':'owner@example.com','password':'SecurePassword_1234'}).json()['token']}
    own=client.post('/api/v2/main/tasks',headers=h,json={'prompt':'Main task','request_id':'req_003001'}).json()['id']
    other=client.post('/api/v2/other/tasks',headers=h,json={'prompt':'Other task','request_id':'req_003002'}).json()['id']
    w=client.post('/api/v2/main/workers',headers=h,json={'id':'remote-01'}).json()
    wh={'X-Worker-Secret':w['secret']}
    wrong=client.post('/api/v2/worker/remote-01/logs',headers=wh,json={'level':'info','message':'should deny','task_id':other})
    assert wrong.status_code==403
    good=client.post('/api/v2/worker/remote-01/logs',headers=wh,json={'level':'info','message':'step reached','task_id':own})
    assert good.status_code==200
    trace=client.get('/api/v2/main/tasks/'+own+'/trace',headers=h).json()
    assert any(e['kind']=='worker_log' for e in trace['events'])
    assert 'step reached' not in str(trace)  # Raw worker text stays in restricted live logs
