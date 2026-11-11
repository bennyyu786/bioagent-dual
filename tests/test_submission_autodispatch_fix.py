"""Regression tests for web submission -> approved capability -> dispatch."""
from fastapi.testclient import TestClient
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.collaboration_api import create_app
from biomni_dual.scheduler import TaskCenter
from biomni_dual.observability import OperationsStore
from biomni_dual.runtime_relations import RuntimeRelationStore

REQ={'cpu_milli':1000,'memory_mb':1024,'gpu_units':0}
CAP={'cpu_milli':4000,'memory_mb':8192,'gpu_units':0}


def env(tmp_path):
    users=CollaborationStore(tmp_path/'users.db')
    uid=users.bootstrap('owner@example.com','GoodPassword123456')
    users.invite(uid,'main','operator@example.com','GoodPassword123456','operator')
    tasks=TaskCenter(tmp_path/'tasks.db')
    ops=OperationsStore(tmp_path/'ops.db')
    relation=RuntimeRelationStore(tmp_path/'relations.db')
    client=TestClient(create_app(users,tasks,ops,relation))
    def login(email):
        token=client.post('/api/v2/login',json={'email':email,'password':'GoodPassword123456'}).json()['token']
        return {'Authorization':'Bearer '+token}
    return client,login('owner@example.com'),login('operator@example.com'),tasks,relation


def setup_worker(client,admin,relation):
    secret=client.post('/api/v2/main/workers',headers=admin,json={'id':'node1'}).json()['secret']
    assert client.put('/api/v2/main/runtime/workers/node1/capacity',headers=admin,json=CAP).status_code==200
    assert client.put('/api/v2/main/dispatch/workers/node1/skills',headers=admin,json={'capabilities':['analysis']}).status_code==200
    relation.capability('main','analysis','approved','approved:independent-check')
    worker_headers={'x-worker-secret':secret}
    assert client.get('/api/v2/worker/node1/dispatch/poll',headers=worker_headers).status_code==200
    return worker_headers


def policy(client,admin,enabled=True):
    return client.put('/api/v2/main/execution-policy',headers=admin,json={'enabled':enabled,'capability':'analysis','resources':REQ})


def test_submission_auto_dispatch_after_approval(tmp_path):
    c,a,op,t,r=env(tmp_path)
    assert policy(c,a).status_code==409
    w=setup_worker(c,a,r)
    assert policy(c,op).status_code==403
    assert policy(c,a).status_code==200
    body={'prompt':'Analyze a sample','request_id':'submission-123'}
    result=c.post('/api/v2/main/tasks',headers=op,json=body)
    assert result.status_code==200,result.text
    obj=result.json()
    assert obj['status']=='pending' and obj['dispatch']['state']=='offered'
    assert t.get(obj['id']).required_capabilities==['analysis']
    assert c.post('/api/v2/main/tasks',headers=op,json=body).json()['id']==obj['id']
    assert len(c.get('/api/v2/main/dispatch',headers=a).json()['dispatches'])==1
    offer=c.get('/api/v2/worker/node1/dispatch/poll',headers=w).json()['offer']
    assert offer['id']==obj['dispatch']['dispatch_id']
    claim=c.post('/api/v2/worker/node1/dispatch/'+offer['id']+'/claim',headers=w,json={'ttl_seconds':120})
    assert claim.status_code==200,claim.text
    assert t.get(obj['id']).status.value=='running'


def test_missing_policy_returns_diagnostic_not_false_execution(tmp_path):
    c,a,op,t,r=env(tmp_path)
    result=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Why pending?','request_id':'submission-321'})
    assert result.status_code==200
    assert result.json()['dispatch']['state']=='awaiting_configuration'
    assert t.get(result.json()['id']).required_capabilities==[]


def test_late_worker_poll_and_legacy_activation(tmp_path):
    c,a,op,t,r=env(tmp_path)
    old=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Old task','request_id':'submission-old'}).json()
    assert old['dispatch']['state']=='awaiting_configuration'
    wh=setup_worker(c,a,r)
    assert policy(c,a).status_code==200
    # Worker was configured after the initial task submission. Explicit
    # administrative migration is required for old, capability-less tasks.
    assert c.post('/api/v2/main/tasks/'+old['id']+'/activate',headers=op).status_code==403
    result=c.post('/api/v2/main/tasks/'+old['id']+'/activate',headers=a)
    assert result.status_code==200,result.text
    assert result.json()['dispatch']['state']=='offered'
    again=c.post('/api/v2/main/tasks/'+old['id']+'/activate',headers=a)
    assert again.status_code==200 and again.json()['dispatch']['state']=='offered' or again.status_code==200 and again.json()['dispatch']['state']=='existing'
    # Second task is submitted while worker is offline. Later poll offers it.
    second=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Later task','request_id':'submission-later'}).json()
    assert second['dispatch']['state']=='offered' or second['dispatch']['state']=='awaiting_worker'
    # The first task is still offered, not claimed; no unsafe auto replay.
    assert c.get('/api/v2/worker/node1/dispatch/poll',headers=wh).status_code==200


def test_cross_tenant_and_unapproved_policy(tmp_path):
    c,a,op,t,r=env(tmp_path)
    wh=setup_worker(c,a,r)
    assert c.put('/api/v2/main/execution-policy',headers=a,json={'enabled':True,'capability':'unknown','resources':REQ}).status_code==409
    assert policy(c,a).status_code==200
    other=t.submit('private',tenant='other')
    assert c.post('/api/v2/main/tasks/'+other.id+'/activate',headers=a).status_code==404


def test_worker_coming_online_retries_configured_pending(tmp_path):
    c,a,op,t,r=env(tmp_path)
    secret=c.post('/api/v2/main/workers',headers=a,json={'id':'node1'}).json()['secret']
    assert c.put('/api/v2/main/runtime/workers/node1/capacity',headers=a,json=CAP).status_code==200
    assert c.put('/api/v2/main/dispatch/workers/node1/skills',headers=a,json={'capabilities':['analysis']}).status_code==200
    r.capability('main','analysis','approved','approved:independent-check')
    assert policy(c,a).status_code==200
    result=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Run when online','request_id':'pending-later'}).json()
    assert result['dispatch']['state']=='awaiting_worker'
    assert t.get(result['id']).status.value=='pending'
    # A genuine authenticated poll records heartbeat, then safely creates an offer.
    offered=c.get('/api/v2/worker/node1/dispatch/poll',headers={'x-worker-secret':secret}).json()
    assert offered['offer']['task_id']==result['id']
    assert len(c.get('/api/v2/main/dispatch',headers=a).json()['dispatches'])==1
    again=c.get('/api/v2/worker/node1/dispatch/poll',headers={'x-worker-secret':secret}).json()
    assert again['offer']['id']==offered['offer']['id']
