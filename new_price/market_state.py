# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 市场状态判断模块

四阶段模型(两本新笔记 + 三书锁定定义):
  trend_up / trend_down   → 急速·窄通道(强趋势): 回调浅, 逆势方难盈利
  broad_up / broad_down   → 宽通道(弱趋势): 回调深, 逆势方能盈利
  range                   → 交易区间(横盘)
  tight_range             → 紧凑区间/带刺铁丝(barbwire): 不交易

核心分界(两本笔记增量): 逆势方能否靠限价/突破单盈利
  → 代理变量 = 回调深度占比 pullback_ratio
    <0.5  => 窄通道/急速(强趋势)
    0.5~0.75 => 宽通道(弱趋势)
    >=0.75 => 回调过深, 趋势失效 → 区间
"""
from dataclasses import dataclass, field

import numpy as np

import config
import indicators as ind


@dataclass
class MarketStateResult:
    state: str = "unknown"          # trend_up/trend_down/broad_up/broad_down/range/tight_range
    direction: int = 0              # +1 多 / -1 空 / 0 中性
    ema20: float = 0.0
    ema_slope: float = 0.0
    atr: float = 0.0
    atr_ratio: float = 1.0
    hurst: float = 0.5
    overlap_ratio: float = 0.5
    pullback_ratio: float = 0.5     # 回调深度占比(宽/窄通道分界)
    price_above_ema_ratio: float = 0.5
    ema_cross_count: int = 0
    range_high: float = 0.0
    range_low: float = 0.0
    swing_highs: list = field(default_factory=list)
    swing_lows: list = field(default_factory=list)
    is_tradeable: bool = False      # tight_range/unknown 为 False


class MarketStateJudge:
    """四阶段市场状态判断器"""

    def judge(self, opens, highs, lows, closes):
        if len(closes) < config.EMA_PERIOD + 5:
            return MarketStateResult()

        r = MarketStateResult()
        r.ema20_series = ind.calc_ema(closes, config.EMA_PERIOD)
        r.ema20 = float(r.ema20_series[-1])
        r.ema_slope = ind.calc_ema_slope(r.ema20_series, config.EMA_SLOPE_PERIOD)
        r.atr = ind.calc_atr(highs, lows, closes, config.ATR_PERIOD)
        r.atr_ratio = ind.calc_atr_ratio(opens, closes, r.atr, config.ATR_RATIO_PERIOD)
        r.hurst = ind.calc_hurst(closes, config.HURST_PERIOD)
        r.overlap_ratio = ind.calc_overlap_ratio(highs, lows, config.OVERLAP_PERIOD)
        r.price_above_ema_ratio = ind.calc_price_above_ema_ratio(
            closes, r.ema20_series, config.EMA_PERIOD)
        r.ema_cross_count = ind.calc_ema_cross_count(
            closes, r.ema20_series, config.EMA_PERIOD)
        r.range_high, r.range_low = ind.find_range_bounds(highs, lows, 50)
        r.swing_highs = ind.find_swing_highs(highs, config.SWING_LOOKBACK)
        r.swing_lows = ind.find_swing_lows(lows, config.SWING_LOOKBACK)
        r.pullback_ratio = ind.calc_pullback_ratio(
            r.swing_highs, r.swing_lows, config.PULLBACK_PAIRS)
        r.direction = self._direction(r)

        r.state = self._determine_state(r)
        r.is_tradeable = r.state not in ("tight_range", "unknown")
        return r

    def _determine_state(self, r):
        # 1) 紧凑区间 barbwire: 波动压缩 + 高重叠 → 不交易
        if (r.atr_ratio < config.ATR_RATIO_THRESHOLD and
                r.overlap_ratio > config.OVERLAP_RATIO_THRESHOLD):
            return "tight_range"

        # 方向
        direction = r.direction

        # 2) 无方向 → 区间
        if direction == 0:
            return "range"

        # 3) 回调过深 → 趋势失效, 归区间
        if r.pullback_ratio >= config.PULLBACK_RATIO_RANGE:
            return "range"

        # 4) 宽 vs 窄通道
        if r.pullback_ratio >= config.PULLBACK_RATIO_TREND:
            return "broad_up" if direction > 0 else "broad_down"
        return "trend_up" if direction > 0 else "trend_down"

    def _direction(self, r):
        """方向判定: 波段结构递进为主, EMA斜率为辅。"""
        hs = r.swing_highs
        ls = r.swing_lows
        bull = False
        bear = False
        if len(hs) >= 3:
            bull = hs[-1][1] > hs[-2][1] and hs[-2][1] > hs[-3][1]
            bear = hs[-1][1] < hs[-2][1] and hs[-2][1] < hs[-3][1]
        if len(ls) >= 3:
            bull = bull or (ls[-1][1] > ls[-2][1] and ls[-2][1] > ls[-3][1])
            bear = bear or (ls[-1][1] < ls[-2][1] and ls[-2][1] < ls[-3][1])

        slope_th = config.TREND_SLOPE_THRESHOLD * r.ema20
        ema_up = r.ema_slope > slope_th
        ema_down = r.ema_slope < -slope_th

        if bull and (ema_up or r.price_above_ema_ratio >= config.TREND_PRICE_ABOVE_EMA_RATIO):
            return 1
        if bear and (ema_down or r.price_above_ema_ratio <= 1 - config.TREND_PRICE_ABOVE_EMA_RATIO):
            return -1
        if ema_up and r.price_above_ema_ratio >= config.TREND_PRICE_ABOVE_EMA_RATIO:
            return 1
        if ema_down and r.price_above_ema_ratio <= 1 - config.TREND_PRICE_ABOVE_EMA_RATIO:
            return -1
        return 0

    def describe(self, r):
        """中文状态描述(用于推送/日志)"""
        m = {
            "trend_up": "急速/窄通道·多头趋势",
            "trend_down": "急速/窄通道·空头趋势",
            "broad_up": "宽通道·多头(弱趋势)",
            "broad_down": "宽通道·空头(弱趋势)",
            "range": "交易区间",
            "tight_range": "紧凑区间(barbwire)不交易",
            "unknown": "数据不足",
        }
        return m.get(r.state, "未知")