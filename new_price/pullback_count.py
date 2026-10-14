# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 数推/回调计数模块(高1-4 / 低1-4)

理论依据(锁定定义 · 学委方方土PA.txt)：
  高1-4(上涨趋势·多头)  [L279-283]
    - 回调出现: 某根K线低点 < 前一根K线低点
    - 高N信号: 回调后第一次出现某根K线高点 > 前一根K线高点
  低1-4(下降趋势·空头)  [L322-326] 对称
    - 回调出现: 某根K线高点 > 前一根K线高点
    - 低N信号: 回调后第一次出现某根K线低点 < 前一根K线低点

两条计数陷阱(必守)：
  陷阱1 [L282/L325] 回调后连续两根高点(低点)抬高(降低)不叫高1高2(低1低2)，
                    必须再有一次回调，下一根破高(破低)才算高2(低2)。
  陷阱2 [L283/L326] 数推过程中高点创新高(低点创新低) → 重新从高1(低1)计数。

机制说明：
  - 计数等级 N = 自上一个参考极值(新高/新低)以来、「未能突破参考极值」的回调次数。
  - 强势趋势: 每轮回调后即创新高 → 反复 高1/低1(强者可任意理由买入)。
  - 弱势趋势/宽通道/楔形: 回调后未能创新高 → 高2→高3(三推楔形), 高4已陷区间。
"""
from dataclasses import dataclass, field
from typing import List, Optional

import config
from signal_bars import BarData


def count_high_levels(bars: List[BarData], ref_lookback: int = None) -> List[int]:
    """多头数推：返回每根K线的高1-4计数等级(0=无信号)。

    参考极值 ref = 最近 ref_lookback 根K线(不含当前棒)的最高点。
    「创新高」= 突破该滚动参考峰 → 重新从高1计数(陷阱2)。
    """
    n = len(bars)
    levels = [0] * n
    if n == 0:
        return levels

    lookback = ref_lookback if ref_lookback is not None else config.COUNT_REF_LOOKBACK
    level = 0
    in_pullback = False

    for i in range(1, n):
        bar, prev = bars[i], bars[i - 1]

        # 陷阱2: 高点突破最近波段峰值 → 计数归零
        lo = max(0, i - lookback)
        ref_high = max(b.high for b in bars[lo:i])
        if bar.high > ref_high:
            level = 0

        # 回调出现
        if bar.low < prev.low:
            in_pullback = True

        # 回调后首次高点突破前高 → 高N信号
        if bar.high > prev.high and in_pullback:
            level += 1
            if level > config.MAX_COUNT_LEVEL:
                level = config.MAX_COUNT_LEVEL
            levels[i] = level
            in_pullback = False          # 陷阱1: 本轮回调已消耗, 连续破高不再计数

    return levels


def count_low_levels(bars: List[BarData], ref_lookback: int = None) -> List[int]:
    """空头数推：返回每根K线的低1-4计数等级(0=无信号)。

    参考极值 ref = 最近 ref_lookback 根K线(不含当前棒)的最低点。
    「创新低」= 跌破该滚动参考谷 → 重新从低1计数(陷阱2)。
    """
    n = len(bars)
    levels = [0] * n
    if n == 0:
        return levels

    lookback = ref_lookback if ref_lookback is not None else config.COUNT_REF_LOOKBACK
    level = 0
    in_pullback = False

    for i in range(1, n):
        bar, prev = bars[i], bars[i - 1]

        # 陷阱2: 低点跌破最近波段谷值 → 计数归零
        lo = max(0, i - lookback)
        ref_low = min(b.low for b in bars[lo:i])
        if bar.low < ref_low:
            level = 0

        # 回调出现(下降趋势中高点抬高)
        if bar.high > prev.high:
            in_pullback = True

        # 回调后首次低点跌破前低 → 低N信号
        if bar.low < prev.low and in_pullback:
            level += 1
            if level > config.MAX_COUNT_LEVEL:
                level = config.MAX_COUNT_LEVEL
            levels[i] = level
            in_pullback = False          # 陷阱1

    return levels


def count_pullback_levels(bars: List[BarData]):
    """同时计算高/低计数等级，返回 (high_levels, low_levels)。"""
    return count_high_levels(bars), count_low_levels(bars)


@dataclass
class PullbackCountResult:
    """数推计数结果(便于后续开仓/过滤模块消费)。"""
    high_levels: List[int] = field(default_factory=list)   # 高1-4 等级逐棒
    low_levels: List[int] = field(default_factory=list)    # 低1-4 等级逐棒
    last_high_level: int = 0                               # 最近一根高N等级(0=无)
    last_low_level: int = 0                                # 最近一根低N等级(0=无)


def summarize(bars: List[BarData]) -> PullbackCountResult:
    """对棒线序列执行数推计数，返回结果对象。"""
    high_levels, low_levels = count_pullback_levels(bars)
    return PullbackCountResult(
        high_levels=high_levels,
        low_levels=low_levels,
        last_high_level=high_levels[-1] if high_levels else 0,
        last_low_level=low_levels[-1] if low_levels else 0,
    )


def level_at(levels: List[int], index: int) -> int:
    """读取指定棒线索引处的计数等级，越界返回 0。"""
    if 0 <= index < len(levels):
        return levels[index]
    return 0