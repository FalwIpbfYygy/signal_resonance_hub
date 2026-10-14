"""公允价值缺口（FVG）识别 + 反转检测 + 拐点 + BSG判断"""
from typing import List
from data.candle import Candle, FVG, SwingPoint


def find_fvgs(candles: List[Candle], swings: List[SwingPoint] = None) -> List[FVG]:
    """
    识别所有FVG
    空头FVG: 阴线K线[i]，且 low[i-1] > high[i+1] → FVG区间 [high[i+1], low[i-1]]
    多头FVG: 阳线K线[i]，且 high[i-1] < low[i+1] → FVG区间 [high[i-1], low[i+1]]
    """
    fvgs = []
    n = len(candles)
    if n < 3:
        return fvgs

    swing_highs = set()
    swing_lows = set()
    if swings:
        for s in swings:
            if s.is_high:
                swing_highs.add(s.price)
            else:
                swing_lows.add(s.price)

    for i in range(1, n - 1):
        c = candles[i]
        prev = candles[i - 1]
        next_c = candles[i + 1]

        if c.is_bearish:
            # 空头FVG: 前一根K线低点 > 后一根K线高点
            if prev.low > next_c.high:
                fvg = FVG(
                    index=i,
                    time=c.open_time,
                    direction=-1,
                    upper=prev.low,
                    lower=next_c.high,
                    size=prev.low - next_c.high,
                )
                # 判断BSG：是否突破了摆动低点
                if swings:
                    fvg.is_bsg = _check_bsg_bearish(fvg, swings, candles, i)
                fvgs.append(fvg)

        elif c.is_bullish:
            # 多头FVG: 前一根K线高点 < 后一根K线低点
            if prev.high < next_c.low:
                fvg = FVG(
                    index=i,
                    time=c.open_time,
                    direction=1,
                    upper=next_c.low,
                    lower=prev.high,
                    size=next_c.low - prev.high,
                )
                if swings:
                    fvg.is_bsg = _check_bsg_bullish(fvg, swings, candles, i)
                fvgs.append(fvg)

    return fvgs


def check_fvg_inversion(fvgs: List[FVG], candles: List[Candle]) -> List[FVG]:
    """
    检测FVG是否被反转
    空头FVG反转: 后续阳线收盘价 > FVG上边界
    多头FVG反转: 后续阴线收盘价 < FVG下边界
    返回被反转的FVG列表
    """
    inverted = []
    for fvg in fvgs:
        if fvg.is_inverted:
            continue
        for j in range(fvg.index + 2, len(candles)):
            c = candles[j]
            if fvg.direction == -1:  # 空头FVG
                if c.close > fvg.upper:
                    fvg.is_inverted = True
                    inverted.append(fvg)
                    break
            else:  # 多头FVG
                if c.close < fvg.lower:
                    fvg.is_inverted = True
                    inverted.append(fvg)
                    break
    return inverted


def find_inflection_points(fvgs: List[FVG], swings: List[SwingPoint],
                           candles: List[Candle]) -> None:
    """
    在FVG内部寻找拐点（Inflection Point）
    空头FVG: 区间内最近的Swing High
    多头FVG: 区间内最近的Swing Low
    """
    for fvg in fvgs:
        closest = None
        closest_dist = float('inf')
        for s in swings:
            if s.index >= fvg.index:
                continue
            if fvg.direction == -1:  # 空头FVG，找Swing High
                if s.is_high and fvg.lower <= s.price <= fvg.upper:
                    dist = fvg.index - s.index
                    if dist < closest_dist:
                        closest_dist = dist
                        closest = s.price
            else:  # 多头FVG，找Swing Low
                if not s.is_high and fvg.lower <= s.price <= fvg.upper:
                    dist = fvg.index - s.index
                    if dist < closest_dist:
                        closest_dist = dist
                        closest = s.price
        if closest is not None:
            fvg.inflection_point = closest


def get_active_fvgs(fvgs: List[FVG], candles: List[Candle]) -> List[FVG]:
    """获取当前仍活跃的FVG（未被反转、未被完全穿越）"""
    active = []
    last_price = candles[-1].close
    for fvg in fvgs:
        if fvg.is_inverted:
            continue
        # 检查FVG是否已被完全穿越（上下边界都被突破）
        crossed = False
        for j in range(fvg.index + 1, len(candles)):
            c = candles[j]
            if c.low < fvg.lower and c.high > fvg.upper:
                crossed = True
                break
        if not crossed:
            active.append(fvg)
    return active


def _check_bsg_bearish(fvg: FVG, swings: List[SwingPoint],
                       candles: List[Candle], fvg_idx: int) -> bool:
    """判断空头FVG是否为BSG（突破结构缺口）"""
    for s in swings:
        if not s.is_high:
            continue
        if s.index < fvg_idx and s.price < fvg.upper:
            return True
    return False


def _check_bsg_bullish(fvg: FVG, swings: List[SwingPoint],
                       candles: List[Candle], fvg_idx: int) -> bool:
    """判断多头FVG是否为BSG"""
    for s in swings:
        if s.is_high:
            continue
        if s.index < fvg_idx and s.price > fvg.lower:
            return True
    return False