"""Real SDK/CLI adapters; network/auth are only touched when run() is called.

SDK adapters are optional imports. For untrusted work, launch adapters inside a
separate VM/container/worker service, not in the coordinator process.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from .contracts import AgentRequest, AgentResult
from .reliability import SessionStore, classify_error

class ClaudeSDKRuntime:
    """Claude Agent SDK with explicit tool disablement for planning.

    Writing/executing roles should use a separately isolated worker, not this
    no-tools planning adapter.
    """
    def __init__(self, *, max_turns: int = 4, allow_code_tools: bool = False):
        self.max_turns = max_turns
        self.allow_code_tools = allow_code_tools
        self._tasks: dict[str, asyncio.Task] = {}

    async def run(self, request: AgentRequest) -> AgentResult:
        if self.allow_code_tools and (request.role not in ('executor','capability-builder') or not request.workspace):
            raise PermissionError('Claude code tools require an approved worker role and isolated workspace')
        try:
            from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
        except ImportError as e:
            raise RuntimeError('Install optional dependency: pip install claude-agent-sdk') from e
        tools = ['Read', 'Edit', 'Write', 'Bash'] if self.allow_code_tools else []
        options = ClaudeAgentOptions(
            system_prompt='Follow the caller role and return only requested output.',
            tools=tools, allowed_tools=tools,
            disallowed_tools=['WebFetch','WebSearch'] if self.allow_code_tools else ['Bash','Write','Edit','Read','WebFetch','WebSearch'],
            setting_sources=[], max_turns=self.max_turns,
            resume=request.session_id,
            permission_mode='default',
            cwd=request.workspace or os.getcwd(),
        )
        async def execute():
            result = None
            async for message in query(prompt=request.prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
            if result is None:
                raise RuntimeError('Claude did not return a result')
            if getattr(result, 'is_error', False):
                raise RuntimeError(str(result.result))
            return AgentResult(True, str(result.result or ''), getattr(result, 'session_id', None))
        task = asyncio.create_task(execute())
        self._tasks[request.task_id] = task
        try:
            return await asyncio.wait_for(task, request.timeout)
        finally:
            self._tasks.pop(request.task_id, None)

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.cancel()
        return True

class CodexSDKRuntime:
    """Codex Python SDK via AsyncCodex, sandboxed read_only by default.

    Keeps no long-lived shared process; persisted session IDs permit resumption
    with SDK thread_resume. Cancellation cancels the task; for strict process
    termination use an isolated worker supervisor.
    """
    def __init__(self, *, writable: bool = False):
        self.writable = writable
        self._tasks: dict[str, asyncio.Task] = {}

    async def run(self, request: AgentRequest) -> AgentResult:
        if self.writable and (request.role not in ('executor','capability-builder') or not request.workspace):
            raise PermissionError('Writable Codex requires approved worker role and explicit isolated workspace')
        try:
            from openai_codex import AsyncCodex, Sandbox
        except ImportError as e:
            raise RuntimeError('Install optional dependency: pip install openai-codex') from e
        async def execute():
            async with AsyncCodex() as codex:
                if request.session_id:
                    thread = await codex.thread_resume(request.session_id)
                else:
                    thread = await codex.thread_start(
                        sandbox=Sandbox.workspace_write if self.writable else Sandbox.read_only,
                        cwd=request.workspace,
                    )
                result = await thread.run(request.prompt,
                    sandbox=Sandbox.workspace_write if self.writable else Sandbox.read_only)
                return AgentResult(True, str(result.final_response or ''), str(thread.id or ''))
        task = asyncio.create_task(execute())
        self._tasks[request.task_id] = task
        try:
            return await asyncio.wait_for(task, request.timeout)
        finally:
            self._tasks.pop(request.task_id, None)

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None: return False
        task.cancel()
        return True

class CLIServiceRuntime:
    """Explicitly opted-in CLI adapter (one subprocess per task).

    Never inserts prompt in shell command; uses stdin, process group kill on
    timeout/cancel. Arguments are fixed by operator, not model-generated.
    """
    def __init__(self, kind: str, *, enabled: bool = False, cwd: str | None = None):
        if kind not in ('claude', 'codex'):
            raise ValueError('Unsupported CLI')
        self.kind = kind
        self.enabled = enabled
        self.cwd = cwd
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    async def run(self, request: AgentRequest) -> AgentResult:
        if not self.enabled:
            raise PermissionError('CLI runtime is opt-in only')
        if request.session_id:
            raise NotImplementedError('CLI resume is not implemented; use SDK runtime for session recovery')
        # codex exec reads prompt from stdin when '-' is supplied
        argv = (['claude', '-p', '--output-format', 'json', '--tools', ''] if self.kind == 'claude' and request.role=='planner'
                else ['claude', '-p', '--output-format', 'json'] if self.kind == 'claude'
                else ['codex', 'exec', '--sandbox', 'read-only', '--json', '-'])
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.cwd or request.workspace,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        self._procs[request.task_id] = proc
        try:
            out, err = await asyncio.wait_for(proc.communicate(request.prompt.encode()), request.timeout)
            if proc.returncode != 0:
                raise RuntimeError(f'{self.kind} exited {proc.returncode}: {err.decode(errors="replace")[-2000:]}')
            return AgentResult(True, out.decode(errors='replace')[-100000:])
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await self._terminate(proc)
            raise
        finally:
            self._procs.pop(request.task_id, None)

    @staticmethod
    async def _terminate(proc):
        if proc.returncode is None:
            try:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

    async def cancel(self, task_id: str) -> bool:
        proc = self._procs.get(task_id)
        if not proc: return False
        await self._terminate(proc)
        return True

class SDKWorker:
    """Sync adapter to v0.3 ParallelScheduler; each call has isolated task ID."""
    def __init__(self, runtime, role='executor', workspace=None,
                 session_store: SessionStore | None = None, runtime_name: str | None = None):
        self.runtime, self.role, self.workspace = runtime, role, workspace
        self.session_store = session_store
        self.runtime_name = runtime_name or type(runtime).__name__

    def run(self, task, capabilities):
        previous = self.session_store.get(task.id) if self.session_store else None
        if previous and previous['status'] == 'COMPLETED':
            return previous['result'] or ''
        attempt_id, session_id = (self.session_store.begin(task.id, self.runtime_name)
                                  if self.session_store else (None, None))
        request = AgentRequest(task.id, self.role, task.prompt, self.workspace,
                               tuple(c.id for c in capabilities), session_id=session_id)
        try:
            result = asyncio.run(self.runtime.run(request))
            if not result.success:
                raise RuntimeError(result.text)
            if self.session_store:
                self.session_store.checkpoint(task.id, attempt_id,
                    session_id=result.session_id, status='COMPLETED', result=result.text)
            return result.text
        except BaseException:
            if self.session_store:
                self.session_store.checkpoint(task.id, attempt_id,
                    session_id=session_id, status='UNCERTAIN')
            raise
