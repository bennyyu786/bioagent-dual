"""Restrictive Docker validation with injectable process runner for offline tests.

A standalone rootless Docker daemon is a deployment prerequisite. This module
must NOT receive privileged Docker socket access from untrusted agents.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import uuid
from .contracts import BuildError

@dataclass(frozen=True)
class Validation:
    kind: str
    digest: str
    passed: bool
    logs: str
    container_id: str
    image: str

class DockerSandbox:
    def __init__(self, *, image: str = 'biomni-sandbox:py311', docker: str = 'docker',
                 timeout: float = 120, runner=None, enforce_rootless: bool = True):
        self.image, self.docker, self.timeout = image, docker, timeout
        self.runner, self.enforce_rootless = runner, enforce_rootless

    @staticmethod
    def digest_tree(folder: str | Path) -> str:
        root = Path(folder).resolve(strict=True)
        if not root.is_dir(): raise BuildError('Candidate must be directory')
        h = hashlib.sha256()
        files = sorted(x for x in root.rglob('*') if x.is_file() or x.is_symlink())
        if len(files) > 1000: raise BuildError('Too many files')
        for path in files:
            if path.is_symlink(): raise BuildError('Symlinks are forbidden in candidates')
            relative = path.relative_to(root).as_posix().encode()
            if path.stat().st_size > 10*1024*1024: raise BuildError('Oversized file')
            h.update(len(relative).to_bytes(4,'big'))
            h.update(relative)
            h.update(hashlib.sha256(path.read_bytes()).digest())
        return h.hexdigest()

    async def _run_command(self, argv: list[str], stdin: bytes = b'', timeout=None):
        if self.runner is not None:
            return await self.runner(argv, stdin)
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
              stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout or self.timeout)
            return proc.returncode, out.decode(errors='replace')[-30000:], err.decode(errors='replace')[-30000:]
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                await proc.wait()
            raise

    async def _assert_rootless(self):
        if not self.enforce_rootless: return
        rc, out, err = await self._run_command([self.docker,'info','--format','{{json .SecurityOptions}}'])
        if rc != 0 or 'rootless' not in out.lower():
            raise BuildError('Sandbox requires a dedicated rootless Docker daemon')

    def _args(self, path: Path, cid: str, kind: str) -> list[str]:
        if not re.fullmatch(r'biomni-v04-[a-f0-9]{24}', cid):
            raise BuildError('Invalid generated container identifier')
        test_command = (['python','-m','pytest','-q','-p','no:cacheprovider','/workspace/tests']
                        if kind=='skill' else ['python','/harness/mcp_probe.py'])
        # Image must contain pytest, MCP Python SDK, and harness/mcp_probe.py.
        return [self.docker,'run','--name',cid,'--label','biomni.dual.sandbox=v04','--network','none','--read-only',
                '--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--pids-limit','128','--memory','1g','--cpus','1',
                '--user','10001:10001','--tmpfs','/tmp:rw,nosuid,size=128m',
                '--mount',f'type=bind,src={path},dst=/workspace,readonly',
                '-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTEST_DISABLE_PLUGIN_AUTOLOAD=1',
                self.image,*test_command]

    async def cleanup_orphans(self) -> int:
        """Remove only containers created by our own labeled sandbox controller."""
        await self._assert_rootless()
        rc,out,err=await self._run_command([self.docker,'ps','-aq','--filter','label=biomni.dual.sandbox=v04'])
        if rc!=0: raise BuildError('Could not enumerate sandbox containers')
        count=0
        for cid in out.splitlines():
            cid=cid.strip()
            if not re.fullmatch('[0-9a-f]{12,64}',cid):
                raise BuildError('Unexpected container ID from daemon')
            rc,_,_=await self._run_command([self.docker,'rm','-f',cid],timeout=15)
            if rc==0: count+=1
        return count

    async def validate(self, path: str | Path, *, kind: str) -> Validation:
        if kind not in ('skill','mcp'): raise ValueError('kind must be skill or mcp')
        folder = Path(path).resolve(strict=True)
        if kind=='skill':
            if not (folder/'SKILL.md').is_file() or not (folder/'tests').is_dir():
                raise BuildError('Skill requires SKILL.md and tests/')
        else:
            if not (folder/'mcp_manifest.json').is_file(): raise BuildError('MCP requires manifest')
            manifest = json.loads((folder/'mcp_manifest.json').read_text())
            if not isinstance(manifest,dict) or not isinstance(manifest.get('argv'),list):
                raise BuildError('MCP manifest requires argv')
            if not manifest['argv'] or any(not isinstance(s,str) or not s for s in manifest['argv']):
                raise BuildError('Invalid MCP argv')
            # no absolute binary commands or parent traversals; run from /workspace
            if any('..' in x or x.startswith('/') for x in manifest['argv']):
                raise BuildError('Unsafe MCP manifest argv')
        digest = self.digest_tree(folder)
        await self._assert_rootless()
        cid = 'biomni-v04-'+uuid.uuid4().hex[:24]
        args = self._args(folder, cid, kind)
        try:
            rc,out,err = await self._run_command(args)
        except Exception:
            raise
        finally:
            # Always attempt container cleanup, including when Docker CLI dies.
            try:
                await self._run_command([self.docker,'rm','-f',cid], timeout=15)
            except Exception:
                pass
        if self.digest_tree(folder) != digest:
            raise BuildError('Candidate changed during validation')
        return Validation(kind,digest,rc==0,(out+'\n'+err)[-30000:],cid,self.image)
