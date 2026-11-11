"""v0.9.2: paginated durable history, event stream and readable answers."""
from test_web_planning import rig
from biomni_dual.result_presentation import present_result


def test_history_retains_sessions_and_tasks_and_workspace_isolation(tmp_path):
    c, admin, op, tasks, rel, calls = rig(tmp_path, 'not a plan')
    for index in range(5):
        result=c.post('/api/v2/main/assistant/turn', headers=op,
            json={'message':f'测试任务 {index}', 'mode':'task','session':f'session{index}',
                  'request_id':f'history-092-{index}'})
        assert result.status_code==200,result.text
    first=c.get('/api/v2/main/assistant/history?kind=sessions&limit=2',headers=op)
    assert first.status_code==200,first.text
    assert len(first.json()['items'])==2 and first.json()['next_offset']==2
    next_page=c.get('/api/v2/main/assistant/history?kind=sessions&limit=2&offset=2',headers=op).json()
    assert len(next_page['items'])==2
    assert set(x['id'] for x in first.json()['items']).isdisjoint(x['id'] for x in next_page['items'])
    all_sessions=first.json()['items']+next_page['items']+c.get(
        '/api/v2/main/assistant/history?kind=sessions&limit=2&offset=4',headers=op).json()['items']
    assert {f'session{i}' for i in range(5)}.issubset({x['id'] for x in all_sessions})
    tasks_result=c.get('/api/v2/main/assistant/history?kind=tasks&limit=2',headers=op).json()
    assert len(tasks_result['items'])==2 and tasks_result['next_offset']==2
    assert all(x['id'] and x['status']=='pending' for x in tasks_result['items'])
    assert c.get('/api/v2/other/assistant/history',headers=op).status_code==403
    assert c.get('/api/v2/main/assistant/history?kind=bad',headers=op).status_code==400


def test_event_history_has_pagination_and_worker_filter_is_only_ui(tmp_path):
    c, admin, op, tasks, rel, calls = rig(tmp_path, 'not a plan')
    for i in range(4):
        r=c.post('/api/v2/main/assistant/turn',headers=op,
            json={'message':f'hello-{i}', 'mode':'task', 'request_id':f'event-092-{i}'})
        assert r.status_code==200
    result=c.get('/api/v2/main/events/history?limit=3',headers=op)
    assert result.status_code==200
    data=result.json();assert len(data['events'])==3 and data['next_before']
    assert data['events'][0]['id']>data['events'][-1]['id']
    older=c.get('/api/v2/main/events/history?limit=3&before='+str(data['next_before']),headers=op)
    assert older.status_code==200
    assert all(e['id']<data['next_before'] for e in older.json()['events'])
    assert c.get('/api/v2/other/events/history',headers=op).status_code==403
    assert c.get('/api/v2/main/events/history?before=-1',headers=op).status_code==400


def test_readable_result_decode_including_hex_utf8():
    assert present_result(r'\u4f60\u597d')['text']=='你好'
    assert present_result(r'\xe4\xbd\xa0\xe5\xa5\xbd')['text']=='你好'
    assert present_result(r'\xGG')['text']==r'\xGG'
    assert present_result('你好')['text']=='你好'
    assert present_result('{"answer": "\\u4f60\\u597d"}')['text']=='你好'
