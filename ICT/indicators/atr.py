"""ATR计算 + SFP检测 + 流动性清扫检测"""
from typing import List
from data.candle import Candle, SwingPoint


def calc_atr(candles: List[Candle], period: int = 14) -> float:
    """计算ATR（平均真实波幅）"""
    if len(candles) < period + 1:
        return 0

    tr_values = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev = candles[i - 1]
        tr = max(
            c.high - c.low,
            abs(c.high - prev.close),
            abs(c.low - prev.close),
        )
        tr_values.append(tr)

    return sum(tr_values[-period:]) / period


def detect_sfp(candles: List[Candle], swings: List[SwingPoint],
               lookback: int = 5) -> dict:
    """
    检测摆动失败模式（SFP）
    空头SFP: 价格扫过前一个Swing High但收盘价 < 该Swing High
    多头SFP: 价格扫过前一个Swing Low但收盘价 > 该Swing Low

    返回: {"bearish_sfp": bool, "bullish_sfp": bool, "sfp_price": float}
    """
    result = {"bearish_sfp": False, "bullish_sfp": False, "sfp_price": 0.0}
    n = len(candles)
    if n < 3 or not swings:
        return result

    # 检查最近的K线
    recent = candles[-lookback:]
    for c in recent:
        for s in swings:
            if s.index >= n - lookback:
                continue
            # 空头SFP：扫过高点但未收盘突破
            if s.is_high and c.high > s.price and c.close < s.price:
                result["bearish_sfp"] = True
                result["sfp_price"] = s.price
                return result
            # 多头SFP：扫过低点但未收盘突破
            if not s.is_high and c.low < s.price and c.close > s.price:
                result["bullish_sfp"] = True
                result["sfp_price"] = s.price
                return result
    return result


def detect_liquidity_sweep(candles: List[Candle], direction: str,
                           lookback: int = 10) -> bool:
    """
    检测流动性清扫（Liquidity Sweep / Stop Run）

    空头趋势中的流动性清扫：价格向上扫过一个短期Swing High后回落
    多头趋势中的流动性清扫：价格向下扫过一个短期Swing Low后反弹

    direction: "bearish" 或 "bullish"
    """
    n = len(candles)
    if n < lookback:
        return False

    if direction == "bearish":
        # 检查最近K线是否扫过高点
        for i in range(n - lookback, n - 2):
            if i > 0 and i < n - 1:
                if candles[i - 1].high < candles[i].high > candles[i + 1].high:
                    swing_high = candles[i].high
                    # 检查之后是否有K线扫过这个高点
                    for j in range(i + 1, n):
                        if candles[j].high > swing_high:
                            # 检查当前K线是否回落
                            if candles[-1].close < swing_high:
                                return True
    else:  # bullish
        for i in range(n - lookback, n - 2):
            if i > 0 and i < n - 1:
                if candles[i - 1].low > candles[i].low < candles[i + 1].low:
                    swing_low = candles[i].low
                    for j in range(i + 1, n):
                        if candles[j].low < swing_low:
                            if candles[-1].close > swing_low:
                                return True

    return False