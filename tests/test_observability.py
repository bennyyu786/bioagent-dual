import json
import sqlite3
import time
import urllib.error
import urllib.request

import pytest
from biomni_dual import OperationsStore, ObservabilityServer


def test_health_metrics_and_safe_payload(tmp_path):
    ops=OperationsStore(str(tmp_path/'o.db'))
    ops.heartbeat()
    ops.register_worker('biomni-1','x'*30,kind='biomni',capacity=2)
    server=ObservabilityServer(ops,port=0).start()
    url='http://127.0.0.1:'+str(server.address[1])
    try:
        assert json.loads(urllib.request.urlopen(url+'/health/live').read())['alive']
        assert json.loads(urllib.request.urlopen(url+'/health/ready').read())['ready']
        req=urllib.request.Request(url+'/internal/worker-heartbeat',
            data=json.dumps({'worker_id':'biomni-1','token':'x'*30}).encode(),
            headers={'Content-Type':'application/json'},method='POST')
        assert urllib.request.urlopen(req).status==200
        assert ops.snapshot()['workers'][0]['state']=='online'
        metrics=urllib.request.urlopen(url+'/metrics').read().decode()
        assert 'biomni_worker_online 1' in metrics and 'x'*30 not in metrics
        status=urllib.request.urlopen(url+'/status').read().decode()
        assert 'token_sha256' not in status and 'x'*30 not in status
    finally:server.stop()


def test_unregistered_worker_cannot_heartbeat(tmp_path):
    ops=OperationsStore(str(tmp_path/'o.db'))
    ops.register_worker('w','s'*32)
    assert not ops.worker_heartbeat('w','wrong')
    assert not ops.worker_heartbeat('unknown','s'*32)
    with pytest.raises(ValueError):ops.register_worker('../../unsafe','s'*32)
    with pytest.raises(ValueError):ObservabilityServer(ops,host='0.0.0.0',port=0)


def test_stale_worker_alert_lifecycle_and_cooldown(tmp_path):
    ops=OperationsStore(str(tmp_path/'o.db'))
    ops.heartbeat(now=100)
    ops.register_worker('w','z'*30)
    assert ops.worker_heartbeat('w','z'*30,now=100)
    a=ops.evaluate_alerts(now=200,worker_timeout=30,service_timeout=30,cooldown=30)
    assert {x['code'] for x in a}=={'service_unavailable','worker_stale'}
    a=ops.evaluate_alerts(now=205,worker_timeout=30,service_timeout=30,cooldown=30)
    assert all(x['occurrences']==1 for x in a)
    a=ops.evaluate_alerts(now=240,worker_timeout=30,service_timeout=30,cooldown=30)
    assert all(x['occurrences']==2 for x in a)
    ops.heartbeat(now=250)
    ops.worker_heartbeat('w','z'*30,now=250)
    assert ops.evaluate_alerts(now=251,worker_timeout=30,service_timeout=30)==[]


def test_store_reopens_and_counts_idempotent_runtime(tmp_path):
    path=str(tmp_path/'ops.db')
    a=OperationsStore(path)
    a.count('events_processed',3)
    a.heartbeat()
    b=OperationsStore(path)
    assert b.snapshot()['counters']['events_processed']==3
    assert b.snapshot()['ready']


def test_disallowed_requests(tmp_path):
    ops=OperationsStore(str(tmp_path/'ops.db'))
    server=ObservabilityServer(ops,port=0).start()
    url='http://127.0.0.1:'+str(server.address[1])
    try:
        with pytest.raises(urllib.error.HTTPError) as ex:
            urllib.request.urlopen(url+'/health/ready')
        assert ex.value.code==503
        with pytest.raises(urllib.error.HTTPError) as ex:
            urllib.request.urlopen(urllib.request.Request(url+'/execute',data=b'{}',method='POST'))
        assert ex.value.code==404
    finally:server.stop()


def test_runtime_aggregates_without_sensitive_payload(tmp_path):
    from biomni_dual import PersistentRuntimeStore,EventKind
    path=str(tmp_path/'full.sqlite')
    rt=PersistentRuntimeStore(path)
    rt.register_goal('secret-internal-goal')
    rt.submit('secret-internal-goal',EventKind.DATA_CHANGED,'secret-data-key')
    ops=OperationsStore(str(tmp_path/'ops.sqlite'),runtime_db=path)
    ops.heartbeat()
    s=ops.snapshot()
    assert s['runtime']['goals']=={'active':1}
    assert s['runtime']['events']=={'pending':1}
    assert s['runtime']['runs']==0
    out=ops.prometheus()
    assert 'biomni_events_total{status="pending"} 1' in out
    assert 'secret-internal-goal' not in out and 'secret-data-key' not in out


def test_observed_service_lifecycle(tmp_path):
    import asyncio
    from biomni_dual import serve_observed
    ops=OperationsStore(str(tmp_path/'observed.sqlite'))
    class FakeRuntime:
        async def serve(self,*,max_loops=None):
            assert max_loops==1
            await asyncio.sleep(.08)
    asyncio.run(serve_observed(FakeRuntime(),ops,port=0,max_loops=1,interval=.01))
    assert ops.snapshot()['service']=='unavailable'
    with ops._connect() as db:
        row=db.execute('SELECT state FROM ops_service WHERE name=?',('runtime',)).fetchone()
    assert row==('stopping',)


def test_observed_service_failure_persisted(tmp_path):
    import asyncio
    from biomni_dual import serve_observed
    ops=OperationsStore(str(tmp_path/'observed.sqlite'))
    class BrokenRuntime:
        async def serve(self,*,max_loops=None): raise RuntimeError('intentional failure')
    with pytest.raises(RuntimeError):
        asyncio.run(serve_observed(BrokenRuntime(),ops,port=0,max_loops=1,interval=.01))
    assert ops.snapshot()['counters']['runtime_failures']==1
    with ops._connect() as db:
        row=db.execute('SELECT state FROM ops_service WHERE name=?',('runtime',)).fetchone()
    assert row==('failed',)
