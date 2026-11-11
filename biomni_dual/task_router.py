"""Conservative task complexity routing; this module never grants execution rights.

Routing must not classify multi-step, unknown or side-effecting instructions as
simple. A direct answer is still a normal authorized/reviewed dispatch task.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

_SIDE_EFFECT = re.compile(r"(?i)\b(?:delete|install|deploy|send|write|modify|commit|push|publish|remove|transfer|execute|shell|sudo)\b|删除|安装|部署|修改|执行|发送|提交|发布|支付|转账|下载|上传|保存|创建|开发|修复|实现|运行")
_GREETING = re.compile(r"^(?:你好|您好|嗨|哈喽|hello|hi|hey)[!！,.，。?？\s]*$", re.I)
_IDENTITY = re.compile(r"^(?:你好[,， ]*)?(?:你是谁|你叫(?:什么名字|啥名字)|你的名字(?:是什么|叫(?:什么|啥))?|你是什么(?:模型|助手)?|请介绍一下你自己|介绍一下你自己|你能做什么|分析你有什么能力)[?？。!！\s]*$|^(?:who are you|what can you do)[?!\s]*$",re.I)
_ONE_STEP = re.compile(r"^(?:查询|列出|查看|显示|统计|检查)(?:当前|现在|一下|一下当前)?[^，,；;\n]{1,70}$")

@dataclass(frozen=True)
class Route:
    kind: str
    reason: str


def classify_prompt(prompt: str) -> Route:
    text=prompt.strip()
    if not text or len(text)>4000:
        return Route('complex','empty_or_out_of_bounds')
    if _SIDE_EFFECT.search(text):
        return Route('complex','potential_side_effect')
    if len(text)<=90 and (_GREETING.fullmatch(text) or _IDENTITY.fullmatch(text)):
        return Route('simple','bounded_greeting_or_identity')
    if len(text)<=90 and _ONE_STEP.fullmatch(text):
        return Route('single','bounded_single_read_only_operation')
    return Route('complex','requires_planning_or_uncertain')
