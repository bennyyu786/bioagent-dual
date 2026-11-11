"""Read-only environment acceptance checks; no remote code execution."""
from __future__ import annotations
import argparse
import importlib.util
import json
import shutil
import urllib.error
import urllib.request


def check(url: str):
    result={"python_sdk":{"claude_agent_sdk":bool(importlib.util.find_spec('claude_agent_sdk')),
                           "openai_codex":bool(importlib.util.find_spec('openai_codex'))},
            "executables":{name:bool(shutil.which(name)) for name in ('docker','claude','codex')},
            "health":{}}
    for path in ('/health/live','/health/ready','/metrics'):
        try:
            with urllib.request.urlopen(url.rstrip('/')+path, timeout=3) as response:
                result['health'][path]={'code':response.status,'bytes':len(response.read(10000))}
        except (urllib.error.URLError,TimeoutError) as e:
            result['health'][path]={'error':type(e).__name__}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return int(any('error' in result['health'][p] or result['health'][p].get('code')!=200 for p in result['health']))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--url',default='http://127.0.0.1:8765');a=p.parse_args()
    raise SystemExit(check(a.url))
