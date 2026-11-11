#!/usr/bin/env python3
"""Start the read-only dashboard without starting any Agent or changing tasks."""
import argparse
import time
from biomni_dual.observability import ObservabilityServer, OperationsStore

p=argparse.ArgumentParser()
p.add_argument('--ops-db',default='operations.sqlite')
p.add_argument('--runtime-db',default=None)
p.add_argument('--host',default='127.0.0.1',choices=['127.0.0.1','localhost','::1'])
p.add_argument('--port',type=int,default=8765)
args=p.parse_args()
server=ObservabilityServer(OperationsStore(args.ops_db,runtime_db=args.runtime_db),host=args.host,port=args.port).start()
print(f'Open http://{args.host}:{server.address[1]}/dashboard',flush=True)
try:
    while True:time.sleep(1)
except KeyboardInterrupt:pass
finally:server.stop()
