"""Bounded, reviewable web goal planning. No code tools or implicit capability approval.

A goal stays unbound (and thus undispatchable) until its validated DAG is
committed. The ledger provides idempotent claims and explicit recovery after
uncertain crashes; it does not pretend to be a distributed transaction.
"""
from __future__ import annotations
import asyncio
import logging
import uuid
import json
import re
import sqlite3
import time
from .contracts import AgentRequest, PlanError
from .dag import DAGPatch, DAGVersionManager
from .planner import LLMPlanner

LOG=logging.getLogger(__name__)

BACKENDS = ('codex_sdk', 'claude_sdk', 'codex_cli', 'claude_cli')


def runtime_for_backend(backend: str):
    from .runtimes import CodexSDKRuntime, ClaudeSDKRuntime, CLIServiceRuntime
    if backend == 'codex_sdk': return CodexSDKRuntime(writable=False)
    if backend == 'claude_sdk': return ClaudeSDKRuntime(allow_code_tools=False)
    if backend == 'codex_cli': return CLIServiceRuntime('codex', enabled=True)
    if backend == 'claude_cli': return CLIServiceRuntime('claude', enabled=True)
    raise ValueError('Unknown planner backend')


def cli_planner_text(text: str, backend: str) -> str:
    """Unwrap Claude JSON result or Codex JSONL agent messages."""
    if backend not in ('claude_cli','codex_cli'):
        return text
    if backend == 'claude_cli':
        try:
            payload=json.loads(text)
            if isinstance(payload,dict) and isinstance(payload.get('result'),str):
                return payload['result']
        except ValueError:
            pass
    else:
        for line in reversed(text.splitlines()):
            try:obj=json.loads(line)
            except ValueError:continue
            if not isinstance(obj,dict):continue
            item=obj.get('item') or {}
            if obj.get('type')=='item.completed' and isinstance(item,dict) and item.get('type')=='agent_message':
                return str(item.get('text',''))
    return text


DANGEROUS_STEP=re.compile(r'(?i)\b(?:rm\s+-rf|delete|destroy|deploy|publish|purchase|transfer|exfiltrat|email|upload|production|credential|secret|permission|sudo)\b|删除|部署|付款|转账|发送邮件|生产环境|发布能力|修改权限')


class WebPlanningCoordinator:
    def __init__(self, tasks, runtime_factory=None):
        self.tasks = tasks
        self.dag = DAGVersionManager(tasks)
        self.runtime_factory = runtime_factory or runtime_for_backend
        with tasks._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS web_goal_plans (
                task_id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
                status TEXT NOT NULL, backend TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL)''')

    def status(self, task_id: str, workspace: str):
        with self.tasks._connect() as db:
            row = db.execute('SELECT status,backend,result_json FROM web_goal_plans WHERE task_id=? AND workspace=?',
                             (task_id,workspace)).fetchone()
        if not row: return None
        data=json.loads(row[2]);return {'status':row[0],'backend':row[1],**data}

    def _claim(self, task_id, workspace, backend, retry=False):
        with self.tasks._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT status FROM web_goal_plans WHERE task_id=?',(task_id,)).fetchone()
            if old and (not retry or old[0]!='failed'):
                return False
            if old:
                db.execute('UPDATE web_goal_plans SET status=?,backend=?,result_json=?,updated=? WHERE task_id=? AND workspace=?',
                           ('planning',backend,'{}',time.time(),task_id,workspace))
            else:
                db.execute('INSERT INTO web_goal_plans VALUES (?,?,?,?,?,?)',
                           (task_id,workspace,'planning',backend,'{}',time.time()))
        return True

    def mark_direct(self,task_id,workspace,backend='direct_approved_worker'):
        self._finish(task_id,workspace,'direct',{'count':1,'message':'Approved direct execution; planner bypassed'})

    def _finish(self,task_id,workspace,status,data):
        with self.tasks._connect() as db:
            db.execute('UPDATE web_goal_plans SET status=?,result_json=?,updated=? WHERE task_id=? AND workspace=?',
                       (status,json.dumps(data,sort_keys=True),time.time(),task_id,workspace))

    async def plan(self, task, workspace, backend, capability, *, retry=False, max_steps=8, timeout=45, auto_approve_trusted=True):
        if task.tenant != workspace or task.status.value != 'pending' or task.attempts:
            raise ValueError('Only untouched pending goals in this workspace may be planned')
        if backend not in BACKENDS:raise ValueError('Unsupported planner backend')
        if not self._claim(task.id,workspace,backend,retry=retry):
            return self.status(task.id,workspace)
        try:
            runtime=self.runtime_factory(backend)
            request=AgentRequest(task_id='web-plan-'+task.id,role='planner',workspace=None,
                prompt=('You are a strictly READ-ONLY scientific task planning agent. '
                        'Do not use tools, shell, code execution, or external services. '
                        'Return ONLY a JSON object with keys additions,dependencies,reason. '
                        'Generate 1 to '+str(max_steps)+' verifiable steps. Each addition has '
                        'key,prompt,depends_on,required_capabilities,priority. '
                        'depends_on may reference ONLY keys of preceding additions. '
                        'dependencies must be []. Do not set tenant or use arbitrary capabilities. '
                        'The only approved execution capability is '+json.dumps(capability)+'. '
                        'Use this for all steps; high risk changes must be described for later human review. '
                        'Goal: '+task.prompt[:4000]), timeout=timeout)
            response=await asyncio.wait_for(runtime.run(request),timeout=timeout+2)
            if not response.success:raise PlanError('Planner returned unsuccessful result')
            proposed=LLMPlanner.parse(cli_planner_text(response.text,backend))
            if proposed.dependencies or proposed.recovery or not 1<=len(proposed.additions)<=max_steps:
                raise PlanError('Planner must return 1..'+str(max_steps)+' new steps only')
            entries=[]; keys=set()
            for step in proposed.additions:
                key=step['key']
                if step.get('tenant') not in (None,workspace):raise PlanError('Cross-workspace step rejected')
                deps=step.get('depends_on',[])
                if not all(dep in keys for dep in deps):raise PlanError('Unknown or forward dependency rejected')
                declared=step.get('required_capabilities',[])
                if declared not in ([],[capability]):raise PlanError('Unknown capability requires independent approval')
                entries.append({'key':key,'prompt':step['prompt'][:4000],
                                'depends_on':deps,'required_capabilities':[capability],
                                'priority':max(-10,min(10,step.get('priority',0))),
                                'tenant':workspace,
                                'requires_approval':bool(step.get('requires_approval',False)) or not auto_approve_trusted or bool(DANGEROUS_STEP.search(step['prompt']))})
                keys.add(key)
            # Every child must finish before the original goal can execute its
            # final synthesis step. No cross-workspace/existing-ID references.
            edges=tuple((task.id,entry['key']) for entry in entries)
            mapping=self.dag.commit(DAGPatch(tuple(entries),edges,
                                                        'web_auto_plan:'+task.id))
            self.tasks.bind_approved_execution(task.id,workspace,capability)
            outcome={'task_ids':mapping,'count':len(mapping),'reason':proposed.reason}
            self._finish(task.id,workspace,'planned',outcome)
            return {'status':'planned','backend':backend,**outcome}
        except Exception as exc:
            # Do not leak upstream response/keys into user-visible errors.
            # A partially committed DAG is never replayed automatically.
            with self.tasks._connect() as db:
                committed=db.execute('SELECT 1 FROM dag_versions WHERE reason=? LIMIT 1',
                                     ('web_auto_plan:'+task.id,)).fetchone()
            status='reconcile_required' if committed else 'failed'
            reference=uuid.uuid4().hex[:12]
            LOG.warning('Planner failed reference=%s backend=%s type=%s', reference, backend, type(exc).__name__)
            name=type(exc).__name__
            category=('timeout' if isinstance(exc,(asyncio.TimeoutError,TimeoutError)) else
                      'invalid_plan' if isinstance(exc,PlanError) else
                      'backend_unavailable' if isinstance(exc,(ImportError,ModuleNotFoundError,FileNotFoundError)) else
                      'backend_error')
            data={'reason_code':name,'error_type':category,'reference':reference,
                  'message':('Planner failed ('+category+'); diagnostic reference '+reference if status=='failed' else
                             'DAG may be committed; manual reconciliation required, reference '+reference)}
            self._finish(task.id,workspace,status,data)
            return {'status':status,'backend':backend,**data}
