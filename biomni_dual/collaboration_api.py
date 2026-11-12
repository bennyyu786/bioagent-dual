"""ASGI multiuser facade, scoped by workspace. Install with `pip install -e '.[server]'`."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4
from dataclasses import asdict
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from .collaboration import CollaborationStore, Forbidden, InvalidCredentials
from .scheduler import TaskCenter
from .observability import OperationsStore
from .chat_advisor import OpenAICompatibleAdvisor
from .dag_view import task_graph, task_trace
from .execution_topology import execution_topology
from .runtime_relations import RuntimeRelationStore, resources
from .dispatch_runtime import ResourceAwareDispatcher, DispatchConflict
from .web_planning import WebPlanningCoordinator, BACKENDS
from .task_router import classify_prompt
from .conversation_router import conversation_route
from .adaptive_router import AdaptiveTaskRouter
from .result_acceptance import AcceptancePolicy, classify_task, delivery_check, validator_check, artifact_evidence
from .result_presentation import present_result

UI=Path(__file__).parent.joinpath('web','collaboration.html').read_text('utf-8')

class Login(BaseModel):
    email: str = Field(max_length=80)
    password: str = Field(max_length=256)
class Message(BaseModel):
    session: str = 'main'
    body: str = Field(min_length=1,max_length=4000)
class TaskInput(BaseModel):
    prompt: str = Field(min_length=1,max_length=4000)
    request_id: str = Field(min_length=8,max_length=80)
    source_message_id: str | None = None
class NewMember(BaseModel):
    email:str
    password:str
    role:str = 'viewer'
class NewWorker(BaseModel):
    id:str
class NewCommand(BaseModel):
    action:str
    payload:dict = Field(default_factory=dict)
class Ack(BaseModel):
    status:str
    receipt:str = ''
class Log(BaseModel):
    level:str
    message:str
    task_id:str | None = None
class WorkerCapacity(BaseModel):
    cpu_milli: int
    memory_mb: int
    gpu_units: int
class ExecutionPolicy(BaseModel):
    enabled: bool = False
    capability: str = Field(min_length=1,max_length=128)
    resources: WorkerCapacity

class RoutingPolicy(BaseModel):
    enabled: bool = False
    backend: str = "codex_sdk"

class PlanningPolicy(BaseModel):
    enabled: bool = False
    backend: str = "codex_sdk"
    auto_approve_trusted: bool = True

class AcceptanceConfig(BaseModel):
    enabled: bool = False
    auto_l1: bool = True
    auto_l2: bool = True
    l1_capabilities: list[str] = Field(default_factory=list, max_length=20)

class ReserveTask(BaseModel):
    task_id: str
    worker: str
    request_id: str
    resources: WorkerCapacity
class LeaseRequest(BaseModel):
    ttl_seconds: int = 300
class ReservationReceipt(BaseModel):
    state: str
    receipt: str = ''
class CapabilityEvidence(BaseModel):
    lifecycle: str
    evidence_ref: str
class RepairLink(BaseModel):
    task_id: str
    repair_task_id: str
    evidence_ref: str
class WorkerSkills(BaseModel):
    capabilities: list[str] = Field(default_factory=list, max_length=100)
class DispatchRequest(BaseModel):
    task_id: str
    request_id: str
    resources: WorkerCapacity
    worker: str | None = None
class DispatchNext(BaseModel):
    request_id: str
    resources: WorkerCapacity
class WorkerReport(BaseModel):
    state: str
    result: str = Field(max_length=32000)
class DispatchReview(BaseModel):
    accepted: bool
    evidence_ref: str = Field(min_length=8,max_length=256)
    expected_sha256: str
class Reconcile(BaseModel):
    verified_no_side_effect: bool = False
class ModelConfig(BaseModel):
    base_url:str
    model:str
class UnifiedTurn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session: str = 'main'
    mode: str = 'auto'
    request_id: str = Field(min_length=8, max_length=80)

class ModelPrompt(BaseModel):
    message:str = Field(min_length=1,max_length=4000)
    session:str = 'main'


def create_app(collaboration:CollaborationStore,tasks:TaskCenter,operations:OperationsStore,
               runtime_relations:RuntimeRelationStore | None = None, *, result_validator=None, planning_runtime_factory=None, auto_delivery=True):
    if runtime_relations is None:
        runtime_relations=RuntimeRelationStore(collaboration.path+'.runtime_relations.sqlite')
    dispatcher=ResourceAwareDispatcher(runtime_relations,tasks)
    web_planner=WebPlanningCoordinator(tasks,planning_runtime_factory)
    adaptive_router=AdaptiveTaskRouter(planning_runtime_factory)
    with collaboration.db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS workspace_execution_policy(
            workspace TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
            capability TEXT NOT NULL, resources TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS workspace_acceptance_policy(
            workspace TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
            auto_l1 INTEGER NOT NULL, auto_l2 INTEGER NOT NULL,
            l1_capabilities TEXT NOT NULL DEFAULT '[]')""")
        db.execute("""CREATE TABLE IF NOT EXISTS workspace_routing_policy(
            workspace TEXT PRIMARY KEY, enabled INTEGER NOT NULL, backend TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS workspace_planning_policy(
            workspace TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
            backend TEXT NOT NULL, auto_approve_trusted INTEGER NOT NULL)""")

    def _policy(ws):
        with collaboration.db() as db:
            row=db.execute('SELECT enabled,capability,resources FROM workspace_execution_policy WHERE workspace=?',(ws,)).fetchone()
        return ({'enabled':bool(row[0]),'capability':row[1], 'resources':json.loads(row[2])}
                if row else None)

    def _planning_policy(ws):
        with collaboration.db() as db:
            row=db.execute("SELECT enabled,backend,auto_approve_trusted FROM workspace_planning_policy WHERE workspace=?",(ws,)).fetchone()
        return ({"enabled":bool(row[0]),"backend":row[1],"auto_approve_trusted":bool(row[2])} if row else {"enabled":False,"backend":"codex_sdk","auto_approve_trusted":True})

    def _routing_policy(ws):
        with collaboration.db() as db:
            row=db.execute('SELECT enabled,backend FROM workspace_routing_policy WHERE workspace=?', (ws,)).fetchone()
        return {'enabled':bool(row[0]),'backend':row[1]} if row else {'enabled':False,'backend':'codex_sdk'}

    def _acceptance_policy(ws):
        with collaboration.db() as db:
            row=db.execute('SELECT enabled,auto_l1,auto_l2,l1_capabilities FROM workspace_acceptance_policy WHERE workspace=?',(ws,)).fetchone()
        return AcceptancePolicy(bool(row[0]),bool(row[1]),bool(row[2]),tuple(json.loads(row[3]))) if row else AcceptancePolicy()

    def _automatic_acceptance(ws, did):
        """Runs after durable worker report. Any uncertainty leaves the review queue intact."""
        policy=_acceptance_policy(ws)
        if not policy.enabled:
            return {'status':'manual_required','reason':'Auto acceptance is not enabled'}
        try:
            e=dispatcher.evidence(ws,did)
            task=tasks.get(e['task_id'])
            if task is None or task.tenant!=ws:
                return {'status':'manual_required','reason':'Task context unavailable'}
            tier=classify_task(task)
            if e['state']!='review_required' or e['worker_state']!='completed':
                return {'status':'manual_required','tier':tier,'reason':'Worker result must be reviewed'}
            if tier=='L1' and policy.auto_l1:
                if list(task.required_capabilities)!=[c for c in task.required_capabilities if c in policy.l1_capabilities]:
                    return {'status':'manual_required','tier':'L1','reason':'Execution capability not explicitly approved for L1 auto-acceptance'}
                ok, reason=delivery_check(task.prompt,e['worker_state'],e['result'])
                if not ok:return {'status':'manual_required','tier':tier,'reason':reason}
                evidence=artifact_evidence(e['sha256'])
            elif tier=='L2' and policy.auto_l2:
                ok,evidence=validator_check(result_validator,task,e['result'])
                if ok is not True:
                    return {'status':'manual_required','tier':tier,'reason':evidence}
            else:
                return {'status':'manual_required','tier':tier,'reason':'Independent verification and human sign-off required' if tier=='L3' else 'Tier auto-accept disabled'}
            out=dispatcher.review(ws,did,'auto:acceptance:'+tier,accepted=True,
                                  evidence_ref=evidence,expected_sha256=e['sha256'])
            collaboration.emit(ws,'dispatch_verified',did,{'task_id':out['task_id'],'tier':tier,'verification':'automated_policy'})
            return {'status':'auto_verified','tier':tier,'dispatch':out}
        except Exception:
            # Never convert a failure to 'verified' or silently retry an uncertain side effect.
            return {'status':'manual_required','reason':'Acceptance transition failed; inspect state before retry'}

    def _auto_offer(ws,task):
        policy=_policy(ws)
        if not policy or not policy['enabled']:
            return {'state':'awaiting_configuration', 'reason':'Administrator has not enabled a verified workspace execution policy'}
        if list(task.required_capabilities)!=[policy['capability']]:
            return {'state':'awaiting_capability', 'reason':'Task capability differs from the approved execution policy'}
        with runtime_relations.db() as db:
            approved=db.execute('SELECT lifecycle FROM capability_evidence WHERE workspace=? AND capability=?',
                                (ws,policy['capability'])).fetchone()
        if not approved or approved[0]!='approved':
            return {'state':'awaiting_capability', 'reason':'Execution capability is not currently approved'}
        try:
            offered=dispatcher.plan_and_offer(ws,task.id,'auto:'+task.id,
                                                policy['resources'],_available_workers(ws))
        except DispatchConflict as exc:
            return {'state':'awaiting_worker', 'reason':str(exc)}
        collaboration.emit(ws,'dispatch_offered',offered['id'],{'task_id':task.id,'worker':offered['worker'],'source':'workspace_policy'})
        return {'state':'offered', 'worker':offered['worker'], 'dispatch_id':offered['id']}

    app=FastAPI(title='Biomni Collaborative Control Plane',version='0.9.1',docs_url=None,redoc_url=None)

    @app.middleware('http')
    async def secure_headers(req,call_next):
        res=await call_next(req)
        res.headers['Cache-Control']='no-store'
        res.headers['X-Content-Type-Options']='nosniff'
        res.headers['X-Frame-Options']='DENY'
        res.headers['Referrer-Policy']='no-referrer'
        res.headers['Content-Security-Policy']="default-src 'self'; connect-src 'self' ws: wss:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'"
        return res

    @app.exception_handler(Forbidden)
    async def forbidden_handler(_,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail':'Forbidden'},status_code=403)
    @app.exception_handler(ValueError)
    async def bad_handler(_,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail':'Invalid request'},status_code=400)

    def current(req:Request):
        auth=req.headers.get('authorization','')
        if not auth.startswith('Bearer '):raise HTTPException(401,'Authentication required')
        try:return collaboration.identity(auth[7:])
        except Forbidden:raise HTTPException(401,'Authentication required')

    def member(req,ws,role='viewer'):
        user=current(req)
        collaboration.require(user['id'],ws,role)
        return user

    def worker_creds(req:Request,worker):
        credential=req.headers.get('x-worker-secret','')
        try: return credential,collaboration.worker_auth(worker,credential)
        except Forbidden:raise HTTPException(401,'Worker unauthorized')

    @app.get('/',response_class=HTMLResponse)
    @app.get('/control',response_class=HTMLResponse)
    def index():return UI

    @app.get('/health/live')
    def live():return {'alive':True}
    @app.get('/health/ready')
    def ready():
        try:
            with collaboration.db() as db:db.execute('SELECT 1').fetchone()
            return {'ready':True,'database':'connected'}
        except Exception:raise HTTPException(503,'Database not ready')

    @app.post('/api/v2/login')
    def login(body:Login):
        try:return collaboration.login(body.email,body.password)
        except InvalidCredentials:raise HTTPException(401,'Invalid credentials')

    @app.post('/api/v2/logout')
    def logout(req:Request):
        user=current(req)
        collaboration.logout(req.headers['authorization'][7:])
        return {'ok':True}

    @app.get('/api/v2/me')
    def me(req:Request):
        user=current(req)
        return {**user,'workspaces':collaboration.my_workspaces(user['id'])}

    @app.get('/api/v2/{ws}/overview')
    def overview(ws:str,req:Request):
        user=member(req,ws)
        return {'workspace':ws,'tasks':collaboration.tasks(user['id'],ws,tasks),
                'model':collaboration.model_info(user['id'],ws),
                'workers':[{'id':x['id'],'state':x['state'],'age_seconds':x['age_seconds']}
                           for x in operations.snapshot()['workers']
                           if x['id'] in _workspace_workers(ws)],
                'recent_events':collaboration.events(user['id'],ws,0,50)}

    def _workspace_workers(ws):
        with collaboration.db() as db:
            return {r[0] for r in db.execute('SELECT id FROM worker_credentials WHERE workspace=?',(ws,))}

    @app.get('/api/v2/{ws}/graph')
    def graph(ws:str,req:Request):
        user=member(req,ws)
        return task_graph(collaboration.tasks(user['id'],ws,tasks))

    @app.get('/api/v2/{ws}/topology')
    def topology(ws:str,req:Request):
        user=member(req,ws)
        visible=collaboration.tasks(user['id'],ws,tasks)
        # Never resolve opaque dependencies through TaskCenter before workspace
        # ownership has been checked. Topology is a read-only projection.
        scoped_workers=_workspace_workers(ws)
        workers=[w for w in operations.snapshot()['workers'] if w['id'] in scoped_workers]
        events=collaboration.events(user['id'],ws,0,100)
        return execution_topology(visible,tasks,workers,events,workspace=ws,
                                  runtime=runtime_relations.snapshot(ws),dispatches=dispatcher.list(ws))

    def _available_workers(ws):
        allowed=_workspace_workers(ws)
        return {w['id'] for w in operations.snapshot()['workers'] if w['id'] in allowed and w['state']=='online'}

    @app.get('/api/v2/{ws}/dispatch')
    def dispatch_status(ws:str,req:Request):
        member(req,ws)
        return {'dispatches':dispatcher.list(ws),'semantics':'Delivered means execution finished and artifact available; not independent scientific validation'}

    @app.put('/api/v2/{ws}/dispatch/workers/{worker}/skills')
    def worker_skills(ws:str,worker:str,body:WorkerSkills,req:Request):
        actor=member(req,ws,'admin')['id']
        collaboration._valid(worker)
        if worker not in _workspace_workers(ws):raise HTTPException(404,'Worker not in workspace')
        dispatcher.set_capabilities(ws,worker,body.capabilities)
        collaboration.emit(ws,'worker_skills_updated',worker,{'actor':actor})
        return {'ok':True}

    @app.post('/api/v2/{ws}/dispatch')
    def dispatch_create(ws:str,body:DispatchRequest,req:Request):
        actor=member(req,ws,'admin')['id']
        collaboration._valid(body.task_id);collaboration._valid(body.request_id)
        if body.worker is not None:collaboration._valid(body.worker)
        try:
            outcome=dispatcher.plan_and_offer(ws,body.task_id,body.request_id,body.resources.model_dump(),_available_workers(ws),body.worker)
        except DispatchConflict as exc:raise HTTPException(409,str(exc))
        collaboration.emit(ws,'dispatch_offered',outcome['id'],{'actor':actor,'task_id':body.task_id,'worker':outcome['worker']})
        return outcome

    @app.post('/api/v2/{ws}/dispatch/next')
    def dispatch_next(ws:str,body:DispatchNext,req:Request):
        actor=member(req,ws,'admin')['id']
        collaboration._valid(body.request_id)
        available=_available_workers(ws)
        # TaskCenter.ready() already ranks priority and aging. Never dispatch
        # tasks from other tenants or tasks with incomplete dependencies.
        for task in tasks.ready():
            if task.tenant != ws:continue
            try:
                result=dispatcher.plan_and_offer(ws,task.id,body.request_id+':'+task.id,
                                  body.resources.model_dump(),available)
            except DispatchConflict:continue
            collaboration.emit(ws,'dispatch_offered',result['id'],{'actor':actor,'task_id':task.id,'worker':result['worker']})
            return result
        return {'state':'no_eligible_task'}

    @app.get('/api/v2/worker/{worker}/dispatch/poll')
    def dispatch_poll(worker:str,req:Request):
        secret,ws=worker_creds(req,worker)
        operations.worker_heartbeat(worker,secret)
        offer=dispatcher.offer_for_worker(ws,worker)
        if offer is None:
            policy=_policy(ws)
            if policy and policy['enabled']:
                for task in tasks.ready():
                    if task.tenant==ws and list(task.required_capabilities)==[policy['capability']]:
                        result=_auto_offer(ws,task)
                        if result['state']=='offered':
                            offer=dispatcher.offer_for_worker(ws,worker)
                            if offer is not None:break
        return {'offer':offer}

    @app.post('/api/v2/worker/{worker}/dispatch/{did}/claim')
    def dispatch_claim(worker:str,did:str,body:LeaseRequest,req:Request):
        _,ws=worker_creds(req,worker)
        try:out=dispatcher.claim(ws,worker,did,body.ttl_seconds)
        except DispatchConflict as exc:raise HTTPException(409,str(exc))
        collaboration.emit(ws,'dispatch_running',did,{'worker':worker,'task_id':out['task_id']})
        return out

    @app.post('/api/v2/worker/{worker}/dispatch/{did}/report')
    def dispatch_report(worker:str,did:str,body:WorkerReport,req:Request):
        _,ws=worker_creds(req,worker)
        try:out=dispatcher.report(ws,worker,did,body.state,body.result)
        except DispatchConflict as exc:raise HTTPException(409,str(exc))
        collaboration.emit(ws,'dispatch_'+out['state'],did,{'worker':worker})
        if out['state']=='review_required' and auto_delivery:
            # No user-facing review gate: safely finish execution after receiving its report.
            # This does not assert that scientific claims were independently validated.
            try:
                final=dispatcher.finalize_delivery(ws,did)
            except DispatchConflict as exc:
                collaboration.emit(ws,'dispatch_uncertain',did,{'worker':worker,'reason':'Cross-store completion requires reconciliation'})
                raise HTTPException(409,str(exc))
            out.update(final)
            collaboration.emit(ws,'dispatch_'+final['state'],did,
                               {'worker':worker,'task_id':final['task_id'],'verification':'delivery_only'})
            if final['state']=='delivered':
                collaboration.emit(ws,'task_completed',final['task_id'],{'dispatch_id':did})
            elif final['state']=='failed':
                collaboration.emit(ws,'task_failed',final['task_id'],{'dispatch_id':did})
        if out['state']=='review_required' and not auto_delivery:
            out['acceptance']=_automatic_acceptance(ws,did)
        return out

    @app.post('/api/v2/{ws}/dispatch/{did}/cancel')
    def dispatch_cancel(ws:str,did:str,req:Request):
        actor=member(req,ws,'admin')['id']
        try:out=dispatcher.cancel_offer(ws,did)
        except DispatchConflict as exc:raise HTTPException(409,str(exc))
        collaboration.emit(ws,'dispatch_cancelled',did,{'actor':actor})
        return out

    @app.get('/api/v2/{ws}/acceptance/policy')
    def acceptance_policy_get(ws:str,req:Request):
        member(req,ws,'admin')
        p=_acceptance_policy(ws)
        return {'enabled':p.enabled,'auto_l1':p.auto_l1,'auto_l2':p.auto_l2,
                'l3_manual_required':True,'l1_capabilities':list(p.l1_capabilities),
                'notice':'L1 only verifies delivery/format; L2 requires a server-side independent validator; L3 requires independent validation and human review.'}

    @app.put('/api/v2/{ws}/acceptance/policy')
    def acceptance_policy_put(ws:str,body:AcceptanceConfig,req:Request):
        actor=member(req,ws,'admin')['id']
        if any(not isinstance(x,str) or not 1<=len(x)<=128 for x in body.l1_capabilities):
            raise HTTPException(400,'Invalid L1 capability ID')
        with collaboration.db() as db:
            db.execute('INSERT INTO workspace_acceptance_policy(workspace,enabled,auto_l1,auto_l2,l1_capabilities) VALUES(?,?,?,?,?) ON CONFLICT(workspace) DO UPDATE SET enabled=excluded.enabled,auto_l1=excluded.auto_l1,auto_l2=excluded.auto_l2,l1_capabilities=excluded.l1_capabilities',
                       (ws,int(body.enabled),int(body.auto_l1),int(body.auto_l2),json.dumps(sorted(set(body.l1_capabilities)))))
        collaboration.emit(ws,'acceptance_policy_updated',ws,{'actor':actor,'enabled':body.enabled})
        return acceptance_policy_get(ws,req)

    @app.get('/api/v2/{ws}/acceptance/queue')
    def acceptance_queue(ws:str,req:Request):
        member(req,ws,'admin')
        rows=[]
        for entry in dispatcher.list(ws,limit=500):
            if entry['state']!='review_required':continue
            task=tasks.get(entry['task_id'])
            if task is None or task.tenant!=ws:continue
            rows.append({**entry,'task_title':task.prompt[:180],
                         'tier':classify_task(task),
                         'independent_validator_available':result_validator is not None})
        return {'pending':rows,'count':len(rows)}

    @app.post('/api/v2/{ws}/acceptance/{did}/evaluate')
    def acceptance_evaluate(ws:str,did:str,req:Request):
        member(req,ws,'admin')
        out=_automatic_acceptance(ws,did)
        return out

    @app.post('/api/v2/{ws}/dispatch/{did}/verify')
    def dispatch_verify(ws:str,did:str,req:Request):
        actor=member(req,ws,'admin')['id']
        if result_validator is None:raise HTTPException(503,'Independent domain validator not configured')
        try:out=dispatcher.validate_and_review(ws,did,actor,result_validator)
        except DispatchConflict as exc:raise HTTPException(409,str(exc))
        collaboration.emit(ws,'dispatch_'+out['state'],did,{'actor':actor,'task_id':out['task_id'],'verification':'domain_validator'})
        return out

    @app.get('/api/v2/{ws}/dispatch/{did}/evidence')
    def dispatch_evidence(ws:str,did:str,req:Request):
        member(req,ws,'admin')
        try:
            output=dispatcher.evidence(ws,did)
            return {**output, 'presentation':present_result(output['result'])}
        except DispatchConflict as exc:raise HTTPException(404,str(exc))

    @app.post('/api/v2/{ws}/dispatch/{did}/review')
    def dispatch_review(ws:str,did:str,body:DispatchReview,req:Request):
        actor=member(req,ws,'admin')['id']
        # In enabled tiered mode, L2/L3 success requires a server-installed
        # independent validator, not merely a human-entered evidence label.
        policy=_acceptance_policy(ws)
        if body.accepted and policy.enabled:
            evidence=dispatcher.evidence(ws,did)
            task=tasks.get(evidence['task_id'])
            if task is None or task.tenant!=ws:raise HTTPException(404,'Task unavailable')
            tier=classify_task(task)
            if tier in ('L2','L3'):
                passed, verified_ref=validator_check(result_validator,task,evidence['result'])
                if passed is None:raise HTTPException(503,'Independent validator required for '+tier+': '+verified_ref)
                if not passed:raise HTTPException(409,'Independent validation did not pass')
                # Use independently obtained evidence, not user-supplied claim.
                body=DispatchReview(accepted=True,evidence_ref=verified_ref,expected_sha256=body.expected_sha256)
            elif tier=='L1':
                ok,reason=delivery_check(task.prompt,evidence['worker_state'],evidence['result'])
                if not ok:raise HTTPException(409,reason)
        try:out=dispatcher.review(ws,did,actor,accepted=body.accepted,
                   evidence_ref=body.evidence_ref,expected_sha256=body.expected_sha256)
        except DispatchConflict as exc:raise HTTPException(409,str(exc))
        collaboration.emit(ws,'dispatch_'+out['state'],did,{'actor':actor,'task_id':out['task_id']})
        return out

    @app.post('/api/v2/{ws}/dispatch/scan')
    def dispatch_scan(ws:str,req:Request):
        actor=member(req,ws,'admin')['id']
        count=dispatcher.quarantine_expired(ws)
        if count:collaboration.emit(ws,'dispatch_uncertain','lease_scan',{'actor':actor,'count':count})
        return {'quarantined':count}

    @app.get('/api/v2/{ws}/runtime/resources')
    def resource_snapshot(ws:str,req:Request):
        member(req,ws)
        return runtime_relations.snapshot(ws)

    @app.put('/api/v2/{ws}/runtime/workers/{worker}/capacity')
    def worker_capacity(ws:str,worker:str,body:WorkerCapacity,req:Request):
        actor=member(req,ws,'admin')['id']
        collaboration._valid(worker)
        if worker not in _workspace_workers(ws):raise HTTPException(404,'Worker not in workspace')
        runtime_relations.configure_worker(ws,worker,body.model_dump())
        collaboration.emit(ws,'runtime_capacity',worker,{'actor':actor})
        return {'ok':True}

    @app.post('/api/v2/{ws}/runtime/reservations')
    def reservation(ws:str,body:ReserveTask,req:Request):
        actor=member(req,ws,'admin')['id']
        for ident in (body.task_id,body.worker,body.request_id):collaboration._valid(ident)
        if body.worker not in _workspace_workers(ws):raise HTTPException(404,'Worker not in workspace')
        t=tasks.get(body.task_id)
        if t is None or t.tenant!=ws or t.status.value!='pending':
            raise HTTPException(409,'Task is not pending in workspace')
        # Reservation is resource intention only: does not claim TaskCenter,
        # start a remote process, or grant Worker execution authority.
        rid=runtime_relations.reserve(ws,body.task_id,body.worker,body.request_id,body.resources.model_dump())
        collaboration.emit(ws,'runtime_reserved',rid,{'task_id':body.task_id,'worker':body.worker,'actor':actor})
        return {'id':rid,'state':'reserved','note':'Resource intent, not execution'}

    @app.post('/api/v2/worker/{worker}/runtime/reservations/{rid}/claim')
    def reservation_claim(worker:str,rid:str,body:LeaseRequest,req:Request):
        _,ws=worker_creds(req,worker)
        result=runtime_relations.claim(ws,worker,rid,body.ttl_seconds)
        collaboration.emit(ws,'runtime_claimed',rid,{'worker':worker,'task_id':result['task_id']})
        return result

    @app.post('/api/v2/worker/{worker}/runtime/reservations/{rid}/finish')
    def reservation_finish(worker:str,rid:str,body:ReservationReceipt,req:Request):
        _,ws=worker_creds(req,worker)
        runtime_relations.finish(ws,worker,rid,body.state,body.receipt)
        collaboration.emit(ws,'runtime_'+body.state,rid,{'worker':worker})
        return {'ok':True}

    @app.post('/api/v2/{ws}/runtime/reservations/{rid}/reconcile')
    def reservation_reconcile(ws:str,rid:str,body:Reconcile,req:Request):
        actor=member(req,ws,'admin')['id']
        runtime_relations.reconcile(ws,rid,verified_no_side_effect=body.verified_no_side_effect)
        collaboration.emit(ws,'runtime_reconciled',rid,{'actor':actor})
        return {'ok':True}

    @app.post('/api/v2/{ws}/runtime/leases/scan')
    def reservation_scan(ws:str,req:Request):
        member(req,ws,'admin')
        count=runtime_relations.quarantine_expired(ws)
        if count:collaboration.emit(ws,'runtime_quarantined','lease_scan',{'count':count})
        return {'quarantined':count}

    @app.put('/api/v2/{ws}/runtime/capabilities/{cid}')
    def capability_lifecycle(ws:str,cid:str,body:CapabilityEvidence,req:Request):
        member(req,ws,'admin')
        collaboration._valid(cid)
        runtime_relations.capability(ws,cid,body.lifecycle,body.evidence_ref)
        collaboration.emit(ws,'runtime_capability',cid,{'lifecycle':body.lifecycle})
        return {'ok':True}

    @app.post('/api/v2/{ws}/runtime/repairs')
    def repair_link(ws:str,body:RepairLink,req:Request):
        member(req,ws,'admin')
        for tid in (body.task_id,body.repair_task_id):
            collaboration._valid(tid)
            t=tasks.get(tid)
            if t is None or t.tenant!=ws:raise HTTPException(404,'Task not in workspace')
        runtime_relations.link_repair(ws,body.task_id,body.repair_task_id,body.evidence_ref)
        collaboration.emit(ws,'runtime_repair',body.task_id,{'repair_task':body.repair_task_id})
        return {'ok':True}

    @app.get('/api/v2/{ws}/results')
    def completed_results(ws:str,req:Request):
        actor=member(req,ws)
        visible=collaboration.tasks(actor['id'],ws,tasks)
        return {'completed':[{'id':t['id'],'prompt':t['prompt'],'status':t['status']}
                             for t in visible if t['status']=='completed']}

    @app.get('/api/v2/{ws}/tasks/{task_id}/result')
    def task_result(ws:str,task_id:str,req:Request):
        actor=member(req,ws)
        visible=collaboration.tasks(actor['id'],ws,tasks)
        if not any(t['id']==task_id for t in visible):
            raise HTTPException(404,'Task not found')
        task=tasks.get(task_id)
        if task is None or task.tenant!=ws:
            raise HTTPException(404,'Task not found')
        if task.status.value!='completed':
            raise HTTPException(409,'Task execution has not completed')
        return {'task_id':task.id,'prompt':task.prompt,'status':task.status.value,
                'result':task.result,'presentation':present_result(task.result)}

    @app.get('/api/v2/{ws}/tasks/{task_id}/trace')
    def trace(ws:str,task_id:str,req:Request):
        user=member(req,ws)
        # Resolve membership and task ownership first; never query the global
        # task/event log using an ID supplied by an untrusted tenant.
        visible=collaboration.tasks(user['id'],ws,tasks)
        if not any(t['id']==task_id for t in visible):
            raise HTTPException(404,'Task not found')
        result=task_trace(task_id,ws,visible,collaboration.events(user['id'],ws,0,100),tasks)
        result['planning']=web_planner.status(task_id,ws)
        stored=tasks.get(task_id)
        result['direct_eligible']=bool(stored and stored.tenant==ws and stored.status.value=='pending' and
                                       not stored.attempts and not stored.depends_on and classify_prompt(stored.prompt).kind=='simple' and
                                       (result['planning'] is None or result['planning']['status'] in ('failed','direct'))) 
        source=collaboration.task_source(user['id'],ws,task_id)
        result['source_message'] = ({'id':source['id'],'session':source['session']} if source else None)
        return result

    @app.get('/api/v2/{ws}/members')
    def members(ws:str,req:Request):return collaboration.members(member(req,ws,'admin')['id'],ws)

    @app.post('/api/v2/{ws}/members')
    def invite(ws:str,body:NewMember,req:Request):
        actor=member(req,ws,'admin')['id']
        return {'id':collaboration.invite(actor,ws,body.email,body.password,body.role)}

    @app.post('/api/v2/{ws}/members/{target}/role')
    def role_update(ws:str,target:str,body:dict,req:Request):
        actor=member(req,ws,'admin')['id']
        if set(body)!={'role'}:raise ValueError('Expected role')
        collaboration.change_membership(actor,ws,target,body['role'])
        return {'ok':True}

    @app.delete('/api/v2/{ws}/members/{target}')
    def remove_member(ws:str,target:str,req:Request):
        actor=member(req,ws,'admin')['id']
        collaboration.change_membership(actor,ws,target,None)
        return {'ok':True}

    @app.get('/api/v2/{ws}/audit')
    def audit(ws:str,req:Request):
        user=member(req,ws,'admin')
        return {'records':collaboration.audit_records(user['id'],ws)}

    @app.get('/api/v2/{ws}/commands')
    def commands(ws:str,req:Request):
        user=member(req,ws,'operator')
        return {'commands':collaboration.commands(user['id'],ws)}

    @app.post('/api/v2/{ws}/chat')
    def chat(ws:str,body:Message,req:Request):
        user=member(req,ws,'operator')
        mid=collaboration.write_message(user['id'],ws,body.session,body.body)
        return {'id':mid,'status':'saved','note':'Chat does not execute tasks. Submit separately.'}

    @app.get('/api/v2/{ws}/chat/{session}')
    def messages(ws:str,session:str,req:Request):
        user=member(req,ws)
        return {'messages':collaboration.messages(user['id'],ws,session)}

    @app.get('/api/v2/{ws}/execution-policy')
    def get_execution_policy(ws:str,req:Request):
        member(req,ws,'viewer')
        return _policy(ws) or {'enabled':False,'capability':None,'resources':None}

    @app.put('/api/v2/{ws}/execution-policy')
    def set_execution_policy(ws:str,body:ExecutionPolicy,req:Request):
        actor=member(req,ws,'admin')['id']
        collaboration._valid(body.capability)
        amount=resources(body.resources.model_dump())
        if body.enabled:
            # Administrative configuration is not capability approval. A
            # separately reviewed capability must exist in the same workspace.
            with runtime_relations.db() as db:
                row=db.execute("SELECT lifecycle FROM capability_evidence WHERE workspace=? AND capability=?",
                               (ws,body.capability)).fetchone()
            if row is None or row[0]!='approved':
                raise HTTPException(409,'Execution capability must first be independently approved')
        with collaboration.db() as db:
            db.execute('INSERT INTO workspace_execution_policy VALUES (?,?,?,?) ON CONFLICT(workspace) DO UPDATE SET enabled=excluded.enabled,capability=excluded.capability,resources=excluded.resources',
                       (ws,int(body.enabled),body.capability,json.dumps(amount,sort_keys=True)))
        collaboration.emit(ws,'execution_policy_updated',body.capability,{'actor':actor,'enabled':body.enabled})
        return {'enabled':body.enabled,'capability':body.capability,'resources':amount}

    @app.get('/api/v2/{ws}/routing-policy')
    def get_routing_policy(ws:str,req:Request):
        member(req,ws,'viewer')
        return _routing_policy(ws)

    @app.put('/api/v2/{ws}/routing-policy')
    def set_routing_policy(ws:str,body:RoutingPolicy,req:Request):
        actor=member(req,ws,'admin')['id']
        if body.backend not in BACKENDS:raise HTTPException(400,'Unsupported router backend')
        with collaboration.db() as db:
            db.execute('INSERT INTO workspace_routing_policy VALUES (?,?,?) '
                'ON CONFLICT(workspace) DO UPDATE SET enabled=excluded.enabled,backend=excluded.backend',
                (ws,int(body.enabled),body.backend))
        collaboration.emit(ws,'routing_policy_updated',body.backend,{'actor':actor,'enabled':body.enabled})
        return _routing_policy(ws)

    @app.get('/api/v2/{ws}/planning-policy')
    def get_planning_policy(ws:str,req:Request):
        member(req,ws,'viewer')
        return _planning_policy(ws)

    @app.put('/api/v2/{ws}/planning-policy')
    def set_planning_policy(ws:str,body:PlanningPolicy,req:Request):
        actor=member(req,ws,'admin')['id']
        if body.backend not in BACKENDS:raise HTTPException(400,'Unsupported planner backend')
        policy=_policy(ws)
        if body.enabled and (not policy or not policy['enabled']):
            raise HTTPException(409,'Configure approved execution policy before enabling auto planning')
        with collaboration.db() as db:
            db.execute('INSERT INTO workspace_planning_policy VALUES (?,?,?,?) ON CONFLICT(workspace) DO UPDATE SET enabled=excluded.enabled,backend=excluded.backend,auto_approve_trusted=excluded.auto_approve_trusted',
                (ws,int(body.enabled),body.backend,int(body.auto_approve_trusted)))
        collaboration.emit(ws,'planning_policy_updated',body.backend,{'actor':actor,'enabled':body.enabled})
        return _planning_policy(ws)

    @app.get('/api/v2/{ws}/tasks/{task_id}/planning')
    def task_planning_status(ws:str,task_id:str,req:Request):
        member(req,ws,'viewer')
        task=tasks.get(task_id)
        if not task or task.tenant!=ws:raise HTTPException(404,'Task not found')
        return web_planner.status(task_id,ws) or {'status':'not_planned'}

    @app.post('/api/v2/{ws}/tasks/{task_id}/replan')
    async def retry_failed_planning(ws:str,task_id:str,req:Request):
        member(req,ws,'admin')
        task=tasks.get(task_id)
        if not task or task.tenant!=ws:raise HTTPException(404,'Task not found')
        ppolicy=_planning_policy(ws);policy=_policy(ws)
        if not ppolicy['enabled'] or not policy or not policy['enabled']:
            raise HTTPException(409,'Planning and execution policies must be enabled')
        previous=web_planner.status(task_id,ws)
        if not previous or previous['status']!='failed':
            raise HTTPException(409,'Only cleanly failed plans may be retried')
        result=await web_planner.plan(task,ws,ppolicy['backend'],policy['capability'],retry=True,auto_approve_trusted=ppolicy['auto_approve_trusted'])
        if result['status']=='planned':
            for child in result['task_ids'].values():
                t=tasks.get(child)
                if t and t.status.value=='pending' and not t.depends_on:_auto_offer(ws,t)
        return result

    @app.post('/api/v2/{ws}/tasks/{task_id}/direct')
    def direct_failed_simple_goal(ws:str,task_id:str,req:Request):
        actor=member(req,ws,'admin')
        task=tasks.get(task_id)
        if task is None or task.tenant!=ws:raise HTTPException(404,'Task not found')
        if classify_prompt(task.prompt).kind!='simple':
            raise HTTPException(409,'Only bounded simple questions are eligible for planner bypass')
        prior=web_planner.status(task_id,ws)
        if prior and prior['status'] not in ('failed','direct'):
            raise HTTPException(409,'Planned or uncertain goal cannot bypass planner')
        if task.status.value!='pending' or task.attempts or task.depends_on:
            raise HTTPException(409,'Only untouched pending goals can use direct dispatch')
        policy=_policy(ws)
        if not policy or not policy['enabled']:
            raise HTTPException(409,'Configure an approved execution policy first')
        with runtime_relations.db() as db:
            row=db.execute('SELECT lifecycle FROM capability_evidence WHERE workspace=? AND capability=?',
                (ws,policy['capability'])).fetchone()
        if not row or row[0]!='approved':raise HTTPException(409,'Execution capability is not approved')
        try:task=tasks.bind_approved_execution(task.id,ws,policy['capability'])
        except ValueError as exc:raise HTTPException(409,str(exc))
        web_planner.mark_direct(task.id,ws)
        collaboration.emit(ws,'task_direct_activated',task.id,{'actor':actor['id'],'route':'simple'})
        return {'task_id':task.id,'route':'simple','planning':web_planner.status(task.id,ws),
                'dispatch':_auto_offer(ws,task)}

    @app.post('/api/v2/{ws}/tasks/{task_id}/activate')
    def activate_legacy_task(ws:str,task_id:str,req:Request):
        user=member(req,ws,'admin')
        if web_planner.status(task_id,ws) is not None:
            raise HTTPException(409,'A planned goal cannot be activated as a legacy single task')
        task=tasks.get(task_id)
        if task is None or task.tenant!=ws:raise HTTPException(404,'Task not found')
        policy=_policy(ws)
        if not policy or not policy['enabled']:
            raise HTTPException(409,'Configure approved workspace execution policy first')
        with runtime_relations.db() as db:
            row=db.execute("SELECT lifecycle FROM capability_evidence WHERE workspace=? AND capability=?",
                           (ws,policy['capability'])).fetchone()
        if row is None or row[0]!='approved':
            raise HTTPException(409,'Execution capability is not approved')
        try: task=tasks.bind_approved_execution(task_id,ws,policy['capability'])
        except ValueError as exc:raise HTTPException(409,str(exc))
        collaboration.emit(ws,'task_execution_activated',task_id,{'actor':user['id']})
        return {'task_id':task.id,'dispatch':_auto_offer(ws,task)}

    @app.post('/api/v2/{ws}/tasks/{task_id}/auto-dispatch')
    def retry_auto_dispatch(ws:str,task_id:str,req:Request):
        member(req,ws,'admin')
        task=tasks.get(task_id)
        if task is None or task.tenant!=ws:raise HTTPException(404,'Task not found')
        if task.status.value!='pending':raise HTTPException(409,'Task is not pending')
        return {'task_id':task.id,'dispatch':_auto_offer(ws,task)}

    @app.post('/api/v2/{ws}/tasks')
    async def submit(ws:str,body:TaskInput,req:Request):
        user=member(req,ws,'operator')
        collaboration._valid(body.request_id)
        if body.source_message_id:
            source=collaboration.source_message(user['id'],ws,body.source_message_id,require_author=True)
            if body.prompt != source['body']:
                raise HTTPException(400,'Task must match linked source message')
        # The task center enforces idempotency atomically and stores tenant.
        policy=_policy(ws)
        # Never silently infer an execution capability from arbitrary user text.
        # An explicit administrator-approved workspace default is required.
        planner_policy=_planning_policy(ws)
        planning_enabled=bool(planner_policy['enabled'] and policy and policy['enabled'])
        route = (await adaptive_router.classify(body.prompt, **_routing_policy(ws))
                 if planning_enabled else None)
        needs_planner=bool(planning_enabled and route.kind=='complex')
        # Keep unplanned goals non-dispatchable until a validated DAG exists.
        caps=[] if needs_planner else ([policy['capability']] if policy and policy['enabled'] else [])
        task=tasks.submit(body.prompt,tenant=ws,required_capabilities=caps,
                          idempotency_key='collab:'+ws+':'+body.request_id)
        try:collaboration.claim_task(user['id'],ws,task.id,body.source_message_id)
        except __import__('sqlite3').IntegrityError:pass
        if needs_planner:
            prior=web_planner.status(task.id,ws)
            planning=prior or await web_planner.plan(task,ws,planner_policy['backend'],policy['capability'],auto_approve_trusted=planner_policy['auto_approve_trusted'])
            offered=[]
            if planning['status']=='planned':
                for child in planning['task_ids'].values():
                    step=tasks.get(child)
                    if step and step.status.value=='pending' and not step.depends_on:
                        offered.append({'id':child,**_auto_offer(ws,step)})
            dispatch={'state':'planned' if planning['status']=='planned' else 'awaiting_planner',
                      'reason':planning.get('message',''), 'offers':offered}
            return {'id':task.id,'status':task.status.value,'planning':planning,'route':'complex','routing_source':route.source,'dispatch':dispatch}
        dispatch=_auto_offer(ws,task) if task.status.value=='pending' else {'state':'not_pending'}
        return {'id':task.id,'status':task.status.value,'route':route.kind if route else 'single', 'routing_source':route.source if route else 'disabled', 'dispatch':dispatch}

    @app.post('/api/v2/{ws}/tickets')
    def ticket(ws:str,req:Request):
        user=member(req,ws)
        return {'ticket':collaboration.ticket(user['id'],ws),'expires_in_seconds':30}

    @app.get('/api/v2/{ws}/events')
    def events(ws:str,req:Request,after:int=0):
        user=member(req,ws)
        return {'events':collaboration.events(user['id'],ws,after)}

    @app.websocket('/ws/{ws}')
    async def ws_events(socket:WebSocket,ws:str,ticket:str='',after:int=0):
        # Browser WebSocket has no Authorization header; use single-use short-lived ticket.
        try:
            uid=collaboration.consume_ticket(ticket,ws)
            collaboration.require(uid,ws)
        except (Forbidden,ValueError):
            await socket.close(code=1008);return
        await socket.accept()
        cursor=max(0,min(after,2**63-1))
        try:
            while True:
                # Re-check role on every delivery; revocations affect open connections.
                collaboration.require(uid,ws)
                updates=collaboration.events(uid,ws,cursor,50)
                for event in updates:
                    await socket.send_json(event)
                    cursor=event['id']
                await asyncio.sleep(.25)
        except (WebSocketDisconnect,Forbidden,RuntimeError):
            try:await socket.close()
            except RuntimeError:pass
        except asyncio.CancelledError:
            raise

    @app.post('/api/v2/{ws}/workers')
    def register(ws:str,body:NewWorker,req:Request):
        user=member(req,ws,'admin')
        secret=collaboration.worker_register(user['id'],ws,body.id)
        operations.register_worker(body.id,secret)
        return {'id':body.id,'secret':secret,'note':'Shown once. Store on worker securely.'}

    @app.post('/api/v2/{ws}/workers/{worker}/commands')
    def command(ws:str,worker:str,body:NewCommand,req:Request):
        user=member(req,ws,'admin')
        return {'id':collaboration.command(user['id'],ws,worker,body.action,body.payload),'state':'queued'}

    @app.get('/api/v2/worker/{worker}/poll')
    def poll(worker:str,req:Request):
        secret,ws=worker_creds(req,worker)
        operations.worker_heartbeat(worker,secret)
        return {'command':collaboration.poll_command(worker,secret)}

    @app.post('/api/v2/worker/{worker}/commands/{cid}/ack')
    def ack(worker:str,cid:str,body:Ack,req:Request):
        secret,ws=worker_creds(req,worker)
        collaboration.command_ack(worker,secret,cid,body.status,body.receipt)
        return {'ok':True}

    @app.post('/api/v2/worker/{worker}/logs')
    def logs(worker:str,body:Log,req:Request):
        secret,ws=worker_creds(req,worker)
        operations.worker_heartbeat(worker,secret)
        return {'event_id':collaboration.worker_log(worker,secret,body.level,body.message,body.task_id)}

    @app.post('/api/v2/{ws}/models/config')
    def model_config(ws:str,body:ModelConfig,req:Request):
        user=member(req,ws,'admin')
        collaboration.model_config(user['id'],ws,body.base_url,body.model)
        return {'configured':True}

    @app.post('/api/v2/{ws}/models/probe')
    async def model_probe(ws:str,req:Request):
        user=member(req,ws,'admin')
        info=collaboration.model_info(user['id'],ws)
        if not info['configured']:raise HTTPException(400,'Model not configured')
        from urllib.request import Request as UR, urlopen
        from urllib.error import HTTPError, URLError
        import ssl
        def query():
            key=os.getenv('BIOMNI_MODEL_API_KEY','')
            if not key:
                return {'connected':False,'models_found':0,'error':{'type':'missing_api_key','message':'BIOMNI_MODEL_API_KEY is not set on this server'}}
            base=info['base_url'].rstrip('/')
            target=base if base.endswith('/models') else base+'/models'
            try:
                with urlopen(UR(target,headers={'Authorization':'Bearer '+key,'Accept':'application/json'}),timeout=8) as response:
                    data=json.loads(response.read(100001))
                if not isinstance(data,dict) or not isinstance(data.get('data'),list):
                    return {'connected':False,'models_found':0,'error':{'type':'invalid_response','message':'Expected OpenAI-compatible data array'}}
                # Preserve v0.8.x successful response shape for existing clients.
                return {'connected':True,'models_found':len(data['data'])}
            except HTTPError as exc:
                return {'connected':False,'models_found':0,'error':{'type':'http_error','http_status':exc.code,'message':'Model provider rejected /models request'}}
            except (TimeoutError, ConnectionError) as exc:
                return {'connected':False,'models_found':0,'error':{'type':'network_error','message':'Model provider connection failed or timed out'}}
            except (URLError,ssl.SSLError,OSError):
                return {'connected':False,'models_found':0,'error':{'type':'network_error','message':'Could not connect to model provider'}}
            except (json.JSONDecodeError,UnicodeDecodeError,ValueError):
                return {'connected':False,'models_found':0,'error':{'type':'invalid_response','message':'Model provider returned non-JSON response'}}
        return await asyncio.to_thread(query)

    @app.get('/api/v2/{ws}/models/status')
    def model_status(ws:str,req:Request):
        """Credential presence only; never expose the secret or test connectivity."""
        user=member(req,ws)
        info=collaboration.model_info(user['id'],ws)
        configured=bool(info['configured'])
        key_present=bool(os.getenv('BIOMNI_MODEL_API_KEY','').strip())
        return {'configured':configured,'credentials_present':key_present,
                'chat_ready':configured and key_present,
                'message':('模型未配置，请管理员在模型连接中设置 URL 和模型'
                           if not configured else
                           '服务器缺少 BIOMNI_MODEL_API_KEY，请管理员配置环境变量并重启服务'
                           if not key_present else
                           '模型设置和密钥已存在；请使用模型发现测试连通性')}

    @app.get('/api/v2/{ws}/assistant/history')
    def assistant_history(ws:str,req:Request,kind:str='sessions',offset:int=0,limit:int=100):
        actor=member(req,ws)
        if kind=='sessions':
            return collaboration.conversation_history(actor['id'],ws,offset,limit)
        if kind!='tasks':raise HTTPException(400,'Unknown history kind')
        if offset<0 or limit<1:raise HTTPException(400,'Invalid pagination')
        take=min(limit,200)
        with collaboration.db() as db:
            rows=db.execute('''SELECT task_id,message_id FROM task_ownership WHERE workspace=?
                ORDER BY rowid DESC LIMIT ? OFFSET ?''',(ws,take+1,offset)).fetchall()
        items=[]
        for tid,mid in rows[:take]:
            task=tasks.get(tid)
            if task is None or task.tenant!=ws:continue
            items.append({'id':tid,'title':task.prompt[:160],'status':task.status.value,'message_id':mid})
        return {'items':items,'next_offset':offset+take if len(rows)>take else None}

    @app.get('/api/v2/{ws}/events/history')
    def events_history(ws:str,req:Request,before:int|None=None,limit:int=100):
        actor=member(req,ws)
        try:return collaboration.recent_events(actor['id'],ws,before,limit)
        except ValueError as exc:raise HTTPException(400,str(exc)) from exc

    @app.get('/api/v2/{ws}/assistant/session/{session}')
    def assistant_session(ws:str,session:str,req:Request):
        """Workspace-scoped conversation, with task projections (no duplicate assistant messages)."""
        user=member(req,ws)
        messages=collaboration.messages(user['id'],ws,session)
        ids=[m['id'] for m in messages]
        linked={}
        if ids:
            with collaboration.db() as db:
                rows=db.execute('SELECT message_id,task_id FROM task_ownership WHERE workspace=? AND message_id IS NOT NULL', (ws,)).fetchall()
            allowed=set(ids)
            for mid,tid in rows:
                if mid not in allowed:continue
                task=tasks.get(tid)
                if task is None or task.tenant!=ws:continue
                linked[mid]={'id':task.id,'status':task.status.value,
                             'result':present_result(task.result)['text'][:32000] if task.status.value=='completed' and isinstance(task.result,str) else None}
        return {'messages':messages,'tasks':linked}

    @app.post('/api/v2/{ws}/assistant/turn')
    async def assistant_turn(ws:str,body:UnifiedTurn,req:Request):
        """Single user entrypoint. Chat never receives execution authority.

        Complex/side-effecting work uses the established task endpoint, which
        enforces capability approval and dispatch policy. No silent tool calls.
        """
        actor=member(req,ws,'operator')
        collaboration._valid(body.session)
        collaboration._valid(body.request_id)
        if body.mode not in ('auto','chat','task'):
            raise HTTPException(400,'Unsupported conversation mode')
        kind=conversation_route(body.message,body.mode)
        if kind=='chat':
            info=collaboration.model_info(actor['id'],ws)
            if not info['configured']:
                raise HTTPException(409,'Model not configured; configure it under Model Connection')
            key=os.getenv('BIOMNI_MODEL_API_KEY','')
            if not key:raise HTTPException(503,'服务器缺少 BIOMNI_MODEL_API_KEY；请管理员在服务端配置密钥并重启服务。也可选择「执行任务」提交不依赖聊天模型的任务。')
            history=collaboration.messages(actor['id'],ws,body.session)
            adapter=OpenAICompatibleAdvisor(info['base_url'],key,info['model'])
            try:
                answer=await asyncio.to_thread(adapter,body.message,tuple(
                    {'role':m['role'],'text':m['body']} for m in history[-15:]))
            except Exception:
                raise HTTPException(502,'Model unavailable; try again or inspect server logs')
            mid=collaboration.write_message(actor['id'],ws,body.session,body.message)
            collaboration.write_message(actor['id'],ws,body.session,answer,role='assistant')
            return {'route':'chat','message_id':mid,'answer':answer}
        # Save provenance before task creation so task_ownership can link it.
        mid=collaboration.write_message(actor['id'],ws,body.session,body.message)
        task=await submit(ws,TaskInput(prompt=body.message,request_id=body.request_id,source_message_id=mid),req)
        return {'route':'task','message_id':mid,'task_id':task['id'],
                'task_route':task.get('route'), 'dispatch':task.get('dispatch'),
                'planning':task.get('planning')}

    @app.post('/api/v2/{ws}/models/chat')
    async def model_chat(ws:str,body:ModelPrompt,req:Request):
        user=member(req,ws,'operator')
        info=collaboration.model_info(user['id'],ws)
        if not info['configured']:raise HTTPException(400,'Model not configured')
        key=os.getenv('BIOMNI_MODEL_API_KEY','')
        if not key:raise HTTPException(503,'Model credentials missing')
        history=collaboration.messages(user['id'],ws,body.session)
        adapter=OpenAICompatibleAdvisor(info['base_url'],key,info['model'])
        try:
            answer=await asyncio.to_thread(adapter,body.message,tuple(
                {'role':m['role'],'text':m['body']} for m in history[-15:]))
        except Exception:raise HTTPException(502,'Model unavailable')
        # Model is not allowed to call tools, issue worker commands, or approve actions.
        collaboration.write_message(user['id'],ws,body.session,body.message)
        collaboration.write_message(user['id'],ws,body.session,answer,role='assistant')
        return {'answer':answer}

    return app
