"""Offline example: independently supplied synthetic outcomes affect advice.

No LLM, Docker, MCP, or biomedical experiment is run here.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from uuid import uuid4

from biomni_dual import (Diagnosis, FailureRoute, JointExperienceStore,
                         RepairContext, RepairOutcome, RepairStrategyPolicy, AuditLog)


def main():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        artifact = root / 'simulated-independent-check.json'
        artifact.write_text('{"source":"offline-test-fixture"}', encoding='utf-8')
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        store = JointExperienceStore(root/'experience.sqlite')
        ctx = RepairContext('longitudinal_omics','capability_unavailable_or_broken','skill.causal')
        diagnosis = Diagnosis(FailureRoute.REPAIR_CAPABILITY, ctx.error_reason, False, 'task-demo', 1)
        policy = RepairStrategyPolicy(store, min_samples=4)
        before = policy.advise(diagnosis,ctx, approved_routes=[FailureRoute.REPLAN_TASK])
        for index in range(6):
            for route, success in [(FailureRoute.REPAIR_CAPABILITY,False),
                                   (FailureRoute.REPLAN_TASK,True)]:
                store.record(RepairOutcome(
                    f'{route.value}-{index}',ctx,route,success,'independent-validator',
                    digest, 2.0, 4.0,str(artifact)))
        after = policy.advise(diagnosis,ctx, approved_routes=[FailureRoute.REPLAN_TASK])
        print('before:', before.recommended.value)
        print('after:', after.recommended.value)
        print('requires explicit approval:', after.requires_approval)
        print('samples:', [(e.route.value,e.samples,round(e.posterior_success,3)) for e in after.evidence])
        print('audit valid:', AuditLog(root/'experience.sqlite').verify())
        print('SIMULATED DATA ONLY; never infer real treatment efficacy from this example')


if __name__ == '__main__':
    main()
