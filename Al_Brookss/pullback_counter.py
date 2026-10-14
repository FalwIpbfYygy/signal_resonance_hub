# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 回撤计数系统模块

理论依据：
- 区间篇P142: "每一波运动和每一次调整都倾向于包含两条腿"
- 区间篇P143: "H2=ABC调整,是多头趋势中最可靠的买进架构之一"
- 区间篇P183: "H1入场需要强趋势+强尖峰+非高潮三大前置条件"
- 趋势篇第10章: "二次入场几乎总是更可能产生可获利交易"
"""
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime

import config
from bar_patterns import BarData, SignalBar, TradingSignal, SignalBarDetector, BarClassifier
from market_state import MarketStateResult


@dataclass
class PullbackCount:
    """回撤计数结果"""
    index: int              # 计数棒在序列中的位置
    count_type: str         # "H1"/"H2"/"H3"/"L1"/"L2"/"L3"
    bar: BarData            # 对应的K线数据


class PullbackCounter:
    """回撤计数系统

    Brooks最高频入场逻辑:
    - H1/L1: 第一次尝试恢复趋势(需强趋势+强尖峰+非高潮)
    - H2/L2: ABC回调=双底牛旗/双顶熊旗(最核心入场架构)
    - H3/L3: 楔形旗形(顺势架构,第一信号即可入场)
    """

    def __init__(self):
        self.detector = SignalBarDetector()
        self.classifier = BarClassifier()

    def analyze(self, bars: List[BarData], market_state: MarketStateResult,
                interval: str) -> List[TradingSignal]:
        """分析回撤计数并生成交易信号

        Args:
            bars: K线序列
            market_state: 市场状态判断结果
            interval: 时间周期(5m/15m/30m/1h/4h)

        Returns:
            交易信号列表
        """
        signals = []

        if len(bars) < config.HL_LOOKBACK + 5:
            return signals

        avg_entity = market_state.avg_entity
        atr = market_state.atr
        if avg_entity <= 0 or atr <= 0:
            return signals

        state = market_state.state

        if state == "trend_up":
            # 多头趋势: 寻找H计数(买进)
            signals.extend(self._detect_long_signals(
                bars, market_state, interval, avg_entity, atr
            ))
        elif state == "trend_down":
            # 空头趋势: 寻找L计数(卖出)
            signals.extend(self._detect_short_signals(
                bars, market_state, interval, avg_entity, atr
            ))
        elif state == "tr":
            # 交易区间: 反向操作(区间顶部H2做空,底部L2做多)
            signals.extend(self._detect_tr_signals(
                bars, market_state, interval, avg_entity, atr
            ))

        # 过滤掉None值(_create_signal可能在信号强度不足/交易者方程不满足时返回None)
        signals = [s for s in signals if s is not None]

        return signals

    # ====================== H/L计数核心逻辑 ======================

    def _count_highs(self, bars: List[BarData], start_index: int) -> List[PullbackCount]:
        """多头趋势中计数H1/H2/H3...

        计数规则:
        - H1: 回撤中第一条高点高于前棒的棒线
        - H2: H1后出现更低高点,再出现高点高于前棒(=ABC回调)
        - H3: 第三条下跌腿结束(=楔形多头旗形)
        - 每个H计数之间必须有一个"更低高点"(回撤继续)
        """
        counts = []
        h_count = 0
        last_h_high = None
        found_lower = False  # 上一个H后是否出现了更低高点

        for i in range(start_index + 1, len(bars)):
            bar = bars[i]
            prev = bars[i - 1]

            # 当前棒高点高于前棒高点 = 尝试恢复上涨
            if bar.high > prev.high:
                if h_count == 0:
                    # 第一次: H1
                    h_count = 1
                    counts.append(PullbackCount(i, "H1", bar))
                    last_h_high = bar.high
                    found_lower = False
                elif found_lower:
                    # 上一个H后出现了更低高点,现在又高点高于前棒 = 新H计数
                    h_count += 1
                    counts.append(PullbackCount(i, f"H{h_count}", bar))
                    last_h_high = bar.high
                    found_lower = False

            # 检查是否出现了更低高点(相对于上一个H点)
            if last_h_high is not None and bar.high < last_h_high:
                found_lower = True

        return counts

    def _count_lows(self, bars: List[BarData], start_index: int) -> List[PullbackCount]:
        """空头趋势中计数L1/L2/L3...(与H对称)"""
        counts = []
        l_count = 0
        last_l_low = None
        found_higher = False

        for i in range(start_index + 1, len(bars)):
            bar = bars[i]
            prev = bars[i - 1]

            # 当前棒低点低于前棒低点 = 尝试恢复下跌
            if bar.low < prev.low:
                if l_count == 0:
                    l_count = 1
                    counts.append(PullbackCount(i, "L1", bar))
                    last_l_low = bar.low
                    found_higher = False
                elif found_higher:
                    l_count += 1
                    counts.append(PullbackCount(i, f"L{l_count}", bar))
                    last_l_low = bar.low
                    found_higher = False

            if last_l_low is not None and bar.low > last_l_low:
                found_higher = True

        return counts

    def _find_pullback_start_bull(self, bars: List[BarData]) -> int:
        """找到多头趋势中回撤的起点(最近的波段高点=尖峰顶部)"""
        search_start = max(0, len(bars) - config.HL_LOOKBACK - 5)
        for i in range(len(bars) - 2, search_start, -1):
            if i < 1:
                continue
            # 检查是否是局部最高点(前后2棒都低于它)
            is_highest = True
            for j in range(max(0, i - 2), min(len(bars), i + 3)):
                if bars[j].high > bars[i].high:
                    is_highest = False
                    break
            if is_highest:
                return i
        return max(0, len(bars) - config.HL_LOOKBACK)

    def _find_pullback_start_bear(self, bars: List[BarData]) -> int:
        """找到空头趋势中回撤的起点(最近的波段低点=尖峰底部)"""
        search_start = max(0, len(bars) - config.HL_LOOKBACK - 5)
        for i in range(len(bars) - 2, search_start, -1):
            if i < 1:
                continue
            is_lowest = True
            for j in range(max(0, i - 2), min(len(bars), i + 3)):
                if bars[j].low < bars[i].low:
                    is_lowest = False
                    break
            if is_lowest:
                return i
        return max(0, len(bars) - config.HL_LOOKBACK)

    # ====================== 信号检测 ======================

    def _detect_long_signals(self, bars: List[BarData], market_state: MarketStateResult,
                             interval: str, avg_entity: float, atr: float) -> List[TradingSignal]:
        """多头趋势中检测买入信号"""
        signals = []

        # 找到回撤起点
        start = self._find_pullback_start_bull(bars)
        if start >= len(bars) - 1:
            return signals

        # 计数H
        counts = self._count_highs(bars, start)
        if not counts:
            return signals

        # 检查连续H1限制(3个连续H1=动量衰退,跳过)
        if len(counts) >= config.H1_CONSECUTIVE_LIMIT:
            recent_types = [c.count_type for c in counts[-config.H1_CONSECUTIVE_LIMIT:]]
            if all(t == "H1" for t in recent_types):
                return signals

        latest = counts[-1]

        # 检测信号棒
        signal_bars = self.detector.detect_all_signals(bars, latest.index, avg_entity, atr)
        bull_signals = [s for s in signal_bars if s.direction == "long" and s.is_tradeable]
        if not bull_signals:
            return signals

        signal_bar = bull_signals[0]

        # 根据计数类型生成信号
        if latest.count_type == "H1":
            # H1入场: 检查三大前置条件
            if self._check_h1_preconditions(bars, start, avg_entity):
                sig = self._create_signal(
                    interval, "h1_strong", "long", signal_bar,
                    market_state, bars, atr
                )
                if sig:
                    signals.append(sig)

        elif latest.count_type == "H2":
            # H2入场(最核心): 检查EMA距离+位置
            ema_dist = abs(bars[latest.index].close - market_state.ema20)
            if ema_dist > config.EMA_DIST_ATR_MULT * atr:
                # H2应位于或高于EMA20(趋势中)
                if bars[latest.index].low >= market_state.ema20 - atr * 0.5:
                    sig = self._create_signal(
                        interval, "h2_trend", "long", signal_bar,
                        market_state, bars, atr
                    )
                    if sig:
                        signals.append(sig)

        elif latest.count_type == "H3":
            # H3 = 楔形多头旗形(顺势,第一信号即可)
            sig = self._create_signal(
                interval, "wedge_flag", "long", signal_bar,
                market_state, bars, atr
            )
            if sig:
                signals.append(sig)

        # 检查二次入场
        second_entry = self._check_second_entry(counts, bars, market_state, interval, avg_entity, atr)
        if second_entry:
            signals.append(second_entry)

        return signals

    def _detect_short_signals(self, bars: List[BarData], market_state: MarketStateResult,
                              interval: str, avg_entity: float, atr: float) -> List[TradingSignal]:
        """空头趋势中检测卖出信号(与多头对称)"""
        signals = []

        start = self._find_pullback_start_bear(bars)
        if start >= len(bars) - 1:
            return signals

        counts = self._count_lows(bars, start)
        if not counts:
            return signals

        # 连续L1限制
        if len(counts) >= config.H1_CONSECUTIVE_LIMIT:
            recent_types = [c.count_type for c in counts[-config.H1_CONSECUTIVE_LIMIT:]]
            if all(t == "L1" for t in recent_types):
                return signals

        latest = counts[-1]

        signal_bars = self.detector.detect_all_signals(bars, latest.index, avg_entity, atr)
        short_signals = [s for s in signal_bars if s.direction == "short" and s.is_tradeable]
        if not short_signals:
            return signals

        signal_bar = short_signals[0]

        if latest.count_type == "L1":
            if self._check_l1_preconditions(bars, start, avg_entity):
                sig = self._create_signal(
                    interval, "l1_strong", "short", signal_bar,
                    market_state, bars, atr
                )
                if sig:
                    signals.append(sig)

        elif latest.count_type == "L2":
            ema_dist = abs(bars[latest.index].close - market_state.ema20)
            if ema_dist > config.EMA_DIST_ATR_MULT * atr:
                if bars[latest.index].high <= market_state.ema20 + atr * 0.5:
                    sig = self._create_signal(
                        interval, "l2_trend", "short", signal_bar,
                        market_state, bars, atr
                    )
                    if sig:
                        signals.append(sig)

        elif latest.count_type == "L3":
            sig = self._create_signal(
                interval, "wedge_flag", "short", signal_bar,
                market_state, bars, atr
            )
            if sig:
                signals.append(sig)

        # 二次入场
        second_entry = self._check_second_entry(counts, bars, market_state, interval, avg_entity, atr)
        if second_entry:
            signals.append(second_entry)

        return signals

    def _detect_tr_signals(self, bars: List[BarData], market_state: MarketStateResult,
                           interval: str, avg_entity: float, atr: float) -> List[TradingSignal]:
        """交易区间中的反向操作

        原文P142-143:
        - TR顶部H2 → 做空(预期H2失败)
        - TR底部L2 → 做多(预期L2失败成双重底)
        """
        signals = []

        # 判断当前价格在区间中的位置
        current_price = bars[-1].close
        range_high = market_state.range_high
        range_low = market_state.range_low
        range_mid = (range_high + range_low) / 2.0

        # 在区间上半部分: 寻找H2做空
        if current_price > range_mid:
            start = self._find_pullback_start_bull(bars)
            counts = self._count_highs(bars, start)
            if counts and counts[-1].count_type == "H2":
                signal_bars = self.detector.detect_all_signals(
                    bars, counts[-1].index, avg_entity, atr
                )
                short_signals = [s for s in signal_bars if s.direction == "short" and s.is_tradeable]
                if short_signals:
                    sig = self._create_signal(
                        interval, "tr_reversal", "short", short_signals[0],
                        market_state, bars, atr
                    )
                    if sig:
                        signals.append(sig)

        # 在区间下半部分: 寻找L2做多
        if current_price < range_mid:
            start = self._find_pullback_start_bear(bars)
            counts = self._count_lows(bars, start)
            if counts and counts[-1].count_type == "L2":
                signal_bars = self.detector.detect_all_signals(
                    bars, counts[-1].index, avg_entity, atr
                )
                long_signals = [s for s in signal_bars if s.direction == "long" and s.is_tradeable]
                if long_signals:
                    sig = self._create_signal(
                        interval, "tr_reversal", "long", long_signals[0],
                        market_state, bars, atr
                    )
                    if sig:
                        signals.append(sig)

        return signals

    # ====================== 前置条件检查 ======================

    def _check_h1_preconditions(self, bars: List[BarData], spike_index: int,
                                 avg_entity: float) -> bool:
        """检查H1入场的三大前置条件

        1. 强趋势(非TR) - 已由调用方保证
        2. 强趋势尖峰(最近5棒含≥3根强势多头趋势棒)
        3. 非高潮(H1前尖峰未达耗尽标准,实体<3×平均)
        """
        # 条件2: 强趋势尖峰
        lookback = min(config.SPIKE_MIN_BARS + 2, spike_index + 1)
        strong_bull_count = 0
        for i in range(max(0, spike_index - lookback + 1), spike_index + 1):
            if i >= len(bars):
                continue
            bar = bars[i]
            if bar.is_bull and self.classifier.is_strong_trend_bar(bar, avg_entity):
                strong_bull_count += 1
        if strong_bull_count < config.SPIKE_MIN_BARS:
            return False

        # 条件3: 非高潮(尖峰棒未达耗尽标准)
        for i in range(max(0, spike_index - lookback + 1), spike_index + 1):
            if i >= len(bars):
                continue
            if self.detector.is_exhaustion_bar(bars[i], avg_entity):
                return False

        return True

    def _check_l1_preconditions(self, bars: List[BarData], spike_index: int,
                                 avg_entity: float) -> bool:
        """检查L1入场的三大前置条件(空头对称)"""
        lookback = min(config.SPIKE_MIN_BARS + 2, spike_index + 1)
        strong_bear_count = 0
        for i in range(max(0, spike_index - lookback + 1), spike_index + 1):
            if i >= len(bars):
                continue
            bar = bars[i]
            if bar.is_bear and self.classifier.is_strong_trend_bar(bar, avg_entity):
                strong_bear_count += 1
        if strong_bear_count < config.SPIKE_MIN_BARS:
            return False

        for i in range(max(0, spike_index - lookback + 1), spike_index + 1):
            if i >= len(bars):
                continue
            if self.detector.is_exhaustion_bar(bars[i], avg_entity):
                return False

        return True

    def _check_second_entry(self, counts: List[PullbackCount], bars: List[BarData],
                            market_state: MarketStateResult, interval: str,
                            avg_entity: float, atr: float) -> Optional[TradingSignal]:
        """检查二次入场

        原文第10章: "二次入场几乎总是更可能产生可获利交易"
        规则: 首次入场信号后几棒内,同逻辑信号再次出现
        """
        if len(counts) < 2:
            return None

        first = counts[-2]
        second = counts[-1]

        # 相同类型(H2+H2, L2+L2等)
        if first.count_type != second.count_type:
            return None

        # 间距2-5棒
        gap = second.index - first.index
        if gap < 2 or gap > config.SECOND_ENTRY_LOOKBACK:
            return None

        # 二次入场信号棒
        signal_bars = self.detector.detect_all_signals(bars, second.index, avg_entity, atr)
        tradeable = [s for s in signal_bars if s.is_tradeable]
        if not tradeable:
            return None

        signal_bar = tradeable[0]
        direction = "long" if second.count_type.startswith("H") else "short"

        return self._create_signal(
            interval, "second_entry", direction, signal_bar,
            market_state, bars, atr
        )

    # ====================== 信号创建 ======================

    def _create_signal(self, interval: str, setup_type: str, direction: str,
                       signal_bar: SignalBar, market_state: MarketStateResult,
                       bars: List[BarData], atr: float) -> Optional[TradingSignal]:
        """创建交易信号(含止损止盈计算)"""
        grade = signal_bar.grade
        if grade not in config.SIGNAL_GRADES_ALLOWED:
            return None

        # 计算止损(信号棒极值±0.3×ATR)
        if direction == "long":
            stop_price = signal_bar.signal_low - config.SL_ATR_MULT * atr
            risk = signal_bar.entry_price - stop_price
            if risk <= 0:
                return None
            tp1 = signal_bar.entry_price + risk * config.TP1_R_MULT
            tp2 = signal_bar.entry_price + risk * config.TP2_R_MULT
        else:
            stop_price = signal_bar.signal_high + config.SL_ATR_MULT * atr
            risk = stop_price - signal_bar.entry_price
            if risk <= 0:
                return None
            tp1 = signal_bar.entry_price - risk * config.TP1_R_MULT
            tp2 = signal_bar.entry_price - risk * config.TP2_R_MULT

        # 交易者方程检查
        eq_params = config.TRADER_EQUATION.get(setup_type, {"win_rate": 0.5, "min_rr": 1.5})
        actual_rr = config.TP2_R_MULT
        if actual_rr < eq_params["min_rr"]:
            return None

        # 获取K线收盘时间
        bar_close_time = 0
        if signal_bar.index < len(bars):
            bar_close_time = bars[signal_bar.index].close_time

        # 信号时间 = K 线 close_time（毫秒）；兜底才用当前时间
        if bar_close_time:
            timestamp = datetime.fromtimestamp(bar_close_time / 1000).strftime("%Y-%m-%d %H:%M:%S")
        else:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        return TradingSignal(
            interval=interval,
            setup_type=setup_type,
            direction=direction,
            grade=grade,
            entry_price=signal_bar.entry_price,
            stop_price=stop_price,
            tp1_price=tp1,
            tp2_price=tp2,
            tp1_ratio=config.TP1_CLOSE_RATIO,
            tp2_ratio=config.TP2_CLOSE_RATIO,
            signal_high=signal_bar.signal_high,
            signal_low=signal_bar.signal_low,
            risk_reward=actual_rr,
            market_state=market_state.state if market_state else "",
            ema20=market_state.ema20 if market_state else 0,
            atr=atr,
            reasons=signal_bar.reasons + [f"架构={setup_type}"],
            bar_index=signal_bar.index,
            bar_close_time=bar_close_time,
            timestamp=timestamp,
        )
