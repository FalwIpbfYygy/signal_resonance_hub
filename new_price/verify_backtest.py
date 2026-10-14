# -*- coding: utf-8 -*-
"""
验证 backtest.py 的关键正确性:
  1. CausalAnalyzer.judge_at(i) 与朴素 judge(切片[:i+1]) 逐棒一致(无前视)
  2. simulate_trade 触及成交 + TP1后保本 的 R 口径正确
运行: python verify_backtest.py
"""
import numpy as np

import config
import market_state as ms
import signal_bars as sb
import entry as en
from backtest import CausalAnalyzer, simulate_trade, cost_r_of


def _synth(n=400, seed=42):
    rng = np.random.RandomState(seed)
    price = 4000.0
    opens, highs, lows, closes = [], [], [], []
    vols, tb, ot, ct = [], [], [], []
    for i in range(n):
        o = price
        c = o + rng.randn() * 8
        h = max(o, c) + abs(rng.randn()) * 3
        l = min(o, c) - abs(rng.randn()) * 3
        opens.append(o); closes.append(c); highs.append(h); lows.append(l)
        vols.append(100.0); tb.append(50.0)
        ot.append(i * 60000); ct.append((i + 1) * 60000)
        price = c
    return {
        "opens": opens, "highs": highs, "lows": lows, "closes": closes,
        "volumes": vols, "taker_buys": tb, "open_times": ot, "close_times": ct,
    }


def test_parity():
    arr = _synth()
    ca = CausalAnalyzer(arr)
    judge = ms.MarketStateJudge()
    mism = 0
    for i in range(60, len(arr["closes"]), 3):
        r1 = ca.judge_at(i)
        r2 = judge.judge(arr["opens"][:i + 1], arr["highs"][:i + 1],
                         arr["lows"][:i + 1], arr["closes"][:i + 1])
        ok = (r1.state == r2.state and r1.direction == r2.direction
              and abs(r1.pullback_ratio - r2.pullback_ratio) < 1e-9
              and abs(r1.atr - r2.atr) < 1e-9
              and abs(r1.atr_ratio - r2.atr_ratio) < 1e-9
              and abs(r1.hurst - r2.hurst) < 1e-9
              and abs(r1.overlap_ratio - r2.overlap_ratio) < 1e-9)
        if not ok:
            mism += 1
            if mism <= 3:
                print(f"MISMATCH i={i} state {r1.state}/{r2.state} "
                      f"dir {r1.direction}/{r2.direction} "
                      f"pb {r1.pullback_ratio:.6f}/{r2.pullback_ratio:.6f}")
    print(f"[parity] 检查点 mismatches={mism}")
    return mism == 0


def _mk_bars(closes, highs, lows):
    arr = {
        "opens": [c for c in closes], "highs": list(highs), "lows": list(lows),
        "closes": list(closes), "volumes": [1.0] * len(closes),
        "taker_buys": [0.5] * len(closes),
        "open_times": [0] * len(closes),
        "close_times": [i * 60000 for i in range(len(closes))],
    }
    return sb.build_bars(arr)


def test_simulate():
    # 多头 stop: entry=100, stop=99(risk=1), tp1=101, tp2=102
    # 注意: 信号棒 idx=0, 未来棒从 idx+1=1 起
    plan = en.TradePlan(interval="5m", setup_type="t", direction="long",
                        entry_price=100.0, stop_price=99.0, tp1_price=101.0,
                        tp2_price=102.0, order_type="stop")
    # 情景1: 直接止损(未来首根 low<=99 触发入场+止损, high 未到101)
    bars = _mk_bars([100]*10, [100.5]*10, [100]*10)
    bars[1].high = 100.5; bars[1].low = 98.0
    r = simulate_trade(plan, bars, 0, 100)
    assert r["result"] == "loss" and abs(r["gross_r"] - (-1.0)) < 1e-9, r

    # 情景2: 触及TP1后回保本(先高到101.5 再回到100)
    plan2 = en.TradePlan("5m", "t", "long", "", 100.0, "stop", 99.0, 101.0, 102.0)
    bars2 = _mk_bars([100]*10, [100]*10, [100]*10)
    bars2[1].high = 101.5   # 触TP1 -> 半仓+0.5R, 余仓保本
    bars2[2].low = 99.5     # 未破保本(100)也不破stop
    bars2[3].low = 100.0    # 触及保本 -> 余仓0R
    r2 = simulate_trade(plan2, bars2, 0, 100)
    assert r2["result"] == "win" and abs(r2["gross_r"] - 0.5) < 1e-9, r2
    assert r2["tp1_hit"] is True, r2

    # 情景3: 未触及入场(high 永远 < 100) -> not_filled
    bars3 = _mk_bars([100]*10, [99.9]*10, [99.5]*10)
    r3 = simulate_trade(plan2, bars3, 0, 100)
    assert r3["result"] == "not_filled", r3

    # 情景4: 触及TP1后直达TP2 -> 0.5 + 0.5*2 = 1.5R
    bars4 = _mk_bars([100]*10, [100]*10, [100]*10)
    bars4[1].high = 101.0   # TP1
    bars4[2].high = 102.0   # TP2
    bars4[2].low = 100.01   # 未破保本(>100), 触发 TP2
    r4 = simulate_trade(plan2, bars4, 0, 100)
    assert r4["result"] == "win" and abs(r4["gross_r"] - 1.5) < 1e-9, r4
    assert r4["tp2_hit"] is True, r4

    print("[simulate] 情景1-4 通过")


def test_cost():
    plan = en.TradePlan(interval="5m", setup_type="t", direction="long",
                        entry_price=4300.0, stop_price=4294.0)  # risk=6
    cr = cost_r_of(plan)
    expect = 2 * (config.COST_SLIPPAGE_PCT + config.COST_COMMISSION_PCT) * 4300 / 6
    assert abs(cr - expect) < 1e-12, (cr, expect)
    print(f"[cost] cost_r={cr:.4f} (entry=4300, risk=6)")


if __name__ == "__main__":
    a = test_parity()
    test_simulate()
    test_cost()
    print("ALL OK" if a else "PARITY FAILED")