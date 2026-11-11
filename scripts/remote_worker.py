#!/usr/bin/env python3
"""Remote worker process; trusted adapters are configured locally, not via the API."""
import argparse
import json
import os
import time
from pathlib import Path
from biomni_dual.remote_worker import WorkerTransport,TrustedAdapterRunner,run_once


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--server',required=True)
    p.add_argument('--worker',required=True)
    p.add_argument('--adapters',required=True,help='Local JSON mapping capability ID to executable argv')
    p.add_argument('--interval',type=float,default=4)
    p.add_argument('--allow-http-loopback',action='store_true')
    p.add_argument('--once',action='store_true')
    args=p.parse_args()
    secret=os.getenv('BIOMNI_WORKER_SECRET')
    if not secret:p.error('BIOMNI_WORKER_SECRET environment variable is required')
    config=json.loads(Path(args.adapters).read_text('utf-8'))
    runner=TrustedAdapterRunner(config)
    transport=WorkerTransport(args.server,args.worker,secret,allow_http_loopback=args.allow_http_loopback)
    while True:
        try:
            result=run_once(transport,runner)
            if result:print(json.dumps(result),flush=True)
        except Exception as exc:
            # No auto replay of a claimed job; after network failures the
            # server's lease expires to an uncertain/quarantined state.
            print('Remote worker cycle error: '+type(exc).__name__,flush=True)
        if args.once:break
        time.sleep(max(args.interval,1))

if __name__=='__main__':main()
