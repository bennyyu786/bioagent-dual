"""Synthetic demo only. No real biomedical outcomes or remote tools are used."""
import hashlib
import json
import tempfile
from pathlib import Path

from biomni_dual import (
    ActiveExperimentStore, ActiveExperimentCoordinator, ExperimentPriorityPolicy,
    JointExperienceStore, RepairContext, Diagnosis, FailureRoute, TrialResult,
    AuditLog,
)


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        evidence_root = root/'trusted'; evidence_root.mkdir()
        store = ActiveExperimentStore(root/'trials.sqlite', evidence_root=evidence_root,
                                      trusted_verifiers=('sandbox-independent-checker',))
        context = RepairContext('multiomics','capability_unavailable_or_broken','skill.causal')
        proposal = ActiveExperimentCoordinator(store).propose_for_failure(
            Diagnosis(FailureRoute.REPAIR_CAPABILITY,'capability_unavailable_or_broken',False,'task-1',1),
            context,experiment_id='pilot-causal-001',max_units=12,max_total_cost=24,max_cost_per_trial=2)
        priority = ExperimentPriorityPolicy(JointExperienceStore(root/'history.sqlite'))
        print('proposed:', proposal.id, 'priority:', priority.assess(proposal,remaining_budget=24).reason)
        print('state:',store.get(proposal.id)[1].value)
        store.transition(proposal.id,approve=True,approver='human-reviewer')
        for i in range(12):
            assignment = store.assign(proposal.id,f'synthetic-{i}')
            # Synthetic outcomes to demonstrate report generation only.
            accepted = assignment.route == FailureRoute.REPLAN_TASK
            values = dict(experiment_id=proposal.id,unit_id=assignment.unit_id,
                          route=assignment.route.value,protocol_version=proposal.protocol_version,
                          verifier_id='sandbox-independent-checker',accepted=accepted)
            artifact = evidence_root/f'check-{i}.json'
            artifact.write_text(json.dumps(values),encoding='utf-8')
            result = TrialResult(proposal.id,assignment.unit_id,assignment.route,accepted,1.0,0.5,
                                 'sandbox-independent-checker',str(artifact),
                                 hashlib.sha256(artifact.read_bytes()).hexdigest())
            store.record(result)
        report = store.report(proposal.id)
        print('arms:', report.control.completed, report.treatment.completed)
        print('pilot decision:',report.decision)
        print('risk difference:',report.observed_difference)
        print('production rollout authorized:',report.production_rollout_authorized)
        print('audit verified:',AuditLog(root/'trials.sqlite').verify())
        store.close(proposal.id)
        print('final state:',store.get(proposal.id)[1].value)


if __name__ == '__main__':
    main()
