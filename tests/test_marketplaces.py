import asyncio
import base64
import json
import pytest
from biomni_dual import (MarketplaceHTTP, MarketplaceError, MarketplaceAuthError,
    OfficialMCPRegistry, GlamaMCP, SkillsSH, AnthropicSkills, FederatedDiscovery,
    MarketCandidate, AutonomousBuildCoordinator, TrustedCatalog, CapabilityRegistry,
    CapabilityAgent, SandboxBuilder, BuildStore, AgentResult)
from biomni_dual.sandbox import DockerSandbox, Validation

SHA = 'a' * 40


def fixtures(url, *, host, token):
    if 'registry.modelcontextprotocol.io' in url:
        assert 'version=latest' in url
        return {'servers': [{'server': {'name': 'io.github.test/variant', 'description': 'variant lookup',
                                       'version': '1.2.0', 'repository': {'url': 'https://github.com/example/mcp'}}}]}
    if 'glama.ai' in url:
        assert token == 'test-key'
        return {'servers': [{'id': '123', 'name': 'Variant MCP', 'slug': 'variant',
                             'namespace': 'example', 'repository': {'url': 'https://github.com/example/mcp'},
                             'url': 'https://glama.ai/mcp/servers/example/variant',
                             'tools': [{'name': 'lookup'}]}]}
    if 'skills.sh' in url:
        if '/search?' in url:
            return {'data': [{'id': 'anthropics/skills/variant-qc', 'name': 'Variant QC',
                              'installUrl': 'https://github.com/anthropics/skills',
                              'url': 'https://skills.sh/anthropics/skills/variant-qc'}]}
        return {'files': [{'path': 'SKILL.md', 'contents': '---\nname: variant-qc\ndescription: variant qc\n---'},
                          {'path': 'tests/test_smoke.py', 'contents': 'def test_ok(): assert True'}],
                'hash': 'upstream-hash'}
    if '/commits/main' in url:
        return {'sha': SHA, 'commit': {'tree': {'sha': 'd'*40}}}
    if '/git/trees/' in url:
        return {'tree': [
            {'type': 'blob', 'path': 'skills/variant-qc/SKILL.md', 'sha': 'b'*40, 'size': 20},
            {'type': 'blob', 'path': 'skills/other/SKILL.md', 'sha': 'c'*40, 'size': 20},
        ]}
    if '/git/blobs/' in url:
        return {'encoding': 'base64', 'content': base64.b64encode(b'---\nname: qc\ndescription: demo\n---\n').decode()}
    raise AssertionError(url)


def http(): return MarketplaceHTTP(fetch=fixtures)


def test_official_mcp_parse():
    p=OfficialMCPRegistry(http())
    result=p.search('variant')
    assert len(result)==1 and result[0].version=='1.2.0'
    assert result[0].metadata['verification']=='metadata-only'
    assert p.resolve(result[0]) is None


def test_glama_auth_and_attribution():
    p=GlamaMCP(http(), api_key='test-key')
    item=p.search('variant')[0]
    assert item.metadata['attribution'].startswith('Data from Glama')
    assert item.metadata['tools']==['lookup']


def test_glama_missing_key(monkeypatch):
    monkeypatch.delenv('GLAMA_API_KEY', raising=False)
    with pytest.raises(MarketplaceAuthError): GlamaMCP(http()).search('test')


def test_skills_sh_resolve_and_stage(tmp_path):
    provider=SkillsSH(http())
    candidate=provider.search('variant')[0]
    resolved=provider.resolve(candidate)
    folder=resolved.stage(tmp_path)
    assert (folder/'SKILL.md').exists()
    assert (folder/'tests/test_smoke.py').exists()


def test_anthropic_pinned_commit_and_resolve(tmp_path):
    provider=AnthropicSkills(http())
    matches=provider.search('variant')
    assert len(matches)==1 and matches[0].version==SHA
    assert provider.resolve(matches[0]).stage(tmp_path).joinpath('SKILL.md').exists()


def test_all_four_federated(tmp_path):
    federated=FederatedDiscovery([OfficialMCPRegistry(http()),GlamaMCP(http(),api_key='test-key'),
                                  SkillsSH(http()),AnthropicSkills(http())],staging_root=tmp_path)
    assert len(federated.search('variant','mcp')['candidates'])==1  # same GitHub repo, deduplicated
    skills=federated.search('variant','skill')['candidates']
    assert len(skills)==2
    assert federated.stage(skills[0]).joinpath('SKILL.md').exists()
    with pytest.raises(MarketplaceError): federated.stage(federated.search('variant','mcp')['candidates'][0])


def test_bad_path_and_missing_skill(tmp_path):
    from biomni_dual.marketplaces import ResolvedSkill
    candidate=SkillsSH(http()).search('test')[0]
    with pytest.raises(MarketplaceError): ResolvedSkill(candidate, {'SKILL.md':'ok','../evil':'bad'},{}).stage(tmp_path)
    with pytest.raises(MarketplaceError): ResolvedSkill(candidate, {'x':'bad'},{}).stage(tmp_path)


def test_http_host_check():
    with pytest.raises(MarketplaceError): http().json('https://example.com/', host='skills.sh')


def test_invalid_query_limit():
    with pytest.raises(ValueError): SkillsSH(http()).search('q')
    with pytest.raises(ValueError): OfficialMCPRegistry(http()).search('x',100)


def test_partial_failure_no_fallback_to_install(tmp_path):
    def broken(url,**kw):
        if 'skills.sh' in url: raise MarketplaceAuthError('401')
        return fixtures(url,**kw)
    fed=FederatedDiscovery([SkillsSH(MarketplaceHTTP(fetch=broken)),AnthropicSkills(http())],staging_root=tmp_path)
    result=fed.search('variant','skill')
    assert len(result['candidates'])==1
    assert 'skills.sh' in result['errors']


class FakeSandbox(DockerSandbox):
    def __init__(self): super().__init__(enforce_rootless=False)
    async def validate(self, path, *, kind):
        return Validation(kind,self.digest_tree(path),True,'mock verification','mock','mock')


class MockLLM:
    def __init__(self): self.calls=0
    async def run(self,request):
        self.calls+=1
        return AgentResult(True,json.dumps({'kind':'skill','files':[{'path':'SKILL.md','content':'fallback'}]}))


def test_marketplace_coordinator_live_path_simulated(tmp_path):
    fed=FederatedDiscovery([SkillsSH(http())],staging_root=tmp_path/'staged')
    registry=CapabilityRegistry()
    builder=SandboxBuilder(BuildStore(tmp_path/'state.sqlite'),FakeSandbox(),registry)
    llm=MockLLM()
    agent=CapabilityAgent(llm,tmp_path/'agent')
    coord=AutonomousBuildCoordinator(tmp_path/'state.sqlite',TrustedCatalog([]),agent,builder,marketplaces=fed)
    req=coord.request('skill.variant','skill','variant QC',request_key='once')
    found=coord.discover_marketplaces(req['id'],query='variant')
    assert len(found['candidates'])==1
    coord.select_market_candidate(req['id'],found['candidates'][0])
    ready=asyncio.run(coord.prepare(req['id']))
    assert ready['state']=='AWAITING_APPROVAL' and llm.calls==0
    assert registry.get('skill.variant') is None
    coord.approve_and_publish(req['id'],approver='reviewer',approve=True)
    assert registry.get('skill.variant') is not None


def test_mcp_market_remains_metadata_only(tmp_path):
    fed=FederatedDiscovery([OfficialMCPRegistry(http())])
    registry=CapabilityRegistry(); sb=SandboxBuilder(BuildStore(tmp_path/'b.db'),FakeSandbox(),registry)
    coord=AutonomousBuildCoordinator(tmp_path/'b.db',TrustedCatalog([]),CapabilityAgent(MockLLM(),tmp_path/'agent'),sb,marketplaces=fed)
    req=coord.request('mcp.variant','mcp','variant lookup',request_key='mcp1')
    result=coord.discover_marketplaces(req['id'])
    with pytest.raises(MarketplaceError): coord.select_market_candidate(req['id'],result['candidates'][0])
    assert registry.get('mcp.variant') is None


def test_selection_requires_discovery_record(tmp_path):
    fed=FederatedDiscovery([SkillsSH(http())],staging_root=tmp_path)
    registry=CapabilityRegistry()
    builder=SandboxBuilder(BuildStore(tmp_path/'s.db'),FakeSandbox(),registry)
    coord=AutonomousBuildCoordinator(tmp_path/'s.db',TrustedCatalog([]),
        CapabilityAgent(MockLLM(),tmp_path/'agent'),builder,marketplaces=fed)
    req=coord.request('skill.need','skill','needed',request_key='k')
    candidate=SkillsSH(http()).search('variant')[0]
    with pytest.raises(MarketplaceError): coord.select_market_candidate(req['id'],candidate)
    coord.discover_marketplaces(req['id'],query='variant')
    forged=MarketCandidate(**{**candidate.to_dict(), 'name': 'forged'})
    with pytest.raises(MarketplaceError): coord.select_market_candidate(req['id'],forged)
    coord.select_market_candidate(req['id'],candidate)


def test_same_repo_multiple_skills_preserved():
    def duplicate_repo(url, *, host, token):
        return {'data':[
            {'id':'owner/repo/skill-one','installUrl':'https://github.com/owner/repo'},
            {'id':'owner/repo/skill-two','installUrl':'https://github.com/owner/repo'}]}
    f=FederatedDiscovery([SkillsSH(MarketplaceHTTP(fetch=duplicate_repo))])
    assert len(f.search('skill','skill')['candidates'])==2
