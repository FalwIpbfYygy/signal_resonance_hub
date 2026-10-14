# -*- coding: utf-8 -*-
"""锤子/针形形态检测器（纯函数，不依赖网络）。

判定逻辑（全部满足才判锤子，阈值见 PRD §5）：
  lower/range >= lower_ratio_min        # 长下影
  upper/range <= upper_ratio_max        # 上影极小 -> 剔除十字星
  body/range  <= body_ratio_max         # 实体小
  (close-low)/range >= close_pos_min    # 收在上部
  lower >= lower_to_body_min * body     # 下影明显长于实体
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))  # 北京时间 UTC+8，无夏令时


def _metrics(o, h, l, c):
    """从 OHLC 算结构比例。range<=0 时比例返回 0。"""
    rng = h - l
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    close_pos = (c - l) / rng if rng > 0 else 0.0
    base = {
        "range": rng, "body": body, "upper": upper, "lower": lower,
        "close_pos": close_pos,
        "body_ratio": body / rng if rng > 0 else 0.0,
        "upper_ratio": upper / rng if rng > 0 else 0.0,
        "lower_ratio": lower / rng if rng > 0 else 0.0,
        "lower_to_body": (lower / body) if body > 0 else 99.0,
    }
    return base


def is_hammer(kline, p) -> tuple[bool, dict]:
    """单根 K 线判定。

    kline: [open_time_ms, o, h, l, c, vol, close_time_ms, ...]（Binance 字段序）
    返回 (是否锤子, 结构指标 dict)。
    """
    o, h, l, c = float(kline[1]), float(kline[2]), float(kline[3]), float(kline[4])
    m = _metrics(o, h, l, c)
    rng = m["range"]
    if rng <= 0:
        return False, m
    if m["lower_ratio"] < p["lower_ratio_min"]:
        return False, m
    if m["upper_ratio"] > p["upper_ratio_max"]:
        return False, m
    if m["body_ratio"] > p["body_ratio_max"]:
        return False, m
    if m["close_pos"] < p["close_pos_min"]:
        return False, m
    if m["lower_to_body"] < p["lower_to_body_min"]:
        return False, m
    return True, m


def is_inverted_hammer(kline, p) -> tuple[bool, dict]:
    """镜像锤子（倒锤子 / 空头方向锤子），与 is_hammer 严格对称：

      upper/range >= lower_ratio_min        # 长上影（镜像：原下影）
      lower/range <= upper_ratio_max        # 下影极小 -> 剔除十字星
      body/range  <= body_ratio_max         # 实体小
      (close-low)/range <= (1-close_pos_min)# 收在下部（镜像：原收在上部）
      upper >= lower_to_body_min * body     # 上影明显长于实体（镜像：原下影）
    """
    o, h, l, c = float(kline[1]), float(kline[2]), float(kline[3]), float(kline[4])
    m = _metrics(o, h, l, c)
    rng = m["range"]
    if rng <= 0:
        return False, m
    if m["upper_ratio"] < p["lower_ratio_min"]:
        return False, m
    if m["lower_ratio"] > p["upper_ratio_max"]:
        return False, m
    if m["body_ratio"] > p["body_ratio_max"]:
        return False, m
    if m["close_pos"] > (1 - p["close_pos_min"]):
        return False, m
    upper_to_body = (m["upper"] / m["body"]) if m["body"] > 0 else 99.0
    if upper_to_body < p["lower_to_body_min"]:
        return False, m
    return True, m


def scan_inverted(klines, p) -> list[dict]:
    """扫描倒锤子（空头方向），与 scan() 对称，含放量过滤。"""
    vols = [float(k[5]) for k in klines]
    out = []
    for i, k in enumerate(klines):
        ok, m = is_inverted_hammer(k, p)
        if not ok:
            continue
        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        vr = _vol_ratio(vols, i, p["vol_ma_window"]) if p["volume_filter"] else None
        if p["volume_filter"]:
            if vr is None or vr < p["vol_mult"]:
                continue
        ts = datetime.fromtimestamp(float(k[0]) / 1000, BJ).strftime("%Y-%m-%d %H:%M")
        out.append({
            "time_bj": ts,
            "open": o, "high": h, "low": l, "close": c,
            "upper_to_body": round(m["upper"] / m["body"], 1) if m["body"] > 0 else 99.0,
            "lower_ratio": round(m["lower_ratio"], 3),
            "upper_ratio": round(m["upper_ratio"], 3),
            "body_ratio": round(m["body_ratio"], 3),
            "close_pos": round(m["close_pos"], 3),
            "vol_ratio": round(vr, 2) if vr is not None else None,
            "with_volume_spike": (vr is not None and vr >= p["vol_mult"]),
        })
    return out


def _vol_ratio(vols, i, window):
    """第 i 根成交量相对前 window 根（含自身）MA 的比值。"""
    if i < window:
        return None
    ma = sum(vols[i - window + 1:i + 1]) / window
    if ma <= 0:
        return None
    return vols[i] / ma


def scan(klines, p) -> list[dict]:
    """扫描一串 K 线，返回所有命中锤子（且满足放量过滤）的结果。"""
    vols = [float(k[5]) for k in klines]
    out = []
    for i, k in enumerate(klines):
        ok, m = is_hammer(k, p)
        if not ok:
            continue
        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        vr = _vol_ratio(vols, i, p["vol_ma_window"]) if p["volume_filter"] else None
        if p["volume_filter"]:
            if vr is None or vr < p["vol_mult"]:
                continue
        ts = datetime.fromtimestamp(float(k[0]) / 1000, BJ).strftime("%Y-%m-%d %H:%M")
        out.append({
            "time_bj": ts,
            "open": o, "high": h, "low": l, "close": c,
            "lower_to_body": round(m["lower_to_body"], 1),
            "lower_ratio": round(m["lower_ratio"], 3),
            "upper_ratio": round(m["upper_ratio"], 3),
            "body_ratio": round(m["body_ratio"], 3),
            "close_pos": round(m["close_pos"], 3),
            "vol_ratio": round(vr, 2) if vr is not None else None,
            "with_volume_spike": (vr is not None and vr >= p["vol_mult"]),
        })
    return out
