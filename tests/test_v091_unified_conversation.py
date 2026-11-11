"""v0.9.1: the minimalist conversation is a real scoped API, not a UI mock."""
from test_web_planning import rig, configure
from biomni_dual.conversation_router import conversation_route
from biomni_dual import collaboration_api


def test_route_readonly_questions_and_side_effects():
    assert conversation_route('你好，你叫什么名字？') == 'chat'
    assert conversation_route('为什么天是蓝的？') == 'chat'
    assert conversation_route('查询当前 Worker 状态') == 'task'
    assert conversation_route('删除数据库') == 'task'
    assert conversation_route('删除数据库', 'chat') == 'chat'  # no tools in chat
    assert conversation_route('你好', 'task') == 'task'


def test_chat_endpoint_scoped_and_uses_model_without_task(monkeypatch, tmp_path):
    c,admin,op,t,rel,calls = rig(tmp_path, 'not a plan')
    assert c.post('/api/v2/main/assistant/turn',headers=op,json={'message':'你好？','request_id':'hello-09101'}).status_code==409
    assert c.post('/api/v2/main/models/config',headers=admin,json={'base_url':'https://example.org/v1','model':'test-model'}).status_code==200
    monkeypatch.setenv('BIOMNI_MODEL_API_KEY','not-real')
    seen=[]
    class FakeAdvisor:
        def __init__(self,base,key,model):seen.append((base,key,model))
        def __call__(self,message,history):
            seen.append((message,history))
            return '我是 Biomni AI。'
    monkeypatch.setattr(collaboration_api,'OpenAICompatibleAdvisor',FakeAdvisor)
    body={'message':'你好，你叫什么名字？','mode':'auto','session':'custom','request_id':'hello-09102'}
    resp=c.post('/api/v2/main/assistant/turn',headers=op,json=body)
    assert resp.status_code==200,resp.text
    assert resp.json()['route']=='chat' and resp.json()['answer']=='我是 Biomni AI。'
    assert len(t.list())==0 and calls==[]
    session=c.get('/api/v2/main/assistant/session/custom',headers=op).json()
    assert [m['role'] for m in session['messages']]==['user','assistant']
    assert [m['body'] for m in session['messages']]==[body['message'],'我是 Biomni AI。']
    assert session['tasks']=={}
    assert c.get('/api/v2/other/assistant/session/custom',headers=op).status_code==403
    assert seen[0]==('https://example.org/v1','not-real','test-model')


def test_task_turn_links_source_and_requires_approved_executor(tmp_path):
    c,admin,op,t,rel,calls=rig(tmp_path,'not a plan')
    response=c.post('/api/v2/main/assistant/turn',headers=op,json={'message':'你好，你是谁？','mode':'task','session':'main','request_id':'hello-task-091'})
    assert response.status_code==200,response.text
    payload=response.json()
    assert payload['route']=='task' and payload['dispatch']['state'] != 'offered'
    session=c.get('/api/v2/main/assistant/session/main',headers=op).json()
    assert len(session['messages'])==1
    assert session['tasks'][payload['message_id']]['id']==payload['task_id']
    assert t.get(payload['task_id']).status.value=='pending'
    assert calls==[]


def test_viewer_cannot_submit_turn_and_invalid_mode_is_rejected(tmp_path):
    c,admin,op,t,rel,calls=rig(tmp_path,'not a plan')
    assert c.post('/api/v2/main/assistant/turn',headers=op,json={'message':'a','mode':'invalid','request_id':'bad-mode-091'}).status_code==400
    viewer=c.post('/api/v2/main/members',headers=admin,json={'email':'viewer@example.org','password':'SomeLongPassword456','role':'viewer'})
    assert viewer.status_code==200
    token=c.post('/api/v2/login',json={'email':'viewer@example.org','password':'SomeLongPassword456'}).json()['token']
    assert c.post('/api/v2/main/assistant/turn',headers={'Authorization':'Bearer '+token},json={'message':'hello','mode':'chat','request_id':'viewer-091'}).status_code==403
