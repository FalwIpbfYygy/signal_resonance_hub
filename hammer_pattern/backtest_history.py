# -*- coding: utf-8 -*-
"""锤子形态历史回测（只读扫描，严格无未来函数）。
- 数据：Binance fapi  5m，从 2026-01-01 抓到当前，分页 + 本地缓存可断点续跑。
- 判定：复用 detector.is_hammer（阈值见 PRD），检测只用当根 OHLC（信号时刻已可知，无未来函数）。
- 前向上下文：信号后 5/15/30 分钟收盘价相对信号收盘的涨跌幅，只用信号之后的 K 线计算
  （属结果观测，不参与信号生成，不构成 P&L/胜率指标）。
- 输出：信号时间清单 + 计数（按月份/小时/星期分布）+ 明细表，落到报告文件。
"""
from __future__ import annotations

import sys, os, json, subprocess, time, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DEFAULTS
from detector import is_hammer

SYMBOL = ""
INTERVAL = "5m"
LIMIT = 1500
PROXY = DEFAULTS["proxy"]
START = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache", "_5m_2026.json")
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report_2026-01_to_now.md")
BJ = datetime.timezone(datetime.timedelta(hours=8))


def _ts(ms, tz=BJ):
    return datetime.datetime.fromtimestamp(ms / 1000, tz)


def fetch_chunk(start_ms, end_ms):
    url = (f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}"
           f"?symbol={SYMBOL}&interval={INTERVAL}&limit={LIMIT}&startTime={start_ms}")
    if end_ms:
        url += f"&endTime={end_ms}"
    cmd = ["curl", "-s", "-m", "120"]
    if PROXY:
        cmd += ["-x", PROXY]
    cmd.append(url)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=130)
    if p.returncode != 0:
        raise RuntimeError(f"curl rc={p.returncode}: {p.stderr.strip()}")
    return json.loads(p.stdout)


def fetch_all():
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    cache = []
    if os.path.exists(CACHE):
        with open(CACHE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        print(f"[缓存] 载入 {len(cache)} 根，将从末尾续跑")
    end_ms = int(time.time() * 1000)
    start_ms = int(START.timestamp() * 1000)
    if cache:
        start_ms = int(cache[-1][0]) + 1
    while start_ms < end_ms:
        try:
            chunk = fetch_chunk(start_ms, end_ms)
        except Exception as e:
            print(f"[重试] {e}")
            time.sleep(3)
            chunk = fetch_chunk(start_ms, end_ms)
        if not chunk:
            # 该区间无数据（如标的尚未上线），向前跳一整段
            start_ms += LIMIT * 5 * 60 * 1000
            continue
        last_ot = cache[-1][0] if cache else -1
        for k in chunk:
            if k[0] > last_ot:
                cache.append(k)
                last_ot = k[0]
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        start_ms = int(chunk[-1][0]) + 1
        print(f"  累计 {len(cache)} 根，末根 {_ts(start_ms-1).strftime('%Y-%m-%d %H:%M')} (北京)", flush=True)
        time.sleep(0.3)
    return cache


def hammer_signals(klines, p):
    """返回 [(index, metrics, vol_ratio)]，严格因果：只看单根 OHLC。"""
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
    klines = fetch_all()
    print(f"\n[数据] 总 K 线 {len(klines)} 根，区间 {_ts(klines[0][0]).strftime('%Y-%m-%d')} ~ {_ts(klines[-1][0]).strftime('%Y-%m-%d')}")
    if not klines:
        print("[中止] 无任何数据，可能标的历史不足或代理异常")
        return

    p_on = dict(DEFAULTS)            # 放量 1.8x（PRD 默认“信号”）
    p_off = dict(DEFAULTS); p_off["volume_filter"] = False  # 不放开量
    sig_on = hammer_signals(klines, p_on)
    sig_off = hammer_signals(klines, p_off)
    print(f"[信号] 锤子(放量≥1.8x) {len(sig_on)} 根；全部锤子(不放开量) {len(sig_off)} 根")

    # 分布统计
    by_month, by_hour, by_wday = {}, {}, {}
    for i, m, vr in sig_on:
        t = _ts(klines[i][0])
        by_month[t.strftime("%Y-%m")] = by_month.get(t.strftime("%Y-%m"), 0) + 1
        by_hour[t.hour] = by_hour.get(t.hour, 0) + 1
        by_wday[t.strftime("%a")] = by_wday.get(t.strftime("%a"), 0) + 1

    # 明细行
    rows = []
    for i, m, vr in sig_on:
        t = _ts(klines[i][0])
        rows.append({
            "time": t.strftime("%Y-%m-%d %H:%M"),
            "lb": round(m["lower_to_body"], 1),
            "lr": round(m["lower_ratio"] * 100),
            "ur": round(m["upper_ratio"] * 100),
            "cp": round(m["close_pos"] * 100),
            "vr": round(vr, 1) if vr else 0,
            "f5": fwd_pct(klines, i, 1),
            "f15": fwd_pct(klines, i, 3),
            "f30": fwd_pct(klines, i, 6),
        })

    # 写报告
    L = []
    L.append(f"# 锤子形态历史回测报告（ 5m，2026-01 至今，严格无未来函数）\n")
    L.append(f"- 数据区间：{_ts(klines[0][0]).strftime('%Y-%m-%d')} ~ {_ts(klines[-1][0]).strftime('%Y-%m-%d')}（北京），共 {len(klines)} 根 5m K 线")
    L.append(f"- 判定：复用 detector.is_hammer；下影/全幅≥55%、上影/全幅≤15%、实体/全幅≤30%、收位≥60%、下影≥2×实体")
    L.append(f"- 信号口径：**放量≥1.8×MA20**（PRD 默认），命中 **{len(sig_on)}** 根；不放开量则 {len(sig_off)} 根")
    L.append(f"- 因果说明：信号判定仅用当根 OHLC；下表 5/15/30m 涨跌幅为信号后前向观测（只用信号之后的 K 线），非 P&L/胜率指标，不作未来函数。\n")
    L.append("## 月份分布")
    for k in sorted(by_month):
        L.append(f"- {k}: {by_month[k]}")
    L.append("\n## 小时分布（北京）")
    for h in sorted(by_hour):
        L.append(f"- {h:02d}:00: {by_hour[h]}")
    L.append("\n## 星期分布")
    for w in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
        if w in by_wday:
            L.append(f"- {w}: {by_wday[w]}")
    L.append("\n## 信号明细（放量锤子）")
    L.append("| 时间(北京) | 下影/实体 | 下影/全幅 | 上影/全幅 | 收位 | 量/MA20 | 后5m% | 后15m% | 后30m% |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        f5 = f"{r['f5']:+.2f}" if r['f5'] is not None else "-"
        f15 = f"{r['f15']:+.2f}" if r['f15'] is not None else "-"
        f30 = f"{r['f30']:+.2f}" if r['f30'] is not None else "-"
        L.append(f"| {r['time']} | {r['lb']}x | {r['lr']}% | {r['ur']}% | {r['cp']}% | {r['vr']}x | {f5} | {f15} | {f30} |")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[报告] 已写出：{REPORT}")
    print(f"[汇总] 放量锤子 {len(sig_on)} 根；月份分布: " + ", ".join(f"{k}={by_month[k]}" for k in sorted(by_month)))


if __name__ == "__main__":
    main()
