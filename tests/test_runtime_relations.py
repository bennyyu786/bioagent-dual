import sqlite3
import threading
import time
from fastapi.testclient import TestClient
import pytest
from biomni_dual.runtime_relations import RuntimeRelationStore
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.collaboration_api import create_app
from biomni_dual.scheduler import TaskCenter
from biomni_dual.observability import OperationsStore

CAP={'cpu_milli':2000,'memory_mb':4096,'gpu_units':1}
REQ={'cpu_milli':1000,'memory_mb':512,'gpu_units':1}


def test_atomic_capacity_and_idempotency(tmp_path):
    s=RuntimeRelationStore(tmp_path/'relations.db')
    s.configure_worker('a','w1',CAP)
    rid=s.reserve('a','t1','w1','request-01',REQ)
    assert rid==s.reserve('a','t1','w1','request-01',REQ)
    with pytest.raises(ValueError,match='Conflicting'):s.reserve('a','t1','w1','request-01',{'cpu_milli':100,'memory_mb':512,'gpu_units':1})
    with pytest.raises(ValueError,match='Insufficient'):s.reserve('a','t2','w1','request-02',REQ)
    assert s.snapshot('a')['capacity'][0]['available']['gpu_units']==0
    r=s.claim('a','w1',rid,60)
    assert r['task_id']=='t1'
    with pytest.raises(ValueError):s.claim('a','w1',rid,60)
    s.finish('a','w1',rid,'completed','validated receipt')
    assert s.snapshot('a')['capacity'][0]['available']['gpu_units']==1
    assert s.reserve('a','t2','w1','request-02',REQ)


def test_unverified_expiry_keeps_capacity(tmp_path):
    s=RuntimeRelationStore(tmp_path/'relations.db')
    s.configure_worker('w','worker',CAP)
    rid=s.reserve('w','task','worker','request-aaa',REQ)
    s.claim('w','worker',rid,10)
    with s.db() as db:db.execute('UPDATE reservations SET expires=? WHERE id=?',(time.time()-1,rid))
    with pytest.raises(ValueError,match='expired'):s.finish('w','worker',rid,'failed')
    snap=s.snapshot('w')
    assert snap['assignments'][0]['state']=='uncertain'
    assert snap['capacity'][0]['available']['gpu_units']==0
    with pytest.raises(ValueError):s.reconcile('w',rid)
    s.reconcile('w',rid,verified_no_side_effect=True)
    assert s.snapshot('w')['capacity'][0]['available']['gpu_units']==1


def test_concurrent_reservations_do_not_oversubscribe(tmp_path):
    s=RuntimeRelationStore(tmp_path/'relations.db')
    s.configure_worker('w','worker',CAP)
    outcomes=[]
    def attempt(i):
        try:outcomes.append(('ok',s.reserve('w','task'+str(i),'worker','req'+str(i),REQ)))
        except (ValueError,sqlite3.IntegrityError) as exc:outcomes.append(('rejected',str(exc)))
    threads=[threading.Thread(target=attempt,args=(i,)) for i in range(8)]
    for t in threads:t.start()
    for t in threads:t.join()
    assert sum(x[0]=='ok' for x in outcomes)==1
    assert s.snapshot('w')['capacity'][0]['reserved']['gpu_units']==1


def test_tenant_snapshot_and_capability_lifecycle(tmp_path):
    s=RuntimeRelationStore(tmp_path/'r.db')
    s.configure_worker('alpha','w',CAP)
    s.capability('alpha','omics-skill','sandbox_verified','sha256:verified')
    s.link_repair('alpha','t1','t2','repair:evidence')
    assert len(s.snapshot('alpha')['repairs'])==1
    assert s.snapshot('beta')=={'capacity':[],'assignments':[],'capabilities':[],'repairs':[],
         'semantics':'Reservations are intents; claimed indicates worker acknowledgment, not verified task execution.'}
    with pytest.raises(ValueError):s.capability('alpha','other','approved','')
    with pytest.raises(ValueError):s.configure_worker('alpha','w',{'cpu_milli':False,'memory_mb':4096,'gpu_units':1})


def test_workspaces_rbac_remote_claim_and_topology(tmp_path):
    c=CollaborationStore(tmp_path/'team.db')
    admin=c.bootstrap('admin@example.com','LongSecurePassword123')
    c.workspace_create(admin,'beta','beta')
    c.invite(admin,'main','viewer@example.com','LongViewerPassword123','viewer')
    tasks=TaskCenter(tmp_path/'tasks.db')
    operations=OperationsStore(tmp_path/'ops.db')
    rt=RuntimeRelationStore(tmp_path/'runtime.db')
    client=TestClient(create_app(c,tasks,operations,rt))
    def login(email,password):
        token=client.post('/api/v2/login',json={'email':email,'password':password}).json()['token']
        return {'Authorization':'Bearer '+token}
    hdr=login('admin@example.com','LongSecurePassword123')
    viewer=login('viewer@example.com','LongViewerPassword123')
    t=client.post('/api/v2/main/tasks',headers=hdr,json={'prompt':'omics workflow','request_id':'request-alpha'}).json()['id']
    t2=client.post('/api/v2/beta/tasks',headers=hdr,json={'prompt':'secret workflow','request_id':'request-beta'}).json()['id']
    secret=client.post('/api/v2/main/workers',headers=hdr,json={'id':'worker1'}).json()['secret']
    url='/api/v2/main/runtime/workers/worker1/capacity'
    assert client.put(url,headers=viewer,json=CAP).status_code==403
    assert client.put(url,headers=hdr,json=CAP).status_code==200
    body={'task_id':t,'worker':'worker1','request_id':'one-reserve','resources':REQ}
    assert client.post('/api/v2/main/runtime/reservations',headers=viewer,json=body).status_code==403
    rid=client.post('/api/v2/main/runtime/reservations',headers=hdr,json=body).json()['id']
    assert client.post('/api/v2/main/runtime/reservations',headers=hdr,json={**body,'task_id':t2,'request_id':'second'}).status_code==409
    assert client.get('/api/v2/beta/runtime/resources',headers=viewer).status_code==403
    assert client.get('/api/v2/beta/runtime/resources',headers=hdr).json()['assignments']==[]
    graph=client.get('/api/v2/main/topology',headers=hdr).json()
    assert any(e['kind']=='reservation' and e['state']=='reserved' for e in graph['edges'])
    assert t2 not in str(graph)
    workerhdr={'x-worker-secret':secret}
    claim='/api/v2/worker/worker1/runtime/reservations/'+rid+'/claim'
    assert client.post(claim,json={'ttl_seconds':60}).status_code==401
    assert client.post(claim,headers=workerhdr,json={'ttl_seconds':60}).status_code==200
    assert client.post(claim,headers=workerhdr,json={'ttl_seconds':60}).status_code==400
    assert client.post('/api/v2/worker/worker1/runtime/reservations/'+rid+'/finish',headers=workerhdr,
                       json={'state':'completed','receipt':'ok'}).status_code==200
    assert client.get('/api/v2/main/runtime/resources',headers=hdr).json()['capacity'][0]['reserved']['gpu_units']==0
    assert client.put('/api/v2/main/runtime/capabilities/omics',headers=hdr,
                      json={'lifecycle':'approved','evidence_ref':'sha256:evidence'}).status_code==200
    assert client.post('/api/v2/main/runtime/repairs',headers=hdr,
                       json={'task_id':t,'repair_task_id':t2,'evidence_ref':'repair:verified'}).status_code==404
    assert client.get('/api/v2/main/topology',headers=hdr).json()['version']==2


def test_frontend_runtime_relationship_controls():
    from pathlib import Path
    html=(Path(__file__).parents[1]/'biomni_dual/web/collaboration.html').read_text()
    for marker in ('runtime/resources','reservation','gpu_units','cpu_milli','repair_dependency'):
        assert marker in html
