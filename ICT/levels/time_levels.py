"""时间级别流动性标记：亚洲盘/伦敦盘/前日高低点/当周高低点/FVG/Swing"""
from typing import List, Dict
from datetime import datetime, timezone, timedelta

from data.candle import Candle, KeyLevel, SwingPoint, FVG
from indicators.swing import find_swings
from indicators.fvg import find_fvgs


UTC = timezone.utc


def mark_asia_london_levels(candles_15m: List[Candle]) -> List[KeyLevel]:
    """
    在15分钟图上标记亚洲盘和伦敦盘高低点
    亚洲盘: 18:00(前日EST) - 00:00(EST)  → 对应GMT 23:00(前日) - 05:00
    伦敦盘: 00:00(EST) - 06:00(EST)        → 对应GMT 05:00 - 11:00

    实际使用GMT时间（英国时区）：
    亚洲盘: 00:00-08:00 GMT
    伦敦盘: 08:00-17:00 GMT
    """
    levels = []
    if not candles_15m:
        return levels

    asia_candles = []
    london_candles = []

    for c in candles_15m:
        gmt = c.open_time.astimezone(UTC)
        hour = gmt.hour

        if 0 <= hour < 8:
            asia_candles.append(c)
        elif 8 <= hour < 17:
            london_candles.append(c)

    # 亚洲盘高低点
    if asia_candles:
        asia_high = max(c.high for c in asia_candles)
        asia_low = min(c.low for c in asia_candles)
        asia_time = asia_candles[0].open_time
        levels.append(KeyLevel("亚洲盘高点", asia_high, "high", "15m", asia_time, False))
        levels.append(KeyLevel("亚洲盘低点", asia_low, "low", "15m", asia_time, True))

    # 伦敦盘高低点
    if london_candles:
        london_high = max(c.high for c in london_candles)
        london_low = min(c.low for c in london_candles)
        london_time = london_candles[0].open_time
        levels.append(KeyLevel("伦敦盘高点", london_high, "high", "15m", london_time, False))
        levels.append(KeyLevel("伦敦盘低点", london_low, "low", "15m", london_time, True))

    return levels


def mark_previous_daily_levels(candles_daily: List[Candle]) -> List[KeyLevel]:
    """标记前一日高低点"""
    levels = []
    if len(candles_daily) < 2:
        return levels

    prev = candles_daily[-2]  # 前一日
    levels.append(KeyLevel("前日高点", prev.high, "high", "daily", prev.open_time, False))
    levels.append(KeyLevel("前日低点", prev.low, "low", "daily", prev.open_time, True))

    return levels


def mark_weekly_levels(candles_daily: List[Candle]) -> List[KeyLevel]:
    """标记当周和前一周高低点"""
    levels = []
    if not candles_daily:
        return levels

    # 简化：取最近5根日线K线作为当周
    week_candles = candles_daily[-5:]
    if week_candles:
        week_high = max(c.high for c in week_candles)
        week_low = min(c.low for c in week_candles)
        levels.append(KeyLevel("当周高点", week_high, "high", "weekly", week_candles[0].open_time, False))
        levels.append(KeyLevel("当周低点", week_low, "low", "weekly", week_candles[0].open_time, True))

    # 前一周
    if len(candles_daily) >= 10:
        prev_week = candles_daily[-10:-5]
        if prev_week:
            pw_high = max(c.high for c in prev_week)
            pw_low = min(c.low for c in prev_week)
            levels.append(KeyLevel("前周高点", pw_high, "high", "weekly", prev_week[0].open_time, False))
            levels.append(KeyLevel("前周低点", pw_low, "low", "weekly", prev_week[0].open_time, True))

    return levels


def mark_fvg_swing_levels(candles: List[Candle], timeframe: str,
                          swings: List[SwingPoint], fvgs: List[FVG]) -> List[KeyLevel]:
    """从FVG和Swing Point生成关键级别"""
    levels = []
    if not candles:
        return levels

    # 取最近的FVG（最多3个）
    for fvg in fvgs[-3:]:
        name = f"{timeframe} FVG {'空' if fvg.direction == -1 else '多'}"
        mid = (fvg.upper + fvg.lower) / 2
        levels.append(KeyLevel(name, mid, "mid", timeframe, fvg.time,
                              fvg.direction == 1))

    # 取最近的Swing Point（最多2个）
    for s in swings[-2:]:
        name = f"{timeframe} Swing {'High' if s.is_high else 'Low'}"
        levels.append(KeyLevel(name, s.price,
                              "high" if s.is_high else "low",
                              timeframe, s.time, not s.is_high))

    return levels


def get_all_levels(data: Dict[str, List[Candle]]) -> List[KeyLevel]:
    """汇总所有关键级别"""
    all_levels = []

    # 15分钟级别：亚洲盘/伦敦盘
    if "15m" in data:
        all_levels.extend(mark_asia_london_levels(data["15m"]))

    # 日线级别：前日高低
    if "1d" in data:
        all_levels.extend(mark_previous_daily_levels(data["1d"]))
        all_levels.extend(mark_weekly_levels(data["1d"]))

    # 1H/4H/日线：FVG和Swing级别
    for tf in ["1h", "4h", "1d"]:
        if tf in data:
            candles = data[tf]
            swings = find_swings(candles)
            fvgs = find_fvgs(candles, swings)
            all_levels.extend(mark_fvg_swing_levels(candles, tf, swings, fvgs))

    return all_levels