"""Opt-in remote worker bridge using only administrator-installed executable adapters.

The control plane never sends shell commands. Trusted adapters are mapped to
fixed argv arrays in local worker configuration, consume JSON on stdin and
return UTF-8 stdout. Deploy in a network- and filesystem-restricted sandbox.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import quote


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Worker secrets to a redirected URL.
        return None


class WorkerTransport:
    def __init__(self, base_url, worker, secret, *, allow_http_loopback=False, timeout=30):
        if not isinstance(base_url,str):raise ValueError('Invalid URL')
        from urllib.parse import urlsplit
        parsed=urlsplit(base_url)
        loopback=parsed.hostname in ('127.0.0.1','localhost','::1')
        if parsed.scheme!='https' and not (allow_http_loopback and loopback and parsed.scheme=='http'):
            raise ValueError('HTTPS required outside explicit loopback development mode')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Do not embed credentials in URL')
        self.base=base_url.rstrip('/')
        self.worker=worker;self.secret=secret;self.timeout=timeout
        self._opener=build_opener(_NoRedirect)

    def request(self,method,path,body=None):
        data=json.dumps(body,ensure_ascii=False).encode('utf-8') if body is not None else None
        req=Request(self.base+path,method=method,data=data,headers={
            'X-Worker-Secret':self.secret,'Content-Type':'application/json'})
        with self._opener.open(req,timeout=self.timeout) as r:
            result=r.read(65536)
        return json.loads(result.decode('utf-8'))


class TrustedAdapterRunner:
    def __init__(self, adapters:dict, *, max_seconds=240):
        if not isinstance(adapters,dict) or not adapters:
            raise ValueError('Explicit adapter allowlist required')
        self.adapters={}
        for capability,argv in adapters.items():
            if not isinstance(capability,str) or not capability or not isinstance(argv,list) or not argv or any(not isinstance(a,str) or not a for a in argv):
                raise ValueError('Invalid executable adapter configuration')
            if not Path(argv[0]).is_absolute():
                raise ValueError('Executable path must be absolute')
            self.adapters[capability]=list(argv)
        self.max_seconds=max_seconds

    def execute(self,offer):
        caps=offer.get('capabilities') or []
        if not caps or any(cap not in self.adapters for cap in caps):
            return 'failed','Worker has no approved local adapter for all required capabilities'
        # v0.8.4 enforces one explicit executable mapping per task, rather than
        # silently composing arbitrary commands for multiple capabilities.
        if len(caps)!=1:return 'failed','Multi-capability execution requires an approved composite adapter'
        body=json.dumps({'task_id':offer['task_id'],'prompt':offer['prompt'],
                         'capabilities':caps},ensure_ascii=False).encode('utf-8')
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':'C.UTF-8'}
        try:
            p=subprocess.run(self.adapters[caps[0]],input=body,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             timeout=self.max_seconds,env=env,check=False,cwd='/')
        except (subprocess.TimeoutExpired,OSError) as exc:
            return 'failed',type(exc).__name__
        output=p.stdout[:32000].decode('utf-8',errors='replace')
        if p.returncode != 0:return 'failed',('Adapter exit '+str(p.returncode)+': '+p.stderr[:1000].decode('utf-8',errors='replace'))
        return 'completed',output


def run_once(transport:WorkerTransport,runner:TrustedAdapterRunner,*,ttl=300):
    worker=quote(transport.worker,safe='')
    root='/api/v2/worker/'+worker+'/dispatch'
    offer=transport.request('GET',root+'/poll').get('offer')
    if offer is None:return None
    job=transport.request('POST',root+'/'+quote(offer['id'],safe='')+'/claim',{'ttl_seconds':ttl})
    state,result=runner.execute(job)
    ack=transport.request('POST',root+'/'+quote(offer['id'],safe='')+'/report',{'state':state,'result':result})
    return {'job_id':offer['id'],'report_state':ack['state'],'sha256':ack.get('sha256')}
