# -*- coding: utf-8 -*-
"""形态检测模块：实现《高胜算形态交易》全书 19 个形态。

每个检测函数签名：detect_xxx(ctx) -> dict 或 None。
detection 返回契约：
    pattern   形态名
    direction +1 做多 / -1 做空
    entry     入场(突破)价
    stop      止损价
    target1   目标1
    target2   目标2
    vol_ok    突破是否放量
    touches   接触点数量(质量参考)
    detail    描述文本
"""

import config
from indicators import sma, atr, swing_points, linreg, line_value, trend_direction, volume_breakout

DIR_LONG = 1
DIR_SHORT = -1


def build_ctx(bars):
    """根据 K 线列表构建检测上下文。"""
    opens = [b["open"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    closes_time = [b["close_time"] for b in bars]
    a = atr(highs, lows, closes, config.ATR_PERIOD)
    last = len(bars) - 1
    cur_atr = a[last] if last >= 0 and a[last] > 0 else (a[last - 1] if last > 0 else 0.0)
    swings = swing_points(highs, lows, config.SWING_LEFT, config.SWING_RIGHT)
    return {
        "bars": bars,
        "opens": opens, "highs": highs, "lows": lows,
        "closes": closes, "volumes": volumes, "closes_time": closes_time,
        "atr": a, "cur_atr": cur_atr, "last": last, "swings": swings,
    }


def _swing_highs(swings):
    return [(i, p) for (i, k, p) in swings if k == "high"]


def _swing_lows(swings):
    return [(i, p) for (i, k, p) in swings if k == "low"]


def _fit_slope(points):
    """对 (idx, price) 点做线性回归，返回 (slope, intercept)。"""
    xs = [i for (i, _) in points]
    ys = [p for (_, p) in points]
    return linreg(xs, ys)


def _flat_level(points, tol):
    """判断一组点是否近似同一水平，返回 (is_flat, mean)。"""
    if len(points) < 2:
        return True, points[0][1]
    prices = [p for (_, p) in points]
    mean = sum(prices) / len(prices)
    is_flat = all(abs(p - mean) <= tol for p in prices)
    return is_flat, mean


def _vol_ok(ctx, idx):
    return volume_breakout(ctx["volumes"], idx, config.VOLUME_MA_N, config.VOLUME_BREAK_MULT)


def _result(pattern, direction, entry, stop, t1, t2, vol_ok, touches, detail):
    return {
        "pattern": pattern, "direction": direction, "entry": round(entry, 4),
        "stop": round(stop, 4), "target1": round(t1, 4), "target2": round(t2, 4),
        "vol_ok": vol_ok, "touches": touches, "detail": detail,
    }


# ======================================================================
# 反转形态
# ======================================================================

def detect_head_shoulders_top(ctx):
    """头肩顶（第14章）：颈线放量向下突破表明反转做空。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    lows = _swing_lows(swings)
    if len(highs) < 3 or len(lows) < 2:
        return None
    tol = ctx["cur_atr"] * config.LEVEL_TOLERANCE_ATR
    # 取最后三个摆动高点，验证「左肩-头-右肩」结构
    h0, h1, h2 = highs[-3][1], highs[-2][1], highs[-1][1]
    if not (h1 > h0 and h1 > h2):
        return None
    # 颈线：位于头两侧的两个摆动低点应近似等高
    neck_lows = [(i, p) for (i, p) in lows if highs[-3][0] < i < highs[-1][0]]
    if len(neck_lows) < 2:
        return None
    l1, l2 = neck_lows[0][1], neck_lows[-1][1]
    if abs(l1 - l2) > tol:
        return None
    neck = (l1 + l2) / 2
    height = h1 - neck
    if height <= 0:
        return None
    entry = ctx["closes"][ctx["last"]]
    if not (entry < neck):
        return None
    # 颈线倾斜校验（±10°）
    slope, intercept = _fit_slope(neck_lows)
    avg = (l1 + l2) / 2
    from indicators import slope_degrees
    if abs(slope_degrees(slope, avg)) > config.NECKLINE_SLOPE_LIMIT_DEG:
        return None
    stop = max(h0, h2) + ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = neck - height * 0.5
    t2 = neck - height
    vol_ok = _vol_ok(ctx, ctx["last"])
    touches = len(neck_lows) + 3
    return _result("头肩顶", DIR_SHORT, entry, stop, t1, t2, vol_ok, touches, "颈线放量下破")


def detect_head_shoulders_bottom(ctx):
    """头肩底（第15章）：颈线放量向上突破表明反转做多。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    lows = _swing_lows(swings)
    if len(lows) < 3 or len(highs) < 2:
        return None
    tol = ctx["cur_atr"] * config.LEVEL_TOLERANCE_ATR
    l0, l1, l2 = lows[-3][1], lows[-2][1], lows[-1][1]
    if not (l1 < l0 and l1 < l2):
        return None
    neck_highs = [(i, p) for (i, p) in highs if lows[-3][0] < i < lows[-1][0]]
    if len(neck_highs) < 2:
        return None
    h1n, h2n = neck_highs[0][1], neck_highs[-1][1]
    if abs(h1n - h2n) > tol:
        return None
    neck = (h1n + h2n) / 2
    height = neck - l1
    if height <= 0:
        return None
    entry = ctx["closes"][ctx["last"]]
    if not (entry > neck):
        return None
    slope, intercept = _fit_slope(neck_highs)
    avg = (h1n + h2n) / 2
    from indicators import slope_degrees
    if abs(slope_degrees(slope, avg)) > config.NECKLINE_SLOPE_LIMIT_DEG:
        return None
    stop = min(l0, l2) - ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = neck + height * 0.5
    t2 = neck + height
    vol_ok = _vol_ok(ctx, ctx["last"])
    touches = len(neck_highs) + 3
    return _result("头肩底", DIR_LONG, entry, stop, t1, t2, vol_ok, touches, "颈线放量上破")


def detect_multiple_top(ctx):
    """多重顶（第17章）：多次触及相似高位后放量跌破确认点。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    if len(highs) < config.MULTI_TOUCH_MIN + 1:
        return None
    tol = ctx["cur_atr"] * config.LEVEL_TOLERANCE_ATR
    recent = highs[-(config.MULTI_TOUCH_MIN + 2):]
    is_flat, level = _flat_level(recent, tol)
    if not is_flat:
        return None
    # 确认点 = 下方支撑（最近显著摆动低点）
    lows = _swing_lows(swings)
    confirm_lows = [(i, p) for (i, p) in lows if i < recent[-1][0]]
    if not confirm_lows:
        return None
    confirm = confirm_lows[-1][1]
    entry = ctx["closes"][ctx["last"]]
    if not (entry < confirm):
        return None
    height = level - confirm
    if height <= 0:
        return None
    stop = level + ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = confirm - height * 0.5
    t2 = confirm - height
    vol_ok = _vol_ok(ctx, ctx["last"])
    touches = len(recent)
    return _result("多重顶", DIR_SHORT, entry, stop, t1, t2, vol_ok, touches, "跌破确认点(支撑)")


def detect_multiple_bottom(ctx):
    """多重底（第16章）：多次触及相似低位后放量突破确认点。"""
    swings = ctx["swings"]
    lows = _swing_lows(swings)
    if len(lows) < config.MULTI_TOUCH_MIN + 1:
        return None
    tol = ctx["cur_atr"] * config.LEVEL_TOLERANCE_ATR
    recent = lows[-(config.MULTI_TOUCH_MIN + 2):]
    is_flat, level = _flat_level(recent, tol)
    if not is_flat:
        return None
    highs = _swing_highs(swings)
    confirm_highs = [(i, p) for (i, p) in highs if i < recent[-1][0]]
    if not confirm_highs:
        return None
    confirm = confirm_highs[-1][1]
    entry = ctx["closes"][ctx["last"]]
    if not (entry > confirm):
        return None
    height = confirm - level
    if height <= 0:
        return None
    stop = level - ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = confirm + height * 0.5
    t2 = confirm + height
    vol_ok = _vol_ok(ctx, ctx["last"])
    touches = len(recent)
    return _result("多重底", DIR_LONG, entry, stop, t1, t2, vol_ok, touches, "突破确认点(阻力)")


def detect_diamond_top(ctx):
    """钻石顶（第9章）：左发散右收敛，跌破收敛下边做空。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    lows = _swing_lows(swings)
    if len(highs) < 3 or len(lows) < 2:
        return None
    # 左半发散：前两高点递增 → 右半收敛：后一高低点区间收窄
    h = highs[-3:]
    if not (h[1][1] > h[0][1]):  # 发散（较高高点）
        return None
    lo = lows[-2:]
    if len(lo) < 2:
        return None
    # 收敛：最后一个低点抬高
    if not (lo[-1][1] > lo[-2][1]):
        return None
    top = max(p for (_, p) in h)
    bot = min(p for (_, p) in lo)
    span = top - bot
    if span <= 0:
        return None
    entry = ctx["closes"][ctx["last"]]
    # 突破收敛下边（近似用最后一个低点下方）
    if not (entry < lo[-1][1]):
        return None
    stop = top + ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = entry - span * 0.5
    t2 = entry - span
    vol_ok = _vol_ok(ctx, ctx["last"])
    return _result("钻石顶", DIR_SHORT, entry, stop, t1, t2, vol_ok, 4, "跌破钻石收敛下边")


def detect_diamond_bottom(ctx):
    """钻石底（第9章）：镜像，突破收敛上边做多。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    lows = _swing_lows(swings)
    if len(highs) < 2 or len(lows) < 3:
        return None
    l = lows[-3:]
    if not (l[1][1] < l[0][1]):
        return None
    hi = highs[-2:]
    if len(hi) < 2:
        return None
    if not (hi[-1][1] < hi[-2][1]):
        return None
    top = max(p for (_, p) in hi)
    bot = min(p for (_, p) in l)
    span = top - bot
    if span <= 0:
        return None
    entry = ctx["closes"][ctx["last"]]
    if not (entry > hi[-1][1]):
        return None
    stop = bot - ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = entry + span * 0.5
    t2 = entry + span
    vol_ok = _vol_ok(ctx, ctx["last"])
    return _result("钻石底", DIR_LONG, entry, stop, t1, t2, vol_ok, 4, "突破钻石收敛上边")


def detect_saucer(ctx):
    """碟形/圆形底（第19章）：逐渐下跌→走平→逐渐上涨，放量突破阻力。"""
    swings = ctx["swings"]
    lows = _swing_lows(swings)
    highs = _swing_highs(swings)
    if len(lows) < 2 or len(highs) < 2:
        return None
    # 底部低点：近似等高的两个低点（U 形底）
    tol = ctx["cur_atr"] * config.LEVEL_TOLERANCE_ATR
    bot_points = lows[-2:]
    is_flat, bot = _flat_level(bot_points, tol * 2)
    if not is_flat:
        return None
    # 阻力：上方的显著高点
    resist = highs[-1][1]
    if resist <= bot:
        return None
    entry = ctx["closes"][ctx["last"]]
    if not (entry > resist):
        return None
    depth = resist - bot
    stop = bot - ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = entry + depth * 0.5
    t2 = entry + depth
    vol_ok = _vol_ok(ctx, ctx["last"])
    return _result("碟形底", DIR_LONG, entry, stop, t1, t2, vol_ok, 3, "放量突破圆形底阻力")


def detect_rounding_top(ctx):
    """圆顶（第20章）：急速上涨后涨幅递减的较高高点，跌破支撑卖出。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    lows = _swing_lows(swings)
    if len(highs) < 3 or len(lows) < 1:
        return None
    tol = ctx["cur_atr"] * config.LEVEL_TOLERANCE_ATR
    h = highs[-3:]
    # 涨幅递减：后两个高点之间的抬升小于前两个
    if not (h[1][1] > h[0][1] and h[2][1] > h[1][1]):
        return None
    if not ((h[2][1] - h[1][1]) < (h[1][1] - h[0][1])):
        return None
    support = lows[-1][1]
    entry = ctx["closes"][ctx["last"]]
    if not (entry < support):
        return None
    span = h[-1][1] - support
    if span <= 0:
        return None
    stop = h[-1][1] + ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = entry - span * 0.5
    t2 = entry - span
    vol_ok = _vol_ok(ctx, ctx["last"])
    return _result("圆顶", DIR_SHORT, entry, stop, t1, t2, vol_ok, 3, "跌破圆顶支撑线")


# ======================================================================
# 持续 / 整理形态
# ======================================================================

def detect_ascending_triangle(ctx):
    """上升三角形（第3章）：水平阻力 + 上升支撑，向上放量突破。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    lows = _swing_lows(swings)
    if len(highs) < config.TRIANGLE_TOUCH_MIN or len(lows) < config.TRIANGLE_TOUCH_MIN:
        return None
    tol = ctx["cur_atr"] * config.LEVEL_TOLERANCE_ATR
    res_points = highs[-3:]
    is_flat, resist = _flat_level(res_points, tol)
    if not is_flat:
        return None
    sup_points = lows[-3:]
    # 上升支撑：低点依次抬高
    if not (sup_points[-1][1] > sup_points[-2][1] > sup_points[-3][1]):
        return None
    sup_start = sup_points[-3][1]
    height = resist - sup_start
    if height <= 0:
        return None
    entry = ctx["closes"][ctx["last"]]
    if not (entry > resist):
        return None
    stop = sup_points[-1][1] - ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = resist + height * 0.5
    t2 = resist + height
    vol_ok = _vol_ok(ctx, ctx["last"])
    return _result("上升三角形", DIR_LONG, entry, stop, t1, t2, vol_ok, len(res_points) + len(sup_points), "放量突破水平阻力")


def detect_descending_triangle(ctx):
    """下降三角形（第7章）：水平支撑 + 下降阻力，向下放量突破。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    lows = _swing_lows(swings)
    if len(highs) < config.TRIANGLE_TOUCH_MIN or len(lows) < config.TRIANGLE_TOUCH_MIN:
        return None
    tol = ctx["cur_atr"] * config.LEVEL_TOLERANCE_ATR
    sup_points = lows[-3:]
    is_flat, support = _flat_level(sup_points, tol)
    if not is_flat:
        return None
    res_points = highs[-3:]
    if not (res_points[-1][1] < res_points[-2][1] < res_points[-3][1]):
        return None
    res_start = res_points[-3][1]
    height = res_start - support
    if height <= 0:
        return None
    entry = ctx["closes"][ctx["last"]]
    if not (entry < support):
        return None
    stop = res_points[-1][1] + ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = support - height * 0.5
    t2 = support - height
    vol_ok = _vol_ok(ctx, ctx["last"])
    return _result("下降三角形", DIR_SHORT, entry, stop, t1, t2, vol_ok, len(sup_points) + len(res_points), "跌破水平支撑")


def detect_rising_wedge(ctx):
    """上升楔形（第4章）：两条上行收敛线，跌破下边做空。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    lows = _swing_lows(swings)
    if len(highs) < config.WEDGE_TOUCH_MIN or len(lows) < config.WEDGE_TOUCH_MIN:
        return None
    h = highs[-3:]
    lo = lows[-3:]
    if not (h[-1][1] > h[-2][1] > h[-3][1]):  # 高点上行
        return None
    if not (lo[-1][1] > lo[-2][1] > lo[-3][1]):  # 低点上行
        return None
    # 收敛：低点上升速度 > 高点上升速度
    lo_slope, _ = _fit_slope(lo)
    hi_slope, _ = _fit_slope(h)
    if not (lo_slope > hi_slope):
        return None
    entry = ctx["closes"][ctx["last"]]
    # 跌破下边（近似用最后一个低点）
    if not (entry < lo[-1][1]):
        return None
    stop = h[-1][1] + ctx["cur_atr"] * config.STOP_BUFFER_ATR
    # 无清晰目标，用楔形高度近似
    height = h[-1][1] - lo[-1][1]
    t1 = entry - height
    t2 = entry - height * 1.5
    vol_ok = _vol_ok(ctx, ctx["last"])
    return _result("上升楔形", DIR_SHORT, entry, stop, t1, t2, vol_ok, 6, "跌破上行楔形下边")


def detect_falling_wedge(ctx):
    """下降楔形（第8章）：两条下行收敛线，突破上边做多。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    lows = _swing_lows(swings)
    if len(highs) < config.WEDGE_TOUCH_MIN or len(lows) < config.WEDGE_TOUCH_MIN:
        return None
    h = highs[-3:]
    lo = lows[-3:]
    if not (h[-1][1] < h[-2][1] < h[-3][1]):
        return None
    if not (lo[-1][1] < lo[-2][1] < lo[-3][1]):
        return None
    lo_slope, _ = _fit_slope(lo)
    hi_slope, _ = _fit_slope(h)
    if not (lo_slope < hi_slope):  # 下行且收敛
        return None
    entry = ctx["closes"][ctx["last"]]
    if not (entry > h[-1][1]):
        return None
    stop = lo[-1][1] - ctx["cur_atr"] * config.STOP_BUFFER_ATR
    height = h[-1][1] - lo[-1][1]
    t1 = entry + height
    t2 = entry + height * 1.5
    vol_ok = _vol_ok(ctx, ctx["last"])
    return _result("下降楔形", DIR_LONG, entry, stop, t1, t2, vol_ok, 6, "突破下行楔形上边")


def _pole_and_flag(ctx, direction):
    """识别旗杆 + 逆向整理（旗形/三角旗的公共部分），返回结构或 None。"""
    closes = ctx["closes"]
    last = ctx["last"]
    # 用 last 截断，避免回测中取到未来数据（未来函数）
    window = closes[max(0, last + 1 - config.FLAG_MAX_BARS): last + 1]
    if len(window) < 20:
        return None
    pole = None
    if direction == DIR_LONG:
        # 上涨旗杆：找一段强力上涨
        lowest = min(window[:len(window) // 2])
        pole_top = max(window)
        pole = pole_top - lowest
        if lowest >= pole_top:
            return None
    else:
        highest = max(window[:len(window) // 2])
        pole_bot = min(window)
        pole = highest - pole_bot
        if highest <= pole_bot:
            return None
        lowest = pole_bot
        pole_top = highest
    if ctx["cur_atr"] > 0 and pole < config.FLAG_POLE_MIN_ATR * ctx["cur_atr"]:
        return None
    return {"pole": pole, "lowest": lowest, "top": pole_top}


def detect_flag(ctx, direction):
    """旗形（第12章）：前置旗杆 + 逆向平行倾斜整理，突破后沿趋势延续。"""
    struct = _pole_and_flag(ctx, direction)
    if struct is None:
        return None
    swings = ctx["swings"]
    entry = ctx["closes"][ctx["last"]]
    if direction == DIR_LONG:
        highs = _swing_highs(swings)[-2:]
        if len(highs) < 2:
            return None
        # 整理上边（近似两点）
        if not (entry > highs[-1][1]):
            return None
        stop = min(p for (_, p) in _swing_lows(swings)[-2:]) - ctx["cur_atr"] * config.STOP_BUFFER_ATR
        t1 = entry + struct["pole"] * 0.5
        t2 = entry + struct["pole"]
        vol_ok = _vol_ok(ctx, ctx["last"])
        return _result("旗形(看涨)", DIR_LONG, entry, stop, t1, t2, vol_ok, 2, "突破看涨旗形上边")
    else:
        lows = _swing_lows(swings)[-2:]
        if len(lows) < 2:
            return None
        if not (entry < lows[-1][1]):
            return None
        stop = max(p for (_, p) in _swing_highs(swings)[-2:]) + ctx["cur_atr"] * config.STOP_BUFFER_ATR
        t1 = entry - struct["pole"] * 0.5
        t2 = entry - struct["pole"]
        vol_ok = _vol_ok(ctx, ctx["last"])
        return _result("旗形(看跌)", DIR_SHORT, entry, stop, t1, t2, vol_ok, 2, "跌破看跌旗形下边")


def detect_pennant(ctx, direction):
    """三角旗（第18章）：同旗形，但两条线交汇（收敛）。"""
    struct = _pole_and_flag(ctx, direction)
    if struct is None:
        return None
    swings = ctx["swings"]
    entry = ctx["closes"][ctx["last"]]
    if direction == DIR_LONG:
        highs = _swing_highs(swings)[-2:]
        lows = _swing_lows(swings)[-2:]
        if len(highs) < 2 or len(lows) < 2:
            return None
        hi_slope, _ = _fit_slope(highs)
        lo_slope, _ = _fit_slope(lows)
        if not (lo_slope > hi_slope):  # 收敛
            return None
        if not (entry > highs[-1][1]):
            return None
        stop = lows[-1][1] - ctx["cur_atr"] * config.STOP_BUFFER_ATR
        t1 = entry + struct["pole"] * 0.5
        t2 = entry + struct["pole"]
        vol_ok = _vol_ok(ctx, ctx["last"])
        return _result("三角旗(看涨)", DIR_LONG, entry, stop, t1, t2, vol_ok, 4, "突破看涨三角旗")
    else:
        highs = _swing_highs(swings)[-2:]
        lows = _swing_lows(swings)[-2:]
        if len(highs) < 2 or len(lows) < 2:
            return None
        hi_slope, _ = _fit_slope(highs)
        lo_slope, _ = _fit_slope(lows)
        if not (lo_slope > hi_slope):  # 收敛
            return None
        if not (entry < lows[-1][1]):
            return None
        stop = highs[-1][1] + ctx["cur_atr"] * config.STOP_BUFFER_ATR
        t1 = entry - struct["pole"] * 0.5
        t2 = entry - struct["pole"]
        vol_ok = _vol_ok(ctx, ctx["last"])
        return _result("三角旗(看跌)", DIR_SHORT, entry, stop, t1, t2, vol_ok, 4, "跌破看跌三角旗")


def detect_cup_handle(ctx):
    """杯柄（第6章）：U 形杯 + 柄回调 ≤ 1/3 杯幅，突破柄上沿做多。"""
    swings = ctx["swings"]
    highs = _swing_highs(swings)
    lows = _swing_lows(swings)
    if len(highs) < 2 or len(lows) < 1:
        return None
    tol = ctx["cur_atr"] * config.LEVEL_TOLERANCE_ATR
    # 杯：两个近似等高高点（杯沿）
    if len(highs) < 2:
        return None
    cup_highs = highs[-3:-1]
    is_flat, rim = _flat_level(cup_highs, tol)
    if not is_flat:
        return None
    bottom = min(p for (_, p) in lows[-3:])
    cup_depth = rim - bottom
    if cup_depth <= 0:
        return None
    # 柄：最近的更小 U 形回踩
    handle_low = lows[-1][1]
    if handle_low >= rim:
        return None
    handle_retrace = rim - handle_low
    if handle_retrace > cup_depth * config.CUP_HANDLE_RETRACE_RATIO + tol:
        return None
    entry = ctx["closes"][ctx["last"]]
    if not (entry > rim):
        return None
    stop = handle_low - ctx["cur_atr"] * config.STOP_BUFFER_ATR
    t1 = rim + cup_depth * 0.5
    t2 = rim + cup_depth
    vol_ok = _vol_ok(ctx, ctx["last"])
    return _result("杯柄", DIR_LONG, entry, stop, t1, t2, vol_ok, 3, "突破杯沿")


def detect_channel(ctx):
    """通道（第5章）：两条近平行趋势线，突破方向决定买卖。"""
    last = ctx["last"]
    start = max(0, last - config.CHANNEL_LOOKBACK)
    # 仅取近期摆动点做回归，避免跨越多个价格阶段导致通道线失真
    highs = [(i, p) for (i, p) in _swing_highs(ctx["swings"]) if i >= start]
    lows = [(i, p) for (i, p) in _swing_lows(ctx["swings"]) if i >= start]
    if len(highs) < config.CHANNEL_TOUCH_MIN or len(lows) < config.CHANNEL_TOUCH_MIN:
        return None
    hi_slope, hi_int = _fit_slope(highs)
    lo_slope, lo_int = _fit_slope(lows)
    if abs(hi_slope - lo_slope) > abs(hi_slope) * 0.3 + 1e-9:  # 近似平行
        return None
    entry = ctx["closes"][last]
    up_line = line_value(hi_slope, hi_int, last)
    lo_line = line_value(lo_slope, lo_int, last)
    width = abs(up_line - lo_line)
    # 通道宽度必须达到最小可交易幅度，否则视为噪声拟合直接丢弃
    if width < config.CHANNEL_MIN_WIDTH_ATR * ctx["cur_atr"]:
        return None
    # 真实接触点：落在通道线容差内的摆动点数量（质量参考，而非全部摆动点数）
    tol = config.CHANNEL_TOUCH_TOL_ATR * ctx["cur_atr"]
    touches = sum(1 for (i, p) in highs if abs(p - line_value(hi_slope, hi_int, i)) <= tol)
    touches += sum(1 for (i, p) in lows if abs(p - line_value(lo_slope, lo_int, i)) <= tol)
    vol_ok = _vol_ok(ctx, last)
    if entry > up_line:
        stop = lo_line - ctx["cur_atr"] * config.STOP_BUFFER_ATR
        return _result("通道(上破)", DIR_LONG, entry, stop, entry + width * 0.5, entry + width,
                       vol_ok, touches, "向上突破通道上轨")
    if entry < lo_line:
        stop = up_line + ctx["cur_atr"] * config.STOP_BUFFER_ATR
        return _result("通道(下破)", DIR_SHORT, entry, stop, entry - width * 0.5, entry - width,
                       vol_ok, touches, "向下突破通道下轨")
    return None


# ======================================================================
# 事件 / 结构形态
# ======================================================================

def detect_gap(ctx):
    """缺口（第13章）：跳空事件，与总体趋势一致时提供方向信号。"""
    closes = ctx["closes"]
    opens = ctx["opens"]
    highs = ctx["highs"]
    lows = ctx["lows"]
    last = ctx["last"]
    if last < 1:
        return None
    gap_min = ctx["cur_atr"] * config.GAP_MIN_ATR
    if gap_min <= 0:
        gap_min = 0.0001
    up_gap = lows[last] - highs[last - 1]
    dn_gap = lows[last - 1] - highs[last]
    entry = closes[last]
    if up_gap >= gap_min:
        return _result("向上缺口", DIR_LONG, entry, highs[last - 1] - ctx["cur_atr"] * config.STOP_BUFFER_ATR,
                       entry + gap_min * 2, entry + gap_min * 4, True, 1, "向上跳空")
    if dn_gap >= gap_min:
        return _result("向下缺口", DIR_SHORT, entry, lows[last - 1] + ctx["cur_atr"] * config.STOP_BUFFER_ATR,
                       entry - gap_min * 2, entry - gap_min * 4, True, 1, "向下跳空")
    return None


def detect_support_failure(ctx):
    """支撑失效（第21章）：事件，价格有效跌破支撑 → 做空/离场。"""
    swings = ctx["swings"]
    lows = _swing_lows(swings)
    if len(lows) < 2:
        return None
    tol = ctx["cur_atr"] * config.LEVEL_TOLERANCE_ATR
    support_points = lows[-3:-1]  # 至少两个近似等低点作为支撑
    if len(support_points) < 2:
        return None
    is_flat, support = _flat_level(support_points, tol)
    if not is_flat:
        return None
    entry = ctx["closes"][ctx["last"]]
    if not (entry < support):
        return None
    stop = support + ctx["cur_atr"] * config.STOP_BUFFER_ATR
    span = support - min(p for (_, p) in lows)
    span = max(span, ctx["cur_atr"] * 2)
    t1 = entry - span
    t2 = entry - span * 1.5
    vol_ok = _vol_ok(ctx, ctx["last"])
    return _result("支撑失效", DIR_SHORT, entry, stop, t1, t2, vol_ok, len(support_points), "有效跌破支撑")


# ======================================================================
# 斐波纳契（观察模式，不产生信号）
# ======================================================================

def fib_retracement_levels(ctx):
    """计算斐波纳契回调水平（仅报告用）。"""
    closes = ctx["closes"]
    window = closes[-config.FIB_FRIENDLY_WINDOW:]
    if len(window) < 2:
        return None
    hi = max(window)
    lo = min(window)
    if hi <= lo:
        return None
    rng = hi - lo
    levels = {}
    for lv in config.FIB_LEVELS:
        levels[lv] = hi - rng * lv / 100.0
    return {"high": hi, "low": lo, "levels": levels}


# ======================================================================
# 检测器注册表
# ======================================================================

DETECTORS = [
    ("头肩顶", detect_head_shoulders_top),
    ("头肩底", detect_head_shoulders_bottom),
    ("多重顶", detect_multiple_top),
    ("多重底", detect_multiple_bottom),
    ("钻石顶", detect_diamond_top),
    ("钻石底", detect_diamond_bottom),
    ("碟形底", detect_saucer),
    ("圆顶", detect_rounding_top),
    ("上升三角形", detect_ascending_triangle),
    ("下降三角形", detect_descending_triangle),
    ("上升楔形", detect_rising_wedge),
    ("下降楔形", detect_falling_wedge),
    ("杯柄", detect_cup_handle),
    ("通道", detect_channel),
    ("向上缺口", detect_gap),
    ("向下缺口", detect_gap),
    ("支撑失效", detect_support_failure),
]

# 方向绑定的持续形态（需要显式传入方向）
DIRECTIONAL_DETECTORS = [
    ("旗形", detect_flag),
    ("三角旗", detect_pennant),
]


def run_all_detectors(ctx):
    """运行全部检测器，返回检测结果列表（已去重同形态）。"""
    results = []
    seen = set()
    for name, fn in DETECTORS:
        r = None
        try:
            r = fn(ctx)
        except Exception as e:
            continue
        if r and r["pattern"] not in seen:
            seen.add(r["pattern"])
            results.append(r)
    for name, fn in DIRECTIONAL_DETECTORS:
        for d in (DIR_LONG, DIR_SHORT):
            r = None
            try:
                r = fn(ctx, d)
            except Exception as e:
                continue
            if r and r["pattern"] not in seen:
                seen.add(r["pattern"])
                results.append(r)
    # 信号时间：取自接口数据里「最后一根已收盘 K 线」的 close_time（毫秒，GMT/UTC）
    bar_time = ctx["closes_time"][ctx["last"]]
    for r in results:
        r["bar_time"] = bar_time
    return results