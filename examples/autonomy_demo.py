"""Offline v0.5 demonstration, deliberately uses fake LLM and sandbox.

Replace MockSandbox with DockerSandbox on dedicated rootless Docker host;
replace MockLLM with an authenticated Claude/Codex SDK runtime.
"""
import asyncio
import json
from pathlib import Path
import tempfile

from biomni_dual import (AgentResult, AutonomousBuildCoordinator, BuildStore,
    CapabilityAgent, CapabilityRegistry, SandboxBuilder, TaskCenter, TrustedCatalog)
from biomni_dual.sandbox import DockerSandbox, Validation


class MockLLM:
    async def run(self, request):
        return AgentResult(True,json.dumps({'kind':'skill','files':[
            {'path':'SKILL.md','content':'---\nname: variant-qc\ndescription: mock variant QC\n---\n# Variant QC'},
            {'path':'tests/test_qc.py','content':'def test_qc():\n    assert 1 + 1 == 2\n'}]}))


class MockSandbox(DockerSandbox):
    async def validate(self, path, *, kind):
        return Validation(kind,self.digest_tree(path),True,'MOCK TEST PASS', 'mock-container', 'mock-image')


async def main():
    with tempfile.TemporaryDirectory(prefix='biomni-v05-demo-') as temp:
        root=Path(temp)
        registry=CapabilityRegistry()
        tasks=TaskCenter(root/'tasks.sqlite3')
        task=tasks.submit('Variant QC using skill',required_capabilities=['skill.variant_qc'])
        tasks.require_approval(task.id)
        builder=SandboxBuilder(BuildStore(root/'builds.sqlite3'),MockSandbox(),registry)
        coordinator=AutonomousBuildCoordinator(root/'builds.sqlite3',TrustedCatalog([]),
            CapabilityAgent(MockLLM(),root/'candidates'),builder,tasks)
        req=coordinator.request('skill.variant_qc','skill','Variant quality control tests',
                                request_key='variant-qc-v1',task_id=task.id)
        print('requested:',req['state'])
        review=await coordinator.prepare(req['id'])
        print('validated:',review['state'])
        print('task before approval:',tasks.get(task.id).status.value)
        result=coordinator.approve_and_publish(req['id'],approver='example-operator',approve=True)
        print('released:',result['state'])
        print('task after approval:',tasks.get(task.id).status.value)
        print('registered:',registry.get('skill.variant_qc').id)
        print('NOTE: Docker and LLM are mocked; this is not a live sandbox result.')

if __name__=='__main__':
    asyncio.run(main())
