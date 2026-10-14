# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 开仓逻辑模块

职责: 在「市场状态 + 信号棒 + 数推点位」三关齐备后，把信号转化为可执行的交易计划
     (入场价 / 订单类型 / 止损 / 止盈 / 目标依据 / 波段·剥头皮模式)。

锁定定义(太妃PA学习 + 学委方方土PA):
  · 逆1失败+顺1成功(高1/低1, 趋势早期)   [太妃 L992-1006]
      先低1失败(假反转) → 再高1成功顺势; 止损推动起点外, 止盈 AB=CD 等距。
  · 双重顶/底(高2/低2)                    [太妃 L1076-1080, 学委 L342/L348]
      第二推触及第一推极值/AB=CD 后, 以停止单介入高1/低1;
      止损 a1前重要波段点(宽)/a2旗形回调极值(窄); 止盈 b1原趋势极值/b2形态高度等距/b3 AB=CD。
  · 楔形顶/底(高3/低3, 三推)              [太妃 L1110-1111, 学委 L697]
      第三推触及第二推极值后, 以停止单介入高1/低1; 胜率最高, 可拿 2R+。
  · 看衰突破 fade(区间)                   [太妃 L1192-1199]
      区间边线失败突破 → 上1/3做空/下1/3做多; 止损边线外, 止盈区间中轴。
  · 末段旗形 final flag(反转)             [太妃 L1329-1330]
      磁体被触及但突破失去跟进 → 触线后高/低1反向入场; 止损信号K线外, 争取二推逆行。

订单类型约定(PRD §6.4):
  · 趋势顺势(逆1顺1/双重/楔形) + 末段旗形 → Stop Order(停止单)。
  · 区间 fade → Limit Order(限价单)。

边界: 本模块不判「是否入场」(由 filter 层负责), 只负责「入场后如何做」。
"""
from dataclasses import dataclass, field
from typing import List, Optional

import config
from signal_bars import BarData, SignalBar
from market_state import MarketStateResult


# ====================== 数据结构 ======================

@dataclass
class TradePlan:
    """最终可执行交易计划(供飞书推送/回测消费)。"""
    interval: str = ""                # 周期
    setup_type: str = ""              # 方案名(中文, 如「双重底牛旗(高2)」)
    direction: str = ""               # long / short
    grade: str = ""                   # strong / strongest
    entry_price: float = 0.0          # 入场价
    order_type: str = "stop"          # stop / limit / market
    stop_price: float = 0.0           # 止损价
    tp1_price: float = 0.0            # 止盈1(1R, 半仓)
    tp2_price: float = 0.0            # 止盈2(等距/2R, 半仓)
    tp1_ratio: float = 0.5            # TP1 平仓比例
    tp2_ratio: float = 0.5            # TP2 平仓比例
    risk_reward: float = 1.0          # 盈亏比(tp2 对应)
    mode: str = "scalp"               # scalp(1R/高胜率) / swing(2R/高盈亏比)
    market_state: str = ""            # 市场状态(中文)
    ema20: float = 0.0
    atr: float = 0.0
    risk_points: float = 0.0          # 每单位风险(价格), 供仓位反推
    target_basis: str = ""            # 目标依据(AB=CD等距/原趋势极值/区间中轴/...)
    reasons: List[str] = field(default_factory=list)


_TREND_STATES = ("trend_up", "trend_down", "broad_up", "broad_down")


def _ds(direction: str) -> int:
    """方向符号: long=+1, short=-1。"""
    return 1 if direction == "long" else -1


def _is_trend(state: str) -> bool:
    return state in _TREND_STATES


# ====================== 目标锚点 ======================

def _swing_anchors(mstate: MarketStateResult, direction: str):
    """提取趋势锚点(用最近确认波段点)。

    返回 (原趋势极值, 推动起点/回调极值, 推动腿高度)。
      - 原趋势极值: 顺势方守护的最近波段极值(多头=前高, 空头=前低)
      - 推动起点:   前一个重要波段点(多头=前低, 空头=前高), 止损宽口径
      - 推动腿高度: 最近高点-低点幅度, AB=CD/形态高度等距的基准
    """
    sh = mstate.swing_highs   # [(idx, price)]
    sl = mstate.swing_lows
    if direction == "long":
        prior_extreme = sh[-1][1] if sh else None
        pullback_extreme = sl[-1][1] if sl else None
    else:
        prior_extreme = sl[-1][1] if sl else None
        pullback_extreme = sh[-1][1] if sh else None
    leg = None
    if sh and sl:
        leg = abs(sh[-1][1] - sl[-1][1])
    return prior_extreme, pullback_extreme, leg


def _measured_target(direction: str, entry: float, mstate: MarketStateResult):
    """等距目标(AB=CD / 形态高度投映), 优先返回 (目标价, 依据) 或 None。"""
    prior_extreme, _, leg = _swing_anchors(mstate, direction)
    if leg and leg > 0:
        return (entry + _ds(direction) * leg), "AB=CD等距"
    if prior_extreme is not None:
        # 止盈到前波段点外(原趋势极值)
        return prior_extreme, "原趋势极值"
    return None, ""


# ====================== 开仓引擎 ======================

class EntryEngine:
    """按市场状态路由, 把放行信号转为交易计划。"""

    def route(self, sig: SignalBar, mstate: MarketStateResult, level: int,
              grade: str = "", bars: List[BarData] = None,
              interval: str = "") -> Optional[TradePlan]:
        """顶层路由: 趋势→顺势方案; 区间→fade; 趋势末段反转棒→final flag。"""
        state = mstate.state
        if _is_trend(state):
            if sig.direction in ("long", "short"):
                return self.build_trend_plan(sig, mstate, level, grade, interval)
            # 趋势态反转棒(方向与趋势相反) → 末段旗形
            return self.build_final_flag_plan(sig, mstate, grade, interval)
        if state == "range":
            return self.build_fade_plan(sig, mstate, grade, interval)
        return None

    # ---------- 趋势顺势方案 ----------

    def build_trend_plan(self, sig: SignalBar, mstate: MarketStateResult,
                         level: int, grade: str = "",
                         interval: str = "") -> Optional[TradePlan]:
        """趋势顺势: level1=逆1顺1, level2=双重顶底, level3=楔形。"""
        direction = sig.direction
        ds = _ds(direction)
        buf = config.SL_BUFFER_ATR * mstate.atr

        # 入场: 停止单, 高出/低出信号棒极值 1 tick(学委 L295)
        entry = sig.entry_price
        if entry <= 0:
            entry = (sig.signal_high + config.TICK_SIZE * config.ENTRY_STOP_TICK) if ds > 0 \
                else (sig.signal_low - config.TICK_SIZE * config.ENTRY_STOP_TICK)

        # 止损: 窄口径「旗形回调极值=信号棒极值」外 0.3×ATR(泰妃 a2)
        if ds > 0:
            stop = sig.signal_low - buf
        else:
            stop = sig.signal_high + buf

        risk = abs(entry - stop)
        if risk <= 0:
            return None

        # 目标: AB=CD / 原趋势极值; 回退到 2R
        target, basis = _measured_target(direction, entry, mstate)
        tp1 = entry + ds * config.TP1_R * risk
        if target is None:
            tp2 = entry + ds * config.TP2_R_SWING * risk
        else:
            tp2 = target
            min_tp2 = entry + ds * config.TP2_R_MIN * risk
            if ds > 0 and tp2 < min_tp2:
                tp2 = min_tp2
            elif ds < 0 and tp2 > min_tp2:
                tp2 = min_tp2

        # 方案名 / 模式 / 分级
        if level >= 3:
            setup = "楔形底牛旗(高3)" if ds > 0 else "楔形顶熊旗(低3)"
            mode = "swing"
            final_grade = grade or "strongest"
        elif level == 2:
            setup = "双重底牛旗(高2)" if ds > 0 else "双重顶熊旗(低2)"
            mode = "swing"
            final_grade = grade or ("strongest" if sig.grade == "strongest" else "strong")
        else:
            setup = "逆1顺1(高1)" if ds > 0 else "逆1顺1(低1)"
            mode = "scalp"
            final_grade = grade or "strong"

        rr = abs(tp2 - entry) / risk
        return TradePlan(
            interval=interval, setup_type=setup, direction=direction,
            grade=final_grade, entry_price=round(entry, 2), order_type="stop",
            stop_price=round(stop, 2), tp1_price=round(tp1, 2),
            tp2_price=round(tp2, 2), tp1_ratio=config.TP1_CLOSE_RATIO,
            tp2_ratio=config.TP2_CLOSE_RATIO, risk_reward=round(rr, 2),
            mode=mode, market_state=mstate.state, ema20=round(mstate.ema20, 2),
            atr=round(mstate.atr, 2), risk_points=round(risk, 2),
            target_basis=basis,
            reasons=[f"数推点位=高/低{level} 状态={mstate.state} 信号棒={sig.pattern}"])

    # ---------- 区间方案: 看衰突破 fade ----------

    def build_fade_plan(self, sig: SignalBar, mstate: MarketStateResult,
                        grade: str = "", interval: str = "") -> Optional[TradePlan]:
        """看衰突破: 区间下1/3做多、上1/3做空; 限价入场, 止损边线外, 止盈中轴。"""
        rl, rh = mstate.range_low, mstate.range_high
        if rh <= rl:
            return None
        buf = config.SL_BUFFER_ATR * mstate.atr
        edge_zone = config.FADE_EDGE_ATR * mstate.atr
        mid = (rh + rl) / 2.0

        ds = _ds(sig.direction)
        # 多头 fade: 反转棒(收高)落在区间下缘; 空头 fade: 反转棒(收低)落在上缘
        if ds > 0:
            if sig.signal_low - rl > edge_zone:
                return None
            entry = rl                                  # 限价买入(下缘)
            stop = rl - buf                            # 止损边线之外
            tp = mid
            setup = "看衰突破(区间下缘做多)"
        else:
            if rh - sig.signal_high > edge_zone:
                return None
            entry = rh                                  # 限价卖出(上缘)
            stop = rh + buf
            tp = mid
            setup = "看衰突破(区间上缘做空)"

        risk = abs(entry - stop)
        if risk <= 0:
            return None
        tp1 = entry + ds * config.TP1_R * risk
        tp2 = tp                                        # 中轴为止盈(可过半仓后守余仓)
        rr = abs(tp2 - entry) / risk

        return TradePlan(
            interval=interval, setup_type=setup, direction=sig.direction,
            grade=grade or sig.grade, entry_price=round(entry, 2), order_type="limit",
            stop_price=round(stop, 2), tp1_price=round(tp1, 2), tp2_price=round(tp2, 2),
            tp1_ratio=config.TP1_CLOSE_RATIO, tp2_ratio=config.TP2_CLOSE_RATIO,
            risk_reward=round(rr, 2), mode="scalp", market_state="range",
            ema20=round(mstate.ema20, 2), atr=round(mstate.atr, 2),
            risk_points=round(risk, 2), target_basis="区间中轴",
            reasons=[f"区间{rl:.2f}~{rh:.2f}触边失败突破"])

    # ---------- 反转方案: 末段旗形 final flag ----------

    def build_final_flag_plan(self, sig: SignalBar, mstate: MarketStateResult,
                              grade: str = "", interval: str = "") -> Optional[TradePlan]:
        """末段旗形: 趋势末段反转棒反向闯入原趋势极值(磁体)且无跟进 → 反向入场。"""
        if not _is_trend(mstate.state):
            return None
        trend_dir = 1 if mstate.direction > 0 else -1
        sig_dir = _ds(sig.direction)
        # 须为逆势反转棒(方向与趋势相反)
        if sig_dir == trend_dir:
            return None

        buf = config.SL_BUFFER_ATR * mstate.atr
        dx = "long" if trend_dir > 0 else "short"
        prior_extreme, pullback_extreme, _ = _swing_anchors(mstate, dx)
        if prior_extreme is None:
            return None

        # 触磁判定: 反转棒极值须触及原趋势极值(磁体)
        zone = config.FINAL_FLAG_MAGNET_ATR * mstate.atr
        if sig_dir < 0:                                   # 顶部做空
            if prior_extreme - sig.signal_high > zone:
                return None
            entry = sig.signal_low - config.TICK_SIZE * config.ENTRY_STOP_TICK
            stop = sig.signal_high + buf
            risk = abs(entry - stop)
            if risk <= 0:
                return None
            target = pullback_extreme if pullback_extreme is not None else entry - risk
            tp1 = entry - config.TP1_R * risk
            tp2 = target
            min_tp2 = entry - config.TP2_R_MIN * risk
            if tp2 > min_tp2:
                tp2 = min_tp2
            return TradePlan(
                interval=interval, setup_type="末段旗形反转(做空)", direction="short",
                grade=grade or sig.grade, entry_price=round(entry, 2), order_type="stop",
                stop_price=round(stop, 2), tp1_price=round(tp1, 2),
                tp2_price=round(tp2, 2), tp1_ratio=config.TP1_CLOSE_RATIO,
                tp2_ratio=config.TP2_CLOSE_RATIO, risk_reward=round((entry-tp2)/risk, 2),
                mode="swing", market_state=mstate.state, ema20=round(mstate.ema20, 2),
                atr=round(mstate.atr, 2), risk_points=round(risk, 2),
                target_basis="前波段点(二推逆行)",
                reasons=[f"末段旗形触磁{prior_extreme:.2f}无跟进"])
        else:                                              # 底部做多
            if sig.signal_low - prior_extreme > zone:
                return None
            entry = sig.signal_high + config.TICK_SIZE * config.ENTRY_STOP_TICK
            stop = sig.signal_low - buf
            risk = abs(entry - stop)
            if risk <= 0:
                return None
            target = pullback_extreme if pullback_extreme is not None else entry + risk
            tp1 = entry + config.TP1_R * risk
            tp2 = target
            min_tp2 = entry + config.TP2_R_MIN * risk
            if tp2 < min_tp2:
                tp2 = min_tp2
            return TradePlan(
                interval=interval, setup_type="末段旗形反转(做多)", direction="long",
                grade=grade or sig.grade, entry_price=round(entry, 2), order_type="stop",
                stop_price=round(stop, 2), tp1_price=round(tp1, 2),
                tp2_price=round(tp2, 2), tp1_ratio=config.TP1_CLOSE_RATIO,
                tp2_ratio=config.TP2_CLOSE_RATIO, risk_reward=round((tp2-entry)/risk, 2),
                mode="swing", market_state=mstate.state, ema20=round(mstate.ema20, 2),
                atr=round(mstate.atr, 2), risk_points=round(risk, 2),
                target_basis="前波段点(二推逆行)",
                reasons=[f"末段旗形触磁{prior_extreme:.2f}无跟进"])