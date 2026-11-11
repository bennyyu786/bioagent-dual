"""Local operator interface for worker registration and read-only operations."""
import argparse
import json
import secrets
from biomni_dual import OperationsStore

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    sub=parser.add_subparsers(dest='cmd',required=True)
    reg=sub.add_parser('register-worker')
    reg.add_argument('name')
    reg.add_argument('--kind',default='remote')
    reg.add_argument('--capacity',type=int,default=1)
    sub.add_parser('status')
    sub.add_parser('evaluate-alerts')
    args=parser.parse_args()
    store=OperationsStore(args.db)
    if args.cmd=='register-worker':
        token=secrets.token_urlsafe(32)
        store.register_worker(args.name,token,kind=args.kind,capacity=args.capacity)
        print(json.dumps({'worker_id':args.name,'token':token}))
        print('Store this token securely; it is displayed only during registration.',file=__import__('sys').stderr)
    elif args.cmd=='status': print(json.dumps(store.snapshot(),indent=2))
    else: print(json.dumps(store.evaluate_alerts(),indent=2))

if __name__=='__main__':main()
