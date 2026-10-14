# -*- coding: utf-8 -*-
"""锤子检测器单测（黄金用例 + 十字星反例）。运行：python -m pytest tests/"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DEFAULTS
from detector import is_hammer, scan

# 真实数据还原（来自 2026-10-02 09:10 那根  5m）
K_0910 = [0, 4144.87, 4145.39, 4139.17, 4145.35, 6398.0, 0]
# 十字星反例（09-29 08:10：上影占比 22.7%，上下影都明显）
K_DOJI = [0, 4077.3, 4100.0, 4000.0, 4061.3, 1000.0, 0]
# 大实体反例（09-30 00:55：实体占全幅 30.6%）
K_BIGBODY = [0, 4099.8, 4100.0, 4000.0, 4069.2, 1000.0, 0]


def test_0910_is_hammer():
    ok, m = is_hammer(K_0910, DEFAULTS)
    assert ok is True
    assert m["lower_ratio"] >= 0.55
    assert m["upper_ratio"] <= 0.15
    assert m["body_ratio"] <= 0.30
    assert m["close_pos"] >= 0.60


def test_doji_rejected():
    ok, m = is_hammer(K_DOJI, DEFAULTS)
    assert ok is False
    # 关键：上影占比超阈值 -> 十字星被剔出
    assert m["upper_ratio"] > DEFAULTS["upper_ratio_max"]


def test_bigbody_rejected():
    ok, m = is_hammer(K_BIGBODY, DEFAULTS)
    assert ok is False
    assert m["body_ratio"] > DEFAULTS["body_ratio_max"]


def test_scan_volume_filter():
    # 构造：两根锤子，一根放量一根不放量；开启放量过滤应只命中放量那根
    hammer_hit = [0, 4144.87, 4145.39, 4139.17, 4145.35, 5000.0, 0]   # 放量
    hammer_quiet = [0, 4144.87, 4145.39, 4139.17, 4145.35, 100.0, 0]  # 不放量
    klines = [hammer_quiet] * 25 + [hammer_hit]  # 前面25根安静锤子，最后1根放量
    p = dict(DEFAULTS)
    hits = scan(klines, p)
    assert len(hits) == 1
    assert hits[0]["with_volume_spike"] is True


def test_range_zero_safe():
    flat = [0, 4100.0, 4100.0, 4100.0, 4100.0, 100.0, 0]
    ok, m = is_hammer(flat, DEFAULTS)
    assert ok is False  # range<=0 不应崩、应判非锤子


if __name__ == "__main__":
    test_0910_is_hammer()
    test_doji_rejected()
    test_bigbody_rejected()
    test_scan_volume_filter()
    test_range_zero_safe()
    print("ALL TESTS PASSED")
