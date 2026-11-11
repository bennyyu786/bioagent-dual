"""Example using installed Biomni; can execute LLM-produced code. Opt in knowingly."""
from biomni.agent import A1
from biomni_dual import Capability, CapabilityKind, CapabilityRegistry, TaskCenter, DualCenter, BiomniWorker

registry = CapabilityRegistry()
registry.register(Capability('tool.biomni', 'biomedical research tools and analysis', CapabilityKind.TOOL))
tasks = TaskCenter('research_jobs.sqlite3')
tasks.submit('Analyze the biomedical question with relevant Biomni tools; cite limitations.')
# Explicitly restrict automatic data download by passing expected_data_lake_files=[]
worker = BiomniWorker(lambda: A1(expected_data_lake_files=[]))
engine = DualCenter(tasks, registry, worker)
for item in engine.run_until_idle(max_steps=1):
    print(item.id, item.status.value, item.result or item.error)
