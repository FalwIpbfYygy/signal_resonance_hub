"""决策引擎：把「信号 + regime + LLM 分析提案」装配为结构化决策 JSON。

铁律：SL/TP/仓位由确定性风险引擎计算（LLM 禁算）。本模块只做编排与字段填充。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from risk.engine import (
    RiskConfig,
    compute_position,
    compute_stop_loss,
    compute_take_profit,
    validate_decision,
)


def _parse_price(zone: Any) -> Optional[float]:
    if zone is None:
        return None
    if isinstance(zone, (list, tuple)):
        if not zone:
            return None
        return float(sum(zone) / len(zone))
    try:
        return float(zone)
    except (TypeError, ValueError):
        return None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def build_decision(
    signal: Dict[str, Any],
    regime_label: str,
    regime_conf: float,
    llm_proposal: Dict[str, Any],
    risk_cfg: RiskConfig,
    equity: float,
    kill_switch_triggered: bool = False,
) -> Dict[str, Any]:
    direction = signal.get("direction", "long")
    entry = _parse_price(signal.get("entry_zone")) or 0.0
    sl_zone = _parse_price(signal.get("sl_zone"))
    tp_zone = _parse_price(signal.get("tp_zone"))
    atr = signal.get("atr")

    # ---- 确定性 SL / TP ----
    if sl_zone is not None:
        sl = sl_zone
    elif atr:
        sl = compute_stop_loss(entry, float(atr), direction, risk_cfg.atr_sl_multiplier)
    else:
        sl = None

    if tp_zone is not None:
        tp = tp_zone
    elif sl is not None:
        tp = compute_take_profit(entry, sl, direction, risk_cfg.tp_r_multiplier)
    else:
        tp = None

    # ---- 确定性仓位 ----
    if sl is not None and entry > 0:
        pos = compute_position(entry, sl, equity, risk_cfg.max_risk_per_trade_pct, risk_cfg.contract_multiplier)
        size_pct = pos["notional_pct"]
    else:
        size_pct = None

    # ---- Guardian 校验 ----
    decision, flags = validate_decision(
        llm_proposal.get("decision", "OBSERVE"), float(llm_proposal.get("confidence", 0.0)), risk_cfg
    )
    if kill_switch_triggered:
        decision = "REJECT"
        flags.append("KILL_SWITCH engaged -> 强制 REJECT")

    raw_score = signal.get("raw_score") or 0.0
    signal_quality = _clamp01(float(raw_score))
    risk_score = signal_quality  # 占位：风险评分与信号质量同源，后续可细化

    return {
        "signal_id": signal.get("signal_id"),
        "symbol": signal.get("symbol"),
        "decision": decision,
        "direction": direction,
        "confidence": float(llm_proposal.get("confidence", 0.0)),
        "market_regime": regime_label,
        "regime_confidence": regime_conf,
        "setup_type": signal.get("setup_type"),
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "position_size_pct": size_pct,
        "risk_score": risk_score,
        "signal_quality": signal_quality,
        "reasons": llm_proposal.get("reasons", []),
        "risk_flags": flags,
        "memory_refs": llm_proposal.get("memory_refs", []),
    }
