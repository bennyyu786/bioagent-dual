import asyncio
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from biomni_dual.collaboration import CollaborationStore,Forbidden,InvalidCredentials
from biomni_dual.collaboration_api import create_app
from biomni_dual.scheduler import TaskCenter
from biomni_dual.observability import OperationsStore

@pytest.fixture
def team(tmp_path):
    store=CollaborationStore(tmp_path/'members.db')
    store.bootstrap('admin@example.com','A-long-secure-password!')
    tasks=TaskCenter(tmp_path/'tasks.db')
    ops=OperationsStore(tmp_path/'ops.db')
    client=TestClient(create_app(store,tasks,ops))
    login=client.post('/api/v2/login',json={'email':'admin@example.com','password':'A-long-secure-password!'})
    assert login.status_code==200
    token=login.json()['token']
    headers={'Authorization':'Bearer '+token}
    return store,tasks,ops,client,headers

def test_bootstrap_login_throttling_and_revocation(team):
    db,tasks,ops,c,h=team
    with pytest.raises(Forbidden):db.bootstrap('next@example.com','anothersecurepassword!')
    assert c.get('/api/v2/me').status_code==401
    assert c.get('/api/v2/me',headers=h).status_code==200
    assert c.post('/api/v2/logout',headers=h).status_code==200
    assert c.get('/api/v2/me',headers=h).status_code==401
    for i in range(5):assert c.post('/api/v2/login',json={'email':'admin@example.com','password':'wrong password'}).status_code==401
    assert c.post('/api/v2/login',json={'email':'admin@example.com','password':'A-long-secure-password!'}).status_code==401

def test_members_roles_and_cross_workspace_isolation(team):
    db,tasks,ops,c,h=team
    assert c.post('/api/v2/main/members',headers=h,json={'email':'bob@example.com','password':'Long-secret-for-bob!','role':'viewer'}).status_code==200
    login=c.post('/api/v2/login',json={'email':'bob@example.com','password':'Long-secret-for-bob!'})
    bob={'Authorization':'Bearer '+login.json()['token']}
    assert c.get('/api/v2/main/overview',headers=bob).status_code==200
    assert c.post('/api/v2/main/tasks',headers=bob,json={'prompt':'unauthorized','request_id':'12345678'}).status_code==403
    assert c.get('/api/v2/main/members',headers=bob).status_code==403
    assert c.get('/api/v2/other/overview',headers=bob).status_code==403
    assert c.get('/api/v2/other/overview',headers=h).status_code==403
    db.workspace_create(db.identity(h['Authorization'][7:])['id'],'other','Second workspace')
    assert c.get('/api/v2/other/overview',headers=h).status_code==200

def test_task_tenant_and_chat_isolation(team):
    db,tasks,ops,c,h=team
    db.workspace_create(db.identity(h['Authorization'][7:])['id'],'other','Other workspace')
    first=c.post('/api/v2/main/tasks',headers=h,json={'prompt':'Research a dataset','request_id':'task_0011'})
    assert first.status_code==200
    again=c.post('/api/v2/main/tasks',headers=h,json={'prompt':'Research a dataset','request_id':'task_0011'})
    assert again.json()['id']==first.json()['id']
    assert tasks.get(first.json()['id']).tenant=='main'
    assert len(c.get('/api/v2/other/overview',headers=h).json()['tasks'])==0
    mid=c.post('/api/v2/main/chat',headers=h,json={'session':'main','body':'secret research'}).json()['id']
    assert mid
    assert c.get('/api/v2/other/chat/main',headers=h).json()['messages']==[]
    assert 'secret research' not in c.get('/api/v2/other/overview',headers=h).text

def test_websocket_single_use_ticket_and_event_push(team):
    db,tasks,ops,c,h=team
    result=c.post('/api/v2/main/tickets',headers=h)
    assert result.status_code==200
    ticket=result.json()['ticket']
    with c.websocket_connect('/ws/main?ticket='+ticket) as socket:
        assert c.post('/api/v2/main/chat',headers=h,json={'session':'main','body':'hello'}).status_code==200
        event=socket.receive_json()
        assert event['kind']=='chat_message'
    with pytest.raises(Exception):
        with c.websocket_connect('/ws/main?ticket='+ticket) as ws:ws.receive_json()
    other=c.post('/api/v2/main/tickets',headers=h).json()['ticket']
    with pytest.raises(Exception):
        with c.websocket_connect('/ws/other?ticket='+other) as ws:ws.receive_json()

def test_worker_registration_commands_claim_and_ack(team):
    db,tasks,ops,c,h=team
    r=c.post('/api/v2/main/workers',headers=h,json={'id':'worker-01'})
    assert r.status_code==200
    secret=r.json()['secret']
    wh={'X-Worker-Secret':secret}
    assert c.get('/api/v2/worker/worker-01/poll').status_code==401
    assert c.post('/api/v2/main/workers/worker-01/commands',headers=h,json={'action':'run_shell','payload':{'cmd':'rm -rf /'}}).status_code==400
    job=c.post('/api/v2/main/workers/worker-01/commands',headers=h,json={'action':'drain','payload':{}})
    assert job.status_code==200
    cid=job.json()['id']
    claimed=c.get('/api/v2/worker/worker-01/poll',headers=wh).json()['command']
    assert claimed['id']==cid and claimed['action']=='drain'
    assert c.get('/api/v2/worker/worker-01/poll',headers=wh).json()['command'] is None
    ack=c.post('/api/v2/worker/worker-01/commands/'+cid+'/ack',headers=wh,json={'status':'done','receipt':'drained'})
    assert ack.status_code==200
    assert c.post('/api/v2/worker/worker-01/commands/'+cid+'/ack',headers=wh,json={'status':'done'}).status_code==400
    assert c.post('/api/v2/worker/worker-01/logs',headers=wh,json={'level':'info','message':'ready\x1b[0m'}).status_code==200
    response=c.get('/api/v2/main/events',headers=h).json()['events']
    assert any(e['kind']=='worker_log' for e in response)
    assert '\\u001b' not in str(response)

def test_worker_operator_cannot_issue_commands(team):
    db,tasks,ops,c,h=team
    c.post('/api/v2/main/members',headers=h,json={'email':'ed@example.com','password':'Long-password-for-editor','role':'operator'})
    editor={'Authorization':'Bearer '+c.post('/api/v2/login',json={'email':'ed@example.com','password':'Long-password-for-editor'}).json()['token']}
    assert c.post('/api/v2/main/workers',headers=editor,json={'id':'myworker'}).status_code==403
    assert c.post('/api/v2/main/workers/abc/commands',headers=editor,json={'action':'drain','payload':{}}).status_code==403
    assert c.post('/api/v2/main/tasks',headers=editor,json={'prompt':'allowed','request_id':'submit001'}).status_code==200

def test_model_config_probe_no_credentials_leak(team):
    db,tasks,ops,c,h=team
    assert c.post('/api/v2/main/models/config',headers=h,json={'base_url':'http://metadata.google.internal/v1','model':'m'}).status_code==400
    assert c.post('/api/v2/main/models/config',headers=h,json={'base_url':'https://example.org/v1','model':'research-model'}).status_code==200
    assert c.get('/api/v2/main/overview',headers=h).json()['model']['model']=='research-model'
    assert 'api_key' not in c.get('/api/v2/main/overview',headers=h).text
    assert c.post('/api/v2/main/models/probe',headers=h).json()['connected'] is False
    assert c.post('/api/v2/main/models/chat',headers=h,json={'message':'hello'}).status_code==503

def test_html_page_and_database_events_persistent(team):
    db,tasks,ops,c,h=team
    assert 'Team workspace' in c.get('/control').text
    assert 'WebSocket' in c.get('/control').text
    c.post('/api/v2/main/chat',headers=h,json={'session':'main','body':'Persistent hello'})
    reopened=CollaborationStore(db.path)
    uid=db.identity(h['Authorization'][7:])['id']
    assert reopened.messages(uid,'main','main')[0]['body']=='Persistent hello'
    assert reopened.events(uid,'main')[0]['kind']=='chat_message'

def test_member_revocation_and_audit(team):
    db,tasks,ops,c,h=team
    uid=c.post('/api/v2/main/members',headers=h,json={'email':'agent@example.com','password':'First-long-password!','role':'operator'}).json()['id']
    at=c.post('/api/v2/login',json={'email':'agent@example.com','password':'First-long-password!'})
    agent={'Authorization':'Bearer '+at.json()['token']}
    assert c.post('/api/v2/main/tasks',headers=agent,json={'prompt':'allowed','request_id':'agent001'}).status_code==200
    assert c.post('/api/v2/main/members/'+uid+'/role',headers=h,json={'role':'viewer'}).status_code==200
    assert c.post('/api/v2/main/tasks',headers=agent,json={'prompt':'denied','request_id':'agent002'}).status_code==403
    assert c.get('/api/v2/main/overview',headers=agent).status_code==200
    assert c.delete('/api/v2/main/members/'+uid,headers=h).status_code==200
    assert c.get('/api/v2/main/overview',headers=agent).status_code==403
    assert any(r['action']=='membership_revoked' for r in c.get('/api/v2/main/audit',headers=h).json()['records'])
    assert c.delete('/api/v2/main/members/'+db.identity(h['Authorization'][7:])['id'],headers=h).status_code==403

def test_real_http_model_adapter_with_local_openai_compatible_stub(team,monkeypatch):
    """Exercise network HTTP JSON path, not an in-memory mock advisor."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    db,tasks,ops,c,h=team
    seen=[]
    class Stub(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            assert self.path=='/v1/models'
            seen.append(self.headers.get('Authorization'))
            data=json.dumps({'data':[{'id':'fake-science'}]}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        def do_POST(self):
            assert self.path=='/v1/chat/completions'
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['model']=='fake-science'
            assert not body.get('tools')
            seen.append(body['messages'][-1]['content'])
            data=json.dumps({'choices':[{'message':{'content':'Validated upstream reply'}}]}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    upstream=ThreadingHTTPServer(('127.0.0.1',0),Stub)
    runner=threading.Thread(target=upstream.serve_forever,daemon=True);runner.start()
    monkeypatch.setenv('BIOMNI_MODEL_API_KEY','dummy-key-for-local-mock')
    try:
        url='http://127.0.0.1:'+str(upstream.server_port)+'/v1'
        assert c.post('/api/v2/main/models/config',headers=h,json={'base_url':url,'model':'fake-science'}).status_code==200
        assert c.post('/api/v2/main/models/probe',headers=h).json()=={'connected':True,'models_found':1}
        d=c.post('/api/v2/main/models/chat',headers=h,json={'session':'main','message':'Analyze sample'})
        assert d.status_code==200 and d.json()['answer']=='Validated upstream reply'
        assert 'Analyze sample' in seen
        assert 'Bearer dummy-key-for-local-mock' in seen
        assert tasks.list()==[]
    finally:
        upstream.shutdown();upstream.server_close();runner.join(timeout=5)
