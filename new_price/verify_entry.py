# -*- coding: utf-8 -*-
"""
验证脚本: 开仓逻辑模块
  1) 合成自测 —— 五种方案(逆1顺1/双重顶底/楔形/fade/末段旗形)确定性断言
  2) 拉取 5m K线 —— 实盘路由: 信号棒 → 过滤 → 交易计划
用法: python verify_entry.py
"""
import config
import data
import signal_bars as sb
import market_state as ms
import pullback_count as pc
import filter as fl
import entry as en
from indicators import calc_avg_entity, calc_atr


def make_sig(direction, grade="strong", hi=104.5, lo=103.0, entry=104.51,
             pattern="double_bull_reversal", tradeable=True):
    return sb.SignalBar(0, pattern, direction, grade, entry, hi, lo,
                        tradeable, [])


def self_test():
    print("=" * 60)
    print("自测: 开仓逻辑五方案(锁定定义)")
    print("=" * 60)
    engine = en.EntryEngine()

    # --- 公共趋势锚点: 前高110 / 前低100 ---
    def trend_state(state, direction):
        return ms.MarketStateResult(
            state=state, direction=direction, atr=5.0, ema20=100.0,
            pullback_ratio=0.3, range_high=110.0, range_low=90.0,
            swing_highs=[(90, 110.0)], swing_lows=[(85, 100.0)],
            is_tradeable=True,
        )

    # 1)arning 逆1顺1(高1, 做多): 停止单, 止损<入场<止盈
    mst = trend_state("trend_up", 1)
    p = engine.build_trend_plan(make_sig("long", entry=104.51, hi=104.5, lo=103.0),
                                mst, level=1, grade="strong")
    assert p and p.setup_type == "逆1顺1(高1)", f"用例1失败: {p}"
    assert p.order_type == "stop" and p.stop_price < p.entry_price < p.tp1_price, \
        f"用例1价格关系失败: {p}"
    print(f"  1) 逆1顺1(高1): ✓ {p.setup_type} 入场={p.entry_price} 止损={p.stop_price} "
          f"TP1={p.tp1_price} TP2={p.tp2_price} RR={p.risk_reward}")

    # 2) 双重底牛旗(高2, 做多)
    p = engine.build_trend_plan(make_sig("long", entry=104.51, hi=104.5, lo=103.0),
                                mst, level=2, grade="strongest")
    assert p and p.setup_type == "双重底牛旗(高2)", f"用例2失败: {p}"
    assert p.tp2_price > p.tp1_price and p.mode == "swing", f"用例2失败: {p}"
    print(f"  2) 双重底牛旗(高2): ✓ {p.setup_type} TP2={p.tp2_price} 依据={p.target_basis}")

    # 3) 楔形顶熊旗(低3, 做空): 止损>入场>止盈
    mst_down = trend_state("trend_down", -1)
    p = engine.build_trend_plan(make_sig("short", entry=99.49, hi=101.0, lo=99.5,
                                         pattern="double_bear_reversal"),
                                mst_down, level=3, grade="strongest")
    assert p and p.setup_type == "楔形顶熊旗(低3)", f"用例3失败: {p}"
    assert p.grade == "strongest", f"用例3分级失败: {p.grade}"
    assert p.stop_price > p.entry_price > p.tp1_price > p.tp2_price, f"用例3价格关系失败: {p}"
    print(f"  3) 楔形顶熊旗(低3): ✓ {p.setup_type} 入场={p.entry_price} 止损={p.stop_price} "
          f"TP2={p.tp2_price}")

    # 4) 看衰突破 fade(区间上缘做空): 限价, 止损>入场>止盈到中轴
    mst_range = ms.MarketStateResult(
        state="range", direction=0, atr=5.0, ema20=100.0,
        range_high=110.0, range_low=90.0, swing_highs=[(90, 110.0)],
        swing_lows=[(85, 90.0)], is_tradeable=True,
    )
    p = engine.build_fade_plan(make_sig("short", hi=109.8, lo=108.0, entry=108.0,
                                        pattern="bear_reversal"), mst_range)
    assert p and p.setup_type == "看衰突破(区间上缘做空)", f"用例4失败: {p}"
    assert p.order_type == "limit" and p.stop_price > p.entry_price and p.tp2_price < p.entry_price, \
        f"用例4失败: {p}"
    print(f"  4) 看衰突破fade: ✓ {p.setup_type} 入场={p.entry_price}(限价) 止损={p.stop_price} "
          f"TP2中轴={p.tp2_price}")

    # 5) 末段旗形(顶部做空, 触磁110无跟进)
    p = engine.build_final_flag_plan(make_sig("short", hi=110.0, lo=107.0, entry=106.99,
                                              pattern="bear_reversal"),
                                     mst, grade="strongest")
    assert p and p.setup_type == "末段旗形反转(做空)", f"用例5失败: {p}"
    assert p.stop_price > p.entry_price > p.tp2_price, f"用例5失败: {p}"
    print(f"  5) 末段旗形(做空): ✓ {p.setup_type} 入场={p.entry_price} 止损={p.stop_price} "
          f"TP2前波段点={p.tp2_price}")

    # 6) 末段旗形(底部做多, 触磁90)
    p = engine.build_final_flag_plan(make_sig("long", hi=92.0, lo=90.0, entry=92.01,
                                              pattern="bull_reversal"),
                                     mst_down, grade="strongest")
    assert p and p.setup_type == "末段旗形反转(做多)", f"用例6失败: {p}"
    assert p.stop_price < p.entry_price < p.tp1_price, f"用例6失败: {p}"
    print(f"  6) 末段旗形(做多): ✓ {p.setup_type} 入场={p.entry_price} 止损={p.stop_price} "
          f"TP2={p.tp2_price}")

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
    avg_entity = calc_avg_entity(arr["opens"], arr["closes"], config.AVG_ENTITY_PERIOD)
    atr = calc_atr(arr["highs"], arr["lows"], arr["closes"], config.ATR_PERIOD)

    print("=" * 60)
    print(f"开仓逻辑实盘验证  {interval}   市场状态: {judge.describe(mstate)}")
    print(f"回调深度占比={mstate.pullback_ratio:.2f}  ATR={mstate.atr:.2f}")
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
            level = (high_levels[i] if s.direction == "long" else low_levels[i])
            plan = engine.build_trend_plan(s, mstate, level, res.grade, interval)
            if plan:
                plans.append((s, res, plan))

    # 仅展示最近 10 条计划
    print(f"\n放行且可开仓的信号计划 = {len(plans)} 条, 展示最近 10 条:")
    for s, res, p in plans[-10:]:
        d = "多" if p.direction == "long" else "空"
        print(f"  #{s.index} [{p.setup_type}|{d}|{p.grade}] 入场={p.entry_price} "
              f"止损={p.stop_price} TP1={p.tp1_price} TP2={p.tp2_price} "
              f"RR={p.risk_reward} 模式={p.mode} 依据={p.target_basis}")
    if not plans:
        print("  (无放行信号)")
    print("\n验证完成")


if __name__ == "__main__":
    self_test()
    live_scan()