# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 棒线分类与信号棒识别模块

理论依据：
- 趋势篇P59: "把所有棒线分为趋势棒和非趋势(交易区间)棒"
- 趋势篇P63: 强势趋势棒5大判据
- 趋势篇P89-92: 反转棒标准定义+重叠度过滤
- 趋势篇第7章: 外包棒处理规则
"""
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional

import config
from indicators import calc_avg_entity


# ====================== 数据结构 ======================

@dataclass
class BarData:
    """单根K线数据(Brooks分析的最小单位)"""
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
    def body(self) -> float:
        """实体大小"""
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        """整根K线高低差"""
        return self.high - self.low

    @property
    def upper_tail(self) -> float:
        """上尾线"""
        return self.high - max(self.open, self.close)

    @property
    def lower_tail(self) -> float:
        """下尾线"""
        return min(self.open, self.close) - self.low

    @property
    def is_bull(self) -> bool:
        """是否多头趋势棒"""
        return self.close > self.open

    @property
    def is_bear(self) -> bool:
        """是否空头趋势棒"""
        return self.close < self.open

    @property
    def is_doji(self) -> bool:
        """是否十字星(实体占比≤0.20)"""
        if self.range == 0:
            return True
        return self.body / self.range <= config.DOJI_BODY_RATIO

    @property
    def midpoint(self) -> float:
        """K线中点"""
        return (self.high + self.low) / 2.0

    @property
    def body_ratio(self) -> float:
        """实体占比(实体/整根K线)"""
        if self.range == 0:
            return 0.0
        return self.body / self.range

    @property
    def is_shaved_bottom(self) -> bool:
        """是否光脚(无下尾线)"""
        return self.low == min(self.open, self.close)

    @property
    def is_shaved_top(self) -> bool:
        """是否光头(无上尾线)"""
        return self.high == max(self.open, self.close)

    @property
    def is_shaved(self) -> bool:
        """是否光头光脚"""
        return self.is_shaved_top and self.is_shaved_bottom


@dataclass
class SignalBar:
    """信号棒识别结果"""
    index: int                        # 信号棒在序列中的位置
    pattern: str                      # 形态类型
    direction: str                    # "long" / "short"
    grade: str                        # "strongest" / "strong" / "general"
    entry_price: float                # 入场价(信号棒极值±1跳动)
    signal_high: float                # 信号棒最高价
    signal_low: float                 # 信号棒最低价
    is_tradeable: bool                # 是否可交易(通过重叠度过滤)
    reasons: List[str] = field(default_factory=list)  # 识别理由


@dataclass
class TradingSignal:
    """完整交易信号(系统最终输出)"""
    interval: str                   # 时间周期(5m/15m/30m/1h/4h)
    setup_type: str                 # 架构类型(h2_trend/mtr_conservative/...)
    direction: str                  # "long" / "short"
    grade: str                      # 信号强度(strongest/strong)
    entry_price: float              # 入场价
    stop_price: float               # 止损价
    tp1_price: float                # 止盈1(1R)
    tp2_price: float                # 止盈2(1.5R或2R/3R)
    tp1_ratio: float = 0.5          # TP1平仓比例
    tp2_ratio: float = 0.5          # TP2平仓比例
    signal_high: float = 0.0        # 信号棒最高价
    signal_low: float = 0.0         # 信号棒最低价
    risk_reward: float = 1.5        # 风险回报比
    market_state: str = ""          # 市场状态描述
    ema20: float = 0.0              # EMA20值
    atr: float = 0.0                # ATR值
    reasons: List[str] = field(default_factory=list)
    bar_index: int = 0              # 信号棒索引
    bar_close_time: int = 0         # K线收盘时间戳(ms)
    timestamp: str = ""             # 信号生成时间
    resonance: List[str] = field(default_factory=list)  # 共振周期


# ====================== 棒线分类器 ======================

class BarClassifier:
    """棒线分类器

    Brooks核心观点: 每根K线要么是趋势棒(有实体)，要么是十字星(实体小或无)
    反对细分上吊线/锤形线/孕线等子类，认为对交易无益
    """

    @staticmethod
    def classify(bar: BarData) -> str:
        """分类单根K线

        Returns:
            "trend_bull" / "trend_bear" / "doji"
        """
        if bar.is_doji:
            return "doji"
        if bar.is_bull:
            return "trend_bull"
        return "trend_bear"

    @staticmethod
    def is_strong_trend_bar(bar: BarData, avg_entity: float) -> bool:
        """判断是否为强势趋势棒(原文P63)

        5大判据:
        1. 实体≥20棒平均实体
        2. 开盘位于或靠近极值
        3. 收盘位于或靠近极值
        4. 尾线较短
        5. 光头光脚=最强
        """
        if bar.is_doji or bar.range == 0:
            return False

        # 条件1: 实体≥平均实体
        cond1 = bar.body >= avg_entity * config.STRONG_BAR_MULT

        # 条件2: 开盘靠近极值
        if bar.is_bull:
            cond2 = bar.open <= bar.low + bar.range * 0.25
        else:
            cond2 = bar.open >= bar.high - bar.range * 0.25

        # 条件3: 收盘靠近极值
        if bar.is_bull:
            cond3 = bar.close >= bar.high - bar.range * 0.25
        else:
            cond3 = bar.close <= bar.low + bar.range * 0.25

        # 条件4: 尾线较短(上尾+下尾≤棒高50%)
        cond4 = (bar.upper_tail + bar.lower_tail) <= bar.range * 0.50

        # 满足3个以上条件=强势
        conditions = [cond1, cond2, cond3, cond4]
        return sum(conditions) >= 3

    @staticmethod
    def calc_signal_grade(bar: BarData, avg_entity: float) -> str:
        """计算信号强度等级

        Returns:
            "strongest" / "strong" / "general"
        """
        if bar.is_doji or bar.range == 0:
            return "general"

        score = 0

        # 条件1: 实体≥平均实体
        if bar.body >= avg_entity * config.STRONG_BAR_MULT:
            score += 1

        # 条件2: 开盘靠近极值
        if bar.is_bull:
            if bar.open <= bar.low + bar.range * 0.25:
                score += 1
        else:
            if bar.open >= bar.high - bar.range * 0.25:
                score += 1

        # 条件3: 收盘靠近极值
        if bar.is_bull:
            if bar.close >= bar.high - bar.range * 0.25:
                score += 1
        else:
            if bar.close <= bar.low + bar.range * 0.25:
                score += 1

        # 条件4: 尾线短
        if (bar.upper_tail + bar.lower_tail) <= bar.range * 0.50:
            score += 1

        # 条件5: 光头光脚
        if bar.is_shaved:
            score += 1

        if score >= 5:
            return "strongest"
        elif score >= 3:
            return "strong"
        return "general"


# ====================== 信号棒识别器 ======================

class SignalBarDetector:
    """信号棒识别器

    识别Brooks体系中的各种入场信号棒形态
    """

    def __init__(self):
        self.classifier = BarClassifier()

    def detect_all_signals(self, bars: List[BarData], index: int,
                           avg_entity: float, atr: float) -> List[SignalBar]:
        """检测指定位置的所有信号棒形态

        Args:
            bars: K线序列
            index: 当前检测的K线索引
            avg_entity: 20棒平均实体
            atr: 当前ATR

        Returns:
            识别到的信号棒列表
        """
        signals = []
        if index < 1 or index >= len(bars):
            return signals

        bar = bars[index]
        prev = bars[index - 1]

        # 多头反转棒
        sig = self.detect_bull_reversal_bar(bars, index, avg_entity)
        if sig:
            signals.append(sig)

        # 空头反转棒
        sig = self.detect_bear_reversal_bar(bars, index, avg_entity)
        if sig:
            signals.append(sig)

        # 双棒反转
        sig = self.detect_double_bar_reversal(bars, index, avg_entity)
        if sig:
            signals.append(sig)

        # 内包棒
        sig = self.detect_inside_bar(bars, index, avg_entity)
        if sig:
            signals.append(sig)

        # ii形态
        sig = self.detect_ii_pattern(bars, index, avg_entity)
        if sig:
            signals.append(sig)

        # ioi形态
        sig = self.detect_ioi_pattern(bars, index, avg_entity)
        if sig:
            signals.append(sig)

        # 微型双重底
        sig = self.detect_micro_double_bottom(bars, index, atr, avg_entity)
        if sig:
            signals.append(sig)

        # 微型双重顶
        sig = self.detect_micro_double_top(bars, index, atr, avg_entity)
        if sig:
            signals.append(sig)

        return signals

    def detect_bull_reversal_bar(self, bars: List[BarData], index: int,
                                  avg_entity: float) -> Optional[SignalBar]:
        """检测多头反转棒(原文P89-90)

        判定条件:
        1. close > open(多头实体)或 close > 中点
        2. 下尾线 ≥ 棒高×0.33
        3. 上尾线 ≤ 棒高×0.25
        4. 实体 ≥ 前棒实体 或 ≥ 20棒平均×0.8
        5. close > 前棒close 且 close > 前5棒中≥3棒的close
        6. 重叠度过滤: 反转棒中点 > 前棒low → 降级
        """
        if index < config.REVERSAL_CLOSE_LOOKBACK or index >= len(bars):
            return None

        bar = bars[index]
        prev = bars[index - 1]

        if bar.range == 0:
            return None

        reasons = []

        # 条件1: 多头实体或收盘高于中点
        cond1 = bar.is_bull or bar.close > bar.midpoint
        if not cond1:
            return None
        reasons.append("多头实体/收盘高于中点")

        # 条件2: 下尾线≥棒高×0.33
        cond2 = bar.lower_tail >= bar.range * config.REVERSAL_TAIL_MIN
        if not cond2:
            return None
        reasons.append(f"下尾线={bar.lower_tail:.2f}≥33%")

        # 条件3: 上尾线≤棒高×0.25
        cond3 = bar.upper_tail <= bar.range * config.REVERSAL_TAIL_MAX
        if not cond3:
            return None
        reasons.append(f"上尾线={bar.upper_tail:.2f}≤25%")

        # 条件4: 实体强度
        cond4 = (bar.body >= prev.body or
                 bar.body >= avg_entity * config.REVERSAL_ENTITY_MIN)
        if not cond4:
            return None
        reasons.append(f"实体={bar.body:.2f}")

        # 条件5: 收盘反转(>前棒close且>前5棒中≥3棒的close)
        lookback = min(config.REVERSAL_CLOSE_LOOKBACK, index)
        recent_closes = [bars[index - i].close for i in range(1, lookback + 1)]
        reversed_count = sum(1 for c in recent_closes if bar.close > c)
        cond5 = bar.close > prev.close and reversed_count >= config.REVERSAL_CLOSE_MIN_REVERSE
        if not cond5:
            return None
        reasons.append(f"收盘反转{reversed_count}/{lookback}棒")

        # 条件6: 重叠度过滤(中点>前棒low=重叠过多→降级)
        overlap_excessive = bar.midpoint > prev.low

        # 计算信号强度
        grade = self.classifier.calc_signal_grade(bar, avg_entity)

        # 重叠过多则降级为general
        if overlap_excessive:
            grade = "general"
            reasons.append("重叠过多→降级")

        # 入场价=信号棒高点上方1跳动(止损单入场)
        tick = self._get_tick_size(bar.close)
        entry_price = bar.high + tick

        return SignalBar(
            index=index,
            pattern="bull_reversal",
            direction="long",
            grade=grade,
            entry_price=entry_price,
            signal_high=bar.high,
            signal_low=bar.low,
            is_tradeable=grade in config.SIGNAL_GRADES_ALLOWED,
            reasons=reasons
        )

    def detect_bear_reversal_bar(self, bars: List[BarData], index: int,
                                  avg_entity: float) -> Optional[SignalBar]:
        """检测空头反转棒(与多头对称)"""
        if index < config.REVERSAL_CLOSE_LOOKBACK or index >= len(bars):
            return None

        bar = bars[index]
        prev = bars[index - 1]

        if bar.range == 0:
            return None

        reasons = []

        # 条件1: 空头实体或收盘低于中点
        cond1 = bar.is_bear or bar.close < bar.midpoint
        if not cond1:
            return None
        reasons.append("空头实体/收盘低于中点")

        # 条件2: 上尾线≥棒高×0.33
        cond2 = bar.upper_tail >= bar.range * config.REVERSAL_TAIL_MIN
        if not cond2:
            return None
        reasons.append(f"上尾线={bar.upper_tail:.2f}≥33%")

        # 条件3: 下尾线≤棒高×0.25
        cond3 = bar.lower_tail <= bar.range * config.REVERSAL_TAIL_MAX
        if not cond3:
            return None
        reasons.append(f"下尾线={bar.lower_tail:.2f}≤25%")

        # 条件4: 实体强度
        cond4 = (bar.body >= prev.body or
                 bar.body >= avg_entity * config.REVERSAL_ENTITY_MIN)
        if not cond4:
            return None
        reasons.append(f"实体={bar.body:.2f}")

        # 条件5: 收盘反转
        lookback = min(config.REVERSAL_CLOSE_LOOKBACK, index)
        recent_closes = [bars[index - i].close for i in range(1, lookback + 1)]
        reversed_count = sum(1 for c in recent_closes if bar.close < c)
        cond5 = bar.close < prev.close and reversed_count >= config.REVERSAL_CLOSE_MIN_REVERSE
        if not cond5:
            return None
        reasons.append(f"收盘反转{reversed_count}/{lookback}棒")

        # 条件6: 重叠度过滤
        overlap_excessive = bar.midpoint < prev.high

        grade = self.classifier.calc_signal_grade(bar, avg_entity)
        if overlap_excessive:
            grade = "general"
            reasons.append("重叠过多→降级")

        tick = self._get_tick_size(bar.close)
        entry_price = bar.low - tick

        return SignalBar(
            index=index,
            pattern="bear_reversal",
            direction="short",
            grade=grade,
            entry_price=entry_price,
            signal_high=bar.high,
            signal_low=bar.low,
            is_tradeable=grade in config.SIGNAL_GRADES_ALLOWED,
            reasons=reasons
        )

    def detect_double_bar_reversal(self, bars: List[BarData], index: int,
                                    avg_entity: float) -> Optional[SignalBar]:
        """检测双棒反转

        定义: 相邻两条趋势棒，大小相仿方向相反，第二棒收盘反转第一棒
        是最常见的反转架构
        """
        if index < 1 or index >= len(bars):
            return None

        bar = bars[index]
        prev = bars[index - 1]

        # 必须方向相反
        if bar.is_bull and prev.is_bear:
            direction = "long"
            pattern = "double_bull_reversal"
        elif bar.is_bear and prev.is_bull:
            direction = "short"
            pattern = "double_bear_reversal"
        else:
            return None

        # 大小相仿(实体差异不超过2倍)
        if prev.body == 0:
            return None
        ratio = bar.body / prev.body
        if ratio < 0.5 or ratio > 2.0:
            return None

        # 第二棒收盘反转第一棒
        if direction == "long" and bar.close <= prev.close:
            return None
        if direction == "short" and bar.close >= prev.close:
            return None

        grade = self.classifier.calc_signal_grade(bar, avg_entity)
        tick = self._get_tick_size(bar.close)

        if direction == "long":
            entry_price = bar.high + tick
        else:
            entry_price = bar.low - tick

        return SignalBar(
            index=index,
            pattern=pattern,
            direction=direction,
            grade=grade,
            entry_price=entry_price,
            signal_high=bar.high,
            signal_low=bar.low,
            is_tradeable=grade in config.SIGNAL_GRADES_ALLOWED,
            reasons=[f"双棒反转, 实体比={ratio:.2f}"]
        )

    def detect_inside_bar(self, bars: List[BarData], index: int,
                           avg_entity: float) -> Optional[SignalBar]:
        """检测内包棒

        定义: high≤前棒high 且 low≥前棒low
        本质: 波幅收缩、暂停
        """
        if index < 1 or index >= len(bars):
            return None

        bar = bars[index]
        prev = bars[index - 1]

        # 内包条件
        if bar.high > prev.high or bar.low < prev.low:
            return None

        # 内包棒本身不直接给出方向信号
        # 需要结合背景判断突破方向
        # 这里返回双向准备信号
        grade = self.classifier.calc_signal_grade(bar, avg_entity) if not bar.is_doji else "general"

        tick = self._get_tick_size(bar.close)

        # 双向信号: 突破内包棒高点做多，突破低点做空
        return SignalBar(
            index=index,
            pattern="inside_bar",
            direction="both",
            grade=grade,
            entry_price=bar.high + tick,  # 默认做多方向
            signal_high=bar.high,
            signal_low=bar.low,
            is_tradeable=False,  # 内包棒需要突破确认才可交易
            reasons=["内包棒, 待突破确认"]
        )

    def detect_ii_pattern(self, bars: List[BarData], index: int,
                           avg_entity: float) -> Optional[SignalBar]:
        """检测ii形态(连续2条内包棒)

        突破型架构: 突破ii极值入场
        """
        if index < 2 or index >= len(bars):
            return None

        bar = bars[index]
        prev = bars[index - 1]
        prev2 = bars[index - 2]

        # 两条都是内包棒
        if bar.high > prev.high or bar.low < prev.low:
            return None
        if prev.high > prev2.high or prev.low < prev2.low:
            return None

        tick = self._get_tick_size(bar.close)
        # 突破第一条内包棒(prev2)的极值
        return SignalBar(
            index=index,
            pattern="ii_pattern",
            direction="both",
            grade="strong",
            entry_price=prev2.high + tick,
            signal_high=prev2.high,
            signal_low=prev2.low,
            is_tradeable=False,  # 需突破确认
            reasons=["ii形态, 突破型架构"]
        )

    def detect_ioi_pattern(self, bars: List[BarData], index: int,
                            avg_entity: float) -> Optional[SignalBar]:
        """检测ioi形态(内包-外包-内包)

        突破方向入场
        """
        if index < 3 or index >= len(bars):
            return None

        bar = bars[index]        # 内包
        prev = bars[index - 1]   # 外包
        prev2 = bars[index - 2]  # 内包

        # 验证: 内包-外包-内包
        if bar.high > prev.high or bar.low < prev.low:
            return None
        if prev.high <= prev2.high or prev.low >= prev2.low:
            return None
        if prev2.high > bars[index - 3].high or prev2.low < bars[index - 3].low:
            # prev2不一定是内包，放宽条件
            pass

        tick = self._get_tick_size(bar.close)
        return SignalBar(
            index=index,
            pattern="ioi_pattern",
            direction="both",
            grade="strong",
            entry_price=prev.high + tick,
            signal_high=prev.high,
            signal_low=prev.low,
            is_tradeable=False,
            reasons=["ioi形态, 突破方向入场"]
        )

    def detect_micro_double_bottom(self, bars: List[BarData], index: int,
                                     atr: float, avg_entity: float) -> Optional[SignalBar]:
        """检测微型双重底

        定义: 连续棒线低点相同或接近(差值≤0.5×ATR)
        尖峰中=旗形; 其他位置=反转
        """
        if index < 2 or index >= len(bars):
            return None

        bar = bars[index]
        prev = bars[index - 1]

        # 低点相近
        if abs(bar.low - prev.low) > atr * 0.5:
            return None

        # 当前棒必须是多头方向
        if not bar.is_bull:
            return None

        grade = self.classifier.calc_signal_grade(bar, avg_entity)
        tick = self._get_tick_size(bar.close)

        return SignalBar(
            index=index,
            pattern="micro_double_bottom",
            direction="long",
            grade=grade,
            entry_price=bar.high + tick,
            signal_high=bar.high,
            signal_low=bar.low,
            is_tradeable=grade in config.SIGNAL_GRADES_ALLOWED,
            reasons=[f"微型双重底, 低点差={abs(bar.low - prev.low):.2f}"]
        )

    def detect_micro_double_top(self, bars: List[BarData], index: int,
                                  atr: float, avg_entity: float) -> Optional[SignalBar]:
        """检测微型双重顶(与双重底对称)"""
        if index < 2 or index >= len(bars):
            return None

        bar = bars[index]
        prev = bars[index - 1]

        if abs(bar.high - prev.high) > atr * 0.5:
            return None

        if not bar.is_bear:
            return None

        grade = self.classifier.calc_signal_grade(bar, avg_entity)
        tick = self._get_tick_size(bar.close)

        return SignalBar(
            index=index,
            pattern="micro_double_top",
            direction="short",
            grade=grade,
            entry_price=bar.low - tick,
            signal_high=bar.high,
            signal_low=bar.low,
            is_tradeable=grade in config.SIGNAL_GRADES_ALLOWED,
            reasons=[f"微型双重顶, 高点差={abs(bar.high - prev.high):.2f}"]
        )

    def is_exhaustion_bar(self, bar: BarData, avg_entity: float) -> bool:
        """检测耗尽棒

        定义: 实体≥20棒平均实体×3.0
        含义: 趋势耗尽警告，不交易，等确认
        """
        if bar.is_doji or avg_entity == 0:
            return False
        return bar.body >= avg_entity * config.EXHAUSTION_MULT

    def is_outside_bar(self, bars: List[BarData], index: int) -> bool:
        """检测外包棒

        定义: high>前棒high 且 low<前棒low
        本质: 单棒交易区间

        原文第7章: 5分钟外包棒突破入场默认不执行
        """
        if index < 1 or index >= len(bars):
            return False
        bar = bars[index]
        prev = bars[index - 1]
        return bar.high > prev.high and bar.low < prev.low

    def _get_tick_size(self, price: float) -> float:
        """获取最小跳动点

        通常最小跳动为0.01
        """
        return 0.01
