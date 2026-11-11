"""Independent artifact integrity and scientific-rule validation.

The validator deliberately does not trust worker-provided scores or paths as
proof of correctness. An external assessor must provide domain test results.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import Task


@dataclass(frozen=True)
class ArtifactEvidence:
    path: str  # relative to the configured evidence root
    sha256: str
    check_id: str


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    passed: bool
    evidence: ArtifactEvidence
    explanation: str = ''


class DomainAssessor(Protocol):
    async def assess(self, task: Task, output: str) -> tuple[CheckResult, ...]: ...


class EvidenceAwareValidator:
    """Checks all mandatory rule IDs and verifies real referenced file bytes.

    The assessor should be independently controlled (separate test fixtures and
    code review). File hashes confirm artifact integrity, not scientific truth.
    """
    def __init__(self, root: str | Path, assessor: DomainAssessor,
                 *, required_checks: tuple[str, ...], max_artifact_bytes: int = 10_000_000):
        self.root=Path(root).resolve(strict=True)
        if not self.root.is_dir() or not required_checks or len(set(required_checks)) != len(required_checks):
            raise ValueError('Invalid evidence root or required checks')
        if not all(isinstance(x,str) and x.strip() for x in required_checks):
            raise ValueError('Invalid check identifiers')
        if max_artifact_bytes <= 0: raise ValueError('Invalid artifact size limit')
        self.assessor,self.required_checks,self.max_artifact_bytes=assessor,required_checks,max_artifact_bytes

    def _validate_artifact(self, artifact: ArtifactEvidence) -> bool:
        if not isinstance(artifact,ArtifactEvidence): return False
        p=Path(artifact.path)
        if p.is_absolute() or '..' in p.parts or not p.parts or '\\' in artifact.path:
            return False
        try:
            target=(self.root/p).resolve(strict=True)
            if not target.is_relative_to(self.root) or not target.is_file(): return False
            if target.stat().st_size > self.max_artifact_bytes: return False
            sha=hashlib.sha256()
            with target.open('rb') as stream:
                while True:
                    chunk=stream.read(65536)
                    if not chunk: break
                    sha.update(chunk)
            return len(artifact.sha256)==64 and sha.hexdigest()==artifact.sha256.lower()
        except (OSError,ValueError,RuntimeError):
            return False

    async def validate(self, task: Task, output: str) -> tuple[bool,str]:
        checks=self.assessor.assess(task,output)
        if inspect.isawaitable(checks): checks=await checks
        if not isinstance(checks,(tuple,list)): return False,'invalid_assessor_result'
        seen=set()
        for check in checks:
            if not isinstance(check,CheckResult) or not isinstance(check.passed,bool):
                return False,'invalid_check_result'
            if check.check_id in seen or check.check_id not in self.required_checks:
                return False,'unexpected_or_duplicate_check'
            seen.add(check.check_id)
            if not isinstance(check.evidence,ArtifactEvidence) or check.check_id != check.evidence.check_id or not self._validate_artifact(check.evidence):
                return False,'unverified_artifact:'+check.check_id
            if not check.passed:
                return False,'check_failed:'+check.check_id
        if seen != set(self.required_checks):
            return False,'missing_required_checks:'+','.join(sorted(set(self.required_checks)-seen))
        return True,'all_independent_checks_passed'
