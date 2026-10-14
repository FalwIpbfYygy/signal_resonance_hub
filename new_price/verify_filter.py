# -*- coding: utf-8 -*-
"""
验证脚本: 过滤模块
  1) 合成自测 —— 趋势态下逐分支断言(对拍锁定定义)
  2) 拉取 5m K线 —— 实盘过滤报告(观察 micro_double_bottom 去噪)
用法: python verify_filter.py
"""
from collections import Counter

import config
import data
import signal_bars as sb
import market_state as ms
import pullback_count as pc
import filter as fl
from indicators import calc_avg_entity, calc_atr


def make_signal(index, pattern, direction, grade, entry=100.0,
                hi=100.5, lo=99.5, tradeable=True):
    return sb.SignalBar(index, pattern, direction, grade, entry, hi, lo,
                        tradeable, [])


def self_test():
    print("=" * 60)
    print("自测: 过滤分支(锁定定义)")
    print("=" * 60)
    flt = fl.SignalFilter()

    # 趋势态(多头) 行情
    mstate = ms.MarketStateResult(
        state="trend_up", direction=1, atr=5.0, pullback_ratio=0.3,
        range_high=110.0, range_low=90.0, ema20=100.0, is_tradeable=True,
    )
    # 数推等级: 索引10=高2, 12=高3, 14=高4, 其余=0
    high_levels = [0] * 20
    high_levels[10] = 2
    high_levels[12] = 3
    high_levels[14] = 4
    low_levels = [0] * 20
    bars = []  # 合成自测不涉及量能/时段(默认关闭)

    # 1) 0计数 → 拒(噪音)
    r = flt.evaluate(make_signal(0, "micro_double_bottom", "long", "strong"),
                     mstate, bars, high_levels, low_levels)
    assert not r.passed and "0计数" in r.reasons[0], f"用例1失败: {r.reasons}"
    print(f"  1) 0计数拒噪音: {'✓' if not r.passed else '✗'} {r.reasons[0]}")

    # 2) 高2 + strong(非最强信号棒) → strong
    r = flt.evaluate(make_signal(10, "double_bull_reversal", "long", "strong"),
                     mstate, bars, high_levels, low_levels)
    assert r.passed and r.grade == "strong", f"用例2失败: {r.grade} {r.reasons}"
    print(f"  2) 高2+strong → strong: {'✓' if r.grade == 'strong' else '✗'}")

    # 3) 高2 + strongest + 强趋势 → strongest
    r = flt.evaluate(make_signal(10, "double_bull_reversal", "long", "strongest"),
                     mstate, bars, high_levels, low_levels)
    assert r.passed and r.grade == "strongest", f"用例3失败: {r.grade}"
    print(f"  3) 高2+strongest+强趋势 → strongest: {'✓' if r.grade == 'strongest' else '✗'}")

    # 4) 高3(楔形三推) → strongest
    r = flt.evaluate(make_signal(12, "micro_double_bottom", "long", "strong"),
                     mstate, bars, high_levels, low_levels)
    assert r.passed and r.grade == "strongest", f"用例4失败: {r.grade}"
    print(f"  4) 高3楔形 → strongest: {'✓' if r.grade == 'strongest' else '✗'}")

    # 5) 高4 → 拒(已陷区间)
    r = flt.evaluate(make_signal(14, "micro_double_bottom", "long", "strong"),
                     mstate, bars, high_levels, low_levels)
    assert not r.passed and "已陷区间" in r.reasons[0], f"用例5失败: {r.reasons}"
    print(f"  5) 高4拒已陷区间: {'✓' if not r.passed else '✗'}")

    # 6) 逆势(空头 in 多头趋势) → 拒(80%法则)
    r = flt.evaluate(make_signal(10, "double_bear_reversal", "short", "strongest"),
                     mstate, bars, high_levels, low_levels)
    assert not r.passed and "逆势" in r.reasons[0], f"用例6失败: {r.reasons}"
    print(f"  6) 逆势反转拒(80%法则): {'✓' if not r.passed else '✗'}")

    # 7) general 级 → 拒
    r = flt.evaluate(make_signal(10, "double_bull_reversal", "long", "general"),
                     mstate, bars, high_levels, low_levels)
    assert not r.passed, f"用例7失败: {r.reasons}"
    print(f"  7) general拒: {'✓' if not r.passed else '✗'}")

    # 8) 紧凑区间 → 拒
    mstate2 = ms.MarketStateResult(state="tight_range", direction=0, is_tradeable=False)
    r = flt.evaluate(make_signal(10, "double_bull_reversal", "long", "strong"),
                     mstate2, bars, high_levels, low_levels)
    assert not r.passed and "barbwire" in r.reasons[0], f"用例8失败: {r.reasons}"
    print(f"  8) 紧凑区间拒: {'✓' if not r.passed else '✗'}")

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
    avg_entity = calc_avg_entity(arr["opens"], arr["closes"], config.AVG_ENTITY_PERIOD)
    atr = calc_atr(arr["highs"], arr["lows"], arr["closes"], config.ATR_PERIOD)

    print("=" * 60)
    print(f"过滤实盘验证  {interval}   市场状态: {judge.describe(mstate)} "
          f"方向={'多' if mstate.direction > 0 else '空' if mstate.direction < 0 else '中性'}")
    print(f"回调深度占比={mstate.pullback_ratio:.2f}  ATR={mstate.atr:.2f}")
    print("=" * 60)

    raw_found = Counter()
    passed = []
    rejected = Counter()

    for i in range(config.AVG_ENTITY_PERIOD, len(bars)):
        for s in detector.detect_all(bars, i, avg_entity, atr):
            if s.pattern in ("inside_bar", "ii_pattern", "ioi_pattern"):
                continue
            if s.direction == "both":
                continue
            raw_found[s.pattern] += 1
            res = flt.evaluate(s, mstate, bars, high_levels, low_levels)
            if res.passed:
                passed.append((s, res))
            else:
                key = res.reasons[0] if res.reasons else "无原因"
                rejected[f"{s.pattern}|{key}"] += 1

    print(f"\n[过滤前] 信号棒形态分布(可交易): {dict(raw_found)}")
    print(f"[过滤后] 放行信号 = {len(passed)} 条")

    for s, res in passed:
        print(f"  #{s.index} {s.pattern} {s.direction} 分级={res.grade} "
              f"入场≈{s.entry_price:.2f}  {';'.join(res.reasons)}")

    print("\n拒绝原因统计(形态|首因 → 次数):")
    for k, v in rejected.most_common(30):
        print(f"  {k} = {v}")
    print("\n验证完成")


if __name__ == "__main__":
    self_test()
    live_scan()