# -*- coding: utf-8 -*-
"""
验证脚本: 拉取 Binance K线 → 市场状态判断 → 打印四阶段报告
用法: python verify_market_state.py
"""
import config
import data
import market_state as ms


def report(interval, r):
    if r.state == "unknown":
        print(f"[{interval}] 数据不足，跳过")
        return
    print(f"  ┌─ {interval} 市场状态 ───────────────────────")
    print(f"  │ 状态: {judge.describe(r)}  可交易: {'✓' if r.is_tradeable else '✗'}")
    print(f"  │ 方向: {'多' if r.direction > 0 else '空' if r.direction < 0 else '中性'}")
    print(f"  │ EMA20={r.ema20:.2f} 斜率={r.ema_slope:.4f}  ATR={r.atr:.2f}")
    print(f"  │ 回调深度占比={r.pullback_ratio:.2f}  (阈值 0.5窄/0.75宽)")
    print(f"  │ ATR比率={r.atr_ratio:.2f}  重叠率={r.overlap_ratio:.2f}  Hurst={r.hurst:.2f}")
    print(f"  │ 穿越EMA={r.ema_cross_count}次  价在EMA上={r.price_above_ema_ratio*100:.0f}%")
    print(f"  │ 区间边界: {r.range_low:.2f} ~ {r.range_high:.2f}")
    print(f"  └──────────────────────────────────")


if __name__ == "__main__":
    cache = data.KlineCache()
    judge = ms.MarketStateJudge()
    print("=" * 50)
    print(f" 价格行为 v2 · 市场状态验证  {config.SYMBOL}")
    print("=" * 50)

    for interval in config.INTERVALS:
        raw = data.fetch_klines(interval)
        if not raw:
            print(f"[{interval}] 拉取失败，跳过")
            continue
        cache.update(interval, raw)
        arr = cache.get_arrays(interval)
        if arr is None:
            print(f"[{interval}] 无缓存数据，跳过")
            continue
        r = judge.judge(arr["opens"], arr["highs"], arr["lows"], arr["closes"])
        report(interval, r)

    print("验证完成")