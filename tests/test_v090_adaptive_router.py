"""v0.9.0 regression for opt-in model-based complexity routing."""
import pytest
from test_web_planning import rig, configure
from biomni_dual.adaptive_router import AdaptiveTaskRouter
from biomni_dual.contracts import AgentResult


class Runtime:
    def __init__(self, reply, calls):
        self.reply, self.calls = reply, calls
    async def run(self, request):
        self.calls.append(request)
        return AgentResult(True, self.reply)


def test_router_uses_agent_for_ambiguous_and_keeps_direct_fastpath(tmp_path):
    c,a,op,t,rel,calls=rig(tmp_path,'{"kind":"single","reason":"one bounded lookup"}')
    configure(c,a,rel,worker=False)
    assert c.put('/api/v2/main/routing-policy',headers=op,json={'enabled':True,'backend':'codex_sdk'}).status_code==403
    assert c.put('/api/v2/main/routing-policy',headers=a,json={'enabled':True,'backend':'codex_sdk'}).status_code==200
    assert c.get('/api/v2/main/routing-policy',headers=op).json()['enabled'] is True
    resp=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'给我一个关于研究数据集的简短概要','request_id':'adaptive-single-0001'})
    assert resp.status_code==200,resp.text
    assert resp.json()['route']=='single'
    assert resp.json()['routing_source']=='agent:codex_sdk'
    assert len(calls)==1 and calls[0].role=='planner' and calls[0].workspace is None
    resp=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'你好，你叫什么名字？','request_id':'adaptive-simple-0002'})
    assert resp.json()['route']=='simple'
    assert resp.json()['routing_source']=='rules'
    assert len(calls)==1


def test_router_invalid_agent_reply_falls_back_complex(tmp_path):
    c,a,op,t,rel,calls=rig(tmp_path,'not JSON')
    configure(c,a,rel,worker=False)
    c.put('/api/v2/main/routing-policy',headers=a,json={'enabled':True,'backend':'codex_sdk'})
    resp=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'Research dataset','request_id':'adaptive-invalid-0003'})
    assert resp.status_code==200,resp.text
    assert resp.json()['route']=='complex'
    assert resp.json()['routing_source']=='fallback'
    assert resp.json()['planning']['status']=='failed'
    assert len(calls)==2   # classifier attempt + ordinary planner


def test_side_effect_cannot_be_downgraded_by_model(tmp_path):
    c,a,op,t,rel,calls=rig(tmp_path,'{"kind":"simple","reason":"safe"}')
    configure(c,a,rel,worker=False)
    c.put('/api/v2/main/routing-policy',headers=a,json={'enabled':True,'backend':'claude_cli'})
    resp=c.post('/api/v2/main/tasks',headers=op,json={'prompt':'删除生产服务器的数据库','request_id':'adaptive-danger-0004'})
    assert resp.status_code==200,resp.text
    assert resp.json()['route']=='complex'
    assert resp.json()['routing_source']=='rules'
    assert resp.json()['planning']['status']=='failed'
    assert len(calls)==1  # only planner, never classified to direct route


def test_routing_policy_rejects_unknown_backend(tmp_path):
    c,a,op,t,rel,calls=rig(tmp_path,'{}')
    assert c.put('/api/v2/main/routing-policy',headers=a,json={'enabled':True,'backend':'not-a-backend'}).status_code==400
