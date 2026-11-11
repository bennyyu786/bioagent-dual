"""One presentation contract for review, task DAG, and verified history."""
import hashlib
import json
import sqlite3
from biomni_dual.result_presentation import present_result
from test_result_acceptance import setup, run_job


def test_preserves_utf8_and_decodes_only_view():
    originals = [
        ('中文回答\n下一行', '中文回答\n下一行'),
        (r'\u4f60\u597d\uff0c\u4e16\u754c', '你好，世界'),
        (json.dumps({'answer': '你好，世界'}, ensure_ascii=True), '你好，世界'),
        (json.dumps({'content': [{'type': 'text', 'text': '第一段'}, {'type': 'text', 'text': '第二段'}]}, ensure_ascii=True), '第一段\n\n第二段'),
        (json.dumps(json.dumps({'result': '成功'})), '成功'),
        ('\\uINVALID untouched', '\\uINVALID untouched'),
        ('<script>alert(1)</script> 中文', '<script>alert(1)</script> 中文'),
    ]
    for raw, expected in originals:
        assert present_result(raw)['text'] == expected
        assert hashlib.sha256(raw.encode()).hexdigest() == hashlib.sha256(raw.encode()).hexdigest()


def test_review_evidence_keeps_raw_hash_and_completed_task_result(tmp_path):
    c,a,v,secret,tasks = setup(tmp_path)
    raw=json.dumps({'answer':'你好，这是完成结果。'},ensure_ascii=True)
    task,did,_=run_job(c,a,secret,tasks,'你是谁？',result=raw,rid='presentation-fixture')
    # Fixtures create the job through TaskCenter directly; record its workspace visibility.
    admin_id=c.get('/api/v2/me',headers=a).json()['id']
    with sqlite3.connect(tmp_path/'users.db') as db:
        db.execute('INSERT INTO task_ownership VALUES(?,?,?,NULL)',(task.id,'main',admin_id))
    response=c.get(f'/api/v2/main/dispatch/{did}/evidence',headers=a)
    assert response.status_code==200
    evidence=response.json()
    assert evidence['result']==raw
    assert evidence['sha256']==hashlib.sha256(raw.encode()).hexdigest()
    assert evidence['presentation']['text']=='你好，这是完成结果。'
    assert c.get(f'/api/v2/main/dispatch/{did}/evidence',headers=v).status_code==403
    assert c.get(f'/api/v2/main/tasks/{task.id}/result',headers=a).status_code==409
    done=c.post(f'/api/v2/main/dispatch/{did}/review',headers=a,json={
        'accepted':True,'evidence_ref':'human:independent-reviewed','expected_sha256':evidence['sha256']})
    assert done.status_code==200, done.text
    history=c.get('/api/v2/main/results',headers=v)
    assert history.status_code==200 and any(x['id']==task.id for x in history.json()['completed'])
    final=c.get(f'/api/v2/main/tasks/{task.id}/result',headers=v)
    assert final.status_code==200,final.text
    assert final.json()['result']==raw
    assert final.json()['presentation']['text']=='你好，这是完成结果。'
    assert c.get(f'/api/v2/other/tasks/{task.id}/result',headers=a).status_code in (403,404)
    assert c.get('/api/v2/main/tasks/not-a-task/result',headers=v).status_code==404
