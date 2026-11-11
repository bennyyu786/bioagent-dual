"""Synthetic integration demo of priority dispatch, trusted adapter and review."""
from pathlib import Path
from tempfile import TemporaryDirectory
from sys import executable
from fastapi.testclient import TestClient
from biomni_dual.collaboration import CollaborationStore
from biomni_dual.collaboration_api import create_app
from biomni_dual.scheduler import TaskCenter
from biomni_dual.observability import OperationsStore
from biomni_dual.runtime_relations import RuntimeRelationStore
from biomni_dual.remote_worker import TrustedAdapterRunner


def main():
    with TemporaryDirectory() as folder:
        p=Path(folder)
        members=CollaborationStore(p/'users.db');members.bootstrap('admin@example.com','DemoPassword123456')
        tasks=TaskCenter(p/'tasks.db');ops=OperationsStore(p/'ops.db');ledger=RuntimeRelationStore(p/'relations.db')
        app=create_app(members,tasks,ops,ledger,result_validator=lambda task,output:(output.startswith('artifact:'),'demo:independent-validation'))
        client=TestClient(app)
        token=client.post('/api/v2/login',json={'email':'admin@example.com','password':'DemoPassword123456'}).json()['token']
        auth={'Authorization':'Bearer '+token}
        secret=client.post('/api/v2/main/workers',headers=auth,json={'id':'lab-worker'}).json()['secret']
        worker={'x-worker-secret':secret}
        capacity={'cpu_milli':4000,'memory_mb':8192,'gpu_units':1}
        request={'cpu_milli':1000,'memory_mb':512,'gpu_units':1}
        client.put('/api/v2/main/runtime/workers/lab-worker/capacity',headers=auth,json=capacity)
        client.put('/api/v2/main/dispatch/workers/lab-worker/skills',headers=auth,json={'capabilities':['analysis']})
        client.put('/api/v2/main/runtime/capabilities/analysis',headers=auth,
                   json={'lifecycle':'approved','evidence_ref':'demo:approved-adapter'})
        tasks.submit('Example task',tenant='main',priority=5,required_capabilities=['analysis'])
        client.get('/api/v2/worker/lab-worker/dispatch/poll',headers=worker)
        offer=client.post('/api/v2/main/dispatch/next',headers=auth,
                          json={'request_id':'demo-next','resources':request}).json()
        base='/api/v2/worker/lab-worker/dispatch/'+offer['id']
        job=client.post(base+'/claim',headers=worker,json={'ttl_seconds':60}).json()
        script=p/'trusted.py';script.write_text('import json,sys\nx=json.load(sys.stdin)\nprint("artifact:"+x["task_id"])\n')
        state,result=TrustedAdapterRunner({'analysis':[executable,str(script)]}).execute(job)
        receipt=client.post(base+'/report',headers=worker,json={'state':state,'result':result}).json()
        review=client.post('/api/v2/main/dispatch/'+offer['id']+'/verify',headers=auth).json()
        print('worker:',offer['worker'],'priority task:',job['task_id'])
        print('worker report:',receipt['state'],'independent review:',review['state'])
        print('task status:',tasks.get(job['task_id']).status.value)
        print('capacity freed:',ledger.snapshot('main')['capacity'][0]['reserved']['gpu_units']==0)

if __name__=='__main__':main()
