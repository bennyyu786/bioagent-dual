"""Automatic completion: no review click, persisted answer available to web UI."""
from test_dispatch_runtime import setup, REQ
from fastapi.testclient import TestClient
from biomni_dual.collaboration_api import create_app
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.observability import OperationsStore


def _submit_and_claim(tmp_path, prompt, key):
    client,admin,viewer,secrets,tasks,ledger=setup(tmp_path)
    # Recreate the API with the new default auto-delivery mode (legacy test setup disables it).
    client=TestClient(create_app(CollaborationStore(tmp_path/'users.db'),tasks,OperationsStore(tmp_path/'ops.db'),ledger))
    created=client.post('/api/v2/main/tasks',headers=admin,json={'prompt':prompt,'request_id':'submit-'+key})
    assert created.status_code==200,created.text
    task=tasks.bind_approved_execution(created.json()['id'],'main','analysis')
    offer=client.post('/api/v2/main/dispatch',headers=admin,json={
        'task_id':task.id,'request_id':key,'resources':REQ}).json()
    worker=offer['worker']
    prefix=f"/api/v2/worker/{worker}/dispatch/{offer['id']}"
    wh={'x-worker-secret':secrets[worker]}
    r=client.post(prefix+'/claim',headers=wh,json={'ttl_seconds':120})
    assert r.status_code==200,r.text
    return client,admin,tasks,ledger,task,offer,prefix,wh


def test_answer_is_completed_and_readable_without_review(tmp_path):
    client,admin,tasks,ledger,task,offer,prefix,wh=_submit_and_claim(tmp_path,'你好，你是谁？','auto-answer-1')
    assert client.get(f'/api/v2/main/tasks/{task.id}/result',headers=admin).status_code==409
    result=client.post(prefix+'/report',headers=wh,json={
        'state':'completed','result':'我是 Biomni 的问答助手，可以回答你的问题。'})
    assert result.status_code==200,result.text
    assert result.json()['state']=='delivered'
    assert tasks.get(task.id).status.value=='completed'
    answer=client.get(f'/api/v2/main/tasks/{task.id}/result',headers=admin)
    assert answer.status_code==200,answer.text
    assert 'Biomni' in answer.json()['result']
    assert ledger.snapshot('main')['capacity'][0]['available']['gpu_units']==1
    assert client.get('/api/v2/main/dispatch',headers=admin).json()['dispatches'][0]['state']=='delivered'
    assert client.post(prefix+'/report',headers=wh,json={'state':'completed','result':'another'}).status_code==409


def test_failed_worker_report_finishes_task_without_manual_review(tmp_path):
    client,admin,tasks,ledger,task,offer,prefix,wh=_submit_and_claim(tmp_path,'a generic task','auto-failed-1')
    response=client.post(prefix+'/report',headers=wh,json={'state':'failed','result':'adapter crashed'})
    assert response.status_code==200,response.text
    assert response.json()['state']=='failed'
    assert tasks.get(task.id).status.value=='failed'
    assert client.get(f'/api/v2/main/tasks/{task.id}/result',headers=admin).status_code==409
    assert ledger.snapshot('main')['capacity'][0]['available']['gpu_units']==1


def test_empty_success_is_not_presented_as_answer(tmp_path):
    client,admin,tasks,ledger,task,offer,prefix,wh=_submit_and_claim(tmp_path,'hello','auto-empty-1')
    response=client.post(prefix+'/report',headers=wh,json={'state':'completed','result':'ok'})
    assert response.status_code==200,response.text
    assert response.json()['state']=='failed'
    assert tasks.get(task.id).status.value=='failed'
    assert ledger.snapshot('main')['capacity'][0]['available']['gpu_units']==1
