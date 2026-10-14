"""decision 自检：装配 + Guardian 闸门。"""
from __future__ import annotations

from decision.engine import build_decision
from decision.guardian import guard
from risk.engine import RiskConfig

SIGNAL = {
    "signal_id": "S1", "symbol": "", "direction": "long",
    "setup_type": "FVG", "entry_zone": [2000, 2004], "sl_zone": 1985.0,
    "tp_zone": 2025.0, "ts": "2026-09-29T12:00:00", "source_subsystem": "ICT",
    "raw_score": 0.8, "atr": 10.0,
}

CFG = RiskConfig()


def test_build_decision_happy_path():
    prop = {"decision": "EXECUTE", "confidence": 0.8, "reasons": ["trend up"], "memory_refs": ["m1"]}
    d = build_decision(SIGNAL, "TRENDING_UP", 0.7, prop, CFG, equity=10000.0)
    assert d["decision"] == "EXECUTE"
    assert d["stop_loss"] == 1985.0          # 用信号给定 SL
    assert d["take_profit"] == 2025.0        # 用信号给定 TP
    assert d["entry"] == 2002.0              # (2000+2004)/2
    assert d["position_size_pct"] is not None
    assert d["position_size_pct"] > 0


def test_build_decision_low_confidence_downgrade():
    prop = {"decision": "EXECUTE", "confidence": 0.4, "reasons": [], "memory_refs": []}
    d = build_decision(SIGNAL, "RANGING", 0.5, prop, CFG, equity=10000.0)
    assert d["decision"] == "WAIT"
    assert any("confidence" in f for f in d["risk_flags"])


def test_build_decision_kill_switch_forces_reject():
    prop = {"decision": "EXECUTE", "confidence": 0.9, "reasons": [], "memory_refs": []}
    d = build_decision(SIGNAL, "TRENDING_UP", 0.7, prop, CFG, equity=10000.0, kill_switch_triggered=True)
    assert d["decision"] == "REJECT"


def test_build_decision_computes_sl_when_no_zone():
    sig = dict(SIGNAL)
    sig.pop("sl_zone")
    prop = {"decision": "EXECUTE", "confidence": 0.8, "reasons": [], "memory_refs": []}
    d = build_decision(sig, "TRENDING_UP", 0.7, prop, CFG, equity=10000.0)
    # SL = entry - 1.5*atr = 2002 - 15 = 1987
    assert d["stop_loss"] == 1987.0


def test_guardian_white_list_and_kill():
    decision, flags = guard("EXECUTE", 0.9, CFG, list(__import__("risk.engine").ALLOWED_ACTIONS), kill_switch_triggered=True)
    assert decision == "REJECT"
    assert any("KILL_SWITCH" in f for f in flags)
