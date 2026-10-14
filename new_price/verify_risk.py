# -*- coding: utf-8 -*-
"""
验证脚本: 风控模块
  1) 合成自测 —— 仓位反推 / 加仓合规 / 无效化判定 确定性断言
  2) 拉取 5m K线 —— 实盘: 若有放行计划, 输出仓位建议与无效化状态
用法: python verify_risk.py
"""
import config
import data
import signal_bars as sb
import market_state as ms
import pullback_count as pc
import filter as fl
import entry as en
import risk as rk
from indicators import calc_avg_entity, calc_atr
from entry import TradePlan


def self_test():
    print("=" * 60)
    print("自测: 风控三要素(仓位/加仓/无效化)")
    print("=" * 60)
    rm = rk.RiskManager()

    # --- 1) 仓位反推: 风险金额 / SL距离 = 数量 ---
    plan = TradePlan(interval="5m", setup_type="双重底牛旗(高2)", direction="long",
                     entry_price=100.0, stop_price=98.0, tp1_price=102.0,
                     tp2_price=104.0, atr=5.0)
    adv = rm.suggest_position(plan, equity=10000.0)
    assert adv.risk_amount == 100.0, f"风险金额失败: {adv.risk_amount}"
    assert adv.quantity == 50.0, f"数量失败: {adv.quantity}"
    assert abs(adv.notional - 5000.0) < 0.01, f"名义金额失败: {adv.notional}"
    print(f"  1) 仓位反推: ✓ 风险={adv.risk_amount}USD 数量={adv.quantity} 名义={adv.notional}USD")

    # --- 2) 加仓合规 ---
    mst_up = ms.MarketStateResult(state="trend_up", direction=1, atr=5.0,
                                  pullback_ratio=0.3, is_tradeable=True)
    # 顺势+早期(level2)+盈利中 → 允许
    ok, reason = rm.evaluate_add(plan, mst_up, entry_price=100.0,
                                 current_price=102.0, count_level=2)
    assert ok, f"加仓应允许: {reason}"
    print(f"  2a) 顺势早期盈利加仓: ✓ {reason}")
    # 摊平亏损(浮亏) → 拒绝
    ok, reason = rm.evaluate_add(plan, mst_up, entry_price=100.0,
                                 current_price=98.0, count_level=2)
    assert not ok, f"摊平应拒绝: {reason}"
    print(f"  2b) 摊平亏损拒绝: ✓ {reason}")
    # 逆势(空头 in 多头趋势) → 拒绝
    plan_short = TradePlan(interval="5m", setup_type="x", direction="short",
                           entry_price=100.0, stop_price=102.0)
    ok, reason = rm.evaluate_add(plan_short, mst_up, entry_price=100.0,
                                 current_price=98.0, count_level=1)
    assert not ok, f"逆势应拒绝: {reason}"
    print(f"  2c) 逆势拒绝: ✓ {reason}")
    # 趋势中后段(level3) → 拒绝
    ok, reason = rm.evaluate_add(plan, mst_up, entry_price=100.0,
                                 current_price=103.0, count_level=3)
    assert not ok, f"中后段应拒绝: {reason}"
    print(f"  2d) 趋势中后段拒绝: ✓ {reason}")

    # --- 3) 无效化判定 ---
    mst_tr = ms.MarketStateResult(state="trend_up", direction=1, atr=5.0,
                                  swing_highs=[(90, 108.0)], swing_lows=[(85, 95.0)])
    plan_long = TradePlan(interval="5m", setup_type="x", direction="long",
                          entry_price=104.0, stop_price=101.0)
    r = rm.invalidation(plan_long, mst_tr, current_price=94.0)
    assert r and "推动起点" in r, f"趋势无效化失败: {r}"
    print(f"  3a) 趋势反向破推动起点: ✓ {r}")
    r = rm.invalidation(plan_long, mst_tr, current_price=105.0)
    assert r is None, f"应无无效化: {r}"
    print(f"  3b) 未破推动起点: ✓ 无离场")

    # 区间放量真突破 → 离场
    mst_rg = ms.MarketStateResult(state="range", direction=0, atr=5.0,
                                  range_high=110.0, range_low=90.0)
    plan_sh = TradePlan(interval="5m", setup_type="fade", direction="short",
                        entry_price=109.0, stop_price=111.0)
    r = rm.invalidation(plan_sh, mst_rg, current_price=111.5,
                        current_volume=300.0, avg_volume=100.0)
    assert r and "放量" in r, f"区间无效化失败: {r}"
    print(f"  3c) 区间放量真突破: ✓ {r}")

    print("自测全部通过\n")


def live_scan():
    interval = config.PRIMARY_INTERVAL
    cache = data.KlineCache()
    raw = data.fetch_klines(interval)
    if not raw:
        print("拉取失败")
        raise SystemExit(1)
    cache.update(interval, raw)
    arr = cache.get_arrays(interval)
    bars = sb.build_bars(arr)

    judge = ms.MarketStateJudge()
    mstate = judge.judge(arr["opens"], arr["highs"], arr["lows"], arr["closes"])
    high_levels, low_levels = pc.count_pullback_levels(bars)

    detector = sb.SignalBarDetector()
    flt = fl.SignalFilter()
    engine = en.EntryEngine()
    rm = rk.RiskManager()
    avg_entity = calc_avg_entity(arr["opens"], arr["closes"], config.AVG_ENTITY_PERIOD)
    atr = calc_atr(arr["highs"], arr["lows"], arr["closes"], config.ATR_PERIOD)

    print("=" * 60)
    print(f"风控实盘验证  {interval}   市场状态: {judge.describe(mstate)}")
    print("=" * 60)

    plans = []
    for i in range(config.AVG_ENTITY_PERIOD, len(bars)):
        for s in detector.detect_all(bars, i, avg_entity, atr):
            if s.pattern in ("inside_bar", "ii_pattern", "ioi_pattern"):
                continue
            if s.direction == "both":
                continue
            res = flt.evaluate(s, mstate, bars, high_levels, low_levels)
            if not res.passed:
                continue
            level = high_levels[i] if s.direction == "long" else low_levels[i]
            plan = engine.build_trend_plan(s, mstate, level, res.grade, interval)
            if plan:
                plans.append((s, plan))

    if not plans:
        print("  (无放行信号, 仓位建议无从谈起)")
    last_close = bars[-1].close
    for s, p in plans[-5:]:
        adv = rm.suggest_position(p)
        inv = rm.invalidation(p, mstate, last_close)
        print(f"  #{s.index} [{p.setup_type}|{p.direction}] 数量建议={adv.quantity} "
              f"(风险{adv.risk_amount}USD) 离场判定={'无' if inv is None else inv}")
    print("\n验证完成")


if __name__ == "__main__":
    self_test()
    live_scan()