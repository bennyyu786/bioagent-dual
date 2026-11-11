"""Server-side, conservative result acceptance policy for dispatch jobs.

L1: delivery/format checks for a narrow class of conversational prompts (not truth).
L2: independent domain validator required.
L3: independent validator AND human sign-off required.

The worker never controls the acceptance tier. Rules err toward higher scrutiny.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AcceptancePolicy:
    enabled: bool = False
    auto_l1: bool = True
    auto_l2: bool = True
    l1_capabilities: tuple[str, ...] = ()
    # L3 is always manual, including when a trusted domain validator passes.


HIGH_RISK = re.compile(
    r"(科研|实验|临床|诊断|治疗|药物|基因|变异|患者|医学|疾病|靶点|因果|多组学|" 
    r"生物安全|个人隐私|删除|销毁|部署|发布|上线|授权|支付|交易|转账|密钥|" 
    r"scientific|clinical|patient|medical|disease|diagnos|gene|genom|" 
    r"causal|production|deploy|publish|delete|payment|secret|security)", re.I)
MEDIUM_RISK = re.compile(
    r"(代码|编程|脚本|程序|数据分析|建模|计算|统计|测试|执行|修改文件|" 
    r"implement|code|script|software|data analy|model train|statistic|run command|patch)", re.I)
CONVERSATIONAL = re.compile(
    r"^(你是谁[？?]?|你好[呀啊!！。]*|hello[!！.]?|hi[!！.]?|" 
    r"介绍一下你自己[。？?]?|你有什么能力[？?]?|分析你有什么能力[？?]?|" 
    r"你能做什么[？?]?|谢谢[!！。]?)$", re.I)


def classify_prompt(prompt: str) -> str:
    """Conservative task type classification, never from worker assertions."""
    if HIGH_RISK.search(prompt):
        return 'L3'
    if CONVERSATIONAL.fullmatch(prompt.strip()):
        return 'L1'
    if MEDIUM_RISK.search(prompt):
        return 'L2'
    # Unknown goals are not assumed to be simple chat.
    return 'L2'


def delivery_check(prompt: str, reported_state: str, result: str):
    """Only verifies a plausible answer was delivered; cannot verify its factual truth."""
    if classify_prompt(prompt) != 'L1' or reported_state != 'completed':
        return False, 'Not an eligible completed L1 response'
    text = result.strip()
    if not (8 <= len(text) <= 32000):
        return False, 'Answer missing or implausibly short'
    if re.fullmatch(r"(?is)(ok|done|success|completed|true|none|n/a|null|pass|通过|完成|成功)[.!！。 ]*", text):
        return False, 'Worker status assertion is not an answer'
    return True, 'format_and_delivery_only'


def validator_check(validator, task, result):
    """Trust only server-installed callable returning (bool, evidence_reference)."""
    if validator is None:
        return None, 'Independent domain validator unavailable'
    try:
        decision = validator(task, result)
    except Exception:
        return None, 'Independent domain validator failed'
    if not (isinstance(decision, tuple) and len(decision) == 2 and
            type(decision[0]) is bool and isinstance(decision[1], str) and
            8 <= len(decision[1]) <= 256):
        return None, 'Independent validator returned invalid evidence'
    return decision


def artifact_evidence(sha256: str) -> str:
    if not re.fullmatch(r'[0-9a-f]{64}', sha256 or ''):
        raise ValueError('Invalid result hash')
    return 'l1:delivery:' + sha256


def classify_task(task) -> str:
    """Include declared capability risk, not just natural-language task description."""
    caps = ' '.join(getattr(task, 'required_capabilities', ()) or ())
    if HIGH_RISK.search(caps):
        return 'L3'
    tier=classify_prompt(task.prompt)
    if tier=='L1' and MEDIUM_RISK.search(caps):
        return 'L2'
    return tier
