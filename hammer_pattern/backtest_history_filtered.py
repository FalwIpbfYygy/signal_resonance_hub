# -*- coding: utf-8 -*-
"""锤子形态历史回测（过滤版）—— 严格无未来函数 + 工作日 + 9:00~23:00(北京)。

- 数据：复用 backtest_history.fetch_all 的缓存/分页逻辑（Binance fapi  5m，2026-01-01 起）。
- 形态判定：复用 detector.is_hammer（与全量报告同一套逻辑，不重新泛扫）。
- 过滤（本次新增）：
  * WEEKDAY_ONLY=True  -> 仅周一~周五，剔除周六/周日
  * HOUR_START=9, HOUR_END=23 -> 信号形成时间(北京)只在 09:00~23:00
- 前向观测：信号后 5/15/30 分钟涨跌幅仍只用信号之后的 K 线计算，属结果观测，非预测、非未来函数。
- 输出：新的过滤版报告文件，原全量报告不动。
"""
from __future__ import annotations
import os, json, datetime
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DEFAULTS
from detector import is_hammer
from backtest_history import fetch_all, _ts

SYMBOL = ""
WEEKDAY_ONLY = True          # 仅工作日（周一=0 .. 周五=4），剔除周六(5)/周日(6)
HOUR_START, HOUR_END = 9, 23 # 信号形成时间(北京) ∈ [9, 23]
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "backtest_report_weekday_9to23_2026-01_to_now.md")
BJ = datetime.timezone(datetime.timedelta(hours=8))


def hammer_signals_filtered(klines, p):
    """返回 [(index, metrics, vol_ratio)]，严格因果 + 工作日 + 时段过滤。"""
    vols = [float(k[5]) for k in klines]
    out = []
    for i, k in enumerate(klines):
        ok, m = is_hammer(k, p)
        if not ok:
            continue
        vr = None
        if p["volume_filter"]:
            if i >= p["vol_ma_window"]:
                ma = sum(vols[i - p["vol_ma_window"] + 1:i + 1]) / p["vol_ma_window"]
                vr = (vols[i] / ma) if ma > 0 else None
            if vr is None or vr < p["vol_mult"]:
                continue
        # —— 过滤：工作日 + 时段 ——
        t = _ts(klines[i][0])  # 北京时区
        if WEEKDAY_ONLY and t.weekday() >= 5:
            continue
        if not (HOUR_START <= t.hour <= HOUR_END):
            continue
        out.append((i, m, vr))
    return out


def fwd_pct(klines, i, bars):
    j = i + bars
    if j >= len(klines):
        return None
    c0 = float(klines[i][4])
    c1 = float(klines[j][4])
    return (c1 / c0 - 1.0) * 100.0 if c0 else None


def main():
    # 直接读缓存（避免补抓网络 hang）；如需刷新到最新可改回 fetch_all()
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache", "_5m_2026.json")
    with open(cache, "r", encoding="utf-8") as f:
        klines = json.load(f)
    print(f"\n[数据] 总 K 线 {len(klines)} 根，区间 {_ts(klines[0][0]).strftime('%Y-%m-%d')} ~ {_ts(klines[-1][0]).strftime('%Y-%m-%d %H:%M')} (北京)")
    if not klines:
        print("[中止] 无数据")
        return

    p = dict(DEFAULTS)
    sig = hammer_signals_filtered(klines, p)
    print(f"[信号] 放量锤子(≥1.8x) 且 工作日+09~23点 = {len(sig)} 根")

    WNAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_month, by_hour, by_wday = {}, {}, {}
    for i, m, vr in sig:
        t = _ts(klines[i][0])
        by_month[t.strftime("%Y-%m")] = by_month.get(t.strftime("%Y-%m"), 0) + 1
        by_hour[t.hour] = by_hour.get(t.hour, 0) + 1
        by_wday[t.strftime("%a")] = by_wday.get(t.strftime("%a"), 0) + 1

    rows = []
    for i, m, vr in sig:
        t = _ts(klines[i][0])
        rows.append({
            "time": t.strftime("%Y-%m-%d %H:%M"),
            "wd": WNAMES[t.weekday()],
            "lb": round(m["lower_to_body"], 1),
            "lr": round(m["lower_ratio"] * 100),
            "ur": round(m["upper_ratio"] * 100),
            "cp": round(m["close_pos"] * 100),
            "vr": round(vr, 1) if vr else 0,
            "f5": fwd_pct(klines, i, 1),
            "f15": fwd_pct(klines, i, 3),
            "f30": fwd_pct(klines, i, 6),
        })

    L = []
    L.append("# 锤子形态历史回测报告（过滤版：工作日 + 09:00~23:00 北京，2026-01 至今，严格无未来函数）\n")
    L.append("- 数据区间：" + _ts(klines[0][0]).strftime('%Y-%m-%d') + " ~ " +
             _ts(klines[-1][0]).strftime('%Y-%m-%d %H:%M') + "（北京），共 " + str(len(klines)) + " 根 5m K 线")
    L.append("- 判定：复用 detector.is_hammer；下影/全幅≥55%、上影/全幅≤15%、实体/全幅≤30%、收位≥60%、下影≥2×实体")
    L.append(f"- **过滤**：仅工作日(周一~周五) + 信号形成时间∈[09:00,23:00] 北京；放量≥1.8×MA20")
    L.append(f"- **命中 {len(sig)} 根**（对照：全量无过滤放量锤子 4898 根；全量仅放量过滤 529 根）")
    L.append("- 因果说明：信号判定仅用当根 OHLC；下表 5/15/30m 涨跌幅为信号后前向观测（只用信号之后的 K 线），非 P&L/胜率指标，不作未来函数。\n")
    L.append("## 月份分布")
    for k in sorted(by_month):
        L.append(f"- {k}: {by_month[k]}")
    L.append("\n## 小时分布（北京，9~23）")
    for h in sorted(by_hour):
        L.append(f"- {h:02d}:00: {by_hour[h]}")
    L.append("\n## 星期分布（应仅含 Mon~Fri）")
    for w in WNAMES[:5]:
        if w in by_wday:
            L.append(f"- {w}: {by_wday[w]}")
    L.append("\n## 信号明细（过滤后·放量锤子·工作日·09~23点）")
    L.append("| 时间(北京) | 星期 | 下影/实体 | 下影/全幅 | 上影/全幅 | 收位 | 量/MA20 | 后5m% | 后15m% | 后30m% |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        f5 = f"{r['f5']:+.2f}" if r['f5'] is not None else "-"
        f15 = f"{r['f15']:+.2f}" if r['f15'] is not None else "-"
        f30 = f"{r['f30']:+.2f}" if r['f30'] is not None else "-"
        L.append(f"| {r['time']} | {r['wd']} | {r['lb']}x | {r['lr']}% | {r['ur']}% | {r['cp']}% | {r['vr']}x | {f5} | {f15} | {f30} |")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[报告] 已写出：{REPORT}")
    print(f"[汇总] 过滤后放量锤子 {len(sig)} 根；月份: " + ", ".join(f"{k}={by_month[k]}" for k in sorted(by_month)))
    print(f"[汇总] 小时: " + ", ".join(f"{h:02d}={by_hour[h]}" for h in sorted(by_hour)))


if __name__ == "__main__":
    main()
