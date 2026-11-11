"""Authenticated, local-only Biomni Control Center.

A separate operator token gates all private data and control actions. The
conversation inbox never executes prompts. Explicit 'create task' is a durable,
idempotent TaskCenter submission; an optional advisor may *suggest* a reply.
No LLM is supplied by default and no autonomous execution is implied.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from .observability import ObservabilityServer, OperationsStore
from .scheduler import TaskCenter
from .persistent_runtime import PersistentRuntimeStore, GoalState, EventKind
from .registry import CapabilityRegistry
from .capabilities import CapabilityCenter

CONTROL_HTML = (Path(__file__).parent / 'web' / 'control.html').read_text('utf-8')


class ConversationStore:
    """Messages and task links are persistent, private to the operator API."""
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS cc_messages (
                  id TEXT PRIMARY KEY, session TEXT NOT NULL, role TEXT NOT NULL,
                  text TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS cc_messages_session
                  ON cc_messages(session,created,id);
                CREATE TABLE IF NOT EXISTS cc_task_links (
                  message_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                  created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cc_actions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                  subject TEXT NOT NULL, created REAL NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute('PRAGMA busy_timeout=15000')
        return db

    @staticmethod
    def _session(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 80 or not all(c.isalnum() or c in '-_' for c in value):
            raise ValueError('Invalid session identifier')
        return value

    def add(self, session: str, role: str, text: str) -> dict:
        self._session(session)
        if role not in ('user', 'assistant') or not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
            raise ValueError('Invalid message')
        item = {'id': uuid4().hex, 'session': session, 'role': role,
                'text': text.strip(), 'created': time.time()}
        with self.connect() as db:
            db.execute('INSERT INTO cc_messages VALUES (?,?,?,?,?)',
                       (item['id'], item['session'], item['role'], item['text'], item['created']))
        return item

    def list(self, session: str, limit: int = 100) -> list[dict]:
        self._session(session)
        with self.connect() as db:
            rows = db.execute('''SELECT id,session,role,text,created FROM
                (SELECT * FROM cc_messages WHERE session=? ORDER BY created DESC,id DESC LIMIT ?)
                ORDER BY created,id''', (session, min(200,max(1,limit)))).fetchall()
        return [dict(zip(('id','session','role','text','created'),row)) for row in rows]

    def user_message(self, session: str, message_id: str) -> str:
        self._session(session)
        with self.connect() as db:
            row=db.execute('SELECT text FROM cc_messages WHERE session=? AND id=? AND role=?',
                           (session,message_id,'user')).fetchone()
        if not row: raise KeyError('User message not found')
        return row[0]

    def link_task(self, message_id: str, task_id: str):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO cc_task_links VALUES (?,?,?)',
                       (message_id,task_id,time.time()))

    def task_for(self, message_id: str) -> str | None:
        with self.connect() as db:
            row=db.execute('SELECT task_id FROM cc_task_links WHERE message_id=?',(message_id,)).fetchone()
        return row[0] if row else None

    def audit(self, name: str, subject: str):
        with self.connect() as db:
            db.execute('INSERT INTO cc_actions(name,subject,created) VALUES (?,?,?)',
                       (name,subject,time.time()))


def _read_rows(path: str | None, sql: str, limit=100):
    """Optional, bounded read-only connection to an existing internal DB."""
    if not path or not Path(path).is_file(): return []
    try:
        uri=Path(path).resolve().as_uri()+'?mode=ro'
        with sqlite3.connect(uri,uri=True,timeout=2) as db:
            db.execute('PRAGMA query_only=ON')
            return db.execute(sql,(limit,)).fetchall()
    except sqlite3.Error:
        return []


class ControlCenter:
    def __init__(self, conversations: ConversationStore, tasks: TaskCenter,
                 operations: OperationsStore, *, token: str,
                 registry: CapabilityRegistry | None = None,
                 capabilities: CapabilityCenter | None = None,
                 runtime: PersistentRuntimeStore | None = None,
                 remediation_db: str | None = None,
                 experiments_db: str | None = None,
                 advisor=None):
        if not isinstance(token,str) or len(token)<32:
            raise ValueError('Operator token must contain at least 32 characters')
        self._token_hash=hashlib.sha256(token.encode()).digest()
        self.conversations=conversations
        self.tasks=tasks
        self.operations=operations
        self.registry=registry
        self.capabilities=capabilities
        self.runtime=runtime
        self.remediation_db=remediation_db
        self.experiments_db=experiments_db
        self.advisor=advisor

    def authenticate(self, authorization: str) -> bool:
        if not authorization.startswith('Bearer '):return False
        supplied=authorization[7:]
        if len(supplied)>4096:return False
        return hmac.compare_digest(self._token_hash,hashlib.sha256(supplied.encode()).digest())

    def overview(self) -> dict:
        tasks=self.tasks.list()
        goal_rows = []
        if self.runtime:
            for row in _read_rows(self.runtime.db_path,
                    'SELECT goal_id,state,updated,last_reason FROM runtime_goals ORDER BY updated DESC LIMIT ?',100):
                goal_rows.append({'id':row[0], 'state':row[1], 'updated':row[2], 'reason':row[3]})
        caps=[]
        if self.registry:
            caps=[{'id':c.id,'kind':c.kind.value,'version':c.version,
                   'healthy':c.healthy,'description':c.description[:240]} for c in self.registry.list()]
        proposals=[]
        if self.capabilities:
            for row in _read_rows(self.capabilities.db_path,
                    'SELECT capability_id,status,reason FROM gap_proposals ORDER BY requested_at DESC LIMIT ?',100):
                proposals.append({'id':row[0],'status':row[1], 'reason':row[2][:280]})
        repairs=[]
        for row in _read_rows(self.remediation_db,
                'SELECT id,task_id,route,state,reason FROM remediation_jobs ORDER BY updated DESC LIMIT ?',100):
            repairs.append({'id':row[0],'task_id':row[1],'route':row[2],
                            'state':row[3],'reason':row[4][:280]})
        experiments=[]
        for row in _read_rows(self.experiments_db,
                'SELECT id,state,created FROM active_experiments ORDER BY created DESC LIMIT ?',100):
            experiments.append({'id':row[0],'state':row[1],'created':row[2]})
        return {'operations':self.operations.snapshot(), 'goals':goal_rows,
                'tasks':[{'id':t.id,'prompt':t.prompt[:1500], 'status':t.status.value,
                          'depends_on':t.depends_on,'required_capabilities':t.required_capabilities,
                          'priority':t.priority,'attempts':t.attempts,
                          'approved':t.approved,'error':t.error[:300] if t.error else None}
                         for t in sorted(tasks,key=lambda x:(-x.priority,x.created_at))[:300]],
                'capabilities':caps,'proposals':proposals,'repairs':repairs,
                'experiments':experiments,
                'approvals':([{'kind':'task','id':t.id,'label':t.prompt[:140]}
                              for t in tasks if t.status.value=='waiting_approval'] +
                             [{'kind':'capability','id':p['id'],'label':p['reason']}
                              for p in proposals if p['status']=='proposed'])[:200]}

    def chat(self, session: str, message: str) -> dict:
        sent=self.conversations.add(session,'user',message)
        if self.advisor is None:
            reply='消息已保存到工作台。若要启动任务，请点击「转为任务」；普通对话不会自动触发 Agent 执行。尚未配置 LLM 对话适配器。'
        else:
            # Advisor is for suggestions only. It has no execution or DB capability.
            try:
                suggested=self.advisor(message, tuple(self.conversations.list(session,30)))
                reply=suggested if isinstance(suggested,str) and suggested.strip() else '对话适配器未返回文本。'
                reply=reply[:4000]
            except Exception:
                reply='对话适配器暂不可用。消息已保存，可手动转为任务。'
        received=self.conversations.add(session,'assistant',reply)
        return {'user':sent,'assistant':received,'can_submit_task':True}

    def submit_from_message(self, session: str, message_id: str) -> dict:
        text=self.conversations.user_message(session,message_id)
        if len(text)>4000:raise ValueError('Task input exceeds limit')
        # Existing TaskCenter applies atomic idempotency by message ID.
        task=self.tasks.submit(text,idempotency_key='cc-chat:'+message_id)
        self.conversations.link_task(message_id,task.id)
        self.conversations.audit('task_submitted',task.id)
        return {'id':task.id,'status':task.status.value,
                'note':'任务已加入 TaskCenter；需要配置实际 Runtime/Worker 才会自动执行。'}

    def approve(self, kind: str, identifier: str, approve: bool) -> dict:
        if kind=='task':
            if not approve: raise ValueError('Task rejection requires separate supervisor workflow')
            self.tasks.approve(identifier)
            self.conversations.audit('approve_task',identifier)
            return {'kind':kind,'id':identifier,'status':'pending'}
        if kind=='capability':
            if self.capabilities is None:raise ValueError('Capability center not configured')
            result=self.capabilities.review(identifier,approve=approve)
            self.conversations.audit('approve_capability' if approve else 'reject_capability',identifier)
            return {'kind':kind,'id':identifier,'status':result.status}
        raise ValueError('Unsupported approval type')

    def goal_state(self, goal_id: str, state: str) -> dict:
        if self.runtime is None:raise ValueError('Runtime not configured')
        if state not in ('paused','active'):
            raise ValueError('Only pause/resume permitted from the console')
        prior=self.runtime.state(goal_id)
        if state=='active' and prior not in (GoalState.PAUSED,GoalState.WAITING):
            raise ValueError('Only a paused or waiting goal can resume')
        if state=='paused' and prior!=GoalState.ACTIVE:
            raise ValueError('Only active goals may be paused')
        self.runtime.set_state(goal_id,GoalState(state),'operator_control_center',operator=True)
        self.conversations.audit('goal_'+state,goal_id)
        return {'id':goal_id,'state':state}


class ControlCenterServer(ObservabilityServer):
    """Same loopback HTTP server; private /api/control/* routes require bearer token.

    No cookie-based auth, no cross-origin access, no browser-held execution key.
    Real remote access needs TLS + authenticated reverse proxy.
    """
    def __init__(self, control: ControlCenter, host='127.0.0.1', port=8765):
        self.control=control
        super().__init__(control.operations,host=host,port=port)

    def _handler(self):
        base=super()._handler()
        control=self.control
        class Handler(base):
            def _auth(self):
                return control.authenticate(self.headers.get('Authorization',''))

            def _json(self, status, payload):
                self._reply(status,json.dumps(payload,ensure_ascii=False,sort_keys=True))

            def _body(self):
                if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                    raise ValueError('JSON content type required')
                try:n=int(self.headers.get('Content-Length','0'))
                except ValueError:raise ValueError('Invalid size')
                if n<2 or n>8192: raise ValueError('Invalid request size')
                value=json.loads(self.rfile.read(n))
                if not isinstance(value,dict):raise ValueError('Expected object')
                return value

            def do_GET(self):
                path=urlsplit(self.path).path
                if path in ('/control','/'):
                    return self._reply(200,CONTROL_HTML,'text/html; charset=utf-8')
                if not path.startswith('/api/control/'):
                    return super().do_GET()
                if not self._auth(): return self._json(401,{'error':'unauthorized'})
                try:
                    if path=='/api/control/overview':return self._json(200,control.overview())
                    if path=='/api/control/messages':
                        from urllib.parse import parse_qs
                        session=parse_qs(urlsplit(self.path).query).get('session',['main'])[0]
                        return self._json(200,{'messages':control.conversations.list(session)})
                    return self._json(404,{'error':'not_found'})
                except ValueError:return self._json(400,{'error':'invalid_request'})

            def do_POST(self):
                path=urlsplit(self.path).path
                if not path.startswith('/api/control/'):
                    return super().do_POST()
                if not self._auth(): return self._json(401,{'error':'unauthorized'})
                # Reject cross-site browser form submissions, even with stolen URL.
                origin=self.headers.get('Origin')
                if origin:
                    from urllib.parse import urlsplit as split
                    parsed=split(origin)
                    hostname=self.headers.get('Host','').split(':')[0]
                    if parsed.scheme!='http' or parsed.hostname!=hostname or parsed.port!=self.server.server_port:
                        return self._json(403,{'error':'invalid_origin'})
                try:
                    data=self._body()
                    if path=='/api/control/chat' and set(data)=={'session','message'}:
                        return self._json(200,control.chat(data['session'],data['message']))
                    if path=='/api/control/submit-task' and set(data)=={'session','message_id'}:
                        return self._json(201,control.submit_from_message(data['session'],data['message_id']))
                    if path=='/api/control/approve' and set(data)=={'kind','id','approve'}:
                        if type(data['approve']) is not bool:raise ValueError('Approval must be boolean')
                        return self._json(200,control.approve(data['kind'],data['id'],data['approve']))
                    if path=='/api/control/goal-state' and set(data)=={'id','state'}:
                        return self._json(200,control.goal_state(data['id'],data['state']))
                    return self._json(404,{'error':'not_found'})
                except (ValueError,TypeError,OverflowError,KeyError):
                    return self._json(400,{'error':'invalid_request_or_transition'})
        return Handler
