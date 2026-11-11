"""LLM planner: structured proposal, strict validation, atomic DAG commit."""
from __future__ import annotations
import json
import re
from .contracts import AgentRequest, PlanError
from .dag import DAGPatch, DAGVersionManager

class LLMPlanner:
    def __init__(self, runtime, dag: DAGVersionManager, *, max_repair: int = 1):
        self.runtime, self.dag, self.max_repair = runtime, dag, max_repair

    @staticmethod
    def parse(text: str) -> DAGPatch:
        stripped = text.strip()
        if stripped.startswith('```'):
            stripped = re.sub(r'^```(?:json)?\s*|\s*```$', '', stripped).strip()
        try:
            obj = json.loads(stripped)
        except (ValueError, TypeError) as exc:
            raise PlanError('Planner output must be a JSON object') from exc
        if not isinstance(obj, dict) or set(obj) - {'additions','dependencies','reason','recovery'}:
            raise PlanError('Invalid top-level plan fields')
        additions = obj.get('additions', [])
        dependencies = obj.get('dependencies', [])
        if not isinstance(additions, list) or not isinstance(dependencies, list):
            raise PlanError('Expected lists')
        if len(additions) + len(dependencies) > 100 or not additions and not dependencies:
            raise PlanError('Empty or oversized plan')
        used = set()
        for n in additions:
            if not isinstance(n, dict) or set(n) - {'key','prompt','depends_on','required_capabilities','priority','tenant','requires_approval'}:
                raise PlanError('Unknown task field')
            if not isinstance(n.get('key'), str) or not re.fullmatch('[a-zA-Z0-9_:-]{1,80}', n['key']):
                raise PlanError('Invalid task key')
            if n['key'] in used: raise PlanError('Duplicate task key')
            used.add(n['key'])
            if not isinstance(n.get('prompt'),str) or not n['prompt'].strip():
                raise PlanError('Missing prompt')
            for k in ('depends_on','required_capabilities'):
                if not isinstance(n.get(k, []), list) or not all(isinstance(s,str) and s for s in n.get(k, [])):
                    raise PlanError(f'Invalid {k}')
            if not isinstance(n.get('priority', 0), int): raise PlanError('priority must be integer')
            if not isinstance(n.get('requires_approval', False), bool): raise PlanError('requires_approval must be bool')
        if any(not isinstance(edge, list) or len(edge)!=2 or not all(isinstance(v,str) for v in edge) for edge in dependencies):
            raise PlanError('Invalid dependency edge')
        recovery=obj.get('recovery')
        if recovery is not None and (not isinstance(recovery,list) or len(recovery)!=2 or
            not all(isinstance(x,str) and x for x in recovery)):
            raise PlanError('Invalid recovery pair')
        return DAGPatch(tuple(additions), tuple(tuple(x) for x in dependencies),
                        str(obj.get('reason','llm_plan'))[:2000],tuple(recovery) if recovery else None)

    async def submit_goal(self, goal: str, *, session_id: str | None = None) -> dict:
        prompt = ('You are a task planner. Return ONLY JSON with keys additions (array of '
                  '{key,prompt,depends_on,required_capabilities,priority,tenant}), '
                  'dependencies (array of [task_id,prerequisite_id]), reason. '
                  'Use topological order: prerequisite steps earlier. No execution, no markdown.\n'
                  'Goal: '+goal)
        error = None
        for attempt in range(self.max_repair+1):
            response = await self.runtime.run(AgentRequest('planner-'+str(attempt), 'planner', prompt,
                                                            session_id=session_id))
            if not response.success:
                raise PlanError(response.text)
            try:
                patch = self.parse(response.text)
                mapping = self.dag.commit(patch)
                return {'task_ids': mapping, 'version': self.dag.versions()[-1]['version'],
                        'session_id': response.session_id}
            except (ValueError, KeyError) as exc:
                error = str(exc)
                prompt += '\nThe previous proposal was rejected: '+error+'\nReturn a corrected JSON proposal.'
                session_id = response.session_id
        raise PlanError('Planner exceeded bounded repairs: '+str(error))

    async def replan(self, *, event: str, task_state: list[dict], max_tasks: int = 20) -> dict:
        """Event-driven graph proposal; immutable task states enforced by DAGVersionManager.

        Caller controls event payload; no permission to mutate existing running/completed jobs.
        """
        if len(task_state)>1000:
            raise PlanError('Oversized task state')
        prompt=('A task workflow changed. Return ONLY JSON DAGPatch with additions, '
                'dependencies and reason. Do not alter completed or running jobs. '
                'New tasks must reference existing ids or earlier new task keys. '
                'Add at most '+str(max_tasks)+' steps.\nEvent: '+event[:3000]+
                '\nCurrent state: '+json.dumps(task_state,ensure_ascii=False)[:30000])
        error=None
        for i in range(self.max_repair+1):
            response=await self.runtime.run(AgentRequest('replan-'+str(i),'planner',prompt))
            if not response.success: raise PlanError(response.text)
            try:
                patch=self.parse(response.text)
                if len(patch.additions)>max_tasks: raise PlanError('Exceeded replan budget')
                mapping=self.dag.commit(patch)
                return {'task_ids':mapping,'version':self.dag.versions()[-1]['version']}
            except (ValueError,KeyError) as exc:
                error=str(exc)
                prompt+='\nRejected: '+error+'; return corrected JSON.'
        raise PlanError('Replan failed: '+str(error))
