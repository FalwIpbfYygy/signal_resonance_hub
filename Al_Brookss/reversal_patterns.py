# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 反转架构识别模块

理论依据：
- 反转篇P46-50: MTR四条件框架(趋势→趋势线突破→测试极点→二次反转)
- 反转篇P72: 高潮反转(走得太远太快,激情冷却)
- 反转篇P107: 楔形三推反转(所有三推形态都是相同潜在行为)
- 反转篇P135: 最终旗形(趋势末期水平旗形突破后反转)
- 反转篇P165: 失败的失败(失败的突破再次失败=突破回撤,高胜率)
"""
from typing import List, Optional
from datetime import datetime
import numpy as np

import config
from bar_patterns import BarData, SignalBar, TradingSignal, SignalBarDetector, BarClassifier
from market_state import MarketStateResult
from indicators import find_swing_highs, find_swing_lows


class ReversalPatternDetector:
    """反转架构识别器

    识别Brooks体系中的五大反转架构:
    1. MTR(重要趋势反转) - 四条件框架
    2. 高潮反转 - 走得太远太快后的反转
    3. 楔形三推反转 - 三推形态
    4. 最终旗形 - 趋势末期水平旗形
    5. 失败的失败 - 突破失败后再次失败=高胜率二次信号
    """

    def __init__(self):
        self.detector = SignalBarDetector()
        self.classifier = BarClassifier()

    def analyze(self, bars: List[BarData], market_state: MarketStateResult,
                interval: str) -> List[TradingSignal]:
        """分析反转架构并生成交易信号"""
        signals = []

        if len(bars) < 30:
            return signals

        avg_entity = market_state.avg_entity
        atr = market_state.atr
        if avg_entity <= 0 or atr <= 0:
            return signals

        # 1. MTR检测
        signals.extend(self._detect_mtr(bars, market_state, interval, avg_entity, atr))

        # 2. 高潮反转
        signals.extend(self._detect_climax_reversal(bars, market_state, interval, avg_entity, atr))

        # 3. 楔形三推反转
        signals.extend(self._detect_wedge_reversal(bars, market_state, interval, avg_entity, atr))

        # 4. 最终旗形
        signals.extend(self._detect_final_flag(bars, market_state, interval, avg_entity, atr))

        # 5. 失败的失败
        signals.extend(self._detect_failed_failure(bars, market_state, interval, avg_entity, atr))

        # 过滤掉None值(_create_signal可能在信号强度不足/交易者方程不满足时返回None)
        signals = [s for s in signals if s is not None]

        return signals

    # ====================== 1. MTR检测 ======================

    def _detect_mtr(self, bars: List[BarData], market_state: MarketStateResult,
                    interval: str, avg_entity: float, atr: float) -> List[TradingSignal]:
        """检测重要趋势反转(MTR)

        四条件(缺一不可):
        1. 已有趋势(≥20棒)
        2. 趋势线突破(强逆势尖峰,突破趋势线+EMA)
        3. 测试极点(回撤测试原高点/低点)
        4. 二次反转确认(测试后强反向尖峰)
        """
        signals = []

        # 条件1: 已有趋势
        if market_state.trend_bars < config.MTR_TREND_MIN_BARS:
            return signals

        # 检测多头→空头MTR
        sig = self._detect_bull_to_bear_mtr(bars, market_state, interval, avg_entity, atr)
        if sig:
            signals.append(sig)

        # 检测空头→多头MTR
        sig = self._detect_bear_to_bull_mtr(bars, market_state, interval, avg_entity, atr)
        if sig:
            signals.append(sig)

        return signals

    def _detect_bull_to_bear_mtr(self, bars: List[BarData], market_state: MarketStateResult,
                                  interval: str, avg_entity: float, atr: float) -> Optional[TradingSignal]:
        """检测多头→空头MTR"""
        ema_series = market_state.ema_series
        if len(ema_series) < len(bars):
            return None

        # 找到最近的高点(原趋势极点)
        lookback = min(50, len(bars))
        recent_bars = bars[-lookback:]
        high_idx_offset = int(np.argmax([b.high for b in recent_bars]))
        recent_high_idx = len(bars) - lookback + high_idx_offset
        recent_high = bars[recent_high_idx].high

        # 条件2: 趋势线突破(跌破EMA + 强空头尖峰)
        break_index = None
        for i in range(recent_high_idx + 1, len(bars)):
            bar = bars[i]
            # 跌破EMA20
            if bar.close < ema_series[i]:
                # 检查强空头尖峰(≥3根强势空头趋势棒)
                strong_bear_count = 0
                check_start = max(recent_high_idx + 1, i - 5)
                for j in range(check_start, i + 1):
                    if j >= len(bars):
                        break
                    if bars[j].is_bear and self.classifier.is_strong_trend_bar(bars[j], avg_entity):
                        strong_bear_count += 1
                if strong_bear_count >= config.MTR_BREAKOUT_MIN_BARS:
                    break_index = i
                    break

        if break_index is None:
            return None

        # 条件3: 测试极点(价格反弹测试前高)
        test_index = None
        for i in range(break_index + 1, len(bars)):
            # 测试前高(更高高点/双重顶/更低高点,在0.5×ATR容差内)
            if abs(bars[i].high - recent_high) <= config.MTR_TEST_ATR_TOLERANCE * atr:
                test_index = i
                break
            # 也接受接近前高
            if bars[i].high >= recent_high - config.MTR_TEST_ATR_TOLERANCE * atr:
                test_index = i
                break

        if test_index is None:
            return None

        # 条件4: 二次反转(测试后强空头尖峰)
        for i in range(test_index + 1, len(bars)):
            strong_bear_count = 0
            for j in range(test_index + 1, min(i + 1, len(bars))):
                if bars[j].is_bear and self.classifier.is_strong_trend_bar(bars[j], avg_entity):
                    strong_bear_count += 1
            if strong_bear_count >= config.MTR_BREAKOUT_MIN_BARS:
                # MTR确认! 检测信号棒
                signal_bars = self.detector.detect_all_signals(bars, i, avg_entity, atr)
                short_signals = [s for s in signal_bars if s.direction == "short" and s.is_tradeable]
                if short_signals:
                    return self._create_signal(
                        interval, "mtr_conservative", "short",
                        short_signals[0], market_state, bars, atr
                    )

        # 激进入场: 条件3测试极点处的反转棒
        signal_bars = self.detector.detect_all_signals(bars, test_index, avg_entity, atr)
        short_signals = [s for s in signal_bars if s.direction == "short" and s.is_tradeable]
        if short_signals:
            return self._create_signal(
                interval, "mtr_aggressive", "short",
                short_signals[0], market_state, bars, atr
            )

        return None

    def _detect_bear_to_bull_mtr(self, bars: List[BarData], market_state: MarketStateResult,
                                  interval: str, avg_entity: float, atr: float) -> Optional[TradingSignal]:
        """检测空头→多头MTR(与多头→空头对称)"""
        ema_series = market_state.ema_series
        if len(ema_series) < len(bars):
            return None

        # 找到最近的低点
        lookback = min(50, len(bars))
        recent_bars = bars[-lookback:]
        low_idx_offset = int(np.argmin([b.low for b in recent_bars]))
        recent_low_idx = len(bars) - lookback + low_idx_offset
        recent_low = bars[recent_low_idx].low

        # 条件2: 突破EMA + 强多头尖峰
        break_index = None
        for i in range(recent_low_idx + 1, len(bars)):
            bar = bars[i]
            if bar.close > ema_series[i]:
                strong_bull_count = 0
                check_start = max(recent_low_idx + 1, i - 5)
                for j in range(check_start, i + 1):
                    if j >= len(bars):
                        break
                    if bars[j].is_bull and self.classifier.is_strong_trend_bar(bars[j], avg_entity):
                        strong_bull_count += 1
                if strong_bull_count >= config.MTR_BREAKOUT_MIN_BARS:
                    break_index = i
                    break

        if break_index is None:
            return None

        # 条件3: 测试极点
        test_index = None
        for i in range(break_index + 1, len(bars)):
            if abs(bars[i].low - recent_low) <= config.MTR_TEST_ATR_TOLERANCE * atr:
                test_index = i
                break
            if bars[i].low <= recent_low + config.MTR_TEST_ATR_TOLERANCE * atr:
                test_index = i
                break

        if test_index is None:
            return None

        # 条件4: 二次反转
        for i in range(test_index + 1, len(bars)):
            strong_bull_count = 0
            for j in range(test_index + 1, min(i + 1, len(bars))):
                if bars[j].is_bull and self.classifier.is_strong_trend_bar(bars[j], avg_entity):
                    strong_bull_count += 1
            if strong_bull_count >= config.MTR_BREAKOUT_MIN_BARS:
                signal_bars = self.detector.detect_all_signals(bars, i, avg_entity, atr)
                long_signals = [s for s in signal_bars if s.direction == "long" and s.is_tradeable]
                if long_signals:
                    return self._create_signal(
                        interval, "mtr_conservative", "long",
                        long_signals[0], market_state, bars, atr
                    )

        # 激进入场
        signal_bars = self.detector.detect_all_signals(bars, test_index, avg_entity, atr)
        long_signals = [s for s in signal_bars if s.direction == "long" and s.is_tradeable]
        if long_signals:
            return self._create_signal(
                interval, "mtr_aggressive", "long",
                long_signals[0], market_state, bars, atr
            )

        return None

    # ====================== 2. 高潮反转 ======================

    def _detect_climax_reversal(self, bars: List[BarData], market_state: MarketStateResult,
                                 interval: str, avg_entity: float, atr: float) -> List[TradingSignal]:
        """检测高潮反转

        买进高潮: 连续3+多头趋势棒,最后一棒实体≥3×平均,随后暂停棒+空头反转
        卖出高潮: 对称定义
        """
        signals = []

        # 买进高潮反转(多头→空头)
        sig = self._detect_buy_climax(bars, market_state, interval, avg_entity, atr)
        if sig:
            signals.append(sig)

        # 卖出高潮反转(空头→多头)
        sig = self._detect_sell_climax(bars, market_state, interval, avg_entity, atr)
        if sig:
            signals.append(sig)

        return signals

    def _detect_buy_climax(self, bars: List[BarData], market_state: MarketStateResult,
                            interval: str, avg_entity: float, atr: float) -> Optional[TradingSignal]:
        """检测买进高潮反转"""
        if len(bars) < 5:
            return None

        # 从最新棒向前找耗尽型多头棒
        for i in range(len(bars) - 1, max(0, len(bars) - 20), -1):
            bar = bars[i]
            # 必须是耗尽棒(实体≥3×平均)且是多头趋势棒
            if not self.detector.is_exhaustion_bar(bar, avg_entity):
                continue
            if not bar.is_bull:
                continue

            # 前面至少2根多头趋势棒(连续3+多头趋势棒)
            bull_count = 0
            for j in range(i - 1, max(0, i - 5), -1):
                if bars[j].is_bull:
                    bull_count += 1
                else:
                    break
            if bull_count < 2:
                continue

            # 检查高潮后是否有暂停棒(上尾线长/十字星/空头棒)
            if i + 1 >= len(bars):
                continue
            pause_bar = bars[i + 1]
            is_pause = (
                pause_bar.upper_tail > pause_bar.range * 0.33 or
                pause_bar.is_doji or
                pause_bar.is_bear
            )
            if not is_pause:
                continue

            # 检查暂停棒后是否有空头反转信号
            signal_bars = self.detector.detect_all_signals(bars, i + 1, avg_entity, atr)
            short_signals = [s for s in signal_bars if s.direction == "short" and s.is_tradeable]
            if short_signals:
                return self._create_signal(
                    interval, "climax_reversal", "short",
                    short_signals[0], market_state, bars, atr
                )

        return None

    def _detect_sell_climax(self, bars: List[BarData], market_state: MarketStateResult,
                             interval: str, avg_entity: float, atr: float) -> Optional[TradingSignal]:
        """检测卖出高潮反转(与买进高潮对称)"""
        if len(bars) < 5:
            return None

        for i in range(len(bars) - 1, max(0, len(bars) - 20), -1):
            bar = bars[i]
            if not self.detector.is_exhaustion_bar(bar, avg_entity):
                continue
            if not bar.is_bear:
                continue

            bear_count = 0
            for j in range(i - 1, max(0, i - 5), -1):
                if bars[j].is_bear:
                    bear_count += 1
                else:
                    break
            if bear_count < 2:
                continue

            if i + 1 >= len(bars):
                continue
            pause_bar = bars[i + 1]
            is_pause = (
                pause_bar.lower_tail > pause_bar.range * 0.33 or
                pause_bar.is_doji or
                pause_bar.is_bull
            )
            if not is_pause:
                continue

            signal_bars = self.detector.detect_all_signals(bars, i + 1, avg_entity, atr)
            long_signals = [s for s in signal_bars if s.direction == "long" and s.is_tradeable]
            if long_signals:
                return self._create_signal(
                    interval, "climax_reversal", "long",
                    long_signals[0], market_state, bars, atr
                )

        return None

    # ====================== 3. 楔形三推反转 ======================

    def _detect_wedge_reversal(self, bars: List[BarData], market_state: MarketStateResult,
                                interval: str, avg_entity: float, atr: float) -> List[TradingSignal]:
        """检测楔形三推反转

        楔形顶: 3次上推,高点递增但超出幅度递减
        楔形底: 3次下推,低点递减但超出幅度递减
        持续≥20棒
        楔形反转是逆势架构,最好等待二次信号
        """
        signals = []

        highs_arr = [b.high for b in bars]
        lows_arr = [b.low for b in bars]
        swing_highs = find_swing_highs(highs_arr, lookback=2)
        swing_lows = find_swing_lows(lows_arr, lookback=2)

        # 楔形顶(多头→空头反转)
        if len(swing_highs) >= 3:
            last_3 = swing_highs[-3:]
            # 高点依次升高
            if last_3[0][1] < last_3[1][1] < last_3[2][1]:
                # 超出幅度递减(收缩)
                excess1 = last_3[1][1] - last_3[0][1]
                excess2 = last_3[2][1] - last_3[1][1]
                if excess2 < excess1 and excess1 > 0:
                    # 持续≥20棒
                    duration = last_3[2][0] - last_3[0][0]
                    if duration >= 20:
                        # 寻找楔形顶后的做空信号(二次信号优先)
                        latest_idx = last_3[2][0]
                        if latest_idx < len(bars) - 1:
                            # 检查是否有更低高点(二次信号)
                            for i in range(latest_idx + 1, len(bars)):
                                if bars[i].high < bars[latest_idx].high:
                                    # 出现更低高点,检测做空信号
                                    signal_bars = self.detector.detect_all_signals(
                                        bars, i, avg_entity, atr
                                    )
                                    short_sigs = [s for s in signal_bars
                                                  if s.direction == "short" and s.is_tradeable]
                                    if short_sigs:
                                        signals.append(self._create_signal(
                                            interval, "mtr_aggressive", "short",
                                            short_sigs[0], market_state, bars, atr
                                        ))
                                        break

        # 楔形底(空头→多头反转)
        if len(swing_lows) >= 3:
            last_3 = swing_lows[-3:]
            # 低点依次降低
            if last_3[0][1] > last_3[1][1] > last_3[2][1]:
                excess1 = last_3[0][1] - last_3[1][1]
                excess2 = last_3[1][1] - last_3[2][1]
                if excess2 < excess1 and excess1 > 0:
                    duration = last_3[2][0] - last_3[0][0]
                    if duration >= 20:
                        latest_idx = last_3[2][0]
                        if latest_idx < len(bars) - 1:
                            for i in range(latest_idx + 1, len(bars)):
                                if bars[i].low > bars[latest_idx].low:
                                    signal_bars = self.detector.detect_all_signals(
                                        bars, i, avg_entity, atr
                                    )
                                    long_sigs = [s for s in signal_bars
                                                 if s.direction == "long" and s.is_tradeable]
                                    if long_sigs:
                                        signals.append(self._create_signal(
                                            interval, "mtr_aggressive", "long",
                                            long_sigs[0], market_state, bars, atr
                                        ))
                                        break

        return signals

    # ====================== 4. 最终旗形 ======================

    def _detect_final_flag(self, bars: List[BarData], market_state: MarketStateResult,
                            interval: str, avg_entity: float, atr: float) -> List[TradingSignal]:
        """检测最终旗形反转

        条件:
        1. 趋势已行进≥50棒
        2. 出现水平旗形(5-10棒横盘)
        3. 旗形内强双向交易(多空棒混合)
        4. 突破后快速反转
        """
        signals = []

        # 条件1: 趋势≥50棒
        if market_state.trend_bars < config.FINAL_FLAG_MIN_TREND_BARS:
            return signals

        # 条件2: 最近5-10棒横盘(水平旗形)
        flag_bars = config.FINAL_FLAG_BARS
        if len(bars) < flag_bars + 2:
            return signals

        recent_bars = bars[-(flag_bars + 1):-1]  # 不含最新棒
        range_high = max(b.high for b in recent_bars)
        range_low = min(b.low for b in recent_bars)
        flag_range = range_high - range_low

        # 旗形范围相对较小(< 2×ATR)
        if flag_range > 2 * atr or flag_range <= 0:
            return signals

        # 条件3: 旗形内强双向交易(多空棒混合)
        bull_count = sum(1 for b in recent_bars if b.is_bull)
        bear_count = sum(1 for b in recent_bars if b.is_bear)
        if bull_count == 0 or bear_count == 0:
            return signals  # 单向运动,不是旗形

        # 条件4: 突破后快速反转
        last_bar = bars[-1]
        prev_bar = bars[-2]

        # 向上突破后空头反转
        if prev_bar.close > range_high and last_bar.is_bear:
            signal_bars = self.detector.detect_all_signals(bars, len(bars) - 1, avg_entity, atr)
            short_sigs = [s for s in signal_bars if s.direction == "short" and s.is_tradeable]
            if short_sigs:
                signals.append(self._create_signal(
                    interval, "final_flag", "short",
                    short_sigs[0], market_state, bars, atr
                ))

        # 向下突破后多头反转
        if prev_bar.close < range_low and last_bar.is_bull:
            signal_bars = self.detector.detect_all_signals(bars, len(bars) - 1, avg_entity, atr)
            long_sigs = [s for s in signal_bars if s.direction == "long" and s.is_tradeable]
            if long_sigs:
                signals.append(self._create_signal(
                    interval, "final_flag", "long",
                    long_sigs[0], market_state, bars, atr
                ))

        return signals

    # ====================== 5. 失败的失败 ======================

    def _detect_failed_failure(self, bars: List[BarData], market_state: MarketStateResult,
                                interval: str, avg_entity: float, atr: float) -> List[TradingSignal]:
        """检测失败的失败

        逻辑链:
        原始突破A → A失败(回撤突破点) → 反向运动B → B也失败 → 原方向恢复 = 突破回撤
        失败的失败拥有较高的胜率(二次信号更可靠)
        """
        signals = []

        highs_arr = [b.high for b in bars]
        lows_arr = [b.low for b in bars]
        swing_highs = find_swing_highs(highs_arr, lookback=2)
        swing_lows = find_swing_lows(lows_arr, lookback=2)

        # 向上突破失败的失败
        if swing_highs:
            last_swing = swing_highs[-1]
            breakout_idx = last_swing[0]
            breakout_price = last_swing[1]

            # 突破后1-5棒内回撤突破点下方(A失败)
            for i in range(breakout_idx + 1, min(len(bars), breakout_idx + 6)):
                if bars[i].close < breakout_price:
                    # A失败! 检查B是否也失败
                    for j in range(i + 1, min(len(bars), i + 6)):
                        # B失败: 价格恢复原方向(回到突破点上方)
                        if bars[j].close > breakout_price:
                            # 失败的失败! 检测做多信号
                            signal_bars = self.detector.detect_all_signals(
                                bars, j, avg_entity, atr
                            )
                            long_sigs = [s for s in signal_bars
                                         if s.direction == "long" and s.is_tradeable]
                            if long_sigs:
                                signals.append(self._create_signal(
                                    interval, "failed_failure", "long",
                                    long_sigs[0], market_state, bars, atr
                                ))
                            break
                    break

        # 向下突破失败的失败(对称)
        if swing_lows:
            last_swing = swing_lows[-1]
            breakout_idx = last_swing[0]
            breakout_price = last_swing[1]

            for i in range(breakout_idx + 1, min(len(bars), breakout_idx + 6)):
                if bars[i].close > breakout_price:
                    for j in range(i + 1, min(len(bars), i + 6)):
                        if bars[j].close < breakout_price:
                            signal_bars = self.detector.detect_all_signals(
                                bars, j, avg_entity, atr
                            )
                            short_sigs = [s for s in signal_bars
                                          if s.direction == "short" and s.is_tradeable]
                            if short_sigs:
                                signals.append(self._create_signal(
                                    interval, "failed_failure", "short",
                                    short_sigs[0], market_state, bars, atr
                                ))
                            break
                    break

        return signals

    # ====================== 信号创建 ======================

    def _create_signal(self, interval: str, setup_type: str, direction: str,
                       signal_bar: SignalBar, market_state: MarketStateResult,
                       bars: List[BarData], atr: float) -> Optional[TradingSignal]:
        """创建交易信号(含止损止盈计算)"""
        grade = signal_bar.grade
        if grade not in config.SIGNAL_GRADES_ALLOWED:
            return None

        # 计算止损
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
