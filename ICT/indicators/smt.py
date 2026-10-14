# -*- coding: utf-8 -*-
"""SMT 背离检测（黄金  vs 合成美元指数 DXY，负相关）

ICT 的 SMT（Smart Money Technique）背离：用两个负相关市场的结构背离，
判断某一方突破的"真实性"。

- 看跌 SMT：黄金形成更高的高点（HH），但 DXY 未形成更低的低点（LL，反而 HL）
  → 黄金上涨未获美元走弱确认，是虚假突破，倾向反转做空。
- 看涨 SMT：黄金形成更低的低点（LL），但 DXY 未形成更高的高点（HH，反而 LH）
  → 黄金下跌未获美元走强确认，是虚假突破，倾向反转做多。

数据源：黄金用 1m K 线 swing 点；DXY 用 TwelveData 合成序列（见 data.dxy）。
"""
from typing import List, Tuple, Optional

from data.candle import Candle, SwingPoint


def _dxy_swings(series: List[Tuple[int, float]]):
    """从 DXY 合成序列 [(ts_ms, value)] 提取 swing 高低点（3-bar 标准）。

    返回 (highs, lows)，各为 [(ts_ms, value), ...] 升序。
    """
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    n = len(series)
    if n < 3:
        return highs, lows
    for i in range(1, n - 1):
        prev, cur, nxt = series[i - 1][1], series[i][1], series[i + 1][1]
        if prev < cur > nxt:
            highs.append(series[i])
        if prev > cur < nxt:
            lows.append(series[i])
    return highs, lows


def detect_smt(candles_1m: List[Candle],
               dxy_series: List[Tuple[int, float]]) -> Optional[str]:
    """检测 SMT 背离，返回 "bearish" / "bullish" / None。

    黄金与 DXY 负相关：
    - bearish：黄金 HH 但 DXY 低点未创新低（LL 缺失，低点抬高）
    - bullish：黄金 LL 但 DXY 高点未创新高（HH 缺失，高点降低）
    """
    if not candles_1m or len(candles_1m) < 3:
        return None
    if not dxy_series or len(dxy_series) < 3:
        return None

    # 黄金 1m swing 高低点
    xau_highs: List[float] = []
    xau_lows: List[float] = []
    n = len(candles_1m)
    for i in range(1, n - 1):
        c0, c1, c2 = candles_1m[i - 1], candles_1m[i], candles_1m[i + 1]
        if c0.high < c1.high > c2.high:
            xau_highs.append(c1.high)
        if c0.low > c1.low < c2.low:
            xau_lows.append(c1.low)

    dxy_highs, dxy_lows = _dxy_swings(dxy_series)

    # 看跌：黄金 HH（最近高点 > 前高点），DXY 低点抬高（最近低点 > 前低点，未创新低）
    if len(xau_highs) >= 2 and len(dxy_lows) >= 2:
        xau_hh = xau_highs[-1] > xau_highs[-2]
        dxy_no_ll = dxy_lows[-1][1] > dxy_lows[-2][1]
        if xau_hh and dxy_no_ll:
            return "bearish"

    # 看涨：黄金 LL（最近低点 < 前低点），DXY 高点降低（最近高点 < 前高点，未创新高）
    if len(xau_lows) >= 2 and len(dxy_highs) >= 2:
        xau_ll = xau_lows[-1] < xau_lows[-2]
        dxy_no_hh = dxy_highs[-1][1] < dxy_highs[-2][1]
        if xau_ll and dxy_no_hh:
            return "bullish"

    return None
