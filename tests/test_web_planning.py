"""Web submission -> safe read-only planner -> DAG -> approved dispatch."""
import json
from fastapi.testclient import TestClient
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.collaboration_api import create_app
from biomni_dual.scheduler import TaskCenter
from biomni_dual.observability import OperationsStore
from biomni_dual.runtime_relations import RuntimeRelationStore
from biomni_dual.contracts import AgentResult

REQUEST={'cpu_milli':1000,'memory_mb':1024,'gpu_units':0}

class StubRuntime:
    def __init__(self, answer, calls):
        self.answer,self.calls=answer,calls
    async def run(self,request):
        self.calls.append(request)
        return AgentResult(True, self.answer)
    async def cancel(self,task_id):return False


def rig(tmp_path,answer):
    cstore=CollaborationStore(tmp_path/'users.db')
    owner=cstore.bootstrap('owner@example.org','SomeLongPassword456')
    cstore.invite(owner,'main','op@example.org','SomeLongPassword456','operator')
    tasks=TaskCenter(tmp_path/'tasks.db'); ops=OperationsStore(tmp_path/'ops.db')
    rel=RuntimeRelationStore(tmp_path/'rel.db')
    calls=[]
    c=TestClient(create_app(cstore,tasks,ops,rel,planning_runtime_factory=lambda backend:StubRuntime(answer,calls)))
    def login(who):
        token=c.post('/api/v2/login',json={'email':who,'password':'SomeLongPassword456'}).json()['token']
        return {'Authorization':'Bearer '+token}
    admin,op=login('owner@example.org'),login('op@example.org')
    return c,admin,op,tasks,rel,calls


def configure(c,admin,rel,auto=True,worker=True):
    secret=None
    rel.capability('main','analysis','approved','independent:reviewed')
    assert c.put('/api/v2/main/execution-policy',headers=admin,json={'enabled':True,'capability':'analysis','resources':REQUEST}).status_code==200
    resp=c.put('/api/v2/main/planning-policy',headers=admin,json={'enabled':True,'backend':'codex_sdk','auto_approve_trusted':auto})
    assert resp.status_code==200,resp.text
    if worker:
        secret=c.post('/api/v2/main/workers',headers=admin,json={'id':'planner-worker'}).json()['secret']
        c.put('/api/v2/main/runtime/workers/planner-worker/capacity',headers=admin,json={'cpu_milli':2000,'memory_mb':3000,'gpu_units':0})
        c.put('/api/v2/main/dispatch/workers/planner-worker/skills',headers=admin,json={'capabilities':['analysis']})
        c.get('/api/v2/worker/planner-worker/dispatch/poll',headers={'x-worker-secret':secret})
    return secret


def sample_plan():
    return json.dumps({'additions':[{'key':'review','prompt':'Review dataset','depends_on':[],'required_capabilities':['analysis']},
                                     {'key':'analyze','prompt':'Analyze dataset','depends_on':['review'],'required_capabilities':['analysis']}],
                       'dependencies':[],'reason':'Two safe steps'})


def test_web_planning_auto_creates_dag_and_dispatches_ready_step(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,sample_plan());configure(c,a,r)
    body={'prompt':'Research dataset','request_id':'planned-task-001'}
    reply=c.post('/api/v2/main/tasks',headers=op,json=body)
    assert reply.status_code==200,reply.text
    result=reply.json(); assert result['planning']['status']=='planned'
    root=t.get(result['id']); assert root.required_capabilities==['analysis']
    children=result['planning']['task_ids']; assert len(children)==2
    assert set(root.depends_on)==set(children.values())
    assert t.get(children['analyze']).depends_on==[children['review']]
    assert t.get(children['review']).required_capabilities==['analysis']
    assert result['dispatch']['state']=='planned'
    assert any(e['state']=='offered' for e in result['dispatch']['offers'])
    assert calls[0].role=='planner' and calls[0].workspace is None
    assert 'Do not use tools' in calls[0].prompt
    assert c.get('/api/v2/main/tasks/'+root.id+'/planning',headers=op).json()['status']=='planned'
    again=c.post('/api/v2/main/tasks',headers=op,json=body).json()
    assert again['id']==root.id and len(calls)==1
    assert len(t.list())==3


def test_planner_policy_requires_admin_and_approved_executor(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,sample_plan())
    p={'enabled':True,'backend':'codex_sdk','auto_approve_trusted':True}
    assert c.put('/api/v2/main/planning-policy',headers=op,json=p).status_code==403
    assert c.put('/api/v2/main/planning-policy',headers=a,json=p).status_code==409
    configure(c,a,r)
    assert c.put('/api/v2/main/planning-policy',headers=a,json={**p,'backend':'evil'}).status_code==400


def test_untrusted_capability_rejected_no_dispatch(tmp_path):
    bad=json.dumps({'additions':[{'key':'bad','prompt':'Unsafe', 'required_capabilities':['unapproved']}], 'dependencies':[]})
    c,a,op,t,r,calls=rig(tmp_path,bad);configure(c,a,r)
    x=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Unsafe goal','request_id':'unapproved-001'}).json()
    assert x['planning']['status']=='failed';assert x['dispatch']['state']=='awaiting_planner'
    assert t.get(x['id']).required_capabilities==[] and len(t.list())==1
    assert c.get('/api/v2/main/dispatch',headers=a).json()['dispatches']==[]


def test_auto_approval_can_be_disabled(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,sample_plan());configure(c,a,r,auto=False)
    x=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Review first','request_id':'review-approval-1'}).json()
    assert x['planning']['status']=='planned'
    for tid in x['planning']['task_ids'].values():
        assert t.get(tid).status.value=='waiting_approval'
    assert x['dispatch']['offers']==[]


def test_planning_failure_can_retry_as_admin(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,'not JSON');configure(c,a,r,worker=False)
    x=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Plan it','request_id':'retry-planner-1'}).json()
    assert x['planning']['status']=='failed'
    assert c.post('/api/v2/main/tasks/'+x['id']+'/replan',headers=op).status_code==403
    assert c.post('/api/v2/main/tasks/'+x['id']+'/replan',headers=a).json()['status']=='failed'
    assert len(t.list())==1


def test_workspace_rejects_access_to_other_plan(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,sample_plan());configure(c,a,r,worker=False)
    x=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Sensitive','request_id':'isolated-goal-1'}).json()
    assert c.get('/api/v2/other/tasks/'+x['id']+'/planning',headers=a).status_code in (403,404)


def test_high_risk_step_needs_review_despite_default_approval(tmp_path):
    plan=json.dumps({'additions':[{'key':'deploy','prompt':'Deploy code to production environment','required_capabilities':['analysis']}], 'dependencies':[]})
    c,a,op,t,r,calls=rig(tmp_path,plan);configure(c,a,r)
    x=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Manage production','request_id':'risky-step-01'}).json()
    assert x['planning']['status']=='planned'
    assert t.get(next(iter(x['planning']['task_ids'].values()))).status.value=='waiting_approval'
    assert x['dispatch']['offers']==[]


def test_planner_explicitly_requires_approval(tmp_path):
    plan=json.dumps({'additions':[{'key':'review','prompt':'Check records','requires_approval':True}], 'dependencies':[]})
    c,a,op,t,r,calls=rig(tmp_path,plan);configure(c,a,r)
    x=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Review','request_id':'explicit-approve-1'}).json()
    assert x['planning']['status']=='planned'
    child=next(iter(x['planning']['task_ids'].values()))
    assert t.get(child).status.value=='waiting_approval'


def test_reject_cross_tenant_dependency_and_preserve_unbound_goal(tmp_path):
    bad=json.dumps({'additions':[{'key':'other','tenant':'other','prompt':'Read other workspace','required_capabilities':['analysis']}], 'dependencies':[]})
    c,a,op,t,r,calls=rig(tmp_path,bad);configure(c,a,r)
    x=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Cross workspace','request_id':'cross-scope-plan-1'}).json()
    assert x['planning']['status']=='failed'
    assert len(t.list())==1 and t.get(x['id']).required_capabilities==[]


def test_cli_response_normalization():
    from biomni_dual.web_planning import cli_planner_text
    value='{"additions":[{"key":"one","prompt":"ok"}],"dependencies":[]}'
    assert cli_planner_text(json.dumps({'type':'result','result':value}),'claude_cli')==value
    events='\n'.join([json.dumps({'type':'thread.started','thread_id':'x'}),json.dumps({'type':'item.completed','item':{'type':'agent_message','text':value}})])
    assert cli_planner_text(events,'codex_cli')==value


def test_planner_failed_request_idempotent_and_not_reexecuted(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,'not valid');configure(c,a,r)
    body={'prompt':'Failed safely','request_id':'failed-idem-plan-1'}
    x=c.post('/api/v2/main/tasks',headers=op,json=body).json()
    again=c.post('/api/v2/main/tasks',headers=op,json=body).json()
    assert x['id']==again['id'] and len(calls)==1
    assert x['planning']['status']=='failed' and len(t.list())==1


def test_planned_goal_cannot_bypass_dependencies_through_legacy_activation(tmp_path):
    c,a,op,t,r,calls=rig(tmp_path,sample_plan());configure(c,a,r)
    x=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Multi-step study','request_id':'protected-goal-1'}).json()
    assert x['planning']['status']=='planned'
    assert c.post('/api/v2/main/tasks/'+x['id']+'/activate',headers=a).status_code==409
    assert set(t.get(x['id']).depends_on)==set(x['planning']['task_ids'].values())
    trace=c.get('/api/v2/main/tasks/'+x['id']+'/trace',headers=op)
    assert trace.status_code==200 and trace.json()['planning']['status']=='planned'
