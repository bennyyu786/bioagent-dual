"""Model credential diagnosis is non-secret and chat failures are actionable."""
from test_web_planning import rig, configure


def test_status_reports_configuration_without_exposing_key(tmp_path, monkeypatch):
    c, admin, op, tasks, relations, calls = rig(tmp_path, 'not a plan')
    monkeypatch.delenv('BIOMNI_MODEL_API_KEY', raising=False)
    url='/api/v2/main/models/status'
    initial=c.get(url, headers=op)
    assert initial.status_code == 200
    assert initial.json()['chat_ready'] is False
    assert initial.json()['configured'] is False
    assert 'API_KEY' not in str(initial.json()) or '缺少' not in initial.json()['message']
    c.post('/api/v2/main/models/config',headers=admin,
           json={'base_url':'https://example.org/v1','model':'test-model'})
    missing=c.get(url,headers=op)
    assert missing.status_code == 200
    assert missing.json()['credentials_present'] is False
    assert 'BIOMNI_MODEL_API_KEY' in missing.json()['message']
    response=c.post('/api/v2/main/assistant/turn',headers=op,json={
      'message':'你好','mode':'chat','session':'reopen','request_id':'reopen-09501'})
    assert response.status_code == 503
    assert 'BIOMNI_MODEL_API_KEY' in response.json()['detail']
    monkeypatch.setenv('BIOMNI_MODEL_API_KEY','secret-not-for-response')
    status=c.get(url,headers=op).json()
    assert status['credentials_present'] and status['chat_ready']
    assert 'secret-not-for-response' not in str(status)
    assert c.get('/api/v2/other/models/status',headers=op).status_code == 403


def test_task_submission_without_chat_model_key(tmp_path, monkeypatch):
    c,admin,op,t,rel,calls=rig(tmp_path,'not a plan')
    monkeypatch.delenv('BIOMNI_MODEL_API_KEY',raising=False)
    result=c.post('/api/v2/main/assistant/turn',headers=op,json={
        'message':'继续处理任务','mode':'task',
        'session':'task-followup-existing','request_id':'followup-09501'})
    assert result.status_code == 200,result.text
    assert result.json()['route']=='task'
    history=c.get('/api/v2/main/assistant/session/task-followup-existing',headers=op).json()
    assert len(history['messages'])==1
