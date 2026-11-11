"""No-tools LLM candidate generator; privileged code execution happens only in Docker.

Model output is data, not a shell command. Only the controller writes staging files.
"""
from __future__ import annotations
import json
import re
import tempfile
from pathlib import Path, PurePosixPath
from .contracts import AgentRequest, BuildError

class CapabilityAgent:
    def __init__(self, runtime, staging_root: str | Path, *, max_bytes: int = 100000):
        self.runtime = runtime
        self.staging_root = Path(staging_root).resolve()
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    @staticmethod
    def _parse(text: str, kind: str) -> list[tuple[str,str]]:
        text=text.strip()
        if text.startswith('```'):
            text=re.sub(r'^```(?:json)?\s*|\s*```$', '', text).strip()
        try:
            payload=json.loads(text)
        except (TypeError,ValueError) as exc:
            raise BuildError('Builder must return a JSON object') from exc
        if not isinstance(payload,dict) or set(payload)!={'kind','files'} or payload['kind']!=kind:
            raise BuildError('Invalid candidate schema')
        files=payload['files']
        if not isinstance(files,list) or not 1<=len(files)<=40:
            raise BuildError('Invalid file list')
        parsed=[]; seen=set()
        for entry in files:
            if not isinstance(entry,dict) or set(entry)!={'path','content'}:
                raise BuildError('Invalid file schema')
            p,c=entry['path'],entry['content']
            if not isinstance(p,str) or not isinstance(c,str):
                raise BuildError('Expected textual file')
            rel=PurePosixPath(p)
            if (p.startswith('./') or '//' in p or rel.is_absolute() or len(rel.parts)>8 or not rel.parts or
                any(part in ('..','.') or part.startswith('.') for part in rel.parts) or
                '\\' in p or '\x00' in p or len(p)>180 or p in seen):
                raise BuildError('Unsafe file path')
            if any(ord(x)<32 and x not in '\n\r\t' for x in p):
                raise BuildError('Invalid path character')
            seen.add(p);parsed.append((p,c))
        names={p for p,_ in parsed}
        if kind=='skill' and ('SKILL.md' not in names or not any(x.startswith('tests/test_') for x in names)):
            raise BuildError('Skill requires SKILL.md and tests/test_*.py')
        if kind=='mcp' and ('mcp_manifest.json' not in names or not any(x.endswith('.py') for x in names)):
            raise BuildError('MCP requires manifest and server code')
        return parsed

    async def propose(self, capability_id: str, kind: str, requirement: str) -> Path:
        if kind not in ('skill','mcp'):
            raise ValueError('Invalid capability kind')
        prompt=(f'Generate a minimal {kind} implementing this requirement: {requirement}\n'
                'Respond ONLY JSON {"kind":"'+kind+'","files":[{"path":"...","content":"..."}]}. '
                'Include self-contained tests and no external network dependencies. '
                'For MCP include mcp_manifest.json with expected_tools and test_calls. '
                'Avoid secrets, nonstandard dependencies, and destructive operations.')
        result=await self.runtime.run(AgentRequest('builder-'+capability_id,'capability-builder',prompt))
        if not result.success: raise BuildError(result.text)
        parsed=self._parse(result.text,kind)
        total=sum(len(content.encode('utf8')) for _,content in parsed)
        if total>self.max_bytes: raise BuildError('Candidate exceeds size limit')
        # Temporary workspace is created by trusted coordinator, with fresh unique name.
        folder=Path(tempfile.mkdtemp(prefix='candidate-',dir=self.staging_root))
        folder.chmod(0o755)  # rootless container test UID can traverse
        for rel,content in parsed:
            dest=folder/rel
            dest.parent.mkdir(parents=True,exist_ok=True)
            for parent in [dest.parent,*list(dest.parent.parents)]:
                if parent==folder.parent: break
                parent.chmod(0o755)
            dest.write_text(content,encoding='utf-8')
            dest.chmod(0o644)
        return folder
