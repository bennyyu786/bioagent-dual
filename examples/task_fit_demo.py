"""Offline task-first capability matching; no external market/Agent calls."""
from biomni_dual import (Capability, CapabilityKind, CapabilityRegistry,
                         TaskRequirements, Requirement, TaskFitCoordinator)

registry=CapabilityRegistry()
registry.register(Capability('biomni.qc','genomics quality control',CapabilityKind.TOOL,
    cost=0.5, metadata={'provides':['genomic.qc'], 'input_schema':'vcf.v1',
                        'output_schema':'qc.report.v1', 'license':'MIT'}))
registry.register(Capability('biomni.qc.duplicate','duplicated QC',CapabilityKind.TOOL,
    cost=10, metadata={'provides':['genomic.qc'], 'input_schema':'vcf.v1',
                        'output_schema':'qc.report.v1', 'license':'MIT'}))

requirements=TaskRequirements('research-001',(Requirement('genomic.qc'),),
    input_schema='vcf.v1',output_schema='qc.report.v1',allowed_licenses=('MIT',),max_cost=2)
result=TaskFitCoordinator(registry).evaluate(requirements)
print('task:',result.task_id)
print('selected:',result.selected)
print('coverage:',result.coverage)
print('external market required:',result.search_external)

need=TaskRequirements('research-002',(Requirement('genomic.qc'),Requirement('causal.longitudinal')),
    input_schema='vcf.v1',output_schema='qc.report.v1')
result2=TaskFitCoordinator(registry).evaluate(need)
print('second task missing:',result2.gaps)
print('market search recommended:',result2.search_external)
