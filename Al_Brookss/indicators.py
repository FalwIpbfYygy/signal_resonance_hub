# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 指标计算模块
计算EMA、ATR、Hurst、棒线重叠率等Brooks理论所需量化指标
"""
import numpy as np


def calc_ema(values, period):
    """计算EMA序列

    Args:
        values: 收盘价数组
        period: EMA周期

    Returns:
        np.ndarray: EMA数组(与输入等长)
    """
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
    """计算ATR(Wilder平滑法)

    Args:
        highs: 最高价数组
        lows: 最低价数组
        closes: 收盘价数组
        period: ATR周期

    Returns:
        float: 最新ATR值
    """
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)
    closes = np.array(closes, dtype=float)
    n = len(closes)
    if n < period + 1:
        return np.mean(highs - lows) if n > 0 else 0.0

    # 计算真实波幅TR
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )

    # Wilder平滑
    atr = np.zeros(n)
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr[-1]


def calc_atr_series(highs, lows, closes, period=14):
    """计算ATR序列(用于历史回溯)

    Returns:
        np.ndarray: ATR数组
    """
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)
    closes = np.array(closes, dtype=float)
    n = len(closes)
    if n < period + 1:
        return np.zeros(n)

    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )

    atr = np.zeros(n)
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def calc_ema_slope(ema_values, lookback=5):
    """计算EMA斜率

    Args:
        ema_values: EMA数组
        lookback: 斜率回溯周期

    Returns:
        float: 斜率值(正=上升, 负=下降, 0=水平)
    """
    if len(ema_values) < lookback + 1:
        return 0.0
    return (ema_values[-1] - ema_values[-1 - lookback]) / lookback


def calc_avg_entity(opens, closes, period=20):
    """计算平均实体大小

    Args:
        opens: 开盘价数组
        closes: 收盘价数组
        period: 计算周期

    Returns:
        float: 平均实体大小
    """
    entities = np.abs(np.array(closes, dtype=float) - np.array(opens, dtype=float))
    if len(entities) < period:
        return float(np.mean(entities)) if len(entities) > 0 else 0.0
    return float(np.mean(entities[-period:]))


def calc_atr_ratio(opens, closes, atr, period=10):
    """计算ATR比率(最近N棒平均实体/ATR)

    ATR比率<0.8表示低波动压缩(紧凑TR)，不应交易

    Returns:
        float: ATR比率
    """
    if atr <= 0:
        return 1.0
    entities = np.abs(np.array(closes, dtype=float) - np.array(opens, dtype=float))
    if len(entities) < period:
        return float(np.mean(entities) / atr) if len(entities) > 0 else 1.0
    avg_entity = float(np.mean(entities[-period:]))
    return avg_entity / atr


def calc_hurst(prices, period=50):
    """计算Hurst指数(R/S分析法)

    Hurst<0.5: 均值回归(区间状态)
    Hurst=0.5: 随机游走
    Hurst>0.5: 趋势持续性(趋势状态)

    Returns:
        float: Hurst指数(0~1)
    """
    if len(prices) < period:
        return 0.5  # 数据不足返回默认值

    series = np.array(prices[-period:], dtype=float)
    # 过滤非正值(防止log报错)
    series = series[series > 0]
    if len(series) < 20:
        return 0.5

    # 对数收益率
    returns = np.diff(np.log(series))
    n = len(returns)
    if n < 20:
        return 0.5

    # R/S分析: 将序列分成不同大小的子区间
    max_k = int(np.floor(np.log2(n)))
    if max_k < 2:
        return 0.5

    rs_list = []
    n_list = []

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
            S = std_chunk
            rs_values.append(R / S)
        if rs_values:
            rs_list.append(np.mean(rs_values))
            n_list.append(chunk_size)

    if len(rs_list) < 2:
        return 0.5

    # 对数回归求Hurst
    log_n = np.log(n_list)
    log_rs = np.log(rs_list)
    hurst = np.polyfit(log_n, log_rs, 1)[0]
    return float(np.clip(hurst, 0.0, 1.0))


def calc_overlap_ratio(highs, lows, period=10):
    """计算棒线重叠率

    相邻棒线高低点重叠比例的平均值
    重叠率>60%倾向于紧凑交易区间

    Returns:
        float: 重叠率(0~1)
    """
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)
    if len(highs) < period + 1:
        return 0.5
    overlaps = 0.0
    count = 0
    for i in range(len(highs) - period, len(highs)):
        if i < 1:
            continue
        overlap = min(highs[i], highs[i - 1]) - max(lows[i], lows[i - 1])
        total_range = max(highs[i], highs[i - 1]) - min(lows[i], lows[i - 1])
        if total_range > 0:
            overlaps += max(0, overlap / total_range)
            count += 1
    return overlaps / count if count > 0 else 0.5


def calc_close_series_direction(closes, period=20):
    """收盘价序列方向统计

    Returns:
        int: 正值=上升主导, 负值=下降主导, 0=平衡
    """
    if len(closes) < period:
        return 0
    recent = closes[-period:]
    up_count = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
    down_count = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])
    return up_count - down_count


def calc_price_above_ema_ratio(closes, ema, period=20):
    """计算价格在EMA上方的比例

    Returns:
        float: 比例(0~1), >0.6=多头趋势, <0.4=空头趋势
    """
    if len(closes) < period or len(ema) < period:
        return 0.5
    count = 0
    for i in range(len(closes) - period, len(closes)):
        if closes[i] > ema[i]:
            count += 1
    return count / period


def calc_ema_cross_count(closes, ema, period=20):
    """计算价格穿越EMA的次数

    穿越次数多=交易区间状态

    Returns:
        int: 穿越次数
    """
    if len(closes) < period or len(ema) < period:
        return 0
    crosses = 0
    for i in range(len(closes) - period, len(closes)):
        if i < 1:
            continue
        prev_above = closes[i - 1] > ema[i - 1]
        curr_above = closes[i] > ema[i]
        if prev_above != curr_above:
            crosses += 1
    return crosses


def find_swing_highs(highs, lookback=2):
    """寻找波段高点

    一个波段高点是指其左右各lookback根K线的高点都低于它

    Returns:
        list: [(index, price), ...] 波段高点列表
    """
    highs = np.array(highs, dtype=float)
    swings = []
    for i in range(lookback, len(highs) - lookback):
        is_swing = True
        for j in range(1, lookback + 1):
            if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                is_swing = False
                break
        if is_swing:
            swings.append((i, highs[i]))
    return swings


def find_swing_lows(lows, lookback=2):
    """寻找波段低点

    Returns:
        list: [(index, price), ...] 波段低点列表
    """
    lows = np.array(lows, dtype=float)
    swings = []
    for i in range(lookback, len(lows) - lookback):
        is_swing = True
        for j in range(1, lookback + 1):
            if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                is_swing = False
                break
        if is_swing:
            swings.append((i, lows[i]))
    return swings


def find_range_bounds(highs, lows, lookback=50):
    """寻找区间上下边界(最近N棒的最高点和最低点)

    Returns:
        tuple: (range_high, range_low)
    """
    if len(highs) == 0 or len(lows) == 0:
        return 0.0, 0.0
    lookback = min(lookback, len(highs))
    range_high = float(np.max(highs[-lookback:]))
    range_low = float(np.min(lows[-lookback:]))
    return range_high, range_low


def calc_taker_buy_ratio(taker_buy_volumes, total_volumes, period=10):
    """计算主动买入量占比(买压/卖压辅助指标)

    主动买入占比>0.5=买压主导
    主动买入占比<0.5=卖压主导

    Returns:
        float: 最近N棒主动买入量占比(0~1)
    """
    if len(taker_buy_volumes) < period or len(total_volumes) < period:
        return 0.5
    recent_buy = float(np.sum(taker_buy_volumes[-period:]))
    recent_total = float(np.sum(total_volumes[-period:]))
    if recent_total <= 0:
        return 0.5
    return recent_buy / recent_total
