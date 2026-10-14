# -*- coding: utf-8 -*-
"""指标模块：ATR / SMA / 摆动点 / 线性回归 / 趋势 / 均量等纯 Python 实现。"""

import math


def sma(values, n):
    """简单移动平均；前 n-1 处填 0。"""
    out = [0.0] * len(values)
    if n <= 0 or n > len(values):
        return out
    s = 0.0
    for i in range(len(values)):
        s += values[i]
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def atr(highs, lows, closes, period=14):
    """Wilder ATR。"""
    n = len(closes)
    if n <= period:
        return [0.0] * n
    trs = [0.0] * n
    trs[0] = highs[0] - lows[0]
    for i in range(1, n):
        trs[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    out = [0.0] * n
    out[period] = sum(trs[1:period + 1]) / period
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


def swing_points(highs, lows, left=3, right=3):
    """摆动点检测（ZigZag 近似）。

    返回按索引升序的列表，每项 (index, kind, price)，kind ∈ {'high','low'}。
    一个 bar 若同时是高点与低点，优先记录低点。
    """
    n = len(highs)
    swings = []
    for i in range(left, n - right):
        is_high = True
        for j in range(1, left + 1):
            if highs[i] < highs[i - j]:
                is_high = False
                break
        if is_high:
            for j in range(1, right + 1):
                if highs[i] < highs[i + j]:
                    is_high = False
                    break
        is_low = True
        for j in range(1, left + 1):
            if lows[i] > lows[i - j]:
                is_low = False
                break
        if is_low:
            for j in range(1, right + 1):
                if lows[i] > lows[i + j]:
                    is_low = False
                    break
        if is_low and not is_high:
            swings.append((i, "low", lows[i]))
        elif is_high:
            swings.append((i, "high", highs[i]))
    return swings


def linreg(xs, ys):
    """最小二乘线性回归，返回 (slope, intercept)。"""
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = 0.0
    sxy = 0.0
    for x, y in zip(xs, ys):
        sxx += (x - mx) * (x - mx)
        sxy += (x - mx) * (y - my)
    if sxx == 0:
        return 0.0, my
    slope = sxy / sxx
    intercept = my - slope * mx
    return slope, intercept


def line_value(slope, intercept, x):
    return slope * x + intercept


def trend_direction(closes, period=50):
    """趋势方向：窗口内线性回归斜率经价格归一化后取符号。

    返回 1（上升）/ -1（下降）/ 0（无趋势）。
    """
    n = len(closes)
    if n < period:
        period = max(2, n)
    ys = closes[-period:]
    xs = list(range(period))
    slope, _ = linreg(xs, ys)
    avg = sum(ys) / period
    if avg == 0:
        return 0
    norm = slope / avg
    if norm > 0.0005:
        return 1
    if norm < -0.0005:
        return -1
    return 0


def volume_breakout(volumes, idx, ma_n=20, mult=1.8):
    """判断 idx 处是否放量突破：volumes[idx] >= SMA(前 ma_n 根) × mult。"""
    if idx <= ma_n:
        return False
    avg = sum(volumes[idx - ma_n:idx]) / ma_n
    if avg <= 0:
        return False
    return volumes[idx] >= avg * mult


def slope_degrees(slope, avg_price):
    """把（价格/根）斜率粗略换算为角度（仅用于颈线倾斜阈值判断）。"""
    if avg_price <= 0:
        return 90.0
    return math.degrees(math.atan(slope / avg_price))