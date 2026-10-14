# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 过滤模块

职责: 对「信号棒」叠加「市场状态 + 数推位置」，输出放行/拒绝 + 最终分级。
     (回车式三层合一: 背景 × 信号棒质量 × 数推叠加, PRD §5.3)

过滤链(按 PRD §7, 锁定定义):
  1. 紧凑区间排除     tight_range 一律不产信号
  2. 信号棒质量过滤   general 禁用, 仅 strong/strongest
  3. 方向过滤(80%法则) 趋势态只顺势, 逆势反转尝试(80%失败)拒绝 [太妃PA L170]
  4. 数推位置过滤     信号棒须落在 高1-3/低1-3 点位 [学委 L293]
     - 0计数 = 不在回调位置(噪音,如micro双底) → 拒
     - 高4/低4 = 已陷区间, 动能枯竭 → 拒
  5. 回调深度过滤     回调≥75% = 趋势失效 [太妃PA L344-347]
  6. 可选: 磁体/时段/量能 (V1默认关闭, 骨架预留)

分级(仅放行者):
  - strongest: 高3/低3(楔形三推最稳) 或 (高2/低2 + 信号棒strongest + 强趋势态)
  - strong   : 其余放行者(高1/低1, 或宽通道的高2/低2)

边界: V1 过滤模块只处理「趋势顺势」信号; range/tight_range 不产趋势信号
     (区间fade与反转MTR交由开仓模块单独路由, 此处不越界)。
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import config
from signal_bars import BarData, SignalBar
from market_state import MarketStateResult


BJT = timezone(timedelta(hours=8))


@dataclass
class FilterResult:
    """过滤判决结果。"""
    passed: bool
    grade: str = ""                       # strongest / strong / ""(拒绝)
    direction: str = ""                   # long / short / both
    reasons: List[str] = field(default_factory=list)


def _is_trend_state(state: str) -> bool:
    return state in ("trend_up", "trend_down", "broad_up", "broad_down")


def _is_strong_trend_state(state: str) -> bool:
    return state in ("trend_up", "trend_down")


class SignalFilter:
    """趋势顺势信号的过滤与分级器。"""

    def evaluate(self, signal_bar: SignalBar, market_state: MarketStateResult,
                 bars: List[BarData], high_levels: List[int],
                 low_levels: List[int]) -> FilterResult:
        reasons: List[str] = []
        state = market_state.state

        # 1) 紧凑区间排除
        if state == "tight_range":
            return FilterResult(False, "", signal_bar.direction,
                                ["紧凑区间(barbwire)不交易"])
        if state == "unknown":
            return FilterResult(False, "", signal_bar.direction, ["数据不足"])

        # 2) 信号棒质量过滤(双保险, 业务层已 is_tradeable=False)
        if signal_bar.grade not in config.SIGNAL_GRADES_ALLOWED:
            return FilterResult(False, "", signal_bar.direction,
                                [f"信号棒质量不足({signal_bar.grade})"])
        if not signal_bar.is_tradeable:
            return FilterResult(False, "", signal_bar.direction, ["信号棒不可交易"])

        # 突破型架构(both) V1 不直接交易
        if signal_bar.direction == "both":
            return FilterResult(False, "", "both", ["突破型架构需后续确认, V1不直接入场"])

        # 区间态: 非趋势顺势场景, 交由区间方案处理
        if state == "range":
            return FilterResult(False, "", signal_bar.direction,
                                ["区间态, 交由区间方案(fade)处理, 趋势模块不越界"])

        # 3) 方向过滤(80%法则)
        if config.DIRECTION_ALIGN_REQUIRED:
            want_long = market_state.direction > 0
            is_long = signal_bar.direction == "long"
            if want_long != is_long:
                return FilterResult(False, "", signal_bar.direction,
                                    ["逆势反转尝试(80%失败)已过滤"])

        # 4) 数推位置过滤
        level = self._level_at(signal_bar, high_levels, low_levels)
        if config.REQUIRE_PULLBACK_POSITION and config.EXCLUDE_COUNT_LEVEL_0 and level == 0:
            return FilterResult(False, "", signal_bar.direction,
                                ["信号棒不在回调点位(0计数, 噪音)"])
        if config.EXCLUDE_COUNT_LEVEL_4 and level >= config.MAX_COUNT_LEVEL:
            return FilterResult(False, "", signal_bar.direction,
                                [f"已陷区间(高/低{config.MAX_COUNT_LEVEL}, 动能枯竭)"])

        # 5) 回调深度过滤(趋势失效)
        if _is_trend_state(state) and market_state.pullback_ratio >= config.MAX_PULLBACK_RATIO:
            return FilterResult(False, "", signal_bar.direction,
                                ["回调深度≥75%, 趋势已失效"])

        # 6) 可选过滤
        if config.MAGNET_FILTER_ENABLED:
            r = self._magnet_filter(signal_bar, market_state)
            if r:
                reasons.append(r)

        if config.TIME_FILTER_ENABLED:
            r = self._time_filter(signal_bar, bars)
            if r:
                reasons.append(r)

        if config.VOLUME_FILTER_ENABLED:
            r = self._volume_filter(signal_bar, bars)
            if r:
                reasons.append(r)

        # 分级
        grade = self._grade(signal_bar, market_state, level)
        reasons.append(f"数推点位=高/低{level}")
        reasons.append(f"市场状态={state} 信号棒={signal_bar.grade} -> {grade}")
        return FilterResult(True, grade, signal_bar.direction, reasons)

    # ---------- 子判定 ----------

    @staticmethod
    def _level_at(signal_bar: SignalBar, high_levels: List[int],
                  low_levels: List[int]) -> int:
        idx = signal_bar.index
        if signal_bar.direction == "short":
            return low_levels[idx] if 0 <= idx < len(low_levels) else 0
        return high_levels[idx] if 0 <= idx < len(high_levels) else 0

    @staticmethod
    def _grade(signal_bar: SignalBar, market_state: MarketStateResult,
               level: int) -> str:
        """PRD §5.3: 背景 × 信号棒 × 数推。"""
        if level >= 3:
            return "strongest"                       # 高3/低3 楔形三推最稳
        if level == 2 and signal_bar.grade == "strongest" \
                and _is_strong_trend_state(market_state.state):
            return "strongest"                       # 强趋势 + 双底 + 最强信号棒
        return "strong"

    @staticmethod
    def _magnet_filter(signal_bar: SignalBar,
                       market_state: MarketStateResult) -> Optional[str]:
        """磁体拦阻: 入场方向近处存在强磁体(区间边界/EMA) → 降级提示。"""
        atr_dist = market_state.atr * config.MAGNET_PROXIMITY_ATR
        if atr_dist <= 0:
            return None
        if signal_bar.direction == "long":
            # 上方磁体: 区间上边界 / EMA
            magnets = [market_state.range_high, market_state.ema20]
            near = [m for m in magnets if m > signal_bar.entry_price
                    and (m - signal_bar.entry_price) < atr_dist]
        else:
            magnets = [market_state.range_low, market_state.ema20]
            near = [m for m in magnets if m < signal_bar.entry_price
                    and (signal_bar.entry_price - m) < atr_dist]
        if near:
            return f"磁体拦阻(近处强磁体{near[0]:.2f})"
        return None

    @staticmethod
    def _time_filter(signal_bar: SignalBar, bars: List[BarData]) -> Optional[str]:
        """美盘时段过滤: BJT 20:00-04:00 放行, 其余时段降级。"""
        idx = signal_bar.index
        if not (0 <= idx < len(bars)):
            return None
        ct = bars[idx].close_time
        if not ct:
            return None
        bjt = datetime.fromtimestamp(ct / 1000.0, tz=BJT)
        h = bjt.hour
        start, end = config.TIME_SESSION_START_H, config.TIME_SESSION_END_H
        in_session = (h >= start) or (h < end)    # 跨午夜: 20-23 或 0-3
        if not in_session:
            return f"非美盘时段({bjt.strftime('%H:%M')}BJT)"
        return None

    @staticmethod
    def _volume_filter(signal_bar: SignalBar, bars: List[BarData]) -> Optional[str]:
        """量能背离: 主动买入占比与信号方向相悖。"""
        idx = signal_bar.index
        if not (0 <= idx < len(bars)):
            return None
        bar = bars[idx]
        if bar.volume <= 0:
            return None
        ratio = bar.taker_buy / bar.volume
        if signal_bar.direction == "long" and ratio < config.TAKER_BUY_ALIGN_THRESHOLD:
            return f"量能背离(主动买入占比{ratio:.2f}<0.5)"
        if signal_bar.direction == "short" and ratio > config.TAKER_BUY_ALIGN_THRESHOLD:
            return f"量能背离(主动买入占比{ratio:.2f}>0.5)"
        return None