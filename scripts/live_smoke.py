"""Explicit real-environment smoke tests. May consume hosted LLM API usage.

Run only on a dedicated Linux host with a ROOTLESS Docker daemon installed and
configured, and after authenticating the chosen agent SDK yourself.
"""
from __future__ import annotations
import argparse
import asyncio
from pathlib import Path
from biomni_dual import AgentRequest, ClaudeSDKRuntime, CodexSDKRuntime, DockerSandbox

ROOT=Path(__file__).resolve().parents[1]

async def go(which):
    if which in ('docker','all'):
        sandbox=DockerSandbox()
        for name, path,kind in [
            ('skill',ROOT/'examples'/'skill_candidate','skill'),
            ('mcp',ROOT/'examples'/'mcp_candidate','mcp')]:
            result=await sandbox.validate(path,kind=kind)
            print(f'REAL Docker {name}: {result.passed}; digest={result.digest}')
            if not result.passed:
                print(result.logs)
                raise SystemExit(1)
    if which in ('claude','all'):
        result=await ClaudeSDKRuntime().run(AgentRequest('live-claude','planner',
             'Respond with the exact word OK and nothing else.',str(ROOT),timeout=120))
        print('REAL Claude SDK:',repr(result.text[:150]))
        if not result.success: raise SystemExit(1)
    if which in ('codex','all'):
        result=await CodexSDKRuntime().run(AgentRequest('live-codex','planner',
             'Respond with the exact word OK and nothing else.',str(ROOT),timeout=120))
        print('REAL Codex SDK:',repr(result.text[:150]))
        if not result.success: raise SystemExit(1)

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--check',choices=['docker','claude','codex','all'],required=True)
    args=p.parse_args()
    asyncio.run(go(args.check))
