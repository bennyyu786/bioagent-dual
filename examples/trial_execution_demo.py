"""Offline Docker-transport simulation. No real containers or biology data."""
import asyncio
import tempfile
from pathlib import Path

from biomni_dual import (
    ActiveExperimentStore, ExperimentPlan, RepairContext, FailureRoute,
    TrialMode, TrialExecutor, TrialExecutionLedger, PilotLearningView,
    DockerSandbox, AuditLog,
)


async def main():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        evidence = root/'evidence'; evidence.mkdir()
        store=ActiveExperimentStore(root/'experiments.db',evidence_root=evidence,
                                    trusted_verifiers=('independent-docker-checker',))
        plan=ExperimentPlan('demo-067',RepairContext('research','bad_capability','skill.qc'),
                            FailureRoute.REPAIR_CAPABILITY,FailureRoute.REPLAN_TASK,
                            mode=TrialMode.SANDBOX,max_units=8,max_total_cost=8,max_cost_per_trial=1)
        store.propose(plan)
        store.transition(plan.id,approve=True,approver='demo-human')
        candidates={}
        for arm in (plan.control,plan.treatment):
            path=root/arm.value
            (path/'tests').mkdir(parents=True)
            (path/'SKILL.md').write_text('---\nname: demo\ndescription: offline trial\n---\n')
            (path/'tests'/'test_example.py').write_text('def test_example(): assert True\n')
            candidates[arm]=path

        async def fake_docker(argv,stdin):
            if argv[1]=='info':return 0,'["name=rootless"]',''
            if argv[1]=='run':return 0,'1 passed',''
            if argv[1]=='rm':return 0,'',''
            return 1,'','unexpected command'

        async def independent_checker(assignment,validation):
            return validation.passed and bool(validation.digest)

        ledger=TrialExecutionLedger(root/'execution.db')
        executor=TrialExecutor(experiments=store,ledger=ledger,
            sandbox=DockerSandbox(runner=fake_docker), candidates=candidates,
            verifier_id='independent-docker-checker',independent_checker=independent_checker)
        for i in range(8):
            receipt=await executor.execute(plan.id,f'unit-{i}')
            assert receipt.state.value=='recorded'

        report=store.report(plan.id)
        pilots=PilotLearningView(store).estimates(plan.id)
        print('completed trials:',report.control.completed+report.treatment.completed)
        print('pilot posterior:',[(p.route.value,round(p.posterior_mean,3)) for p in pilots])
        print('production rollout authorized:',report.production_rollout_authorized)
        print('audit valid:',AuditLog(root/'experiments.db').verify() and AuditLog(root/'execution.db').verify())
        print('environment: fake Docker transport; no live SDK or real biomedical result')


if __name__=='__main__':
    asyncio.run(main())
