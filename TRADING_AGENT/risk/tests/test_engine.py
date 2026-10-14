"""risk.engine 自检：单元 + 边界 + 安全闸门。运行: pytest risk/tests/test_engine.py"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from risk.engine import (
    ALLOWED_ACTIONS,
    BEIJING,
    KillSwitchState,
    RiskConfig,
    check_kill_switch,
    compute_position,
    compute_stop_loss,
    compute_take_profit,
    is_tradeable_now,
    should_warn_no_overnight,
    validate_decision,
)


# ========================= 基础数学 =========================
def test_stop_loss_long():
    assert compute_stop_loss(2000.0, 10.0, "long", 1.5) == 1985.0


def test_stop_loss_short():
    assert compute_stop_loss(2000.0, 10.0, "short", 1.5) == 2015.0


def test_stop_loss_negative_atr_raises():
    with pytest.raises(ValueError):
        compute_stop_loss(2000.0, -1.0, "long")


def test_stop_loss_bad_direction_raises():
    with pytest.raises(ValueError):
        compute_stop_loss(2000.0, 10.0, "sideways")


def test_take_profit_long():
    # entry 2000, sl 1985 -> risk 15 -> tp = 2000 + 1.5*15 = 2022.5
    assert compute_take_profit(2000.0, 1985.0, "long", 1.5) == 2022.5


def test_take_profit_short():
    # entry 2000, sl 2015 -> risk 15 -> tp = 2000 - 1.5*15 = 1977.5
    assert compute_take_profit(2000.0, 2015.0, "short", 1.5) == 1977.5


def test_take_profit_zero_risk_raises():
    with pytest.raises(ValueError):
        compute_take_profit(2000.0, 2000.0, "long")


# ========================= 仓位 =========================
def test_position_sizing_basic():
    pos = compute_position(entry=2000.0, stop_loss=1985.0, equity=10000.0, risk_pct=0.03, contract_multiplier=100.0)
    # risk_per_unit=15, risk_amount=300 -> units = 300/(15*100)=0.2
    assert pos["units"] == pytest.approx(0.2)
    assert pos["risk_amount"] == pytest.approx(300.0)
    # notional = 0.2*2000=400, notional_pct=400/10000=4%
    assert pos["notional_pct"] == pytest.approx(0.04)


def test_position_zero_equity_raises():
    with pytest.raises(ValueError):
        compute_position(2000.0, 1985.0, 0.0)


def test_position_zero_risk_raises():
    with pytest.raises(ValueError):
        compute_position(2000.0, 2000.0, 10000.0)


# ========================= Kill Switch =========================
def test_kill_switch_daily_loss():
    cfg = RiskConfig(daily_loss_stop_pct=0.05, consecutive_loss_stop=5)
    st = KillSwitchState()
    st.record_trade(-0.06, "2026-09-29")
    triggered, reason = check_kill_switch(st, cfg, "2026-09-29")
    assert triggered is True
    assert "当日亏损" in reason


def test_kill_switch_consecutive_loss():
    cfg = RiskConfig(daily_loss_stop_pct=0.05, consecutive_loss_stop=5)
    st = KillSwitchState()
    # 单笔仅亏 0.5%，5 笔累计 -2.5% 不触发日亏损闸门，专门验证连续亏损路径
    for _ in range(5):
        st.record_trade(-0.005, "2026-09-29")
    triggered, reason = check_kill_switch(st, cfg, "2026-09-29")
    assert triggered is True
    assert "连续亏损" in reason


def test_kill_switch_ok():
    cfg = RiskConfig(daily_loss_stop_pct=0.05, consecutive_loss_stop=5)
    st = KillSwitchState()
    st.record_trade(0.02, "2026-09-29")
    st.record_trade(-0.01, "2026-09-29")
    triggered, _ = check_kill_switch(st, cfg, "2026-09-29")
    assert triggered is False


def test_kill_switch_resets_on_new_day():
    cfg = RiskConfig(daily_loss_stop_pct=0.05, consecutive_loss_stop=5)
    st = KillSwitchState()
    st.record_trade(-0.06, "2026-09-29")
    # 新的一天应清零
    assert st.daily_pnl_pct == -0.06
    check_kill_switch(st, cfg, "2026-09-30")
    assert st.daily_pnl_pct == 0.0
    assert st.consecutive_losses == 0


# ========================= 交易时段 =========================
def _beijing_dt(hour, minute, weekday_offset=0):
    # 以 2026-09-29(周二, weekday=1) 为基准，可偏移
    base = datetime(2026, 9, 29, hour, minute, tzinfo=BEIJING)
    return base + timedelta(days=weekday_offset)


def test_tradeable_within_session_weekday():
    assert is_tradeable_now(_beijing_dt(12, 0)) is True


def test_not_tradeable_outside_hours():
    assert is_tradeable_now(_beijing_dt(23, 0)) is False
    assert is_tradeable_now(_beijing_dt(8, 0)) is False


def test_not_tradeable_weekend():
    # 2026-09-29 是周二；+3 天 = 周五(4)，+4 天 = 周六(5) 不可交易
    assert is_tradeable_now(_beijing_dt(12, 0, weekday_offset=4)) is False


def test_no_overnight_warning_window():
    # 21:00 距 22:00 收盘 60 分钟 -> 触发
    assert should_warn_no_overnight(_beijing_dt(21, 0), warn_minutes=60) is True
    # 20:00 距收盘 120 分钟 -> 不触发
    assert should_warn_no_overnight(_beijing_dt(20, 0), warn_minutes=60) is False
    # 22:30 已过收盘 -> 不触发（m<0）
    assert should_warn_no_overnight(_beijing_dt(22, 30), warn_minutes=60) is False


# ========================= Guardian 决策校验 =========================
def test_validate_decision_low_confidence_downgrade():
    cfg = RiskConfig(confidence_threshold=0.65)
    decision, flags = validate_decision("EXECUTE", 0.50, cfg)
    assert decision == "WAIT"
    assert flags


def test_validate_decision_high_confidence_kept():
    cfg = RiskConfig(confidence_threshold=0.65)
    decision, flags = validate_decision("EXECUTE", 0.80, cfg)
    assert decision == "EXECUTE"
    assert flags == []


def test_validate_decision_invalid_action():
    cfg = RiskConfig()
    with pytest.raises(ValueError):
        validate_decision("HODL", 0.9, cfg)


def test_allowed_actions_five():
    assert ALLOWED_ACTIONS == {"EXECUTE", "REJECT", "WAIT", "REDUCE_RISK", "OBSERVE"}
