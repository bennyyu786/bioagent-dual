#!/usr/bin/env python3
"""Team ASGI entrypoint. Keep behind TLS reverse proxy, set trusted hosts."""
import argparse
import os
import importlib
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.collaboration_api import create_app
from biomni_dual.observability import OperationsStore
from biomni_dual.scheduler import TaskCenter

def main():
    p=argparse.ArgumentParser(description='Biomni Team Control Plane')
    p.add_argument('--collaboration-db',default='state/collaboration.sqlite')
    p.add_argument('--task-db',default='state/tasks.sqlite')
    p.add_argument('--ops-db',default='state/operations.sqlite')
    p.add_argument('--bootstrap-email',default=None)
    p.add_argument('--bootstrap-workspace',default='main')
    p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,default=8766)
    p.add_argument('--result-validator',default=os.getenv('BIOMNI_RESULT_VALIDATOR',''),
                   help='Trusted installed Python callable as module:function; never taken from Web input')
    args=p.parse_args()
    store=CollaborationStore(args.collaboration_db)
    if args.bootstrap_email:
        pw=os.getenv('BIOMNI_BOOTSTRAP_PASSWORD','')
        if not pw:p.error('BIOMNI_BOOTSTRAP_PASSWORD required for local bootstrap')
        store.bootstrap(args.bootstrap_email,pw,args.bootstrap_workspace)
        print('Administrator created. Remove bootstrap credentials from environment.',flush=True)
        return
    from uvicorn import run
    validator=None
    if args.result_validator:
        try:
            module_name, function_name=args.result_validator.split(':',1)
            if not module_name or not function_name:
                raise ValueError('Expected module:function')
            validator=getattr(importlib.import_module(module_name),function_name)
            if not callable(validator):raise TypeError('Validator is not callable')
        except (ValueError,TypeError,ImportError,AttributeError) as exc:
            p.error('Invalid trusted result validator: '+str(exc))
    app=create_app(store,TaskCenter(args.task_db),OperationsStore(args.ops_db),result_validator=validator)
    run(app,host=args.host,port=args.port,proxy_headers=False,access_log=False)

if __name__=='__main__':main()
