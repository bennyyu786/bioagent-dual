"""Opt-in daemon bootstrap for trusted local deployment code.

Usage:
 python scripts/runtime_daemon.py --bootstrap my_trusted_module:create_runtime
The bootstrap callable must return a configured PersistentAutonomousRuntime.
This script never dynamically installs Skills or grants Docker access.
"""
from __future__ import annotations
import argparse
import asyncio
import importlib
import json
import signal
from biomni_dual import PersistentAutonomousRuntime, OperationsStore, serve_observed


def load_runtime(target: str) -> PersistentAutonomousRuntime:
    module_name,sep,callable_name=target.partition(':')
    if not sep or not module_name or not callable_name:
        raise ValueError('Bootstrap must have format module:function')
    module=importlib.import_module(module_name)
    factory=getattr(module,callable_name)
    runtime=factory()
    if not isinstance(runtime,PersistentAutonomousRuntime):
        raise TypeError('Bootstrap factory must return PersistentAutonomousRuntime')
    return runtime


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--bootstrap',required=True,
                        help='Explicit trusted Python factory, module:function')
    parser.add_argument('--max-loops',type=int,default=None)
    parser.add_argument('--reconcile-startup',action='store_true',
                        help='Quarantine orphaned RUNNING tasks before service starts')
    parser.add_argument('--observability-db',default=None,
                        help='Enable local health/metrics endpoint with this SQLite DB')
    parser.add_argument('--metrics-host',default='127.0.0.1')
    parser.add_argument('--metrics-port',type=int,default=8765)
    parser.add_argument('--allow-container-bind',action='store_true',help='Explicitly allow 0.0.0.0 inside an isolated container; host port must be bound to localhost')
    args=parser.parse_args()
    runtime=load_runtime(args.bootstrap)
    if args.reconcile_startup:
        for gid in runtime.controllers:
            recovered=runtime.reconcile_startup(gid)
            if recovered:
                print(json.dumps({'goal':gid,'quarantined':recovered}))
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        try:loop.add_signal_handler(sig,runtime.stop)
        except NotImplementedError: pass
    if args.observability_db:
        await serve_observed(runtime,OperationsStore(args.observability_db,runtime_db=runtime.store.db_path),
            host=args.metrics_host,port=args.metrics_port,max_loops=args.max_loops,allow_non_loopback=args.allow_container_bind)
    else:
        await runtime.serve(max_loops=args.max_loops)


if __name__=='__main__':
    asyncio.run(main())
