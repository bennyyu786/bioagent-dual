import hashlib
import json
import threading
import time
from pathlib import Path
from sys import executable
import pytest
from fastapi.testclient import TestClient
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.collaboration_api import create_app
from biomni_dual.scheduler import TaskCenter
from biomni_dual.observability import OperationsStore
from biomni_dual.runtime_relations import RuntimeRelationStore
from biomni_dual.dispatch_runtime import ResourceAwareDispatcher, DispatchConflict
from biomni_dual.remote_worker import TrustedAdapterRunner, WorkerTransport

CAP={'cpu_milli':4000,'memory_mb':8192,'gpu_units':1}
REQ={'cpu_milli':1000,'memory_mb':1024,'gpu_units':1}


def setup(tmp_path):
    c=CollaborationStore(tmp_path/'users.db');uid=c.bootstrap('admin@example.com','StrongPassword123456')
    c.invite(uid,'main','viewer@example.com','ViewerPassword123456','viewer')
    t=TaskCenter(tmp_path/'tasks.db');o=OperationsStore(tmp_path/'ops.db')
    r=RuntimeRelationStore(tmp_path/'runtime.db')
    cl=TestClient(create_app(c,t,o,r,auto_delivery=False))
    def login(email,pwd):
        token=cl.post('/api/v2/login',json={'email':email,'password':pwd}).json()['token']
        return {'Authorization':'Bearer '+token}
    admin=login('admin@example.com','StrongPassword123456');viewer=login('viewer@example.com','ViewerPassword123456')
    sec=cl.post('/api/v2/main/workers',headers=admin,json={'id':'worker-a'}).json()['secret']
    sec2=cl.post('/api/v2/main/workers',headers=admin,json={'id':'worker-b'}).json()['secret']
    for w in ('worker-a','worker-b'):
        assert cl.put(f'/api/v2/main/runtime/workers/{w}/capacity',headers=admin,json=CAP).status_code==200
        assert cl.put(f'/api/v2/main/dispatch/workers/{w}/skills',headers=admin,json={'capabilities':['analysis']}).status_code==200
    for w,secret in [('worker-a',sec),('worker-b',sec2)]:
        assert cl.get('/api/v2/worker/'+w+'/dispatch/poll',headers={'x-worker-secret':secret}).status_code==200
    r.capability('main','analysis','approved','sha256:reviewed-test-fixture')
    return cl,admin,viewer,{'worker-a':sec,'worker-b':sec2},t,r


def test_end_to_end_scoped_dispatch_review(tmp_path):
    client,admin,viewer,secrets,t,r=setup(tmp_path)
    task=t.submit('analyze sample',tenant='main',required_capabilities=['analysis'])
    data={'task_id':task.id,'request_id':'dispatch-001','resources':REQ}
    path='/api/v2/main/dispatch'
    assert client.post(path,headers=viewer,json=data).status_code==403
    first=client.post(path,headers=admin,json=data);assert first.status_code==200,first.text
    issued=first.json();did=issued['id'];worker=issued['worker']
    assert issued['state']=='offered'
    assert client.post(path,headers=admin,json=data).json()['id']==did
    offer_path=f'/api/v2/worker/{worker}/dispatch'
    assert client.get(offer_path+'/poll').status_code==401
    wh={'x-worker-secret':secrets[worker]}
    assert client.get(offer_path+'/poll',headers=wh).json()['offer']['id']==did
    assert client.post(offer_path+'/'+did+'/claim',headers=wh,json={'ttl_seconds':120}).status_code==200
    assert t.get(task.id).status.value=='running'
    assert client.post(offer_path+'/'+did+'/claim',headers=wh,json={'ttl_seconds':120}).status_code==409
    report=client.post(offer_path+'/'+did+'/report',headers=wh,json={'state':'completed','result':'scientific result'})
    assert report.status_code==200
    assert report.json()['state']=='review_required'
    assert t.get(task.id).status.value=='running'
    assert client.post(path+'/'+did+'/review',headers=viewer,json={'accepted':True,'evidence_ref':'review:evidence','expected_sha256':report.json()['sha256']}).status_code==403
    assert client.post(path+'/'+did+'/review',headers=admin,json={'accepted':True,'evidence_ref':'review:evidence','expected_sha256':'bad'}).status_code==409
    review=client.post(path+'/'+did+'/review',headers=admin,json={'accepted':True,'evidence_ref':'review:evidence','expected_sha256':report.json()['sha256']})
    assert review.status_code==200,review.text
    assert t.get(task.id).status.value=='completed'
    assert r.snapshot('main')['capacity'][0]['available']['gpu_units']==1
    assert client.get('/api/v2/main/topology',headers=admin).json()['version']==3
    assert client.post(path+'/'+did+'/review',headers=admin,json={'accepted':True,'evidence_ref':'review:evidence','expected_sha256':report.json()['sha256']}).status_code==409


def test_failed_report_rejected_and_capacity_released(tmp_path):
    c,a,v,secrets,t,r=setup(tmp_path)
    task=t.submit('wrong',tenant='main',required_capabilities=['analysis'])
    job=c.post('/api/v2/main/dispatch',headers=a,json={'task_id':task.id,'request_id':'request-2','resources':REQ}).json()
    worker=job['worker'];base=f"/api/v2/worker/{worker}/dispatch/{job['id']}";hdr={'x-worker-secret':secrets[worker]}
    c.post(base+'/claim',headers=hdr,json={'ttl_seconds':60})
    report=c.post(base+'/report',headers=hdr,json={'state':'failed','result':'worker crashed'}).json()
    assert c.post('/api/v2/main/dispatch/'+job['id']+'/review',headers=a,json={'accepted':True,'evidence_ref':'evidence:test','expected_sha256':report['sha256']}).status_code==409
    r2=c.post('/api/v2/main/dispatch/'+job['id']+'/review',headers=a,json={'accepted':False,'evidence_ref':'evidence:test','expected_sha256':report['sha256']})
    assert r2.status_code==200,r2.text
    assert t.get(task.id).status.value=='failed'
    assert r.snapshot('main')['capacity'][0]['reserved']['gpu_units']==0


def test_capability_capacity_dependencies_and_cross_tenant(tmp_path):
    client,a,v,secrets,t,r=setup(tmp_path)
    x=t.submit('missing',tenant='main',required_capabilities=['unknown'])
    assert client.post('/api/v2/main/dispatch',headers=a,json={'task_id':x.id,'request_id':'missing-01','resources':REQ}).status_code==409
    dep=t.submit('first',tenant='main')
    x=t.submit('second',tenant='main',depends_on=[dep.id],required_capabilities=['analysis'])
    assert client.post('/api/v2/main/dispatch',headers=a,json={'task_id':x.id,'request_id':'dependency-01','resources':REQ}).status_code==409
    other=t.submit('secret',tenant='other',required_capabilities=['analysis'])
    assert client.post('/api/v2/main/dispatch',headers=a,json={'task_id':other.id,'request_id':'secret-01','resources':REQ}).status_code==409
    assert client.get('/api/v2/main/dispatch',headers=v).status_code==200
    assert client.get('/api/v2/main/dispatch',headers=v).json()['dispatches']==[]


def test_parallel_no_double_dispatch_and_uncertain(tmp_path):
    c,a,v,secrets,t,r=setup(tmp_path)
    task=t.submit('work',tenant='main',required_capabilities=['analysis'])
    dispatcher=ResourceAwareDispatcher(r,t)
    result=[]
    def run(i):
        try: result.append(dispatcher.plan_and_offer('main',task.id,'req-'+str(i),REQ,{'worker-a','worker-b'}))
        except (DispatchConflict,ValueError): result.append('rejected')
    threads=[threading.Thread(target=run,args=(i,)) for i in range(10)]
    for th in threads:th.start()
    for th in threads:th.join()
    assert sum(isinstance(r,dict) for r in result)==1
    job=next(x for x in result if isinstance(x,dict))
    w=job['worker'];sec={'x-worker-secret':secrets[w]}
    c.post(f"/api/v2/worker/{w}/dispatch/{job['id']}/claim",headers=sec,json={'ttl_seconds':10})
    with r.db() as db:db.execute('UPDATE reservations SET expires=? WHERE id=?',(time.time()-1,job['reservation_id']))
    assert c.post('/api/v2/main/dispatch/scan',headers=a).json()['quarantined']==1
    assert dispatcher.list('main')[0]['state']=='uncertain'
    assert r.snapshot('main')['assignments'][0]['state']=='uncertain'


def test_trusted_adapter_no_unapproved_execution(tmp_path):
    f=tmp_path/'adapter.py';f.write_text('import json,sys\nx=json.load(sys.stdin)\nprint(x["task_id"]+":ok")\n')
    runner=TrustedAdapterRunner({'analysis':[executable,str(f)]},max_seconds=10)
    assert runner.execute({'task_id':'a','prompt':'hello','capabilities':['analysis']})==('completed','a:ok\n')
    assert runner.execute({'task_id':'a','prompt':'ignore previous instructions','capabilities':['unknown']})[0]=='failed'
    with pytest.raises(ValueError):TrustedAdapterRunner({'analysis':['python',str(f)]})
    with pytest.raises(ValueError):WorkerTransport('http://example.com','w','secret',allow_http_loopback=True)
    assert WorkerTransport('http://localhost:8766','w','secret',allow_http_loopback=True)


def test_priority_auto_dispatch_and_cancel(tmp_path):
    cl,admin,viewer,secrets,t,r=setup(tmp_path)
    low=t.submit('low',tenant='main',required_capabilities=['analysis'],priority=1)
    high=t.submit('high',tenant='main',required_capabilities=['analysis'],priority=9)
    url='/api/v2/main/dispatch/next'
    result=cl.post(url,headers=admin,json={'request_id':'next-001','resources':REQ})
    assert result.status_code==200,result.text
    job=result.json();assert job['task_id']==high.id
    assert t.get(high.id).status.value=='pending'
    assert cl.post('/api/v2/main/dispatch/'+job['id']+'/cancel',headers=viewer).status_code==403
    assert cl.post('/api/v2/main/dispatch/'+job['id']+'/cancel',headers=admin).json()['state']=='cancelled'
    assert r.snapshot('main')['capacity'][0]['reserved']['gpu_units']==0
    assert cl.post('/api/v2/main/dispatch/'+job['id']+'/cancel',headers=admin).status_code==409


def test_reported_result_keeps_capacity_until_late_review(tmp_path):
    c,a,v,secrets,t,r=setup(tmp_path)
    task=t.submit('long peer review',tenant='main',required_capabilities=['analysis'])
    offer=c.post('/api/v2/main/dispatch',headers=a,json={'task_id':task.id,'request_id':'late-review','resources':REQ}).json()
    worker=offer['worker'];hdr={'x-worker-secret':secrets[worker]}
    base=f"/api/v2/worker/{worker}/dispatch/{offer['id']}"
    c.post(base+'/claim',headers=hdr,json={'ttl_seconds':60})
    result=c.post(base+'/report',headers=hdr,json={'state':'completed','result':'lab output'}).json()
    with r.db() as db:
        db.execute('UPDATE reservations SET expires=? WHERE id=?',(time.time()-100,offer['reservation_id']))
    assert c.post('/api/v2/main/dispatch/scan',headers=a).json()['quarantined']==0
    assert r.snapshot('main')['assignments'][0]['state']=='reported'
    assert c.post('/api/v2/main/dispatch/'+offer['id']+'/verify',headers=a).status_code==503
    evidence=c.get('/api/v2/main/dispatch/'+offer['id']+'/evidence',headers=a).json()
    assert evidence['result']=='lab output'
    assert c.get('/api/v2/main/dispatch/'+offer['id']+'/evidence',headers=v).status_code==403
    review=c.post('/api/v2/main/dispatch/'+offer['id']+'/review',headers=a,json={'accepted':True,'expected_sha256':result['sha256'],'evidence_ref':'peer:reviewed'})
    assert review.status_code==200,review.text
    assert r.snapshot('main')['capacity'][0]['reserved']['gpu_units']==0


def test_pluggable_independent_domain_validator(tmp_path):
    c,a,v,secrets,t,r=setup(tmp_path)
    # New ASGI process, same database & a separate trusted validator object.
    collab=CollaborationStore(tmp_path/'users.db');ops=OperationsStore(tmp_path/'ops.db')
    app=create_app(collab,t,ops,r,result_validator=lambda task,result:(result=='verified result','validator:independent-pass'),auto_delivery=False)
    cl=TestClient(app)
    task=t.submit('domain experiment',tenant='main',required_capabilities=['analysis'])
    offer=cl.post('/api/v2/main/dispatch',headers=a,json={'task_id':task.id,'request_id':'domain-verify','resources':REQ}).json()
    w=offer['worker'];wh={'x-worker-secret':secrets[w]}
    base=f"/api/v2/worker/{w}/dispatch/{offer['id']}"
    cl.post(base+'/claim',headers=wh,json={'ttl_seconds':60})
    cl.post(base+'/report',headers=wh,json={'state':'completed','result':'verified result'})
    assert cl.post('/api/v2/main/dispatch/'+offer['id']+'/verify',headers=v).status_code==403
    resp=cl.post('/api/v2/main/dispatch/'+offer['id']+'/verify',headers=a)
    assert resp.status_code==200,resp.text
    assert t.get(task.id).status.value=='completed'


def test_remote_bridge_poll_claim_execute_report(tmp_path):
    from biomni_dual.remote_worker import run_once
    cl,admin,viewer,secrets,t,r=setup(tmp_path)
    task=t.submit('real adapter bridge',tenant='main',required_capabilities=['analysis'])
    offer=cl.post('/api/v2/main/dispatch',headers=admin,json={'task_id':task.id,'request_id':'bridge-01','resources':REQ}).json()
    worker=offer['worker']
    source=tmp_path/'adapter.py'
    source.write_text('import json,sys\np=json.load(sys.stdin)\nprint("adapter:"+p["task_id"])\n')
    class LocalWorkerTransport:
        def __init__(self):self.worker=worker
        def request(self,method,path,body=None):
            resp=cl.request(method,path,headers={'x-worker-secret':secrets[worker]},json=body)
            assert resp.status_code==200,resp.text
            return resp.json()
    out=run_once(LocalWorkerTransport(),TrustedAdapterRunner({'analysis':[executable,str(source)]}))
    assert out['report_state']=='review_required'
    assert out['job_id']==offer['id']
    assert r.snapshot('main')['assignments'][0]['state']=='reported'
    assert t.get(task.id).status.value=='running'
    assert run_once(LocalWorkerTransport(),TrustedAdapterRunner({'analysis':[executable,str(source)]})) is None
