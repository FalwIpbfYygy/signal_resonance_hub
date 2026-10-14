# -*- coding: utf-8 -*-
"""基于已生成的回测报告所做的「形态→交易计划」分析。

原则（严守用户要求）：
- 不重新泛泛找形态：复用 detector.is_hammer 同一套逻辑，从已落盘的缓存里
  重新提取「已发现锤子」的 OHLC，用于计算入场/止损/TP/盈亏比。形态集合与报告一致。
- 当前锚点价格由外部传入（实时行情），仅作行情信息，不参与形态判定。
- 报告中的后 5/15/30m% 是“事后观测”，本报告仅作历史倾向参考，明确标注非预测。
"""
from __future__ import annotations
import sys, os, json, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DEFAULTS
from detector import is_hammer

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache", "_5m_2026.json")
BJ = datetime.timezone(datetime.timedelta(hours=8))

CUR_PRICE = float(sys.argv[1]) if len(sys.argv) > 1 else 4186.61
N_RECENT = 15

def atr(klines, i, n=14):
    trs = []
    for j in range(max(1, i - n + 1), i + 1):
        h, l, pc = float(klines[j][2]), float(klines[j][3]), float(klines[j - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0

def session(hour):
    if 8 <= hour < 16:   return "亚盘(北京日中)"
    if 16 <= hour < 21:  return "欧盘"
    if 21 <= hour or hour < 5: return "美盘"
    return "亚盘早"

def trend_slope(klines, i, n=20):
    # 返回锤子前 n 根收盘的线性斜率符号与幅度
    if i < n:
        return 0.0
    p0 = float(klines[i - n][4]); p1 = float(klines[i][4])
    return (p1 - p0) / p0 * 100.0

def local_low_rank(klines, i, win=40):
    low = float(klines[i][3])
    lows = [float(klines[j][3]) for j in range(max(0, i - win), i)]
    lows = [x for x in lows if x > 0]
    if not lows:
        return 0
    below = sum(1 for x in lows if x > low)
    return below / len(lows)  # 越接近1，越低（越像落在支撑区）

def main():
    with open(CACHE, "r", encoding="utf-8") as f:
        klines = json.load(f)
    print(f"[数据] 缓存 {len(klines)} 根，末根 {datetime.datetime.fromtimestamp(klines[-1][0]/1000, BJ).strftime('%Y-%m-%d %H:%M')} (北京)")
    print(f"[当前价]  = {CUR_PRICE} (传入锚点)")

    p = dict(DEFAULTS)
    vols = [float(k[5]) for k in klines]
    sig = []
    for i, k in enumerate(klines):
        ok, m = is_hammer(k, p)
        if not ok:
            continue
        vr = None
        if i >= p["vol_ma_window"]:
            ma = sum(vols[i - p["vol_ma_window"] + 1:i + 1]) / p["vol_ma_window"]
            vr = (vols[i] / ma) if ma > 0 else None
        if vr is None or vr < p["vol_mult"]:
            continue
        sig.append((i, m, vr))
    print(f"[信号] 放量锤子共 {len(sig)} 根；取最近 {N_RECENT} 根做当前分析\n")

    recent = sig[-N_RECENT:]
    # 缓存末根时间，用于判断后续数据是否足够
    cache_end = klines[-1][0]

    hdr = f"{'时间(北京)':<17}{'O':>8}{'H':>8}{'L':>8}{'C':>8}{'量比':>7}{'会话':>12}{'前20根%':>9}{'低位分位':>9}{'ATR':>7}"
    print(hdr)
    print("-" * len(hdr))
    plans = []
    for (i, m, vr) in recent:
        o, h, l, c = float(klines[i][1]), float(klines[i][2]), float(klines[i][3]), float(klines[i][4])
        t = datetime.datetime.fromtimestamp(klines[i][0] / 1000, BJ)
        a = atr(klines, i)
        slp = trend_slope(klines, i)
        lrank = local_low_rank(klines, i)
        sess = session(t.hour)
        # 交易计划（多头，锤子=看涨反转）
        tick = 0.5
        entry = h + tick
        sl_buf = max(0.5, 0.3 * a)
        sl = l - sl_buf
        risk = entry - sl
        tp1 = entry + 1.0 * risk
        tp2 = entry + 2.0 * risk
        # 当前状态
        if CUR_PRICE >= entry:
            status = "已触发(多单活跃)"
        elif CUR_PRICE <= sl:
            status = "已止损(失效)"
        else:
            status = "等待突破确认"
        # 事后观测（仅用缓存内后续K线，明确标注非预测）
        def fwd_pct(bars):
            j = i + bars
            if j >= len(klines):
                return None
            return (float(klines[j][4]) / c - 1.0) * 100.0
        f5, f15, f30 = fwd_pct(1), fwd_pct(3), fwd_pct(6)
        # 事后是否触及 TP/SL
        sub = klines[i + 1: min(i + 200, len(klines))]
        reached_tp1 = any(float(x[2]) >= tp1 for x in sub)
        reached_tp2 = any(float(x[2]) >= tp2 for x in sub)
        hit_sl = any(float(x[3]) <= sl for x in sub)
        hindsight = "TP1√" if reached_tp1 else ("SL√" if hit_sl else "未触及")
        if reached_tp2:
            hindsight = "TP2√"
        conf = ("高" if (vr >= 2.5 and lrank >= 0.6 and slp < 0) else
                "中" if (vr >= 1.8 and lrank >= 0.4) else "低")
        print(f"{t.strftime('%Y-%m-%d %H:%M'):<17}{o:>8.1f}{h:>8.1f}{l:>8.1f}{c:>8.1f}{vr:>6.1f}x{sess:>12}{slp:>+8.1f}%{lrank:>8.0%}{a:>7.1f}")
        plans.append(dict(time=t.strftime('%Y-%m-%d %H:%M'), o=o, h=h, l=l, c=c, vr=vr, sess=sess,
                          slp=slp, lrank=lrank, atr=a, entry=entry, sl=sl, risk=risk, tp1=tp1, tp2=tp2,
                          status=status, hindsight=hindsight, conf=conf, f5=f5, f15=f15, f30=f30))
    print()
    print("=" * 100)
    print("交易计划（多头；锤子为看涨反转形态，本轮仅支持做多，做空不在已发现形态范畴）")
    print("=" * 100)
    ph = f"{'时间':<17}{'入场':>8}{'止损':>8}{'风险':>7}{'TP1':>8}{'TP2':>8}{'RR(TP2)':>9}{'状态':>14}{'事后':>8}{'置信':>5}"
    print(ph)
    print("-" * len(ph))
    for x in plans:
        rr = x['tp2'] - x['entry']
        print(f"{x['time']:<17}{x['entry']:>8.1f}{x['sl']:>8.1f}{x['risk']:>7.1f}{x['tp1']:>8.1f}{x['tp2']:>8.1f}{rr/x['risk']:>8.2f}x{x['status']:>14}{x['hindsight']:>8}{x['conf']:>5}")
    # 当前价相对最近一根锤子的位置
    if plans:
        last = plans[-1]
        print(f"\n[当前价 {CUR_PRICE}] 相对最近锤子 {last['time']}：入场={last['entry']:.1f} 止损={last['sl']:.1f} TP1={last['tp1']:.1f} TP2={last['tp2']:.1f} → 状态：{last['status']}")

if __name__ == "__main__":
    main()
