"""v0.8.9 regression: bounded dispatch routing and safe failed-plan recovery."""
from test_web_planning import rig, configure
from biomni_dual.task_router import classify_prompt


def test_router_conservative():
    assert classify_prompt('你好，你是谁？').kind == 'simple'
    assert classify_prompt('你好，你叫什么名字？').kind == 'simple'
    assert classify_prompt('分析你有什么能力').kind == 'simple'
    assert classify_prompt('查询当前 Worker 状态').kind == 'single'
    assert classify_prompt('修复代码并部署到生产环境').kind == 'complex'
    assert classify_prompt('Research dataset').kind == 'complex'


def test_simple_skips_planner_and_still_needs_approved_worker(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,'not JSON')
    configure(c,a,r,worker=True)
    response=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'你好，你是谁？','request_id':'simple-hello-089'})
    assert response.status_code==200,response.text
    x=response.json()
    assert x['route']=='simple' and 'planning' not in x
    assert calls==[]
    task=t.get(x['id'])
    assert task.required_capabilities==['analysis']
    assert x['dispatch']['state']=='offered'
    again=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'你好，你是谁？','request_id':'simple-hello-089'}).json()
    assert again['id']==x['id'] and len(t.list())==1


def test_single_read_only_is_single_node_without_planner(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,'not JSON')
    configure(c,a,r,worker=False)
    x=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'查询当前 Worker 状态','request_id':'single-read-089'}).json()
    assert x['route']=='single' and calls==[] and len(t.list())==1
    assert t.get(x['id']).required_capabilities==['analysis']


def test_failed_planned_simple_can_admin_recover_without_planner(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,'not JSON')
    configure(c,a,r,worker=True)
    task=t.submit('你好，你是谁？',tenant='main',required_capabilities=[])
    # Simulate the exact old failed web planner ledger state.
    from biomni_dual.web_planning import WebPlanningCoordinator
    coord=WebPlanningCoordinator(t)
    coord._claim(task.id,'main','codex_sdk')
    coord._finish(task.id,'main','failed',{'message':'Planner failed'})
    assert c.post('/api/v2/main/tasks/'+task.id+'/direct',headers=op).status_code==403
    response=c.post('/api/v2/main/tasks/'+task.id+'/direct',headers=a)
    assert response.status_code==200,response.text
    x=response.json()
    assert x['route']=='simple' and x['dispatch']['state']=='offered'
    assert c.get('/api/v2/main/tasks/'+task.id+'/planning',headers=op).json()['status']=='direct'
    assert c.post('/api/v2/main/tasks/'+task.id+'/activate',headers=a).status_code==409
    assert c.post('/api/v2/main/tasks/'+task.id+'/direct',headers=a).status_code==200
    assert calls==[]


def test_complex_failed_goal_cannot_bypass_planner(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,'not JSON')
    configure(c,a,r,worker=False)
    x=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Research dataset','request_id':'complex-089'}).json()
    assert x['planning']['status']=='failed' and len(calls)==1
    assert x['planning']['error_type']=='invalid_plan'
    assert c.post('/api/v2/main/tasks/'+x['id']+'/direct',headers=a).status_code==409
    assert t.get(x['id']).required_capabilities==[]
