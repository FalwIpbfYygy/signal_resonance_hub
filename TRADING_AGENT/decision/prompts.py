"""构造给 LLM 的分析 prompt（仅分析，不要求算数字）。"""
from __future__ import annotations

from typing import Any, Dict, List


def build_analysis_prompt(signal: Dict[str, Any], regime: str, recent_memories: List[str]) -> str:
    mem_block = "\n".join(f"- {m}" for m in recent_memories) or "- (无)"
    return (
        f"信号概要：\n"
        f"  标的: {signal.get('symbol')}\n"
        f"  方向: {signal.get('direction')}\n"
        f"  类型: {signal.get('setup_type')}\n"
        f"  入场区: {signal.get('entry_zone')}\n"
        f"  原始评分: {signal.get('raw_score')}\n"
        f"  来源子系统: {signal.get('source_subsystem')}\n"
        f"当前市场状态(regime): {regime}\n\n"
        f"相关记忆：\n{mem_block}\n\n"
        f"请基于以上给出你的交易决策分析与理由（不要给出具体止损止盈数字，由风控引擎计算）。"
    )
