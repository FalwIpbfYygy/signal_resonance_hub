"""Guardian 层：两阶段 Propose(LLM) -> Validate(确定性规则)。

在 decision.engine.build_decision 之外，额外提供一个纯校验入口，
供主流程在调用 LLM 前/后做硬限制闸门（动作白名单、Kill Switch、confidence 门槛）。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from risk.engine import ALLOWED_ACTIONS, RiskConfig, validate_decision


def guard(
    proposed_decision: str,
    confidence: float,
    risk_cfg: RiskConfig,
    allowed_actions: List[str],
    kill_switch_triggered: bool = False,
) -> Tuple[str, List[str]]:
    flags: List[str] = []
    if set(allowed_actions) != ALLOWED_ACTIONS:
        # 白名单被异常修改时告警，但不变更集合
        flags.append(f"guardrails.allowed_actions 与内置不一致: {allowed_actions}")
    decision, vflags = validate_decision(proposed_decision, confidence, risk_cfg)
    flags.extend(vflags)
    if kill_switch_triggered:
        decision = "REJECT"
        flags.append("KILL_SWITCH engaged -> 强制 REJECT")
    return decision, flags


def is_allowed(action: str, allowed_actions: List[str]) -> bool:
    return action in allowed_actions
