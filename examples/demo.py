"""Run with: python examples/demo.py (after pip install -e .). No model, network or Biomni required."""
from tempfile import TemporaryDirectory
from pathlib import Path
from biomni_dual import Capability, CapabilityKind, CapabilityRegistry, TaskCenter, DualCenter

class DemoWorker:
    def run(self, task, capabilities):
        print('EXECUTING', task.id, [c.id for c in capabilities])
        return f"Demo result for: {task.prompt}"

with TemporaryDirectory() as d:
    registry = CapabilityRegistry()
    registry.register(Capability('tool.qc', 'quality control of omics data', CapabilityKind.TOOL, tags=('omics', 'qc')))
    tasks = TaskCenter(Path(d) / 'tasks.db')
    a = tasks.submit('omics QC', task_id='qc')
    b = tasks.submit('omics integration', depends_on=[a.id], task_id='integration')
    engine = DualCenter(tasks, registry, DemoWorker(), approval_for_unmatched=False)
    engine.run_until_idle()
    for t in tasks.list():
        print(t.id, t.status.value, t.result)
