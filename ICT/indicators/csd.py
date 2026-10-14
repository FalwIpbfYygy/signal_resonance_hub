"""交付状态改变（CSD）检测

CSD = Change in State of Delivery
只看K线实体，不看影线。
空头CSD: 当前阴线实体 > 前一根阳线实体（吞噬）
多头CSD: 当前阳线实体 > 前一根阴线实体（吞噬）
"""
from typing import List
from data.candle import Candle, CSDSignal


def detect_csd(candles: List[Candle]) -> List[CSDSignal]:
    """
    检测所有CSD信号
    返回信号列表
    """
    signals = []
    n = len(candles)
    if n < 2:
        return signals

    for i in range(1, n):
        prev = candles[i - 1]
        curr = candles[i]

        # 空头CSD: 阴线吞噬前一根阳线
        if curr.is_bearish and prev.is_bullish:
            if curr.body > prev.body:
                signals.append(CSDSignal(
                    index=i,
                    time=curr.open_time,
                    direction=-1,
                ))

        # 多头CSD: 阳线吞噬前一根阴线
        elif curr.is_bullish and prev.is_bearish:
            if curr.body > prev.body:
                signals.append(CSDSignal(
                    index=i,
                    time=curr.open_time,
                    direction=1,
                ))

    return signals


def get_latest_csd(candles: List[Candle]) -> CSDSignal:
    """获取最新的CSD信号（仅最后一根K线）"""
    n = len(candles)
    if n < 2:
        return None
    prev = candles[-2]
    curr = candles[-1]

    if curr.is_bearish and prev.is_bullish and curr.body > prev.body:
        return CSDSignal(index=n - 1, time=curr.open_time, direction=-1)
    if curr.is_bullish and prev.is_bearish and curr.body > prev.body:
        return CSDSignal(index=n - 1, time=curr.open_time, direction=1)
    return None


def has_bearish_csd(candles: List[Candle], lookback: int = 3) -> bool:
    """检查最近N根K线内是否有空头CSD"""
    n = len(candles)
    start = max(1, n - lookback)
    for i in range(start, n):
        prev = candles[i - 1]
        curr = candles[i]
        if curr.is_bearish and prev.is_bullish and curr.body > prev.body:
            return True
    return False


def has_bullish_csd(candles: List[Candle], lookback: int = 3) -> bool:
    """检查最近N根K线内是否有多头CSD"""
    n = len(candles)
    start = max(1, n - lookback)
    for i in range(start, n):
        prev = candles[i - 1]
        curr = candles[i]
        if curr.is_bullish and prev.is_bearish and curr.body > prev.body:
            return True
    return False