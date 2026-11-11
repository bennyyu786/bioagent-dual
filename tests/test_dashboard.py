import json
from urllib.request import urlopen

from biomni_dual import OperationsStore, ObservabilityServer, PersistentRuntimeStore


def test_dashboard_html_and_api_are_real_and_read_only(tmp_path):
    runtime_db=tmp_path/'runtime.db'
    rt=PersistentRuntimeStore(str(runtime_db))
    rt.register_goal('private-goal-id')
    store=OperationsStore(str(tmp_path/'ops.db'),runtime_db=str(runtime_db))
    store.heartbeat()
    store.register_worker('worker-1','secret-token-which-is-long-enough',kind='biomni')
    server=ObservabilityServer(store,port=0).start()
    base='http://127.0.0.1:'+str(server.address[1])
    try:
        with urlopen(base+'/dashboard') as response:
            html=response.read().decode()
            assert 'text/html' in response.headers['Content-Type']
            assert 'Content-Security-Policy' in response.headers
        assert '系统运行总览' in html
        assert "fetch('/api/dashboard'" in html
        assert 'secret-token' not in html
        with urlopen(base+'/api/dashboard') as response:
            snap=json.load(response)
            assert snap['runtime']['goals']['active']==1
            assert snap['workers'][0]['id']=='worker-1'
            assert 'secret-token' not in json.dumps(snap)
            assert 'private-goal-id' not in json.dumps(snap)
        with urlopen(base+'/') as response:
            assert '系统运行总览' in response.read().decode()
    finally:
        server.stop()


def test_dashboard_missing_runtime_is_safe(tmp_path):
    store=OperationsStore(str(tmp_path/'ops.db'))
    server=ObservabilityServer(store,port=0).start()
    try:
        with urlopen('http://127.0.0.1:'+str(server.address[1])+'/api/dashboard') as response:
            data=json.load(response)
            assert data['runtime']=={}
    finally:server.stop()
