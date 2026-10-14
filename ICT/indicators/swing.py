"""Swing High/Low 识别"""
from typing import List
from data.candle import Candle, SwingPoint


def find_swings(candles: List[Candle]) -> List[SwingPoint]:
    """
    识别所有摆动高低点（3-bar标准：左右各1根K线）
    Swing High: high[i-1] < high[i] > high[i+1]
    Swing Low: low[i-1] > low[i] < low[i+1]
    """
    swings = []
    n = len(candles)
    if n < 3:
        return swings

    for i in range(1, n - 1):
        # Swing High
        if candles[i - 1].high < candles[i].high > candles[i + 1].high:
            swings.append(SwingPoint(
                index=i,
                price=candles[i].high,
                time=candles[i].open_time,
                is_high=True,
                candle=candles[i],
            ))
        # Swing Low
        if candles[i - 1].low > candles[i].low < candles[i + 1].low:
            swings.append(SwingPoint(
                index=i,
                price=candles[i].low,
                time=candles[i].open_time,
                is_high=False,
                candle=candles[i],
            ))

    return swings


def get_swing_highs(swings: List[SwingPoint]) -> List[SwingPoint]:
    """提取所有摆动高点"""
    return [s for s in swings if s.is_high]


def get_swing_lows(swings: List[SwingPoint]) -> List[SwingPoint]:
    """提取所有摆动低点"""
    return [s for s in swings if not s.is_high]


def get_recent_swing_high(candles: List[Candle], lookback: int = 10) -> SwingPoint:
    """获取最近的短期摆动高点（用于流动性清扫检测）"""
    n = len(candles)
    start = max(0, n - lookback)
    for i in range(n - 2, start - 1, -1):
        if i > 0 and i < n - 1:
            if candles[i - 1].high < candles[i].high > candles[i + 1].high:
                return SwingPoint(
                    index=i, price=candles[i].high,
                    time=candles[i].open_time, is_high=True, candle=candles[i]
                )
    return None


def get_recent_swing_low(candles: List[Candle], lookback: int = 10) -> SwingPoint:
    """获取最近的短期摆动低点（用于流动性清扫检测）"""
    n = len(candles)
    start = max(0, n - lookback)
    for i in range(n - 2, start - 1, -1):
        if i > 0 and i < n - 1:
            if candles[i - 1].low > candles[i].low < candles[i + 1].low:
                return SwingPoint(
                    index=i, price=candles[i].low,
                    time=candles[i].open_time, is_high=False, candle=candles[i]
                )
    return None