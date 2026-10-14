# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 指标计算模块
复用旧系统已验证算法，新增「回调深度占比」用于宽/窄通道分界。
"""
import numpy as np


def calc_ema(values, period):
    """EMA序列"""
    if len(values) == 0:
        return np.array([])
    values = np.array(values, dtype=float)
    alpha = 2.0 / (period + 1)
    ema = np.zeros(len(values))
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
    return ema


def calc_atr(highs, lows, closes, period=14):
    """ATR(Wilder平滑)，返回最新值"""
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)
    closes = np.array(closes, dtype=float)
    n = len(closes)
    if n < period + 1:
        return float(np.mean(highs - lows)) if n > 0 else 0.0
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    atr = np.zeros(n)
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr[-1]


def calc_ema_slope(ema_values, lookback=5):
    """EMA斜率(正=上升/负=下降)"""
    if len(ema_values) < lookback + 1:
        return 0.0
    return (ema_values[-1] - ema_values[-1 - lookback]) / lookback


def calc_avg_entity(opens, closes, period=20):
    """平均实体大小"""
    entities = np.abs(np.array(closes, dtype=float) - np.array(opens, dtype=float))
    if len(entities) < period:
        return float(np.mean(entities)) if len(entities) > 0 else 0.0
    return float(np.mean(entities[-period:]))


def calc_atr_ratio(opens, closes, atr, period=10):
    """ATR比率(近N棒平均实体/ATR)，<0.8=低波动压缩"""
    if atr <= 0:
        return 1.0
    entities = np.abs(np.array(closes, dtype=float) - np.array(opens, dtype=float))
    if len(entities) < period:
        return float(np.mean(entities) / atr) if len(entities) > 0 else 1.0
    return float(np.mean(entities[-period:]) / atr)


def calc_hurst(prices, period=50):
    """Hurst指数(<0.5均值回归 / >0.5趋势)"""
    if len(prices) < period:
        return 0.5
    series = np.array(prices[-period:], dtype=float)
    series = series[series > 0]
    if len(series) < 20:
        return 0.5
    returns = np.diff(np.log(series))
    n = len(returns)
    if n < 20:
        return 0.5
    max_k = int(np.floor(np.log2(n)))
    if max_k < 2:
        return 0.5
    rs_list, n_list = [], []
    for k in range(2, max_k + 1):
        chunk_size = n // (2 ** k)
        if chunk_size < 2:
            continue
        rs_values = []
        for i in range(2 ** k):
            start = i * chunk_size
            end = start + chunk_size
            if end > n:
                break
            chunk = returns[start:end]
            mean_chunk = np.mean(chunk)
            std_chunk = np.std(chunk)
            if std_chunk == 0:
                continue
            cumdev = np.cumsum(chunk - mean_chunk)
            R = np.max(cumdev) - np.min(cumdev)
            rs_values.append(R / std_chunk)
        if rs_values:
            rs_list.append(np.mean(rs_values))
            n_list.append(chunk_size)
    if len(rs_list) < 2:
        return 0.5
    hurst = np.polyfit(np.log(n_list), np.log(rs_list), 1)[0]
    return float(np.clip(hurst, 0.0, 1.0))


def calc_overlap_ratio(highs, lows, period=10):
    """相邻棒线高低点重叠率(>0.6倾向紧凑区间)"""
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)
    if len(highs) < period + 1:
        return 0.5
    overlaps, count = 0.0, 0
    for i in range(len(highs) - period, len(highs)):
        if i < 1:
            continue
        overlap = min(highs[i], highs[i - 1]) - max(lows[i], lows[i - 1])
        total = max(highs[i], highs[i - 1]) - min(lows[i], lows[i - 1])
        if total > 0:
            overlaps += max(0, overlap / total)
            count += 1
    return overlaps / count if count > 0 else 0.5


def calc_price_above_ema_ratio(closes, ema, period=20):
    """价格在EMA上方比例"""
    if len(closes) < period or len(ema) < period:
        return 0.5
    count = sum(1 for i in range(len(closes) - period, len(closes)) if closes[i] > ema[i])
    return count / period


def calc_ema_cross_count(closes, ema, period=20):
    """价格穿越EMA次数(多=区间)"""
    if len(closes) < period or len(ema) < period:
        return 0
    crosses = 0
    for i in range(len(closes) - period, len(closes)):
        if i < 1:
            continue
        if (closes[i - 1] > ema[i - 1]) != (closes[i] > ema[i]):
            crosses += 1
    return crosses


def find_swing_highs(highs, lookback=2):
    """波段高点 [(index, price), ...]"""
    highs = np.array(highs, dtype=float)
    swings = []
    for i in range(lookback, len(highs) - lookback):
        if all(highs[i] > highs[i - j] for j in range(1, lookback + 1)) and \
           all(highs[i] > highs[i + j] for j in range(1, lookback + 1)):
            swings.append((i, highs[i]))
    return swings


def find_swing_lows(lows, lookback=2):
    """波段低点 [(index, price), ...]"""
    lows = np.array(lows, dtype=float)
    swings = []
    for i in range(lookback, len(lows) - lookback):
        if all(lows[i] < lows[i - j] for j in range(1, lookback + 1)) and \
           all(lows[i] < lows[i + j] for j in range(1, lookback + 1)):
            swings.append((i, lows[i]))
    return swings


def find_range_bounds(highs, lows, lookback=50):
    """区间上下边界(近N棒极值)"""
    if len(highs) == 0 or len(lows) == 0:
        return 0.0, 0.0
    lookback = min(lookback, len(highs))
    return float(np.max(highs[-lookback:])), float(np.min(lows[-lookback:]))


def calc_pullback_ratio(swing_highs, swing_lows, pairs=3):
    """回调深度占比 = 回调腿幅度 / 推动腿幅度

    核心代理: 逆势方能否靠限价/突破单盈利。
      值越小 → 回调越浅 → 窄通道/急速(强趋势)
      值越大 → 回调越深 → 宽通道/逼近区间

    算法: 取三段极值 low→high→low(或 high→low→high)
          leg = |p1-p2|(推动), pullback = |p2-p3|(回调)
          ratio = pullback / leg，取最近 pairs 个腿均值。
    """
    pts = sorted(
        [(i, 1, p) for i, p in swing_highs] + [(i, -1, p) for i, p in swing_lows],
        key=lambda x: x[0]
    )
    ratios = []
    for k in range(len(pts) - 2):
        _, s1, p1 = pts[k]
        _, s2, p2 = pts[k + 1]
        _, s3, p3 = pts[k + 2]
        if s1 == s3 and s2 == -s1:            # 同型极值夹一个反向极值
            leg = abs(p1 - p2)
            pullback = abs(p2 - p3)
            if leg > 0:
                ratios.append(pullback / leg)
    if not ratios:
        return 0.5
    return float(np.mean(ratios[-pairs:]))