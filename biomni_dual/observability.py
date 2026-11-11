"""Local-only operational visibility. No task payloads or credentials are exposed.

The HTTP API intentionally has no control-plane actions. Worker heartbeats use a
per-worker secret; registration is an operator-only Python/CLI operation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from .dashboard import DASHBOARD_HTML


@dataclass(frozen=True)
class AlertRule:
    code: str
    threshold: float
    severity: str = "warning"
    cooldown: float = 120.0


class OperationsStore:
    def __init__(self, path: str, *, runtime_db: str | None = None):
        self.path = str(path)
        self.runtime_db = runtime_db
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS ops_service (
                    name TEXT PRIMARY KEY, updated REAL NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ops_workers (
                    id TEXT PRIMARY KEY, token_sha256 TEXT NOT NULL,
                    kind TEXT NOT NULL, last_seen REAL NOT NULL,
                    capacity INTEGER NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ops_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL,
                    subject TEXT NOT NULL, severity TEXT NOT NULL, created REAL NOT NULL,
                    last_seen REAL NOT NULL, occurrences INTEGER NOT NULL,
                    resolved REAL, UNIQUE(code,subject));
                CREATE TABLE IF NOT EXISTS ops_counters (
                    name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            ''')

    def _connect(self):
        db=sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA busy_timeout=10000')
        return db

    def heartbeat(self, name='runtime', state='running', *, now=None):
        if not name or state not in ('running','stopping','failed'):
            raise ValueError('Invalid service heartbeat')
        with self._connect() as db:
            db.execute('INSERT INTO ops_service VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET updated=excluded.updated,state=excluded.state',
                       (name,time.time() if now is None else now,state))

    def register_worker(self, worker_id: str, token: str, *, kind='remote', capacity=1):
        if not worker_id or len(worker_id)>100 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for c in worker_id):
            raise ValueError('Invalid worker ID')
        if not isinstance(token,str) or len(token)<24 or capacity<1:
            raise ValueError('Worker token must have at least 24 characters; capacity must be positive')
        digest=hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            db.execute('INSERT INTO ops_workers VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET token_sha256=excluded.token_sha256,kind=excluded.kind,capacity=excluded.capacity,status=excluded.status',
                       (worker_id,digest,kind,0,capacity,'registered'))

    def worker_heartbeat(self, worker_id: str, token: str, *, now=None) -> bool:
        # Do not accept caller-supplied state or timestamps from remote clients.
        with self._connect() as db:
            row=db.execute('SELECT token_sha256 FROM ops_workers WHERE id=?',(worker_id,)).fetchone()
            if row is None or not hmac.compare_digest(row[0],hashlib.sha256(token.encode()).hexdigest()):
                return False
            db.execute('UPDATE ops_workers SET last_seen=?,status=? WHERE id=?',
                       (time.time() if now is None else now,'online',worker_id))
        return True

    def count(self, name, value=1):
        if not name or not name.replace('_','').isalnum() or value<0:
            raise ValueError('Invalid counter')
        with self._connect() as db:
            db.execute('INSERT INTO ops_counters VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value',(name,value))

    def runtime_summary(self) -> dict[str,Any]:
        """Read-only aggregates; never expose goal labels or event payloads."""
        if self.runtime_db is None:
            return {}
        try:
            uri=Path(self.runtime_db).resolve().as_uri().replace('file:', 'file:', 1)+'?mode=ro'
            with sqlite3.connect(uri,uri=True,timeout=2) as db:
                goals=dict(db.execute('SELECT state,COUNT(*) FROM runtime_goals GROUP BY state'))
                events=dict(db.execute('SELECT status,COUNT(*) FROM runtime_events GROUP BY status'))
                run_count=db.execute('SELECT COUNT(*) FROM runtime_runs').fetchone()[0]
            return {'goals':goals,'events':events,'runs':run_count}
        except (sqlite3.Error,OSError):
            return {'available':False}

    def snapshot(self, *, now=None, worker_timeout=90.0, service_timeout=30.0) -> dict[str,Any]:
        now=time.time() if now is None else now
        with self._connect() as db:
            workers=db.execute('SELECT id,kind,last_seen,capacity,status FROM ops_workers ORDER BY id').fetchall()
            service=db.execute('SELECT updated,state FROM ops_service WHERE name=?',('runtime',)).fetchone()
            counters=dict(db.execute('SELECT name,value FROM ops_counters'))
            alerts=db.execute('SELECT code,subject,severity,occurrences,created FROM ops_alerts WHERE resolved IS NULL ORDER BY id').fetchall()
        details=[{'id':r[0],'kind':r[1],'capacity':r[3], 'state': ('never_seen' if r[2]<=0 else 'online' if now-r[2]<=worker_timeout else 'stale'),
                  'age_seconds':None if r[2]<=0 else max(0,round(now-r[2],3))} for r in workers]
        fresh=bool(service and service[1]=='running' and 0<=now-service[0]<=service_timeout)
        return {'ready':fresh,'service':'running' if fresh else 'unavailable',
                'workers':details,'counters':counters,'runtime':self.runtime_summary(),
                'alerts':[{'code':a[0],'subject':a[1],'severity':a[2],'occurrences':a[3]} for a in alerts]}

    def evaluate_alerts(self, *, now=None, worker_timeout=90., service_timeout=30., cooldown=120.):
        now=time.time() if now is None else now
        snapshot=self.snapshot(now=now,worker_timeout=worker_timeout,service_timeout=service_timeout)
        desired={}
        if not snapshot['ready']:
            desired[('service_unavailable','runtime')]='critical'
        for worker in snapshot['workers']:
            if worker['state']=='stale':
                desired[('worker_stale',worker['id'])]='warning'
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for (code,subject),severity in desired.items():
                row=db.execute('SELECT last_seen,resolved FROM ops_alerts WHERE code=? AND subject=?',(code,subject)).fetchone()
                if row is None:
                    db.execute('INSERT INTO ops_alerts(code,subject,severity,created,last_seen,occurrences) VALUES (?,?,?,?,?,1)',
                               (code,subject,severity,now,now))
                elif row[1] is not None:
                    db.execute('UPDATE ops_alerts SET created=?,last_seen=?,occurrences=1,resolved=NULL WHERE code=? AND subject=?',
                               (now,now,code,subject))
                elif now-row[0]>=cooldown:
                    db.execute('UPDATE ops_alerts SET last_seen=?,occurrences=occurrences+1 WHERE code=? AND subject=?',(now,code,subject))
            for code,subject in db.execute('SELECT code,subject FROM ops_alerts WHERE resolved IS NULL').fetchall():
                if (code,subject) not in desired:
                    db.execute('UPDATE ops_alerts SET resolved=? WHERE code=? AND subject=?',(now,code,subject))
        return self.snapshot(now=now,worker_timeout=worker_timeout,service_timeout=service_timeout)['alerts']

    def prometheus(self, *, now=None) -> str:
        state=self.snapshot(now=now)
        metrics=['# TYPE biomni_runtime_ready gauge',f'biomni_runtime_ready {int(state["ready"])}',
                 '# TYPE biomni_worker_total gauge',f'biomni_worker_total {len(state["workers"])}',
                 '# TYPE biomni_worker_online gauge',f'biomni_worker_online {sum(w["state"]=="online" for w in state["workers"])}',
                 '# TYPE biomni_alert_active gauge',f'biomni_alert_active {len(state["alerts"])}']
        for name,value in sorted(state['counters'].items()):
            metrics.append(f'biomni_ops_{name}_total {int(value)}')
        runtime=state['runtime']
        if 'runs' in runtime:
            metrics.extend(['# TYPE biomni_runtime_runs_total counter',
                            f'biomni_runtime_runs_total {runtime["runs"]}'])
            for status, count in sorted(runtime['goals'].items()):
                if status.replace('_','').isalnum():
                    metrics.append(f'biomni_goals_total{{state="{status}"}} {count}')
            for status, count in sorted(runtime['events'].items()):
                if status.replace('_','').isalnum():
                    metrics.append(f'biomni_events_total{{status="{status}"}} {count}')
        return '\n'.join(metrics)+'\n'


class ObservabilityServer:
    """Unauthenticated read-only endpoints MUST bind loopback or protected proxy.

    POST /internal/worker-heartbeat requires a registered worker token. This is
    telemetry only, not a mechanism for authorizing Worker execution.
    """
    def __init__(self, store: OperationsStore, host='127.0.0.1', port=8765, *, allow_non_loopback=False):
        if host not in ('127.0.0.1','::1','localhost') and not allow_non_loopback:
            raise ValueError('Metrics server must bind loopback; use an authenticated reverse proxy for remote access')
        self.store=store
        handler=self._handler()
        self.server=ThreadingHTTPServer((host,int(port)),handler)
        self._thread=None

    def _handler(self):
        store=self.store
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def _reply(self,status,body,content_type='application/json'):
                data=body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('X-Frame-Options','DENY')
                self.send_header('Referrer-Policy','no-referrer')
                if content_type.startswith('text/html'):
                    self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                path=self.path.split('?')[0]
                if path in ('/dashboard','/'):
                    self._reply(200,DASHBOARD_HTML,'text/html; charset=utf-8')
                elif path=='/api/dashboard':
                    self._reply(200,json.dumps(store.snapshot(),sort_keys=True))
                elif path=='/health/live':
                    self._reply(200,'{"alive":true}')
                elif path=='/health/ready':
                    s=store.snapshot()
                    self._reply(200 if s['ready'] else 503,json.dumps({'ready':s['ready'],'service':s['service']}))
                elif path=='/metrics':
                    self._reply(200,store.prometheus(),'text/plain; version=0.0.4')
                elif path=='/status':
                    self._reply(200,json.dumps(store.snapshot(),sort_keys=True))
                else:
                    self._reply(404,'{"error":"not_found"}')
            def do_POST(self):
                if self.path!='/internal/worker-heartbeat':
                    return self._reply(404,'{"error":"not_found"}')
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=2048:
                        return self._reply(413,'{"error":"invalid_size"}')
                    body=json.loads(self.rfile.read(length))
                    if not isinstance(body,dict) or set(body)!={'worker_id','token'}:
                        return self._reply(400,'{"error":"invalid_payload"}')
                    if not isinstance(body['worker_id'],str) or not isinstance(body['token'],str):
                        return self._reply(400,'{"error":"invalid_payload"}')
                    if not store.worker_heartbeat(body['worker_id'],body['token']):
                        return self._reply(401,'{"error":"unauthorized"}')
                    return self._reply(200,'{"ok":true}')
                except (ValueError,TypeError,UnicodeDecodeError,json.JSONDecodeError):
                    return self._reply(400,'{"error":"invalid_payload"}')
        return Handler

    @property
    def address(self): return self.server.server_address
    def start(self):
        self._thread=threading.Thread(target=self.server.serve_forever,name='biomni-metrics',daemon=True)
        self._thread.start()
        return self
    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        if self._thread is not None: self._thread.join(timeout=5)
