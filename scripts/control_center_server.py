#!/usr/bin/env python3
"""Launch the authenticated, local-only Biomni Control Center.

Example: BIOMNI_CONTROL_TOKEN=$(python -c 'import secrets;print(secrets.token_urlsafe(36))')
         python scripts/control_center_server.py --task-db state.sqlite
"""
import argparse
import os
import sys
import time
from biomni_dual import OperationsStore, TaskCenter, PersistentRuntimeStore
from biomni_dual.control_center import ControlCenter, ControlCenterServer, ConversationStore
from biomni_dual.chat_advisor import OpenAICompatibleAdvisor


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--ops-db',default='operations.sqlite')
    p.add_argument('--task-db',default='biomni_dual.db')
    p.add_argument('--conversation-db',default='control.sqlite')
    p.add_argument('--runtime-db',default=None)
    p.add_argument('--remediation-db',default=None)
    p.add_argument('--experiments-db',default=None)
    p.add_argument('--host',default='127.0.0.1',choices=('127.0.0.1','localhost','::1'))
    p.add_argument('--port',type=int,default=8765)
    args=p.parse_args()
    token=os.environ.get('BIOMNI_CONTROL_TOKEN','')
    if len(token)<32:
        p.error('BIOMNI_CONTROL_TOKEN must be set to a secret with at least 32 characters')
    ops=OperationsStore(args.ops_db,runtime_db=args.runtime_db)
    advisor=None
    base=os.environ.get('BIOMNI_CHAT_BASE_URL')
    if base:
        advisor=OpenAICompatibleAdvisor(base,os.environ.get('BIOMNI_CHAT_API_KEY',''),
                                        os.environ.get('BIOMNI_CHAT_MODEL',''))
    ctl=ControlCenter(ConversationStore(args.conversation_db),TaskCenter(args.task_db),ops,
                      token=token,runtime=PersistentRuntimeStore(args.runtime_db) if args.runtime_db else None,
                      remediation_db=args.remediation_db,experiments_db=args.experiments_db,advisor=advisor)
    server=ControlCenterServer(ctl,host=args.host,port=args.port).start()
    print(f'Open http://{args.host}:{server.address[1]}/control',flush=True)
    print('Server is local-only. Submit tasks from chat only after explicit confirmation.',file=sys.stderr)
    try:
        while True:time.sleep(1)
    except KeyboardInterrupt:pass
    finally:server.stop()

if __name__=='__main__':main()
