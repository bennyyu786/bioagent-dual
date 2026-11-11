# Unified package layout

All runtime code now lives directly under `biomni_dual/`. No version-specific Python subpackages are installed.

- `dag.py`, `planning.py`, `planner.py`, `replanning.py`: task DAG and LLM planning
- `registry.py`, `skills.py`, `mcp.py`, `capabilities.py`, `capability_agent.py`: capability inventory and proposals
- `runtime.py`, `runtimes.py`, `adapters.py`: scheduling and model/CLI worker adapters
- `builder.py`, `sandbox.py`: isolated validation and controlled capability publication
- `workflow.py`, `engine.py`, `events.py`: orchestration and feedback

Older tests were retained and re-labeled by behavior rather than release number. External SDK and Docker smoke tests still require installed software and credentials.
