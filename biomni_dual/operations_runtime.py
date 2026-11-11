"""Supervision wrapper for a preconfigured PersistentAutonomousRuntime."""
from __future__ import annotations
import asyncio
from .observability import OperationsStore, ObservabilityServer


async def serve_observed(runtime, operations:OperationsStore, *, host='127.0.0.1',port=8765,
                         interval=2.0,max_loops=None,allow_non_loopback=False,control_center=None):
    if control_center is not None:
        if allow_non_loopback:
            raise ValueError('Operator control center must remain local-only')
        from .control_center import ControlCenterServer
        if control_center.operations is not operations:
            raise ValueError('Control center and observed runtime must share OperationsStore')
        server=ControlCenterServer(control_center,host=host,port=port).start()
    else:
        server=ObservabilityServer(operations,host,port,allow_non_loopback=allow_non_loopback).start()
    operations.heartbeat()
    shutdown=asyncio.Event()
    async def monitor():
        while not shutdown.is_set():
            operations.heartbeat()
            operations.evaluate_alerts()
            try: await asyncio.wait_for(shutdown.wait(),timeout=interval)
            except asyncio.TimeoutError: pass
    monitor_task=asyncio.create_task(monitor())
    try:
        await runtime.serve(max_loops=max_loops)
    except BaseException:
        operations.heartbeat(state='failed')
        operations.count('runtime_failures')
        raise
    finally:
        shutdown.set()
        await monitor_task
        if operations.snapshot()['service']!='unavailable':
            operations.heartbeat(state='stopping')
        server.stop()
