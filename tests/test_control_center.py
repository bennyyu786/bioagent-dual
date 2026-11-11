import json
import sqlite3
import urllib.error
import urllib.request
import pytest
from biomni_dual import (OperationsStore,TaskCenter,CapabilityRegistry,
    CapabilityCenter,PersistentRuntimeStore,GoalState,TaskStatus,
    Capability,CapabilityKind)
from biomni_dual.control_center import ControlCenter,ControlCenterServer,ConversationStore

TOKEN='safe-operator-token-0123456789-abcdefghijklmnopqrstuvwxyz'


def setup(tmp_path, *, with_runtime=True):
    tasks=TaskCenter(tmp_path/'tasks.db')
    ops=OperationsStore(str(tmp_path/'ops.db'))
    ops.heartbeat()
    conversations=ConversationStore(str(tmp_path/'chats.db'))
    runtime=PersistentRuntimeStore(str(tmp_path/'runtime.db')) if with_runtime else None
    registry=CapabilityRegistry()
    registry.register(Capability(id='sample.skill',description='Example skill',kind=CapabilityKind.SKILL))
    capabilities=CapabilityCenter(registry,str(tmp_path/'capabilities.db'))
    ctl=ControlCenter(conversations,tasks,ops,token=TOKEN,registry=registry,
        capabilities=capabilities,runtime=runtime,
        remediation_db=str(tmp_path/'repair.db'),experiments_db=str(tmp_path/'experiments.db'))
    server=ControlCenterServer(ctl,port=0).start()
    base='http://127.0.0.1:'+str(server.address[1])
    return ctl,server,base


def request(url, data=None, token=TOKEN, method=None, origin=None):
    headers={'Authorization':'Bearer '+token}
    if data is not None:headers['Content-Type']='application/json'
    if origin:headers['Origin']=origin
    req=urllib.request.Request(url,data=json.dumps(data).encode() if data is not None else None,
                               headers=headers,method=method)
    return urllib.request.urlopen(req)


def test_control_api_requires_token_and_html_safe(tmp_path):
    ctl,server,base=setup(tmp_path)
    try:
        html=urllib.request.urlopen(base+'/control').read().decode()
        assert 'Biomni Control Center' in html
        assert '对话与任务入口' in html
        assert TOKEN not in html
        with pytest.raises(urllib.error.HTTPError) as ex:
            urllib.request.urlopen(base+'/api/control/overview')
        assert ex.value.code==401
        with pytest.raises(urllib.error.HTTPError) as ex:
            request(base+'/api/control/overview',token='bad')
        assert ex.value.code==401
        with request(base+'/api/control/overview') as res:
            overview=json.load(res)
            assert 'sample.skill' in [x['id'] for x in overview['capabilities']]
            assert 'Authorization' not in res.headers
    finally:server.stop()


def test_chat_persistence_and_explicit_idempotent_task_submit(tmp_path):
    ctl,server,base=setup(tmp_path)
    try:
        msg='对比两种新算法对数据质量的影响'
        response=json.load(request(base+'/api/control/chat',{'session':'main','message':msg}))
        assert response['user']['text']==msg
        assert '尚未配置 LLM' in response['assistant']['text']
        assert ctl.tasks.list()==[]
        message_id=response['user']['id']
        payload={'session':'main','message_id':message_id}
        a=json.load(request(base+'/api/control/submit-task',payload))
        b=json.load(request(base+'/api/control/submit-task',payload))
        assert a['id']==b['id'] and len(ctl.tasks.list())==1
        assert ctl.tasks.list()[0].prompt==msg
        assert ctl.tasks.list()[0].status==TaskStatus.PENDING
        messages=json.load(request(base+'/api/control/messages?session=main'))['messages']
        assert len(messages)==2
        again=ConversationStore(ctl.conversations.path)
        assert again.list('main')[0]['text']==msg
        snapshot=json.load(request(base+'/api/control/overview'))
        assert snapshot['tasks'][0]['depends_on']==[]
    finally:server.stop()


def test_unauthorized_mutations_and_cross_session_rejected(tmp_path):
    ctl,server,base=setup(tmp_path)
    try:
        msg=json.load(request(base+'/api/control/chat',{'session':'main','message':'Create task'}))
        payload={'session':'other','message_id':msg['user']['id']}
        with pytest.raises(urllib.error.HTTPError) as ex:
            request(base+'/api/control/submit-task',payload)
        assert ex.value.code==400
        with pytest.raises(urllib.error.HTTPError) as ex:
            request(base+'/api/control/submit-task',{'session':'main','message_id':msg['user']['id']},token='bad')
        assert ex.value.code==401
        with pytest.raises(urllib.error.HTTPError) as ex:
            request(base+'/api/control/chat',{'session':'main','message':'bad'},origin='https://attacker.example')
        assert ex.value.code==403
        assert ctl.tasks.list()==[]
    finally:server.stop()


def test_task_dag_and_approval(tmp_path):
    ctl,server,base=setup(tmp_path)
    try:
        a=ctl.tasks.submit('First')
        b=ctl.tasks.submit('Second',depends_on=[a.id])
        ctl.tasks.require_approval(b.id)
        overview=json.load(request(base+'/api/control/overview'))
        assert next(x for x in overview['tasks'] if x['id']==b.id)['depends_on']==[a.id]
        assert any(x['kind']=='task' and x['id']==b.id for x in overview['approvals'])
        res=json.load(request(base+'/api/control/approve',{'kind':'task','id':b.id,'approve':True}))
        assert res['status']=='pending'
        assert ctl.tasks.get(b.id).approved
        with pytest.raises(urllib.error.HTTPError):
            request(base+'/api/control/approve',{'kind':'task','id':b.id,'approve':True})
    finally:server.stop()


def test_capability_proposal_approval_but_no_publish(tmp_path):
    ctl,server,base=setup(tmp_path)
    try:
        p=ctl.capabilities.propose_gaps(['missing.mcp'],reason='not available')
        assert p[0].status=='proposed'
        overview=json.load(request(base+'/api/control/overview'))
        assert any(x['id']=='missing.mcp' for x in overview['approvals'])
        res=json.load(request(base+'/api/control/approve',{'kind':'capability','id':'missing.mcp','approve':True}))
        assert res['status']=='approved'
        assert ctl.registry.get('missing.mcp') is None
    finally:server.stop()


def test_goal_pause_resume_and_no_unregistered_goal(tmp_path):
    ctl,server,base=setup(tmp_path)
    try:
        ctl.runtime.register_goal('g1')
        snapshot=json.load(request(base+'/api/control/overview'))
        assert snapshot['goals'][0]['state']=='active'
        assert json.load(request(base+'/api/control/goal-state',{'id':'g1','state':'paused'}))['state']=='paused'
        assert ctl.runtime.state('g1')==GoalState.PAUSED
        assert json.load(request(base+'/api/control/goal-state',{'id':'g1','state':'active'}))['state']=='active'
        with pytest.raises(urllib.error.HTTPError):
            request(base+'/api/control/goal-state',{'id':'unknown','state':'paused'})
    finally:server.stop()


def test_malformed_json_and_payload_limits(tmp_path):
    ctl,server,base=setup(tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as ex:
            request(base+'/api/control/chat',{'session':'main','message':'x'*4500})
        assert ex.value.code==400
        req=urllib.request.Request(base+'/api/control/chat',data=b'{bad json',
            method='POST',headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'})
        with pytest.raises(urllib.error.HTTPError) as ex:
            urllib.request.urlopen(req)
        assert ex.value.code==400
    finally:server.stop()


def test_readonly_auxiliary_stores_never_leak_payload(tmp_path):
    ctl,server,base=setup(tmp_path)
    try:
        with sqlite3.connect(ctl.remediation_db) as db:
            db.execute('CREATE TABLE remediation_jobs(id TEXT,task_id TEXT,route TEXT,state TEXT,reason TEXT,updated REAL)')
            db.execute('INSERT INTO remediation_jobs VALUES (?,?,?,?,?,?)',('r1','t1','replan','proposed','needs repair',1.0))
        with sqlite3.connect(ctl.experiments_db) as db:
            db.execute('CREATE TABLE active_experiments(id TEXT,state TEXT,created REAL,secret TEXT)')
            db.execute('INSERT INTO active_experiments VALUES (?,?,?,?)',('e1','proposed',1.0,'SECRET_NOT_VISIBLE'))
        result=json.load(request(base+'/api/control/overview'))
        assert result['repairs'][0]['route']=='replan'
        assert result['experiments'][0]['id']=='e1'
        assert 'SECRET_NOT_VISIBLE' not in json.dumps(result)
    finally:server.stop()


def test_reject_weak_token_and_remote_bind(tmp_path):
    ops=OperationsStore(str(tmp_path/'ops.db'))
    with pytest.raises(ValueError):
        ControlCenter(ConversationStore(str(tmp_path/'chat.db')),TaskCenter(tmp_path/'tasks.db'),ops,token='weak')
    ctl=ControlCenter(ConversationStore(str(tmp_path/'chat.db')),TaskCenter(tmp_path/'tasks.db'),ops,token=TOKEN)
    with pytest.raises(ValueError):ControlCenterServer(ctl,host='0.0.0.0',port=0)


def test_optional_advisor_is_text_only_and_safe_fallback(tmp_path):
    from biomni_dual.chat_advisor import OpenAICompatibleAdvisor
    with pytest.raises(ValueError):OpenAICompatibleAdvisor('http://remote.example/v1','secret','m')
    with pytest.raises(ValueError):OpenAICompatibleAdvisor('https://example.com/v1','','m')
    ctl,server,base=setup(tmp_path)
    try:
        ctl.advisor=lambda message,history: '可先做基线数据 QC，然后审批任务。'
        ans=json.load(request(base+'/api/control/chat',{'session':'main','message':'推荐分析流程'}))
        assert '数据 QC' in ans['assistant']['text']
        assert ctl.tasks.list()==[]
        ctl.advisor=lambda m,h: (_ for _ in ()).throw(RuntimeError('secret key value'))
        ans=json.load(request(base+'/api/control/chat',{'session':'main','message':'再试一次'}))
        assert 'secret' not in json.dumps(ans)
    finally:server.stop()


def test_observed_runtime_optional_control_server(tmp_path):
    from biomni_dual import serve_observed
    import asyncio
    ctl,server,base=setup(tmp_path)
    server.stop()
    class DummyRuntime:
        async def serve(self,max_loops=None):pass
    async def run():
        await serve_observed(DummyRuntime(),ctl.operations,port=0,
                             interval=.001,max_loops=1,control_center=ctl)
    asyncio.run(run())
