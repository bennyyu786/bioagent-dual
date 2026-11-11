import asyncio
import hashlib
import json
from pathlib import Path
import pytest

from biomni_dual import (AutonomousBuildCoordinator, CatalogEntry, TrustedCatalog,
    CapabilityAgent, SandboxBuilder, BuildStore, CapabilityRegistry, TaskCenter,
    TaskStatus, AgentResult)
from biomni_dual.sandbox import DockerSandbox
from biomni_dual.contracts import BuildError


class FakeRuntime:
    def __init__(self): self.calls=0
    async def run(self, request):
        self.calls+=1
        return AgentResult(True, json.dumps({'kind':'skill','files':[
            {'path':'SKILL.md','content':'---\nname: auto\ndescription: test\n---\n# Auto'},
            {'path':'tests/test_generated.py','content':'def test_auto(): assert True\n'}]}))


class FakeSandbox(DockerSandbox):
    def __init__(self, *, results=(True,)):
        super().__init__(enforce_rootless=False)
        self.results=list(results); self.calls=0
    async def validate(self, path, *, kind):
        from biomni_dual.sandbox import Validation
        self.calls+=1
        success=self.results.pop(0)
        return Validation(kind,self.digest_tree(path),success,'simulated isolated check', 'mock-container','mock-image')


def make(tmp_path, results=(True,), catalog=None, task=False, repairs=2):
    registry=CapabilityRegistry(); sandbox=FakeSandbox(results=results)
    store=BuildStore(tmp_path/'state.sqlite3')
    builder=SandboxBuilder(store,sandbox,registry)
    llm=FakeRuntime(); agent=CapabilityAgent(llm,tmp_path/'staging')
    tasks=TaskCenter(tmp_path/'tasks.sqlite3') if task else None
    coord=AutonomousBuildCoordinator(tmp_path/'state.sqlite3',catalog or TrustedCatalog([]),agent,builder,tasks,max_repairs=repairs)
    return coord,llm,sandbox,registry,tasks


def run(coro): return asyncio.run(coro)


def test_end_to_end_generation_validation_approval_publish_resume(tmp_path):
    coord,llm,sandbox,registry,tasks=make(tmp_path,task=True)
    task=tasks.submit('analyze',required_capabilities=['skill.auto'])
    tasks.require_approval(task.id)
    req=coord.request('skill.auto','skill','interpret variants',request_key='request-1',task_id=task.id)
    assert coord.request('skill.auto','skill','interpret variants',request_key='request-1',task_id=task.id)['id']==req['id']
    assert run(coord.prepare(req['id']))['state']=='AWAITING_APPROVAL'
    assert run(coord.prepare(req['id']))['state']=='AWAITING_APPROVAL'
    assert llm.calls==1 and sandbox.calls==1
    assert tasks.get(task.id).status==TaskStatus.WAITING_APPROVAL
    published=coord.approve_and_publish(req['id'],approver='operator',approve=True)
    assert published['state']=='PUBLISHED'
    assert registry.get('skill.auto').metadata['digest']
    assert tasks.get(task.id).status==TaskStatus.PENDING
    assert coord.approve_and_publish(req['id'],approver='operator',approve=True)['state']=='PUBLISHED'
    assert coord.builder.restore_published()==1


def test_catalog_search_avoids_llm(tmp_path):
    source=tmp_path/'trusted'; (source/'tests').mkdir(parents=True)
    (source/'SKILL.md').write_text('# trusted')
    (source/'tests/test_ok.py').write_text('def test_ok(): assert True')
    digest=DockerSandbox.digest_tree(source)
    catalog=TrustedCatalog([CatalogEntry('skill.catalog','skill',str(source),digest)])
    coord,llm,sandbox,registry,_=make(tmp_path, catalog=catalog)
    req=coord.request('skill.catalog','skill','trusted workflow',request_key='catalog-1')
    assert run(coord.prepare(req['id']))['state']=='AWAITING_APPROVAL'
    assert llm.calls==0


def test_invalid_catalog_digest_blocks_without_llm_fallback(tmp_path):
    source=tmp_path/'trusted'; source.mkdir(); (source/'SKILL.md').write_text('x')
    catalog=TrustedCatalog([CatalogEntry('skill.catalog','skill',str(source),'0'*64)])
    coord,llm,_,_,_=make(tmp_path,catalog=catalog)
    req=coord.request('skill.catalog','skill','reason',request_key='bad-digest')
    with pytest.raises(BuildError): run(coord.prepare(req['id']))
    assert coord._get(req['id'])['state']=='BLOCKED'
    assert llm.calls==0


def test_bounded_auto_repair(tmp_path):
    coord,llm,_,_,_=make(tmp_path,results=(False,False,True),repairs=2)
    req=coord.request('skill.x','skill','repair',request_key='repair')
    assert run(coord.prepare(req['id']))['state']=='REPAIR_PENDING'
    assert run(coord.prepare(req['id']))['state']=='REPAIR_PENDING'
    assert run(coord.prepare(req['id']))['state']=='AWAITING_APPROVAL'
    assert llm.calls==3


def test_repair_budget_exhaustion(tmp_path):
    coord,llm,_,_,_=make(tmp_path,results=(False,False),repairs=1)
    req=coord.request('skill.x','skill','repair',request_key='budget')
    assert run(coord.prepare(req['id']))['state']=='REPAIR_PENDING'
    assert run(coord.prepare(req['id']))['state']=='FAILED'
    assert run(coord.prepare(req['id']))['state']=='FAILED'
    assert llm.calls==2


def test_rejection_stays_inactive(tmp_path):
    coord,_,_,registry,_=make(tmp_path)
    req=coord.request('skill.x','skill','needed',request_key='reject')
    run(coord.prepare(req['id']))
    assert coord.approve_and_publish(req['id'],approver='human',approve=False)['state']=='REJECTED'
    assert registry.get('skill.x') is None
    with pytest.raises(PermissionError): coord.approve_and_publish(req['id'],approver='human',approve=True)


def test_approval_before_validation_forbidden(tmp_path):
    coord,_,_,_,_=make(tmp_path)
    req=coord.request('skill.x','skill','needed',request_key='premature')
    with pytest.raises(PermissionError): coord.approve_and_publish(req['id'],approver='human',approve=True)


def test_idempotency_key_conflict(tmp_path):
    coord,_,_,_,_=make(tmp_path)
    coord.request('skill.x','skill','needed',request_key='same')
    with pytest.raises(BuildError): coord.request('skill.y','skill','needed',request_key='same')


def test_recover_interrupted_fails_closed(tmp_path):
    coord,_,_,_,_=make(tmp_path)
    req=coord.request('skill.x','skill','needed',request_key='crash')
    coord._transition(req['id'],'REQUESTED','PREPARING')
    assert coord.recover()==1
    assert coord._get(req['id'])['state']=='BLOCKED'
    assert coord.recover()==0


def test_untrusted_catalog_hosts_rejected(tmp_path):
    entry=CatalogEntry('skill.x','skill','https://untrusted.example.org/a.tar.gz','0'*64)
    catalog=TrustedCatalog([entry], allowed_hosts=('approved.example.org',))
    with pytest.raises(PermissionError): catalog.materialize(entry,DockerSandbox.digest_tree)


def test_task_scope_enforced(tmp_path):
    coord,_,_,_,tasks=make(tmp_path,task=True)
    task=tasks.submit('Task',required_capabilities=['skill.needed'])
    with pytest.raises(ValueError):
        coord.request('skill.other','skill','other',request_key='wrong',task_id=task.id)


def test_catalog_mcp_route_and_publish(tmp_path):
    source=tmp_path/'mcp';source.mkdir()
    (source/'mcp_manifest.json').write_text(json.dumps({'argv':['python','server.py'],'expected_tools':['ping']}))
    (source/'server.py').write_text('print("sample")')
    digest=DockerSandbox.digest_tree(source)
    coord,llm,sandbox,registry,_=make(tmp_path,catalog=TrustedCatalog([
        CatalogEntry('mcp.ping','mcp',str(source),digest)]))
    req=coord.request('mcp.ping','mcp','ping',request_key='mcp-1')
    assert run(coord.prepare(req['id']))['state']=='AWAITING_APPROVAL'
    assert coord.approve_and_publish(req['id'],approver='human',approve=True)['state']=='PUBLISHED'
    assert registry.get('mcp.ping').kind.value=='mcp'
    assert llm.calls==0


def test_repair_prompt_receives_previous_failure(tmp_path):
    class RecordingRuntime(FakeRuntime):
        def __init__(self): super().__init__();self.prompts=[]
        async def run(self,request):
            self.prompts.append(request.prompt)
            return await super().run(request)
    coord,_,_,_,_=make(tmp_path,results=(False,True),repairs=1)
    recording=RecordingRuntime()
    coord.agent.runtime=recording
    req=coord.request('skill.x','skill','do work',request_key='feedback')
    run(coord.prepare(req['id']))
    run(coord.prepare(req['id']))
    assert 'Previous sandbox failure:' in recording.prompts[1]
    assert 'simulated isolated check' in recording.prompts[1]


def test_https_catalog_index_allowlist(tmp_path,monkeypatch):
    class Response:
        def __init__(self,data,url): self.data=data; self.url=url
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def geturl(self): return self.url
        def read(self,n): return self.data[:n]
    def fake_urlopen(url,timeout):
        assert url == 'https://catalog.example.org/index.json'
        data=json.dumps([{'capability_id':'skill.x','kind':'skill',
                          'source':'https://catalog.example.org/a.tar.gz','digest':'a'*64}]).encode()
        return Response(data,url)
    monkeypatch.setattr('biomni_dual.autonomy.urlopen',fake_urlopen)
    catalog=TrustedCatalog.from_https_index('https://catalog.example.org/index.json',
                                             allowed_hosts=('catalog.example.org',),download_root=tmp_path)
    assert len(catalog.search('skill.x','skill'))==1
    with pytest.raises(PermissionError):
        TrustedCatalog.from_https_index('https://other.example.org/index.json',
                                         allowed_hosts=('catalog.example.org',))


def test_request_reopen_after_new_process(tmp_path):
    coord,_,_,registry,_=make(tmp_path)
    req=coord.request('skill.x','skill','needed',request_key='restart')
    resumed=AutonomousBuildCoordinator(tmp_path/'state.sqlite3',TrustedCatalog([]),coord.agent,
                                         coord.builder,max_repairs=2)
    assert resumed.request('skill.x','skill','needed',request_key='restart')['id']==req['id']
    assert run(resumed.prepare(req['id']))['state']=='AWAITING_APPROVAL'
    resumed.approve_and_publish(req['id'],approver='operator',approve=True)
    assert registry.get('skill.x') is not None


def test_crash_between_artifact_publish_and_autonomy_state(tmp_path):
    coord,_,_,registry,_=make(tmp_path)
    req=coord.request('skill.x','skill','needed',request_key='partial-publish')
    ready=run(coord.prepare(req['id']))
    build_id=ready['build_id']
    coord.builder.approve(build_id,'human',approved=True)
    coord.builder.publish(build_id)
    assert coord._get(req['id'])['state']=='AWAITING_APPROVAL'
    published=coord.approve_and_publish(req['id'],approver='human',approve=True)
    assert published['state']=='PUBLISHED'
    assert registry.get('skill.x') is not None


def test_llm_generated_mcp_candidate(tmp_path):
    class McpLLM:
        async def run(self,request):
            return AgentResult(True,json.dumps({'kind':'mcp','files':[
                {'path':'mcp_manifest.json','content':json.dumps({'argv':['python','server.py'],'expected_tools':['ping']})},
                {'path':'server.py','content':'from mcp.server.fastmcp import FastMCP\napp=FastMCP("ping")\n@app.tool()\ndef ping(): return "pong"\nif __name__=="__main__": app.run()'},
                {'path':'tests/test_manifest.py','content':'def test_manifest(): assert True'}
            ]}))
    coord,_,sandbox,registry,_=make(tmp_path)
    coord.agent.runtime=McpLLM()
    req=coord.request('mcp.generated','mcp','ping',request_key='generated-mcp')
    assert run(coord.prepare(req['id']))['state']=='AWAITING_APPROVAL'
    coord.approve_and_publish(req['id'],approver='human',approve=True)
    assert registry.get('mcp.generated').kind.value=='mcp'
