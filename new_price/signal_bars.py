# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 棒线分类与信号棒识别模块

【已锁定定义(趋势篇)】
- 趋势棒: "带有实体的棒线，收盘价高于或低于开盘价"
- 十字星: "实体很小或根本没有实体…非趋势棒线就叫十字星"(>0.20伪Doji排除)
- 反转棒: "与趋势方向相反的趋势棒，多头反转棒底部有下尾线、收盘靠近顶部"
- 信号棒: "入场棒前面相邻的一条棒线，是架构的最后一棒"
- 内包棒: "高点≤前棒高点，低点≥前棒低点"; ii/ioi/oio 为组合

移植自旧系统 bar_patterns.py(已验证)，含强势趋势棒5判据 + 反转棒重叠度过滤。
"""
from dataclasses import dataclass, field
from typing import List, Optional

import config
from indicators import calc_avg_entity


@dataclass
class BarData:
    """单根K线(Brooks分析最小单位)"""
    index: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    taker_buy: float = 0.0
    open_time: int = 0
    close_time: int = 0

    @property
    def body(self):
        return abs(self.close - self.open)

    @property
    def range(self):
        return self.high - self.low

    @property
    def upper_tail(self):
        return self.high - max(self.open, self.close)

    @property
    def lower_tail(self):
        return min(self.open, self.close) - self.low

    @property
    def is_bull(self):
        return self.close > self.open

    @property
    def is_bear(self):
        return self.close < self.open

    @property
    def is_doji(self):
        if self.range == 0:
            return True
        return self.body / self.range <= config.DOJI_BODY_RATIO

    @property
    def midpoint(self):
        return (self.high + self.low) / 2.0

    @property
    def body_ratio(self):
        if self.range == 0:
            return 0.0
        return self.body / self.range

    @property
    def is_shaved_top(self):
        return self.high == max(self.open, self.close)

    @property
    def is_shaved_bottom(self):
        return self.low == min(self.open, self.close)

    @property
    def is_shaved(self):
        return self.is_shaved_top and self.is_shaved_bottom


@dataclass
class SignalBar:
    """信号棒识别结果"""
    index: int
    pattern: str
    direction: str                    # long / short / both
    grade: str                        # strongest / strong / general
    entry_price: float
    signal_high: float
    signal_low: float
    is_tradeable: bool
    reasons: List[str] = field(default_factory=list)


def build_bars(arr):
    """把缓存数组(data.KlineCache.get_arrays 返回的 dict)转成 BarData 列表。"""
    bars = []
    n = len(arr["closes"])
    for i in range(n):
        bars.append(BarData(
            index=i,
            open=arr["opens"][i], high=arr["highs"][i],
            low=arr["lows"][i], close=arr["closes"][i],
            volume=arr["volumes"][i], taker_buy=arr["taker_buys"][i],
            open_time=0, close_time=arr["close_times"][i],
        ))
    return bars


class BarClassifier:
    """棒线分类器: 每根K线要么是趋势棒(有实体)，要么是十字星(实体小)。"""

    @staticmethod
    def classify(bar: BarData) -> str:
        if bar.is_doji:
            return "doji"
        if bar.is_bull:
            return "trend_bull"
        return "trend_bear"

    @staticmethod
    def is_strong_trend_bar(bar: BarData, avg_entity: float) -> bool:
        """强势趋势棒5判据(原文P63)，满足≥3个=强势。

        1. 实体≥20棒平均实体  2. 开盘靠近极值
        3. 收盘靠近极值        4. 尾线短(≤棒高50%)
        """
        if bar.is_doji or bar.range == 0:
            return False
        cond1 = bar.body >= avg_entity * config.STRONG_BAR_MULT
        if bar.is_bull:
            cond2 = bar.open <= bar.low + bar.range * 0.25
            cond3 = bar.close >= bar.high - bar.range * 0.25
        else:
            cond2 = bar.open >= bar.high - bar.range * 0.25
            cond3 = bar.close <= bar.low + bar.range * 0.25
        cond4 = (bar.upper_tail + bar.lower_tail) <= bar.range * 0.50
        return sum([cond1, cond2, cond3, cond4]) >= 3

    @staticmethod
    def calc_signal_grade(bar: BarData, avg_entity: float) -> str:
        """信号强度分级(5分制)。≥5=strongest, ≥3=strong, 否则general。"""
        if bar.is_doji or bar.range == 0:
            return "general"
        score = 0
        if bar.body >= avg_entity * config.STRONG_BAR_MULT:
            score += 1
        if bar.is_bull:
            if bar.open <= bar.low + bar.range * 0.25:
                score += 1
        else:
            if bar.open >= bar.high - bar.range * 0.25:
                score += 1
        if bar.is_bull:
            if bar.close >= bar.high - bar.range * 0.25:
                score += 1
        else:
            if bar.close <= bar.low + bar.range * 0.25:
                score += 1
        if (bar.upper_tail + bar.lower_tail) <= bar.range * 0.50:
            score += 1
        if bar.is_shaved:
            score += 1
        if score >= 5:
            return "strongest"
        if score >= 3:
            return "strong"
        return "general"


class SignalBarDetector:
    """信号棒识别器: 反转棒 / 双棒反转 / 内包 / ii / ioi / 微型双重顶底 / 耗尽棒 / 外包棒。"""

    def __init__(self):
        self.classifier = BarClassifier()

    @staticmethod
    def _tick(price):
        return config.TICK_SIZE

    def detect_all(self, bars: List[BarData], index: int,
                   avg_entity: float, atr: float) -> List[SignalBar]:
        """检测指定位置的所有信号棒形态。"""
        if index < 1 or index >= len(bars):
            return []
        sigs = []
        for detector in (
            self.bull_reversal_bar, self.bear_reversal_bar,
            self.double_bar_reversal, self.inside_bar,
            self.ii_pattern, self.ioi_pattern,
            self.micro_double_bottom,
        ):
            s = detector(bars, index, avg_entity, atr)
            if s:
                sigs.append(s)
        return sigs

    def bull_reversal_bar(self, bars, index, avg_entity, atr) -> Optional[SignalBar]:
        """多头反转棒(原文P89-90)。"""
        if index < config.REVERSAL_CLOSE_LOOKBACK:
            return None
        bar, prev = bars[index], bars[index - 1]
        if bar.range == 0:
            return None
        reasons = []
        if not (bar.is_bull or bar.close > bar.midpoint):
            return None
        reasons.append("多头实体/收盘高于中点")
        if bar.lower_tail < bar.range * config.REVERSAL_TAIL_MIN:
            return None
        reasons.append(f"下尾线={bar.lower_tail:.2f}≥33%")
        if bar.upper_tail > bar.range * config.REVERSAL_TAIL_MAX:
            return None
        reasons.append(f"上尾线={bar.upper_tail:.2f}≤25%")
        if not (bar.body >= prev.body or bar.body >= avg_entity * config.REVERSAL_ENTITY_MIN):
            return None
        reasons.append(f"实体={bar.body:.2f}")
        lookback = min(config.REVERSAL_CLOSE_LOOKBACK, index)
        recent = [bars[index - i].close for i in range(1, lookback + 1)]
        rev_count = sum(1 for c in recent if bar.close > c)
        if not (bar.close > prev.close and rev_count >= config.REVERSAL_CLOSE_MIN_REVERSE):
            return None
        reasons.append(f"收盘反转{rev_count}/{lookback}棒")
        grade = self.classifier.calc_signal_grade(bar, avg_entity)
        if bar.midpoint > prev.low:          # 重叠过多→降级
            grade = "general"
            reasons.append("重叠过多→降级")
        return SignalBar(index, "bull_reversal", "long", grade,
                         bar.high + self._tick(bar.close), bar.high, bar.low,
                         grade in config.SIGNAL_GRADES_ALLOWED, reasons)

    def bear_reversal_bar(self, bars, index, avg_entity, atr) -> Optional[SignalBar]:
        """空头反转棒(与多头对称)。"""
        if index < config.REVERSAL_CLOSE_LOOKBACK:
            return None
        bar, prev = bars[index], bars[index - 1]
        if bar.range == 0:
            return None
        reasons = []
        if not (bar.is_bear or bar.close < bar.midpoint):
            return None
        reasons.append("空头实体/收盘低于中点")
        if bar.upper_tail < bar.range * config.REVERSAL_TAIL_MIN:
            return None
        reasons.append(f"上尾线={bar.upper_tail:.2f}≥33%")
        if bar.lower_tail > bar.range * config.REVERSAL_TAIL_MAX:
            return None
        reasons.append(f"下尾线={bar.lower_tail:.2f}≤25%")
        if not (bar.body >= prev.body or bar.body >= avg_entity * config.REVERSAL_ENTITY_MIN):
            return None
        reasons.append(f"实体={bar.body:.2f}")
        lookback = min(config.REVERSAL_CLOSE_LOOKBACK, index)
        recent = [bars[index - i].close for i in range(1, lookback + 1)]
        rev_count = sum(1 for c in recent if bar.close < c)
        if not (bar.close < prev.close and rev_count >= config.REVERSAL_CLOSE_MIN_REVERSE):
            return None
        reasons.append(f"收盘反转{rev_count}/{lookback}棒")
        grade = self.classifier.calc_signal_grade(bar, avg_entity)
        if bar.midpoint < prev.high:
            grade = "general"
            reasons.append("重叠过多→降级")
        return SignalBar(index, "bear_reversal", "short", grade,
                         bar.low - self._tick(bar.close), bar.high, bar.low,
                         grade in config.SIGNAL_GRADES_ALLOWED, reasons)

    def double_bar_reversal(self, bars, index, avg_entity, atr) -> Optional[SignalBar]:
        """双棒反转: 相邻两条趋势棒方向相反、大小相仿、第二棒收盘反转第一棒。"""
        if index < 1:
            return None
        bar, prev = bars[index], bars[index - 1]
        if bar.is_bull and prev.is_bear:
            direction, pattern = "long", "double_bull_reversal"
        elif bar.is_bear and prev.is_bull:
            direction, pattern = "short", "double_bear_reversal"
        else:
            return None
        if prev.body == 0:
            return None
        ratio = bar.body / prev.body
        if ratio < 0.5 or ratio > 2.0:
            return None
        if direction == "long" and bar.close <= prev.close:
            return None
        if direction == "short" and bar.close >= prev.close:
            return None
        grade = self.classifier.calc_signal_grade(bar, avg_entity)
        entry = (bar.high + self._tick(bar.close)) if direction == "long" \
            else (bar.low - self._tick(bar.close))
        return SignalBar(index, pattern, direction, grade, entry, bar.high, bar.low,
                         grade in config.SIGNAL_GRADES_ALLOWED,
                         [f"双棒反转,实体比={ratio:.2f}"])

    def inside_bar(self, bars, index, avg_entity, atr) -> Optional[SignalBar]:
        """内包棒: high≤前棒high 且 low≥前棒low。需突破确认，暂不可直接交易。"""
        if index < 1:
            return None
        bar, prev = bars[index], bars[index - 1]
        if bar.high > prev.high or bar.low < prev.low:
            return None
        grade = self.classifier.calc_signal_grade(bar, avg_entity) if not bar.is_doji else "general"
        return SignalBar(index, "inside_bar", "both", grade,
                         bar.high + self._tick(bar.close), bar.high, bar.low,
                         False, ["内包棒,待突破确认"])

    def ii_pattern(self, bars, index, avg_entity, atr) -> Optional[SignalBar]:
        """ii: 连续两条内包棒(突破型架构)。"""
        if index < 2:
            return None
        bar, prev, prev2 = bars[index], bars[index - 1], bars[index - 2]
        if bar.high > prev.high or bar.low < prev.low:
            return None
        if prev.high > prev2.high or prev.low < prev2.low:
            return None
        return SignalBar(index, "ii_pattern", "both", "strong",
                         prev2.high + self._tick(bar.close), prev2.high, prev2.low,
                         False, ["ii形态,突破型架构"])

    def ioi_pattern(self, bars, index, avg_entity, atr) -> Optional[SignalBar]:
        """ioi: 内包-外包-内包。"""
        if index < 3:
            return None
        bar, prev, prev2 = bars[index], bars[index - 1], bars[index - 2]
        if bar.high > prev.high or bar.low < prev.low:
            return None
        if prev.high <= prev2.high or prev.low >= prev2.low:
            return None
        return SignalBar(index, "ioi_pattern", "both", "strong",
                         prev.high + self._tick(bar.close), prev.high, prev.low,
                         False, ["ioi形态,突破方向入场"])

    def micro_double_bottom(self, bars, index, avg_entity, atr) -> Optional[SignalBar]:
        """微型双重底: 连续棒低点差≤0.5×ATR + 当前棒多头。"""
        if index < 1:
            return None
        bar, prev = bars[index], bars[index - 1]
        if abs(bar.low - prev.low) > atr * 0.5:
            return None
        if not bar.is_bull:
            return None
        grade = self.classifier.calc_signal_grade(bar, avg_entity)
        return SignalBar(index, "micro_double_bottom", "long", grade,
                         bar.high + self._tick(bar.close), bar.high, bar.low,
                         grade in config.SIGNAL_GRADES_ALLOWED,
                         [f"微型双重底,低点差={abs(bar.low-prev.low):.2f}"])

    def micro_double_top(self, bars, index, avg_entity, atr) -> Optional[SignalBar]:
        """微型双重顶(与双重底对称)。"""
        if index < 1:
            return None
        bar, prev = bars[index], bars[index - 1]
        if abs(bar.high - prev.high) > atr * 0.5:
            return None
        if not bar.is_bear:
            return None
        grade = self.classifier.calc_signal_grade(bar, avg_entity)
        return SignalBar(index, "micro_double_top", "short", grade,
                         bar.low - self._tick(bar.close), bar.high, bar.low,
                         grade in config.SIGNAL_GRADES_ALLOWED,
                         [f"微型双重顶,高点差={abs(bar.high-prev.high):.2f}"])

    def is_exhaustion_bar(self, bar: BarData, avg_entity: float) -> bool:
        """耗尽棒: 实体≥平均×3。趋势耗尽警告。"""
        if bar.is_doji or avg_entity == 0:
            return False
        return bar.body >= avg_entity * config.EXHAUSTION_MULT

    def is_outside_bar(self, bars, index) -> bool:
        """外包棒: high>前棒high 且 low<前棒low。"""
        if index < 1:
            return False
        bar, prev = bars[index], bars[index - 1]
        return bar.high > prev.high and bar.low < prev.low