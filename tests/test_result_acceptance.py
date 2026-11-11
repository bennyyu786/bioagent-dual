"""Acceptance automation, independent review and UI exposure regression tests."""
from pathlib import Path
import hashlib
from fastapi.testclient import TestClient
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.collaboration_api import create_app
from biomni_dual.scheduler import TaskCenter
from biomni_dual.observability import OperationsStore
from biomni_dual.runtime_relations import RuntimeRelationStore
from biomni_dual.result_acceptance import classify_prompt, delivery_check, classify_task

RES={'cpu_milli':100,'memory_mb':256,'gpu_units':0}
CAP={'cpu_milli':2000,'memory_mb':4096,'gpu_units':1}


def setup(tmp_path, validator=None):
    coll=CollaborationStore(tmp_path/'users.db')
    admin=coll.bootstrap('admin@example.com','StrongPassword123456')
    coll.invite(admin,'main','viewer@example.com','ViewerPassword123456','viewer')
    tasks=TaskCenter(tmp_path/'tasks.db')
    ops=OperationsStore(tmp_path/'ops.db')
    ledger=RuntimeRelationStore(tmp_path/'runtime.db')
    c=TestClient(create_app(coll,tasks,ops,ledger,result_validator=validator,auto_delivery=False))
    def login(email,password):
        resp=c.post('/api/v2/login',json={'email':email,'password':password})
        return {'authorization':'Bearer '+resp.json()['token']}
    ah=login('admin@example.com','StrongPassword123456')
    vh=login('viewer@example.com','ViewerPassword123456')
    secret=c.post('/api/v2/main/workers',headers=ah,json={'id':'worker-qa'}).json()['secret']
    c.put('/api/v2/main/runtime/workers/worker-qa/capacity',headers=ah,json=CAP)
    c.put('/api/v2/main/dispatch/workers/worker-qa/skills',headers=ah,json={'capabilities':['qa']})
    c.get('/api/v2/worker/worker-qa/dispatch/poll',headers={'x-worker-secret':secret})
    ledger.capability('main','qa','approved','evidence:fixture-reviewed')
    return c,ah,vh,secret,tasks


def run_job(c,admin,secret,tasks,prompt,result='This is a substantive answer about the requested subject.',rid='job-1'):
    task=tasks.submit(prompt,tenant='main',required_capabilities=['qa'])
    o=c.post('/api/v2/main/dispatch',headers=admin,json={'task_id':task.id,'request_id':rid,'resources':RES})
    assert o.status_code==200,o.text
    did=o.json()['id']
    wh={'x-worker-secret':secret}
    assert c.post(f'/api/v2/worker/worker-qa/dispatch/{did}/claim',headers=wh,json={'ttl_seconds':300}).status_code==200
    r=c.post(f'/api/v2/worker/worker-qa/dispatch/{did}/report',headers=wh,json={'state':'completed','result':result})
    assert r.status_code==200,r.text
    return task,did,r.json()


def test_l1_requires_admin_explicit_policy_and_capability_allowlist(tmp_path):
    c,a,v,sec,t=setup(tmp_path)
    assert c.get('/api/v2/main/acceptance/policy',headers=v).status_code==403
    assert c.put('/api/v2/main/acceptance/policy',headers=v,json={'enabled':True}).status_code==403
    task,did,r=run_job(c,a,sec,t,'你是谁？',rid='l1-manual')
    assert t.get(task.id).status.value=='running'
    assert r['acceptance']['status']=='manual_required'
    q=c.get('/api/v2/main/acceptance/queue',headers=a).json()
    assert q['count']==1 and q['pending'][0]['tier']=='L1'
    assert c.get('/api/v2/main/acceptance/queue',headers=v).status_code==403
    p=c.put('/api/v2/main/acceptance/policy',headers=a,json={'enabled':True,'auto_l1':True,'auto_l2':True,'l1_capabilities':['qa']})
    assert p.status_code==200,p.text
    auto=c.post(f'/api/v2/main/acceptance/{did}/evaluate',headers=a)
    assert auto.json()['status']=='auto_verified',auto.text
    assert t.get(task.id).status.value=='completed'
    assert c.get('/api/v2/main/acceptance/queue',headers=a).json()['count']==0
    assert c.post(f'/api/v2/main/acceptance/{did}/evaluate',headers=a).json()['status']=='manual_required'


def test_l1_does_not_auto_pass_missing_or_status_only_answer(tmp_path):
    c,a,v,sec,t=setup(tmp_path)
    c.put('/api/v2/main/acceptance/policy',headers=a,json={'enabled':True,'l1_capabilities':['qa']})
    task,did,r=run_job(c,a,sec,t,'hello',result='success',rid='l1-bad')
    assert r['acceptance']['status']=='manual_required'
    assert t.get(task.id).status.value=='running'
    assert c.get('/api/v2/main/acceptance/queue',headers=a).json()['count']==1


def test_l1_without_capability_allowlist_is_not_auto_accepted(tmp_path):
    c,a,v,sec,t=setup(tmp_path)
    c.put('/api/v2/main/acceptance/policy',headers=a,json={'enabled':True,'auto_l1':True})
    task,did,r=run_job(c,a,sec,t,'hello',rid='l1-no-cap')
    assert r['acceptance']['status']=='manual_required'
    assert 'not explicitly' in r['acceptance']['reason']


def test_l2_requires_server_validator_and_rejects_manual_claim(tmp_path):
    c,a,v,sec,t=setup(tmp_path)
    c.put('/api/v2/main/acceptance/policy',headers=a,json={'enabled':True,'auto_l2':True})
    task,did,r=run_job(c,a,sec,t,'编写代码解析 JSON',rid='l2-no-validator')
    assert r['acceptance']['tier']=='L2'
    assert t.get(task.id).status.value=='running'
    evid=c.get(f'/api/v2/main/dispatch/{did}/evidence',headers=a).json()
    x=c.post(f'/api/v2/main/dispatch/{did}/review',headers=a,json={'accepted':True,'evidence_ref':'manual:approval','expected_sha256':evid['sha256']})
    assert x.status_code==503,x.text
    assert t.get(task.id).status.value=='running'
    assert c.post(f'/api/v2/main/dispatch/{did}/review',headers=a,json={'accepted':False,'evidence_ref':'review:rejected','expected_sha256':evid['sha256']}).status_code==200
    assert t.get(task.id).status.value=='failed'


def test_l2_trusted_validator_auto_pass(tmp_path):
    c,a,v,sec,t=setup(tmp_path,validator=lambda task,result:(True,'test:independent-check'))
    c.put('/api/v2/main/acceptance/policy',headers=a,json={'enabled':True,'auto_l2':True})
    task,did,r=run_job(c,a,sec,t,'解析 JSON 数据',rid='l2-valid')
    assert r['acceptance']['status']=='auto_verified'
    assert t.get(task.id).status.value=='completed'


def test_l2_failed_validator_does_not_auto_complete(tmp_path):
    c,a,v,sec,t=setup(tmp_path,validator=lambda task,result:(False,'test:independent-failure'))
    c.put('/api/v2/main/acceptance/policy',headers=a,json={'enabled':True,'auto_l2':True})
    task,did,r=run_job(c,a,sec,t,'解析 JSON 数据',rid='l2-invalid')
    assert r['acceptance']['status']=='manual_required'
    assert t.get(task.id).status.value=='running'
    evidence=c.get(f'/api/v2/main/dispatch/{did}/evidence',headers=a).json()
    assert c.post(f'/api/v2/main/dispatch/{did}/review',headers=a,json={'accepted':True,'evidence_ref':'fake:passed','expected_sha256':evidence['sha256']}).status_code==409


def test_l3_requires_manual_even_when_validator_passed(tmp_path):
    c,a,v,sec,t=setup(tmp_path,validator=lambda task,result:(True,'science:reproducible-pass'))
    c.put('/api/v2/main/acceptance/policy',headers=a,json={'enabled':True,'auto_l1':True,'auto_l2':True,'l1_capabilities':['qa']})
    task,did,r=run_job(c,a,sec,t,'研究基因与疾病因果关系',rid='l3-manual')
    assert r['acceptance']['tier']=='L3'
    assert r['acceptance']['status']=='manual_required'
    assert t.get(task.id).status.value=='running'
    evidence=c.get(f'/api/v2/main/dispatch/{did}/evidence',headers=a).json()
    assert c.post(f'/api/v2/main/dispatch/{did}/review',headers=v,json={'accepted':True,'evidence_ref':'fake:science','expected_sha256':evidence['sha256']}).status_code==403
    approved=c.post(f'/api/v2/main/dispatch/{did}/review',headers=a,json={'accepted':True,'evidence_ref':'fake:science','expected_sha256':evidence['sha256']})
    assert approved.status_code==200,approved.text
    assert t.get(task.id).status.value=='completed'


def test_l3_without_validator_cannot_be_manually_approved(tmp_path):
    c,a,v,sec,t=setup(tmp_path)
    c.put('/api/v2/main/acceptance/policy',headers=a,json={'enabled':True})
    task,did,r=run_job(c,a,sec,t,'临床疾病治疗方案',rid='l3-no-validator')
    evidence=c.get(f'/api/v2/main/dispatch/{did}/evidence',headers=a).json()
    assert c.post(f'/api/v2/main/dispatch/{did}/review',headers=a,json={'accepted':True,'evidence_ref':'manual:reviewed','expected_sha256':evidence['sha256']}).status_code==503
    assert t.get(task.id).status.value=='running'


def test_tier_conservative_and_ui_has_accessible_menu(tmp_path):
    assert classify_prompt('你是谁？')=='L1'
    assert classify_prompt('分析你有什么能力')=='L1'
    assert classify_prompt('多组学因果靶点研究')=='L3'
    assert classify_prompt('帮我写一段代码')=='L2'
    assert classify_prompt('请做点事')=='L2'
    class Task:
        prompt='hello'
        required_capabilities=['deploy-to-production']
    assert classify_task(Task())=='L3'
    ui=Path(__file__).resolve().parents[1]/'biomni_dual/web/collaboration.html'
    txt=ui.read_text()
    assert 'data-page="reviews"' not in txt  # No manual approval menu in default UI
    assert 'review-full-result' in txt
    assert 'acceptance-policy-form' in txt
    assert 'review-confirm' in txt
