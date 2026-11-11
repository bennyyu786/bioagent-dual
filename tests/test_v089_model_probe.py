"""Model discovery diagnostics contain no credentials or raw upstream bodies."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.collaboration_api import create_app
from biomni_dual.scheduler import TaskCenter
from biomni_dual.observability import OperationsStore
from fastapi.testclient import TestClient


def test_model_probe_http_error_has_status_and_no_key(tmp_path, monkeypatch):
    class Provider(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            assert self.path=='/v1/models'
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"should not leak"}')
    server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        store=CollaborationStore(tmp_path/'collab.db')
        store.bootstrap('admin@example.org','AStrongPassword9')
        c=TestClient(create_app(store,TaskCenter(tmp_path/'tasks.db'),OperationsStore(tmp_path/'ops.db')))
        auth={'Authorization':'Bearer '+c.post('/api/v2/login',json={'email':'admin@example.org','password':'AStrongPassword9'}).json()['token']}
        base=f'http://127.0.0.1:{server.server_port}/v1'
        assert c.post('/api/v2/main/models/config',headers=auth,json={'base_url':base,'model':'sample'}).status_code==200
        monkeypatch.setenv('BIOMNI_MODEL_API_KEY','secret-must-not-leak')
        result=c.post('/api/v2/main/models/probe',headers=auth).json()
        assert result['connected'] is False
        assert result['error']['http_status']==401
        assert 'secret-must-not-leak' not in json.dumps(result)
        assert 'should not leak' not in json.dumps(result)
        monkeypatch.delenv('BIOMNI_MODEL_API_KEY')
        assert c.post('/api/v2/main/models/probe',headers=auth).json()['error']['type']=='missing_api_key'
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
