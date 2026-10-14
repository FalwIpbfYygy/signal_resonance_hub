# -*- coding: utf-8 -*-
"""
验证脚本: 信号去重 + 飞书推送(格式化, 不实发)
  1) 去重自测 —— K线级/冷却/升级/方向独立 确定性断言
  2) 推送格式化自测 —— 消息卡片字段完备性(不触发真实 webhook)
用法: python verify_dedup_notify.py
"""
import config
import dedup
import notifier
from entry import TradePlan
from risk import PositionAdvice


def test_dedup():
    print("=" * 60)
    print("自测: 信号去重(PRD §9)")
    print("=" * 60)
    dd = dedup.SignalDedup(cooldown_bars=3)

    # 1) 首发放行
    ok, reason = dd.should_send("5m", "long", "double_bottom", 100, 1000, "strong")
    assert ok, f"首发应放行: {reason}"
    dd.mark_sent("5m", "long", "double_bottom", 100, 1000, "strong")
    print(f"  1) 首发放行: ✓ {reason}")

    # 2) 同K线同形态同级 → 拦
    ok, reason = dd.should_send("5m", "long", "double_bottom", 100, 1000, "strong")
    assert not ok, f"应拦截: {reason}"
    print(f"  2) 同K线同级拦: ✓ {reason}")

    # 3) 同K线同形态升级(strong→strongest) → 放行
    ok, reason = dd.should_send("5m", "long", "double_bottom", 100, 1000, "strongest")
    assert ok and "升级" in reason, f"升级应放行: {reason}"
    dd.mark_sent("5m", "long", "double_bottom", 100, 1000, "strongest")
    print(f"  3) 升级重推: ✓ {reason}")

    # 4) 相邻棒同方向 → 冷却拦
    ok, reason = dd.should_send("5m", "long", "double_bottom", 101, 1001, "strong")
    assert not ok and "冷却" in reason, f"应冷却: {reason}"
    print(f"  4) 冷却拦: ✓ {reason}")

    # 5) 冷却期满(间距≥3) → 放行
    ok, reason = dd.should_send("5m", "long", "double_bottom", 103, 1003, "strong")
    assert ok, f"冷却期满应放行: {reason}"
    print(f"  5) 冷却期满放行: ✓ {reason}")

    # 6) 不同方向独立 → 放行(反向单同一根不受多头冷却影响)
    ok, reason = dd.should_send("5m", "short", "double_top", 100, 1000, "strong")
    assert ok, f"反向应放行: {reason}"
    print(f"  6) 方向独立放行: ✓ {reason}")

    print("去重自测全部通过\n")


def test_notify_format():
    print("=" * 60)
    print("自测: 飞书推送格式化(PRD §10.2, 不实发)")
    print("=" * 60)
    nf = notifier.Notifier()
    plan = TradePlan(
        interval="5m", setup_type="楔形底牛旗(高3)", direction="long",
        grade="strongest", entry_price=4394.51, order_type="stop",
        stop_price=4391.00, tp1_price=4398.02, tp2_price=4402.50,
        risk_reward=2.28, mode="swing", market_state="trend_up",
        ema20=4390.0, atr=4.9, target_basis="AB=CD等距",
    )
    advice = PositionAdvice(risk_pct=1.0, risk_amount=100.0, sl_distance=3.51,
                            quantity=28.49, notional=125180.0)
    text = nf.format_signal(plan, close_time=1786618140000, advice=advice)
    print(text)
    print("-" * 60)
    for must in ("", "楔形底牛旗(高3)", "开仓价格：4394.51", "止损SL：4391.0",
                 "止盈TP1：4398.02", "TP2：4402.5", "信号分级：strongest",
                 "AB=CD等距", "波段", "多"):
        assert must in text, f"缺少字段: {must}"
        print(f"  字段『{must}』 ✓")
    print("\n推送格式化自测全部通过")


if __name__ == "__main__":
    test_dedup()
    test_notify_format()