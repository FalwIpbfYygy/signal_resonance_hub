# -*- coding: utf-8 -*-
"""
验证脚本: 拉取 5m K线 → 棒线分类 + 信号棒识别
用法: python verify_signal_bars.py
"""
import config
import data
import signal_bars as sb
from indicators import calc_avg_entity, calc_atr


if __name__ == "__main__":
    interval = config.PRIMARY_INTERVAL
    cache = data.KlineCache()
    raw = data.fetch_klines(interval)
    if not raw:
        print("拉取失败")
        raise SystemExit(1)
    cache.update(interval, raw)
    arr = cache.get_arrays(interval)
    bars = sb.build_bars(arr)

    classifier = sb.BarClassifier()
    detector = sb.SignalBarDetector()

    avg_entity = calc_avg_entity(arr["opens"], arr["closes"], config.AVG_ENTITY_PERIOD)
    atr = calc_atr(arr["highs"], arr["lows"], arr["closes"], config.ATR_PERIOD)

    # 最近 12 根棒线分类
    print(f"近12根{interval}棒线分类 (avg_entity={avg_entity:.2f} atr={atr:.2f}):")
    for b in bars[-12:]:
        cls = classifier.classify(b)
        extra = ""
        if cls != "doji" and classifier.is_strong_trend_bar(b, avg_entity):
            extra = " [强势]"
        if detector.is_exhaustion_bar(b, avg_entity):
            extra += " [耗尽]"
        if detector.is_outside_bar(bars, b.index):
            extra += " [外包]"
        print(f"  #{b.index:4d} O={b.open:7.2f} H={b.high:7.2f} "
              f"L={b.low:7.2f} C={b.close:7.2f} -> {cls}{extra}")

    # 全程扫描信号棒
    print("\n信号棒扫描结果:")
    found = 0
    for i in range(config.AVG_ENTITY_PERIOD, len(bars)):
        for s in detector.detect_all(bars, i, avg_entity, atr):
            if s.pattern in ("inside_bar", "ii_pattern", "ioi_pattern"):
                continue  # 突破型架构需后续确认，本模块先不列
            if not s.is_tradeable:
                continue
            found += 1
            print(f"  #{s.index} {s.pattern} {s.direction} "
                  f"等级={s.grade} 入场≈{s.entry_price:.2f}  {';'.join(s.reasons[:2])}")
    if found == 0:
        print("  最近无可交易的 strong/strongest 信号棒")
    print("验证完成")