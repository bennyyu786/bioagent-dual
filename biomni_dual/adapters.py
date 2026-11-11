"""Opt-in subprocess-based CLI worker adapter.

No subprocess is ever started by discovery or importing this module.
Set an explicit, trusted executable path and turn allow_execution=True to run.
Use container/VM isolation for untrusted model-generated commands or data.
"""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
from .models import Task, Capability

class CLIWorker:
    def __init__(self, executable: str | Path, args: tuple[str, ...] = (), *,
                 allow_execution: bool = False, timeout: int = 300, cwd: str | Path | None = None):
        self.executable = str(executable)
        self.args = tuple(args)
        self.allow_execution = allow_execution
        self.timeout = timeout
        self.cwd = str(cwd) if cwd else None

    def run(self, task: Task, capabilities: list[Capability]) -> str:
        if not self.allow_execution:
            raise PermissionError('CLI execution requires explicit allow_execution=True')
        payload = json.dumps({'task_id': task.id, 'prompt': task.prompt,
                              'capabilities': [c.id for c in capabilities]})
        # No shell, and user prompts are passed only as stdin, never interpolated into argv.
        proc = subprocess.run([self.executable, *self.args], input=payload, text=True,
                              capture_output=True, check=False, timeout=self.timeout, cwd=self.cwd)
        if proc.returncode != 0:
            raise RuntimeError(f'Worker exit {proc.returncode}: {proc.stderr[:1000]}')
        return proc.stdout[:1000000]
