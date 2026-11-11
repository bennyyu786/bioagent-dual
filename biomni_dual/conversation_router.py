"""Conservative no-tool conversation routing for the unified v0.9.1 homepage."""
from __future__ import annotations
import re
from .task_router import classify_prompt, _SIDE_EFFECT

_QUESTION = re.compile(r"(?:[?？]$|^(?:请问|什么是|为什么|如何理解|介绍一下|解释一下|what is|who is|why is|how does))", re.I)
_MULTI = re.compile(r"(?:然后|接着|并且|同时|分别|完成后|第一步|第二步|再去|并部署|并测试|and then|after that)",re.I)
_SYSTEM_STATE = re.compile(r"(?:当前|实时|状态|日志|服务器|worker|任务中心|能力中心|数据库|工作区|项目代码|仓库)",re.I)

def conversation_route(message: str, mode: str = 'auto') -> str:
    """Choose read-only model chat or the existing authorized Task Center.

    'chat' is always an unprivileged text-only advisor, even for instructions.
    'task' never bypasses approval or the Task Center.
    """
    if mode not in ('auto','chat','task'):
        raise ValueError('Invalid mode')
    if mode=='chat':return 'chat'
    if mode=='task':return 'task'
    text=message.strip()
    if classify_prompt(text).kind=='simple':return 'chat'
    if len(text)<=180 and _QUESTION.search(text) and not (_SIDE_EFFECT.search(text) or _MULTI.search(text) or _SYSTEM_STATE.search(text)):
        return 'chat'
    return 'task'
