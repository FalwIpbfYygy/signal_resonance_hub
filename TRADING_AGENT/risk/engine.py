"""确定性风险引擎（100% Python，LLM 永不参与精确计算）。

职责：
  - 由 ATR 推导止损 SL、由 R 倍数推导止盈 TP
  - 由账户净值/风险% 推导仓位（units + 名义占比）
  - Kill Switch 硬限制（日亏损、连续亏损）
  - 交易时段判定（周一至周五、北京时间日内，不持仓过夜）
  - Guardian 决策校验（置信度门槛、动作白名单）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

# 北京时间为固定 UTC+8，不实行夏令时，无需 tzdata 依赖
BEIJING = timezone(timedelta(hours=8))

ALLOWED_ACTIONS = {"EXECUTE", "REJECT", "WAIT", "REDUCE_RISK", "OBSERVE"}


@dataclass
class RiskConfig:
    max_risk_per_trade_pct: float = 0.03
    daily_loss_stop_pct: float = 0.05
    consecutive_loss_stop: int = 5
    atr_sl_multiplier: float = 1.5
    tp_r_multiplier: float = 1.5
    max_concurrent_signals: int = 3
    confidence_threshold: float = 0.65
    contract_multiplier: float = 100.0  #  标准手 = 100 oz


@dataclass
class KillSwitchState:
    """按交易日追踪当日盈亏与连续亏损笔数（确定性状态）。"""
    daily_pnl_pct: float = 0.0
    consecutive_losses: int = 0
    _day: str = ""

    def _ensure_day(self, day_key: str) -> None:
        if self._day != day_key:
            self._day = day_key
            self.daily_pnl_pct = 0.0
            self.consecutive_losses = 0

    def record_trade(self, pnl_pct: float, day_key: str) -> None:
        self._ensure_day(day_key)
        self.daily_pnl_pct += pnl_pct
        if pnl_pct < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def reset(self, day_key: str) -> None:
        self._day = day_key
        self.daily_pnl_pct = 0.0
        self.consecutive_losses = 0


def _to_beijing(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=BEIJING)
    return now.astimezone(BEIJING)


def compute_stop_loss(entry: float, atr: float, direction: str, mult: float = 1.5) -> float:
    if direction not in ("long", "short"):
        raise ValueError(f"direction 必须是 'long' 或 'short'，收到 {direction!r}")
    if atr <= 0:
        raise ValueError("atr 必须 > 0")
    return entry - mult * atr if direction == "long" else entry + mult * atr


def compute_take_profit(entry: float, stop_loss: float, direction: str, r_multiplier: float = 1.5) -> float:
    if direction not in ("long", "short"):
        raise ValueError(f"direction 必须是 'long' 或 'short'，收到 {direction!r}")
    risk = abs(entry - stop_loss)
    if risk == 0:
        raise ValueError("stop_loss 必须与 entry 不同")
    return entry + risk * r_multiplier if direction == "long" else entry - risk * r_multiplier


def compute_position(
    entry: float,
    stop_loss: float,
    equity: float,
    risk_pct: float = 0.03,
    contract_multiplier: float = 100.0,
) -> Dict[str, float]:
    if equity <= 0:
        raise ValueError("equity 必须 > 0")
    risk_per_unit = abs(entry - stop_loss)
    if risk_per_unit == 0:
        raise ValueError("entry == stop_loss，无法计算仓位")
    risk_amount = equity * risk_pct
    units = risk_amount / (risk_per_unit * contract_multiplier)
    notional = units * entry
    notional_pct = notional / equity
    return {
        "risk_amount": risk_amount,
        "risk_per_unit": risk_per_unit,
        "units": units,
        "notional": notional,
        "notional_pct": notional_pct,
    }


def check_kill_switch(state: KillSwitchState, cfg: RiskConfig, day_key: str) -> Tuple[bool, str]:
    state._ensure_day(day_key)
    if state.daily_pnl_pct <= -cfg.daily_loss_stop_pct:
        return True, f"当日亏损 {state.daily_pnl_pct:.2%} <= -{cfg.daily_loss_stop_pct:.2%}"
    if state.consecutive_losses >= cfg.consecutive_loss_stop:
        return True, f"连续亏损 {state.consecutive_losses} 笔 >= {cfg.consecutive_loss_stop}"
    return False, ""


def is_tradeable_now(
    now: datetime,
    weekdays: Tuple[int, ...] = (0, 1, 2, 3, 4),
    start: str = "09:00",
    end: str = "22:00",
) -> bool:
    now_b = _to_beijing(now)
    if now_b.weekday() not in weekdays:
        return False
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    start_dt = now_b.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_dt = now_b.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start_dt <= now_b <= end_dt


def minutes_to_session_end(now: datetime, end: str = "22:00") -> float:
    now_b = _to_beijing(now)
    eh, em = map(int, end.split(":"))
    end_dt = now_b.replace(hour=eh, minute=em, second=0, microsecond=0)
    return (end_dt - now_b).total_seconds() / 60.0


def should_warn_no_overnight(now: datetime, warn_minutes: int = 60, end: str = "22:00") -> bool:
    m = minutes_to_session_end(now, end)
    return 0.0 <= m <= warn_minutes


def validate_decision(decision: str, confidence: float, cfg: RiskConfig) -> Tuple[str, List[str]]:
    if decision not in ALLOWED_ACTIONS:
        raise ValueError(f"非法决策动作 {decision!r}，允许: {sorted(ALLOWED_ACTIONS)}")
    flags: List[str] = []
    if decision == "EXECUTE" and confidence < cfg.confidence_threshold:
        decision = "WAIT"
        flags.append(
            f"confidence {confidence:.2f} < {cfg.confidence_threshold:.2f}，EXECUTE 降级为 WAIT"
        )
    return decision, flags
