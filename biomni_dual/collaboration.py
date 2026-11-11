"""Tenant-aware collaboration control plane persistence and authorization.

SQLite reference implementation. For multiple replicas, replace this store with a
transactional PostgreSQL implementation; no claim of cross-node HA is made.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

ROLES = {'owner':4, 'admin':3, 'operator':2, 'viewer':1}
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')
EMAIL = re.compile(r'^[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')

class Forbidden(PermissionError): pass
class InvalidCredentials(Forbidden): pass

class CollaborationStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        with self.db() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,disabled INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY,title TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS memberships(user_id TEXT NOT NULL,workspace TEXT NOT NULL,role TEXT NOT NULL,
                  PRIMARY KEY(user_id,workspace));
              CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS login_limits(email TEXT PRIMARY KEY,attempts INTEGER NOT NULL,locked_until REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS ws_tickets(hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,workspace TEXT NOT NULL,expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS conversation_messages(id TEXT PRIMARY KEY,workspace TEXT NOT NULL,session TEXT NOT NULL,
                  author TEXT NOT NULL,role TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS task_ownership(task_id TEXT PRIMARY KEY,workspace TEXT NOT NULL,author TEXT NOT NULL,message_id TEXT);
              CREATE TABLE IF NOT EXISTS worker_credentials(id TEXT PRIMARY KEY,workspace TEXT NOT NULL,secret_hash TEXT NOT NULL,disabled INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS worker_commands(id TEXT PRIMARY KEY,workspace TEXT NOT NULL,worker TEXT NOT NULL,
                  action TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,
                  claimed REAL,finished REAL,receipt TEXT);
              CREATE TABLE IF NOT EXISTS realtime_events(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace TEXT NOT NULL,
                  kind TEXT NOT NULL,subject TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS events_workspace ON realtime_events(workspace,id);
              CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,
                  workspace TEXT NOT NULL,action TEXT NOT NULL,subject TEXT NOT NULL,created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS model_connections(workspace TEXT PRIMARY KEY,base_url TEXT NOT NULL,model TEXT NOT NULL,
                  configured REAL NOT NULL);
            ''')
    def db(self):
        c=sqlite3.connect(self.path,timeout=15)
        c.execute('PRAGMA busy_timeout=15000')
        c.execute('PRAGMA foreign_keys=ON')
        return c

    @staticmethod
    def _valid(value):
        if not isinstance(value,str) or not NAME.fullmatch(value): raise ValueError('Invalid identifier')
        return value

    @staticmethod
    def _email(value):
        if not isinstance(value,str) or len(value)>80 or not EMAIL.fullmatch(value):raise ValueError('Invalid email')
        return value

    @staticmethod
    def _password(password):
        if not isinstance(password,str) or not 12<=len(password)<=256: raise ValueError('Password must be 12-256 characters')
        salt=secrets.token_bytes(16)
        digest=hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1)
        return salt.hex()+':'+digest.hex()

    @staticmethod
    def _verify(password,encoded):
        try:
            salt,digest=encoded.split(':')
            actual=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=2**14,r=8,p=1)
            return hmac.compare_digest(actual,bytes.fromhex(digest))
        except (ValueError,TypeError):return False

    def bootstrap(self,email,password,workspace='main'):
        """Local administrative CLI only. Not exposed via HTTP."""
        self._email(email);self._valid(workspace)
        coded=self._password(password)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM users LIMIT 1').fetchone():raise Forbidden('Already bootstrapped')
            uid=uuid4().hex
            db.execute('INSERT INTO users(id,email,password) VALUES (?,?,?)',(uid,email,coded))
            db.execute('INSERT INTO workspaces VALUES (?,?)',(workspace,workspace))
            db.execute('INSERT INTO memberships VALUES (?,?,?)',(uid,workspace,'owner'))
            return uid

    def login(self,email,password):
        now=time.time()
        if not isinstance(email,str) or len(email)>80 or not isinstance(password,str) or len(password)>256:
            raise InvalidCredentials('Invalid credentials')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            lim=db.execute('SELECT attempts,locked_until FROM login_limits WHERE email=?',(email,)).fetchone()
            if lim and lim[1]>now:raise InvalidCredentials('Invalid credentials')
            user=db.execute('SELECT id,password,disabled FROM users WHERE email=?',(email,)).fetchone()
            if user and not user[2] and self._verify(password,user[1]):
                db.execute('DELETE FROM login_limits WHERE email=?',(email,))
                token=secrets.token_urlsafe(36)
                db.execute('INSERT INTO sessions VALUES (?,?,?,0)',(self._digest(token),user[0],now+8*3600))
                return {'token':token,'expires':now+8*3600,'user_id':user[0]}
            failures=(lim[0] if lim else 0)+1
            db.execute('INSERT INTO login_limits VALUES (?,?,?) ON CONFLICT(email) DO UPDATE SET attempts=excluded.attempts,locked_until=excluded.locked_until',
                       (email,failures,now+900 if failures>=5 else 0))
        raise InvalidCredentials('Invalid credentials')

    @staticmethod
    def _digest(value):return hashlib.sha256(value.encode()).hexdigest()

    def identity(self,token):
        if not isinstance(token,str) or len(token)>512 or len(token)<32:raise InvalidCredentials('Unauthorized')
        with self.db() as db:
            row=db.execute('''SELECT u.id,u.email FROM sessions s JOIN users u ON u.id=s.user_id
              WHERE s.hash=? AND s.expires>? AND s.revoked=0 AND u.disabled=0''',(self._digest(token),time.time())).fetchone()
        if not row:raise InvalidCredentials('Unauthorized')
        return {'id':row[0],'email':row[1]}

    def logout(self,token):
        with self.db() as db: db.execute('UPDATE sessions SET revoked=1 WHERE hash=?',(self._digest(token),))

    def require(self,uid,workspace,role='viewer'):
        self._valid(workspace)
        with self.db() as db:
            row=db.execute('SELECT role FROM memberships WHERE user_id=? AND workspace=?',(uid,workspace)).fetchone()
        if not row or ROLES[row[0]]<ROLES[role]:raise Forbidden('Permission denied')
        return row[0]

    def members(self,uid,workspace):
        self.require(uid,workspace,'admin')
        with self.db() as db:
            rows=db.execute('''SELECT u.id,u.email,m.role,u.disabled FROM memberships m JOIN users u
             ON u.id=m.user_id WHERE m.workspace=? ORDER BY u.email''',(workspace,)).fetchall()
        return [dict(zip(('id','email','role','disabled'),r)) for r in rows]

    def invite(self,actor,workspace,email,password,role):
        self.require(actor,workspace,'admin')
        self._email(email)
        if role not in ('viewer','operator','admin'):raise ValueError('Invalid role')
        coded=self._password(password)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
            if r: uid=r[0]
            else:
                uid=uuid4().hex
                db.execute('INSERT INTO users(id,email,password) VALUES (?,?,?)',(uid,email,coded))
            db.execute('INSERT INTO memberships VALUES (?,?,?)',(uid,workspace,role))
            self._audit(db,actor,workspace,'member_invited',uid)
        return uid

    def change_membership(self,actor,workspace,target,role=None):
        """Revoke or change memberships. Owner cannot be demoted by this API."""
        self.require(actor,workspace,'admin')
        if role is not None and role not in ('viewer','operator','admin'):
            raise ValueError('Invalid role')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT role FROM memberships WHERE user_id=? AND workspace=?',
                                (target,workspace)).fetchone()
            if not existing:raise ValueError('Membership not found')
            if existing[0]=='owner':raise Forbidden('Workspace owner cannot be removed or demoted')
            actor_role=db.execute('SELECT role FROM memberships WHERE user_id=? AND workspace=?',
                                  (actor,workspace)).fetchone()
            if existing[0]=='admin' and actor_role[0]!='owner':
                raise Forbidden('Only owner can change administrators')
            if role is None:
                db.execute('DELETE FROM memberships WHERE user_id=? AND workspace=?',(target,workspace))
            else:
                db.execute('UPDATE memberships SET role=? WHERE user_id=? AND workspace=?',(role,target,workspace))
            self._audit(db,actor,workspace,'membership_revoked' if role is None else 'membership_changed',target)
        self.emit(workspace,'membership_changed',target,{'revoked':role is None},actor=actor)

    def audit_records(self,uid,workspace,limit=100):
        self.require(uid,workspace,'admin')
        with self.db() as db:
            rows=db.execute('SELECT id,actor,action,subject,created FROM audit_events WHERE workspace=? ORDER BY id DESC LIMIT ?',
                            (workspace,min(200,max(1,int(limit))))).fetchall()
        return [dict(zip(('id','actor','action','subject','created'),r)) for r in rows]

    def commands(self,uid,workspace):
        self.require(uid,workspace,'operator')
        with self.db() as db:
            rows=db.execute('SELECT id,worker,action,state,created,claimed,finished FROM worker_commands WHERE workspace=? ORDER BY created DESC LIMIT 100',(workspace,)).fetchall()
        return [dict(zip(('id','worker','action','state','created','claimed','finished'),r)) for r in rows]

    def workspace_create(self,actor,workspace,title):
        self._valid(workspace)
        if not isinstance(title,str) or not 1<=len(title)<=100:raise ValueError('Invalid workspace title')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM memberships WHERE user_id=? AND role IN ("admin","owner")',(actor,)).fetchone():
                raise Forbidden('Only existing admins may create workspaces')
            db.execute('INSERT INTO workspaces VALUES (?,?)',(workspace,title))
            db.execute('INSERT INTO memberships VALUES (?,?,?)',(actor,workspace,'owner'))
            self._audit(db,actor,workspace,'workspace_created',workspace)

    def my_workspaces(self,uid):
        with self.db() as db:
            return [{'id':r[0],'title':r[1],'role':r[2]} for r in db.execute('''SELECT w.id,w.title,m.role FROM workspaces w
              JOIN memberships m ON m.workspace=w.id WHERE m.user_id=? ORDER BY w.id''',(uid,))]

    @staticmethod
    def _audit(db,actor,workspace,action,subject):
        db.execute('INSERT INTO audit_events(actor,workspace,action,subject,created) VALUES (?,?,?,?,?)',
                   (actor,workspace,action,subject,time.time()))

    def emit(self,workspace,kind,subject,payload=None,actor='system'):
        self._valid(workspace)
        data=json.dumps(payload or {},ensure_ascii=False)
        if len(data)>10000:raise ValueError('Event too large')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor=db.execute('INSERT INTO realtime_events(workspace,kind,subject,payload,created) VALUES (?,?,?,?,?)',
                     (workspace,kind,subject,data,time.time()))
            self._audit(db,actor,workspace,kind,subject)
            return cursor.lastrowid

    def events(self,uid,workspace,after=0,limit=100):
        self.require(uid,workspace)
        if not 0<=after<=2**63-1:raise ValueError('Invalid cursor')
        with self.db() as db:
            rows=db.execute('''SELECT id,kind,subject,payload,created FROM realtime_events WHERE workspace=?
              AND id>? ORDER BY id LIMIT ?''',(workspace,after,min(limit,100))).fetchall()
        return [{'id':r[0],'kind':r[1],'subject':r[2],'payload':json.loads(r[3]),'created':r[4]} for r in rows]

    def ticket(self,uid,workspace):
        self.require(uid,workspace)
        token=secrets.token_urlsafe(36)
        with self.db() as db:
            db.execute('INSERT INTO ws_tickets VALUES (?,?,?,?)',(self._digest(token),uid,workspace,time.time()+30))
        return token

    def consume_ticket(self,token,workspace):
        self._valid(workspace)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT user_id FROM ws_tickets WHERE hash=? AND workspace=? AND expires>?',
                           (self._digest(token),workspace,time.time())).fetchone()
            db.execute('DELETE FROM ws_tickets WHERE hash=?',(self._digest(token),))
        if not row:raise Forbidden('Invalid WebSocket ticket')
        return row[0]

    def write_message(self,uid,workspace,session,body,role='user'):
        self.require(uid,workspace,'operator')
        self._valid(session)
        if not isinstance(body,str) or not 1<=len(body.strip())<=4000:raise ValueError('Invalid message')
        mid=uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?)',
                       (mid,workspace,session,uid,role,body.strip(),time.time()))
        self.emit(workspace,'chat_message',mid,{'session':session,'author':uid,'role':role},actor=uid)
        return mid

    def messages(self,uid,workspace,session):
        self.require(uid,workspace)
        self._valid(session)
        with self.db() as db:
            rows=db.execute('SELECT id,author,role,body,created FROM conversation_messages WHERE workspace=? AND session=? ORDER BY created,id LIMIT 200',(workspace,session)).fetchall()
        return [dict(zip(('id','author','role','body','created'),r)) for r in rows]

    def conversation_history(self,uid,workspace,offset=0,limit=100):
        # Durable session pagination; data is never deleted by the UI.
        self.require(uid,workspace)
        offset=max(0,int(offset));limit=max(1,min(200,int(limit)))
        with self.db() as db:
            rows=db.execute('''SELECT session, COUNT(*), MIN(created), MAX(created),
                (SELECT body FROM conversation_messages newest WHERE newest.workspace=m.workspace
                 AND newest.session=m.session ORDER BY newest.created DESC,newest.rowid DESC LIMIT 1)
                FROM conversation_messages m WHERE workspace=?
                GROUP BY session ORDER BY MAX(created) DESC,session DESC LIMIT ? OFFSET ?''',
                (workspace,limit+1,offset)).fetchall()
        return {'items':[{'id':r[0],'count':r[1],'created':r[2],'updated':r[3],
                           'preview':r[4][:180]} for r in rows[:limit]],
                'next_offset':offset+limit if len(rows)>limit else None}

    def recent_events(self,uid,workspace,before=None,limit=100):
        # Backward cursor pagination of workspace events.
        self.require(uid,workspace)
        limit=max(1,min(200,int(limit)))
        with self.db() as db:
            if before is None:
                rows=db.execute('''SELECT id,kind,subject,payload,created FROM realtime_events
                    WHERE workspace=? ORDER BY id DESC LIMIT ?''',(workspace,limit+1)).fetchall()
            else:
                if not 0<int(before)<=2**63-1:raise ValueError('Invalid cursor')
                rows=db.execute('''SELECT id,kind,subject,payload,created FROM realtime_events
                    WHERE workspace=? AND id<? ORDER BY id DESC LIMIT ?''',
                    (workspace,int(before),limit+1)).fetchall()
        return {'events':[{'id':r[0],'kind':r[1],'subject':r[2],
                           'payload':json.loads(r[3]),'created':r[4]} for r in rows[:limit]],
                'next_before':rows[limit-1][0] if len(rows)>limit else None}

    def source_message(self,uid,workspace,message_id,require_author=False):
        """Validate a workspace-local chat message before linking it to a task."""
        self.require(uid,workspace,'operator' if require_author else 'viewer')
        self._valid(message_id)
        with self.db() as db:
            row=db.execute('SELECT id,session,author,body FROM conversation_messages WHERE workspace=? AND id=?',
                           (workspace,message_id)).fetchone()
        if not row or (require_author and row[2] != uid):
            raise Forbidden('Chat message not accessible for task conversion')
        return {'id':row[0],'session':row[1],'author':row[2],'body':row[3]}

    def task_source(self,uid,workspace,task_id):
        self.require(uid,workspace)
        with self.db() as db:
            row=db.execute('SELECT message_id FROM task_ownership WHERE workspace=? AND task_id=?',
                           (workspace,task_id)).fetchone()
        if not row or not row[0]:return None
        return self.source_message(uid,workspace,row[0])

    def claim_task(self,uid,workspace,task_id,message_id=None):
        self.require(uid,workspace,'operator')
        with self.db() as db:
            db.execute('INSERT INTO task_ownership VALUES (?,?,?,?)',(task_id,workspace,uid,message_id))
        self.emit(workspace,'task_submitted',task_id,{},actor=uid)

    def tasks(self,uid,workspace,task_center):
        self.require(uid,workspace)
        with self.db() as db:
            owned={r[0] for r in db.execute('SELECT task_id FROM task_ownership WHERE workspace=?',(workspace,))}
        return [{'id':t.id,'prompt':t.prompt[:1000],'status':t.status.value,'depends_on':t.depends_on,
                 'attempts':t.attempts,'approved':t.approved} for t in task_center.list() if t.id in owned]

    def worker_register(self,actor,workspace,worker):
        self.require(actor,workspace,'admin')
        self._valid(worker)
        secret=secrets.token_urlsafe(36)
        with self.db() as db:
            db.execute('INSERT INTO worker_credentials VALUES (?,?,?,0)',(worker,workspace,self._digest(secret)))
            self._audit(db,actor,workspace,'worker_registered',worker)
        return secret

    def worker_auth(self,worker,secret):
        if not isinstance(secret,str) or len(secret)<24:raise Forbidden('Worker unauthorized')
        with self.db() as db:
            row=db.execute('SELECT workspace FROM worker_credentials WHERE id=? AND secret_hash=? AND disabled=0',
                           (worker,self._digest(secret))).fetchone()
        if not row:raise Forbidden('Worker unauthorized')
        return row[0]

    def command(self,actor,workspace,worker,action,payload):
        self.require(actor,workspace,'admin')
        if action not in ('drain','resume','cancel_job','inspect_job'):
            raise ValueError('Unsupported worker action')
        if not isinstance(payload,dict) or len(json.dumps(payload))>2000:raise ValueError('Invalid payload')
        if action in ('cancel_job','inspect_job') and not isinstance(payload.get('job_id'),str):
            raise ValueError('job_id required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM worker_credentials WHERE id=? AND workspace=? AND disabled=0',(worker,workspace)).fetchone():
                raise ValueError('Worker not in workspace')
            cid=uuid4().hex
            db.execute('INSERT INTO worker_commands(id,workspace,worker,action,payload,state,created) VALUES (?,?,?,?,?,?,?)',
                       (cid,workspace,worker,action,json.dumps(payload),'queued',time.time()))
            self._audit(db,actor,workspace,'worker_command_queued',cid)
        self.emit(workspace,'worker_command_queued',cid,{'worker':worker,'action':action},actor=actor)
        return cid

    def poll_command(self,worker,secret):
        workspace=self.worker_auth(worker,secret)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('''SELECT id,action,payload FROM worker_commands WHERE worker=? AND workspace=? AND state='queued'
              ORDER BY created,id LIMIT 1''',(worker,workspace)).fetchone()
            if not row:return None
            db.execute("UPDATE worker_commands SET state='claimed',claimed=? WHERE id=? AND state='queued'",(time.time(),row[0]))
        self.emit(workspace,'worker_command_claimed',row[0],{'worker':worker},actor=worker)
        return {'id':row[0],'action':row[1],'payload':json.loads(row[2])}

    def command_ack(self,worker,secret,command_id,status,receipt=''):
        workspace=self.worker_auth(worker,secret)
        if status not in ('done','failed') or not isinstance(receipt,str) or len(receipt)>2000:raise ValueError('Invalid receipt')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT state FROM worker_commands WHERE id=? AND worker=? AND workspace=?',(command_id,worker,workspace)).fetchone()
            if not row or row[0]!='claimed':raise ValueError('Command must be claimed before acknowledgement')
            db.execute('UPDATE worker_commands SET state=?,finished=?,receipt=? WHERE id=?',(status,time.time(),receipt,command_id))
        self.emit(workspace,'worker_command_'+status,command_id,{'worker':worker},actor=worker)

    def logs(self,uid,workspace,after=0):
        self.require(uid,workspace)
        return [e for e in self.events(uid,workspace,after,100) if e['kind']=='worker_log']

    def worker_log(self,worker,secret,level,message,task_id=None):
        workspace=self.worker_auth(worker,secret)
        if level not in ('info','warning','error') or not isinstance(message,str) or not 1<=len(message)<=2000:
            raise ValueError('Invalid log')
        # Accept sanitized, bounded telemetry. Do not allow raw ANSI escape sequences.
        clean=''.join(c for c in message if c in '\n\t' or (ord(c)>=32 and ord(c)!=127))
        payload={'level':level,'text':clean}
        if task_id is not None:
            self._valid(task_id)
            with self.db() as db:
                valid=db.execute('SELECT 1 FROM task_ownership WHERE workspace=? AND task_id=?',
                                 (workspace,task_id)).fetchone()
            if not valid:raise Forbidden('Task is not assigned to worker workspace')
            payload['task_id']=task_id
        return self.emit(workspace,'worker_log',worker,payload,actor=worker)

    def model_config(self,actor,workspace,base_url,model):
        self.require(actor,workspace,'admin')
        from urllib.parse import urlsplit
        parts=urlsplit(base_url)
        if parts.scheme!='https' and not (parts.scheme=='http' and parts.hostname in ('127.0.0.1','localhost')):
            raise ValueError('HTTPS required')
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or not model or len(model)>128:
            raise ValueError('Invalid model configuration')
        with self.db() as db:
            db.execute('INSERT INTO model_connections VALUES (?,?,?,?) ON CONFLICT(workspace) DO UPDATE SET base_url=excluded.base_url,model=excluded.model,configured=excluded.configured',
                       (workspace,base_url.rstrip('/'),model,time.time()))
            self._audit(db,actor,workspace,'model_configured',model)
        self.emit(workspace,'model_configured',model,{},actor=actor)

    def model_info(self,uid,workspace):
        self.require(uid,workspace)
        with self.db() as db:
            row=db.execute('SELECT base_url,model FROM model_connections WHERE workspace=?',(workspace,)).fetchone()
        return {'configured':bool(row),'base_url':row[0] if row else None,'model':row[1] if row else None}
