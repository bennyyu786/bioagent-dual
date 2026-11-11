#!/usr/bin/env python3
"""Remote worker reference agent: heartbeat/logs + SAFE control commands only.

Never accepts arbitrary shell code, paths or script execution from server.
"""
import argparse
import json
import os
import time
from urllib.request import Request,urlopen

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--url',required=True,help='HTTPS control-plane base URL')
    p.add_argument('--id',required=True)
    p.add_argument('--interval',type=float,default=5)
    args=p.parse_args()
    if not (args.url.startswith('https://') or args.url.startswith('http://127.0.0.1:')):
        p.error('HTTPS required for non-loopback worker connections')
    secret=os.getenv('BIOMNI_WORKER_SECRET','')
    if len(secret)<24:p.error('BIOMNI_WORKER_SECRET required')
    base=args.url.rstrip('/')+'/api/v2/worker/'+args.id
    headers={'X-Worker-Secret':secret,'Content-Type':'application/json'}
    drained=False
    def call(path,data=None):
        req=Request(base+path,data=json.dumps(data).encode() if data is not None else None,
                    headers=headers,method='POST' if data is not None else 'GET')
        with urlopen(req,timeout=15) as r:return json.load(r)
    while True:
        try:
            command=call('/poll')['command']
            if command:
                action=command['action']
                if action=='drain':drained=True;message='Worker drained; no new jobs will be started'
                elif action=='resume':drained=False;message='Worker accepting jobs'
                else:message='Operation not supported by reference client; requires trusted job supervisor'
                success=action in ('drain','resume')
                call('/commands/'+command['id']+'/ack',{'status':'done' if success else 'failed','receipt':message})
                call('/logs',{'level':'info' if success else 'warning','message':message})
        except Exception as e:
            # No secrets or credential-bearing URLs in logs.
            print('Control-plane connection unavailable:',type(e).__name__,flush=True)
        time.sleep(max(args.interval,1))

if __name__=='__main__':main()
