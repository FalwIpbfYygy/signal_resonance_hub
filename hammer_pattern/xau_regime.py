# -*- coding: utf-8 -*-
""" 四维市场状态量化分类系统 · 单文件实现（依据 PRD -REGIME-PRD-V1.0）

严格纪律：
- 零未来函数：所有指标在 bar i 只使用 [0..i] 的数据（不含未来）；分类、确认、迟滞均因果。
- 实时/监控走服务器零代理：Binance/飞书均直连（curl 不加 -x；requests trust_env=False）。
- 不执行任何下单，只输出分类/指标/策略建议/告警。

运行：
  python xau_regime.py --backtest data_cache/_5m_2026.json   # 历史分类+统计（默认 1h）
  python xau_regime.py --pnl     data_cache/_5m_2026.json   # 分状态 P&L（5m 自动重采样 1h）
  python xau_regime.py --pnl-mtf data_cache/_5m_2026.json   # MTF：1h 定 bias + 5m 触发确认
  python xau_regime.py --monitor                                     # 常驻：状态切换推飞书（默认 1h）
  python xau_regime.py --monitor --mtf                              # 常驻：1h 状态 + 5m 触发门控
  python xau_regime.py --once                                        # 单次分类最新状态

设计决策（2026-10-03）：
- 默认周期 1h。回测证明 5m 噪声过大（REGULAR PF 0.59 / CHOPPY 0.75 负期望），1h 全程正期望
  （SMOOTH/CHOPPY PF≈1.27），故分类/策略以 1h 为基准。
- 5m 仅作「入场触发确认层」：1h 定 regime/方向（bias），5m 同向确认才入场，过滤 1h 信号中
  5m 立即否决的噪声。见 pnl_backtest_mtf / --mtf。

依赖：仅标准库 + requests（仅 --monitor 用）。
"""
from __future__ import annotations

import sys
import os
import json
import math
import time
import signal
import subprocess
import datetime
import argparse
import collections

import requests  # 仅 --monitor 飞书告警使用；其余逻辑零第三方依赖

# ============================== 参数默认值（PRD §5.4） ==============================
ADX_PERIOD = 14
ADX_TREND = 25          # 趋势阈值
ADX_WEAK = 20           # 弱趋势阈值
ER_PERIOD = 20
ER_SMOOTH = 0.55        # 流畅阈值
ER_RANGE = 0.30         # 震荡阈值
ATR_PERIOD = 14
ATR_LONG = 100          # ATR 长期窗口
ATR_LOW = 0.85
ATR_HIGH = 1.25         # 规则震荡用
SMOOTH_ATR_HIGH = 1.50  # 单边流畅用（PRD §5.2 表：0.85~1.5）
CHOP_PERIOD = 14
CHOP_RANGE = 55         # 震荡阈值
CHOP_SMOOTH = 38        # 单边流畅的 CHOP 上限
BB_PERIOD = 20
BB_PCT_WIN = 100        # BBW 百分位窗口
BBW_STAB_WIN = 20       # BBW 稳定性窗口
BBW_STAB_TH = 0.15      # 稳定性阈值
CONFIRM_BARS = 3        # 连续确认根数
MIN_TREND_DUR = 5       # 最小趋势持续根数（迟滞）

SYMBOL = ""
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
POLL_SECONDS = 10
WARMUP = max(ATR_LONG, BB_PCT_WIN) - 1  # 指标满窗口所需最小索引 = 99

BASE = os.path.dirname(os.path.abspath(__file__))
BJ = datetime.timezone(datetime.timedelta(hours=8))

REGIMES = ["SMOOTH_TREND", "CHOPPY_TREND", "REGULAR_RANGE", "IRREGULAR_RANGE", "TRANSITIONAL"]
TRENDS = {"SMOOTH_TREND", "CHOPPY_TREND"}


# ============================== 数据加载 ==============================
def load_bars(path):
    """加载 K 线为 list[ [ts, o, h, l, c, v, ...] ]。支持 Binance klines(JSON list) 与 CSV。"""
    if path.lower().endswith(".csv"):
        bars = []
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        header = lines[0].lower()
        idx = {}
        for col in ("open", "high", "low", "close", "volume", "timestamp", "time", "open_time"):
            for j, h in enumerate(header.split(",")):
                if h.strip() == col:
                    idx[col] = j
        for ln in lines[1:]:
            p = ln.split(",")
            o = float(p[idx.get("open", 1)]); h = float(p[idx.get("high", 2)])
            l = float(p[idx.get("low", 3)]); c = float(p[idx.get("close", 4)])
            v = float(p[idx.get("volume", 5)]) if "volume" in idx else 0.0
            ts = int(float(p[idx.get("timestamp", idx.get("time", idx.get("open_time", 0)))]))
            bars.append([ts, o, h, l, c, v])
        return bars
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Binance klines: [open_time, open, high, low, close, volume, ...]
    bars = [[float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in data]
    return bars


def resample_to_1h(m5_bars):
    """把 5m K 线聚合为 1h（floor 到小时）。返回 list[ [ts, o, h, l, c, v] ]，均为 float。"""
    HOUR = 3600000
    buckets = {}
    order = []
    for b in m5_bars:
        hk = (int(b[0]) // HOUR) * HOUR
        if hk not in buckets:
            buckets[hk] = [float(hk), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])]
            order.append(hk)
        else:
            bb = buckets[hk]
            bb[2] = max(bb[2], float(b[2])); bb[3] = min(bb[3], float(b[3]))
            bb[4] = float(b[4]); bb[5] += float(b[5])
    return [buckets[k] for k in order]


# ============================== 指标引擎（因果，纯 Python O(n)） ==============================
def compute_indicators(bars):
    """返回每根 bar 的指标 dict（未满窗口为 None）。全部只用 [0..i]，零未来函数。"""
    n = len(bars)
    O = [b[1] for b in bars]; H = [b[2] for b in bars]
    L = [b[3] for b in bars]; C = [b[4] for b in bars]

    TR = [0.0] * n
    for i in range(n):
        if i == 0:
            TR[i] = H[i] - L[i]
        else:
            TR[i] = max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))

    # Wilder ATR(14)
    atr = [None] * n
    if n >= ADX_PERIOD:
        atr[ADX_PERIOD - 1] = sum(TR[:ADX_PERIOD]) / ADX_PERIOD
        for i in range(ADX_PERIOD, n):
            atr[i] = (atr[i - 1] * (ADX_PERIOD - 1) + TR[i]) / ADX_PERIOD

    # +DM / -DM + Wilder 平滑
    pdm = [0.0] * n; mdm = [0.0] * n
    for i in range(1, n):
        up = H[i] - H[i - 1]; dn = L[i - 1] - L[i]
        pdm[i] = max(up, 0.0) if up > dn else 0.0
        mdm[i] = max(dn, 0.0) if dn > up else 0.0
    pdm14 = [None] * n; mdm14 = [None] * n; tr14 = [None] * n
    if n >= ADX_PERIOD:
        pdm14[ADX_PERIOD - 1] = sum(pdm[1:ADX_PERIOD]) / ADX_PERIOD
        mdm14[ADX_PERIOD - 1] = sum(mdm[1:ADX_PERIOD]) / ADX_PERIOD
        tr14[ADX_PERIOD - 1] = atr[ADX_PERIOD - 1]
        for i in range(ADX_PERIOD, n):
            pdm14[i] = (pdm14[i - 1] * (ADX_PERIOD - 1) + pdm[i]) / ADX_PERIOD
            mdm14[i] = (mdm14[i - 1] * (ADX_PERIOD - 1) + mdm[i]) / ADX_PERIOD
            tr14[i] = (tr14[i - 1] * (ADX_PERIOD - 1) + TR[i]) / ADX_PERIOD

    pdi = [None] * n; mdi = [None] * n; dx = [None] * n; adx = [None] * n
    for i in range(n):
        if tr14[i] and tr14[i] > 0:
            pdi[i] = 100 * pdm14[i] / tr14[i]
            mdi[i] = 100 * mdm14[i] / tr14[i]
            if (pdi[i] + mdi[i]) > 0:
                dx[i] = 100 * abs(pdi[i] - mdi[i]) / (pdi[i] + mdi[i])
    # ADX = Wilder 平滑 DX，初始为前 14 个 DX 的均值
    s = ADX_PERIOD - 1
    if n > s + ADX_PERIOD - 1:
        first = s + ADX_PERIOD - 1
        valid = [dx[j] for j in range(s, first + 1) if dx[j] is not None]
        if valid:
            adx[first] = sum(valid) / len(valid)
            for i in range(first + 1, n):
                if dx[i] is None:
                    adx[i] = None
                else:
                    adx[i] = (adx[i - 1] * (ADX_PERIOD - 1) + dx[i]) / ADX_PERIOD

    # ER(20)
    er = [0.0] * n
    run = 0.0
    for i in range(1, n):
        run += abs(C[i] - C[i - 1])
        if i >= ER_PERIOD:
            run -= abs(C[i - ER_PERIOD] - C[i - ER_PERIOD - 1])
            den = run
            er[i] = abs(C[i] - C[i - ER_PERIOD]) / den if den > 0 else 0.0

    # BBW(20)
    bbw = [None] * n
    for i in range(BB_PERIOD - 1, n):
        w = C[i - BB_PERIOD + 1:i + 1]
        m = sum(w) / BB_PERIOD
        var = sum((x - m) ** 2 for x in w) / BB_PERIOD
        sd = math.sqrt(var)
        if m > 0:
            bbw[i] = ((m + 2 * sd) - (m - 2 * sd)) / m * 100
        else:
            bbw[i] = 0.0

    # ATR Ratio = ATR(14) / MA(ATR(14), 100)
    atrp = [None] * n
    for i in range(ATR_LONG - 1, n):
        w = atr[i - ATR_LONG + 1:i + 1]
        if None in w:
            continue
        mean = sum(w) / ATR_LONG
        atrp[i] = (atr[i] / mean) if mean > 0 else 1.0

    # CHOP(14)
    chop = [None] * n
    for i in range(CHOP_PERIOD - 1, n):
        w = TR[i - CHOP_PERIOD + 1:i + 1]
        atr_sum = sum(w)
        hh = max(H[i - CHOP_PERIOD + 1:i + 1]); ll = min(L[i - CHOP_PERIOD + 1:i + 1])
        if (hh - ll) > 0:
            chop[i] = 100 * math.log10(atr_sum / (hh - ll)) / math.log10(CHOP_PERIOD)
        else:
            chop[i] = 100.0

    # BBW 百分位（因果：只用 [0..i-1] 的过去窗口）
    bbwpct = [None] * n
    for i in range(n):
        if bbw[i] is None:
            continue
        lo = max(0, i - BB_PCT_WIN)
        ref = [bbw[j] for j in range(lo, i) if bbw[j] is not None]
        if not ref:
            bbwpct[i] = 0.5
            continue
        cnt = sum(1 for x in ref if x <= bbw[i])
        bbwpct[i] = cnt / len(ref)

    # BBW 稳定性 = BBW_Pct 在近 BBW_STAB_WIN 根的标准差
    bbwstab = [None] * n
    for i in range(n):
        if bbwpct[i] is None:
            continue
        lo = max(0, i - BBW_STAB_WIN + 1)
        ref = [bbwpct[j] for j in range(lo, i + 1) if bbwpct[j] is not None]
        if len(ref) < 2:
            bbwstab[i] = 0.0
            continue
        m = sum(ref) / len(ref)
        bbwstab[i] = math.sqrt(sum((x - m) ** 2 for x in ref) / len(ref))

    out = []
    for i in range(n):
        out.append({
            "adx": adx[i], "pdi": pdi[i], "mdi": mdi[i],
            "er": er[i], "atr": atr[i], "atr_ratio": atrp[i], "chop": chop[i],
            "bbw": bbw[i], "bbw_pct": bbwpct[i], "bbw_stability": bbwstab[i],
        })
    return out


# ============================== 原始分类 + 方向 + 置信度 ==============================
def clamp01(x):
    return max(0.0, min(1.0, x))


def classify_raw(m):
    adx, er, atr_r, chp, stab = m["adx"], m["er"], m["atr_ratio"], m["chop"], m["bbw_stability"]
    pdi, mdi = m["pdi"], m["mdi"]
    if None in (adx, er, atr_r, chp, stab):
        return "TRANSITIONAL", "NEUTRAL", 0.0
    direction = "NEUTRAL"
    if pdi is not None and mdi is not None:
        if pdi > mdi + 5:
            direction = "BULL"
        elif mdi > pdi + 5:
            direction = "BEAR"

    if adx >= ADX_TREND and er >= ER_SMOOTH and (ATR_LOW <= atr_r <= SMOOTH_ATR_HIGH) and chp < CHOP_SMOOTH and direction != "NEUTRAL":
        return "SMOOTH_TREND", direction, clamp01(min((adx - ADX_TREND) / ADX_TREND, (er - ER_SMOOTH) / 0.45, (CHOP_SMOOTH - chp) / CHOP_SMOOTH, (atr_r - ATR_LOW) / (SMOOTH_ATR_HIGH - ATR_LOW)))
    if adx >= ADX_WEAK and ER_RANGE <= er < ER_SMOOTH and direction != "NEUTRAL":
        return "CHOPPY_TREND", direction, clamp01(min((adx - ADX_WEAK) / ADX_WEAK, (er - ER_RANGE) / (ER_SMOOTH - ER_RANGE)))
    if adx < ADX_WEAK and er < ER_RANGE:
        if ATR_LOW <= atr_r <= ATR_HIGH and chp > CHOP_RANGE and stab < BBW_STAB_TH:
            return "REGULAR_RANGE", "NEUTRAL", clamp01(min((CHOP_RANGE - chp) / CHOP_RANGE, (BBW_STAB_TH - stab) / BBW_STAB_TH, (atr_r - ATR_LOW) / (ATR_HIGH - ATR_LOW)))
        return "IRREGULAR_RANGE", "NEUTRAL", clamp01(min((CHOP_RANGE - chp) / CHOP_RANGE if chp <= CHOP_RANGE else 0.3, stab / BBW_STAB_TH if stab >= BBW_STAB_TH else 0.5, (1.0 - abs(atr_r - 1.0)) ))
    return "TRANSITIONAL", "NEUTRAL", 0.2


# ============================== 连续确认 + 迟滞 ==============================
class RegimeState:
    def __init__(self):
        self.confirmed = "TRANSITIONAL"
        self.age = 0
        self.candidate = None
        self.cnt = 0

    def update(self, raw_regime):
        if raw_regime == self.confirmed:
            self.age += 1
            self.candidate = None
            self.cnt = 0
            return self.confirmed
        if raw_regime == self.candidate:
            self.cnt += 1
        else:
            self.candidate = raw_regime
            self.cnt = 1
        # 切换条件：连续 CONFIRM_BARS 根一致，且（当前非趋势 或 已持续≥MIN_TREND_DUR 根）
        hysteresis_ok = (self.confirmed not in TRENDS) or (self.age >= MIN_TREND_DUR)
        if self.cnt >= CONFIRM_BARS and hysteresis_ok:
            self.confirmed = raw_regime
            self.age = 0
            self.candidate = None
            self.cnt = 0
        return self.confirmed


# ============================== 策略映射（PRD §4.4） ==============================
def strategy_for(regime, direction):
    if regime == "SMOOTH_TREND":
        return {"action": "TREND_FOLLOW", "entry": "回调至EMA20或38.2%",
                "stop_loss": "结构低点外+0.3ATR", "take_profit": "移动止损", "position_size": "标准"}
    if regime == "CHOPPY_TREND":
        return {"action": "TREND_LIGHT", "entry": "轻仓分批，等回踩确认",
                "stop_loss": "宽止损", "take_profit": "分段止盈", "position_size": "半仓"}
    if regime == "REGULAR_RANGE":
        return {"action": "MEAN_REVERT", "entry": "边界高抛低吸",
                "stop_loss": "区间外止损", "take_profit": "对侧边界", "position_size": "标准"}
    if regime == "IRREGULAR_RANGE":
        return {"action": "STAND_ASIDE", "entry": "观望或极轻仓",
                "stop_loss": "—", "take_profit": "—", "position_size": "极轻/空"}
    return {"action": "NO_TRADE", "entry": "—", "stop_loss": "—", "take_profit": "—", "position_size": "空"}


# ============================== 串联：整段分类 ==============================
def classify_series(bars, interval="5m"):
    metrics = compute_indicators(bars)
    n = len(bars)
    st = RegimeState()
    results = []
    for i in range(n):
        raw_regime, raw_dir, _ = classify_raw(metrics[i]) if i >= WARMUP else ("TRANSITIONAL", "NEUTRAL", 0.0)
        confirmed = st.update(raw_regime)
        # 置信度用确认态对应的指标（取当前 bar）
        reg_for_conf = confirmed if confirmed != "TRANSITIONAL" else raw_regime
        _, _, conf = classify_raw(metrics[i]) if i >= WARMUP else ("TRANSITIONAL", "NEUTRAL", 0.0)
        # 方向：优先用当前 bar 的方向；若确认态为非中性但当前为 NEUTRAL，沿用确认方向
        direction = raw_dir
        if direction == "NEUTRAL" and confirmed in TRENDS:
            direction = st._last_dir if hasattr(st, "_last_dir") else "NEUTRAL"
        # 记录最近趋势方向
        if raw_dir != "NEUTRAL":
            st._last_dir = raw_dir
        m = metrics[i]
        results.append({
            "symbol": SYMBOL, "timeframe": interval,
            "timestamp": int(bars[i][0]),
            "regime": confirmed, "direction": direction,
            "confidence": round(conf, 3),
            "metrics": {
                "adx": round(m["adx"], 2) if m["adx"] is not None else None,
                "er": round(m["er"], 3) if m["er"] is not None else None,
                "atr_ratio": round(m["atr_ratio"], 3) if m["atr_ratio"] is not None else None,
                "chop": round(m["chop"], 2) if m["chop"] is not None else None,
                "bbw_pct": round(m["bbw_pct"], 3) if m["bbw_pct"] is not None else None,
                "bbw_stability": round(m["bbw_stability"], 3) if m["bbw_stability"] is not None else None,
                "di_plus": round(m["pdi"], 2) if m["pdi"] is not None else None,
                "di_minus": round(m["mdi"], 2) if m["mdi"] is not None else None,
            },
            "strategy": strategy_for(confirmed, direction),
        })
    return results


# ============================== 回测统计 ==============================
def backtest_report(bars, interval="5m"):
    t0 = time.time()
    results = classify_series(bars, interval)
    elapsed = time.time() - t0

    counts = {r: 0 for r in REGIMES}
    durations = {r: [] for r in REGIMES}
    cur = None; cur_len = 0
    samples = {}
    for r in results:
        counts[r["regime"]] += 1
        if r["regime"] == cur:
            cur_len += 1
        else:
            if cur is not None:
                durations[cur].append(cur_len)
            cur = r["regime"]; cur_len = 1
            if cur not in samples and r["confidence"] > 0.3:
                samples[cur] = r
    if cur is not None:
        durations[cur].append(cur_len)

    print(f"===  四维市场状态回测统计（{interval}, {len(bars)} 根）===")
    print(f"计算耗时: {elapsed:.2f}s | 单根均值: {elapsed/len(bars)*1000:.3f}ms")
    print("\n[状态分布]")
    for r in REGIMES:
        c = counts[r]
        avg = (sum(durations[r]) / len(durations[r])) if durations[r] else 0
        mx = max(durations[r]) if durations[r] else 0
        print(f"  {r:<16} 占比 {c/len(bars)*100:5.1f}% ({c:6d}根) | 平均持续 {avg:5.1f}根 | 最长 {mx}根")
    print("\n[样本输出（每个状态取首个置信度>0.3 的）]")
    for r, s in samples.items():
        print(f"\n--- {r} @ {datetime.datetime.fromtimestamp(s['timestamp']/1000, BJ).strftime('%Y-%m-%d %H:%M')} ---")
        print(json.dumps(s, ensure_ascii=False, indent=2))
    return results


# ============================== 实时监控 + 飞书切换告警（零代理） ==============================
def send_feishu_card(webhook, card, timeout=30):
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.post(webhook, json={"msg_type": "interactive", "card": card}, timeout=timeout)
    except Exception as e:
        return False, f"ERR:{e}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        body = resp.json()
    except Exception:
        return False, "非JSON"
    if not isinstance(body, dict) or body.get("code") != 0:
        return False, f"业务码 {body.get('code')}"
    return True, "ok"


def fetch_klines(symbol, interval, limit, retries=3):
    url = f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}?symbol={symbol}&interval={interval}&limit={limit}"
    for _ in range(retries):
        try:
            p = subprocess.run(["curl", "-s", "-m", "30", url], capture_output=True, text=True, timeout=40)
            if p.returncode == 0:
                d = json.loads(p.stdout)
                if isinstance(d, list) and d:
                    return [[float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in d]
        except Exception:
            pass
        time.sleep(1)
    return None


def build_alert_card(res):
    ts = datetime.datetime.fromtimestamp(res["timestamp"] / 1000, BJ)
    m = res["metrics"]
    color = {"SMOOTH_TREND": "green", "CHOPPY_TREND": "blue", "REGULAR_RANGE": "grey",
             "IRREGULAR_RANGE": "red", "TRANSITIONAL": "orange"}[res["regime"]]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text",
                    "content": f"📊 市场状态切换 · {res['regime']} ({res['direction']})"}},
        "elements": [
            {"tag": "markdown", "content": "\n".join([
                f"**品种/周期**  {res['symbol']} {res['timeframe']}",
                f"**时间(北京)**  {ts.strftime('%Y-%m-%d %H:%M')}",
                f"**置信度**  {res['confidence']:.2f}",
            ])},
            {"tag": "hr"},
            {"tag": "markdown", "content": "\n".join([
                f"**ADX**  {m['adx']} ｜ **ER**  {m['er']} ｜ **ATR_Ratio**  {m['atr_ratio']}",
                f"**CHOP**  {m['chop']} ｜ **BBW_Pct**  {m['bbw_pct']} ｜ **BBW_Stab**  {m['bbw_stability']}",
                f"**+DI/-DI**  {m['di_plus']} / {m['di_minus']}",
            ])},
            {"tag": "hr"},
            {"tag": "markdown", "content": "\n".join([
                f"**策略动作**  {res['strategy']['action']}",
                f"**入场**  {res['strategy']['entry']}",
                f"**止损**  {res['strategy']['stop_loss']}",
                f"**仓位**  {res['strategy']['position_size']}",
            ])},
            {"tag": "note", "elements": [{"tag": "plain_text",
                "content": "⚠️ 量化研究/监控用途，不构成投资建议，不自动下单。"}]},
        ],
    }


def run_live(interval="1h", mtf=False):
    mode = "MTF(1h定bias+5m触发)" if mtf else interval
    print(f"[live] 启动市场监管（{mode}），每 {POLL_SECONDS}s 一次")
    last_pushed = None
    stop = {"v": False}

    def _sig(*_):
        stop["v"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    while not stop["v"]:
        bars = fetch_klines(SYMBOL, interval, WARMUP + 60)
        if not bars:
            print("[live] 取数失败，稍后重试")
            time.sleep(POLL_SECONDS)
            continue
        res = classify_series(bars, interval)[-1]
        m5 = fetch_klines(SYMBOL, "5m", WARMUP + 60) if mtf else None
        tradeable = res["regime"] in (TRENDS | {"REGULAR_RANGE"})
        confirmed = True
        if mtf and tradeable and m5 is not None:
            d = 1 if res["direction"] == "BULL" else (-1 if res["direction"] == "BEAR" else 0)
            win = list(range(max(0, len(m5) - 12), len(m5)))
            confirmed = _m5_confirm(m5, bars[-1], win, d, res["regime"]) is not None
        key = (res["regime"], res["direction"])
        should_push = (key != last_pushed)
        if mtf and tradeable and not confirmed:
            should_push = False
            print(f"[live][mtf] {res['regime']}/{res['direction']} 1h可交易但5m未确认，armed等待")
        if should_push:
            card = build_alert_card(res)
            ok, detail = send_feishu_card(FEISHU_WEBHOOK, card)
            tag = "[mtf-确认]" if (mtf and tradeable and confirmed) else ""
            print(f"[live]{tag} 状态={res['regime']}/{res['direction']} 置信度={res['confidence']:.2f} "
                  f"飞书推送={'ok' if ok else 'fail('+detail+')'}")
            last_pushed = key
        for _ in range(POLL_SECONDS):
            if stop["v"]:
                break
            time.sleep(1)
    print("[live] 已停止")


# ============================== 分状态 P&L 回测（PRD §9.2 验收核心） ==============================
def pnl_backtest(bars, interval="5m"):
    """分状态回测：趋势态(流畅/不流畅)做方向性 ATR 止损+2R 止盈；规则震荡做均值回归(1R)；
    不规则/过渡按纪律观望(0 交易)。另加对照：把规则震荡的均值回归策略套到不规则震荡上，证明后者该观望。
    严格因果：信号在 bar i 收盘后产生，仅用 atr[i] 定止损，之后用真实价格序列判定触发（标准回测，非未来函数）。"""
    t0 = time.time()
    metrics = compute_indicators(bars)
    results = classify_series(bars, interval)
    n = len(bars)

    def opens(open_as, i):
        res = results[i]
        if res["confidence"] < 0.30:
            return None
        atr = metrics[i]["atr"]
        if atr is None or atr <= 0:
            return None
        entry = bars[i][4]
        R = max(0.5, 1.0 * atr)
        if open_as in ("SMOOTH_TREND", "CHOPPY_TREND"):
            d = 1 if res["direction"] == "BULL" else (-1 if res["direction"] == "BEAR" else 0)
            if d == 0:
                return None
            tpR, maxhold = 2.0, 20
        elif open_as == "REGULAR_RANGE":
            d = -1 if bars[i][4] > bars[i][1] else 1   # 均值回归：阳线看跌/阴线看涨
            tpR, maxhold = 1.0, 10
        else:
            return None
        return {"entry": entry, "sl": entry - d * R, "tp": entry + d * tpR * R,
                "d": d, "R": R, "tpR": tpR, "i0": i, "maxhold": maxhold}

    def resolve_exit(op, i, bar):
        hi, lo, cl = bar[2], bar[3], bar[4]
        if op["d"] > 0:   # 多
            if lo <= op["sl"]:
                return -1.0
            if hi >= op["tp"]:
                return op["tpR"]
            if (i - op["i0"]) >= op["maxhold"]:
                return (cl - op["entry"]) / op["R"]
        else:             # 空
            if hi >= op["sl"]:
                return -1.0
            if lo <= op["tp"]:
                return op["tpR"]
            if (i - op["i0"]) >= op["maxhold"]:
                return (op["entry"] - cl) / op["R"]
        return None

    def run(open_as, entry_pred):
        bk = {"trades": 0, "wins": 0, "rwins": 0.0, "rloss": 0.0, "rsum": 0.0, "eq": [0.0]}
        op = None
        for i in range(WARMUP, n):
            if op is not None:
                ex = resolve_exit(op, i, bars[i])
                if ex is not None:
                    bk["trades"] += 1
                    bk["rsum"] += ex
                    if ex > 0:
                        bk["wins"] += 1
                        bk["rwins"] += ex
                    else:
                        bk["rloss"] += -ex
                    bk["eq"].append(bk["eq"][-1] + ex)
                    op = None
            if op is None and entry_pred(i):
                opn = opens(open_as, i)
                if opn:
                    op = opn
        if op is not None:   # 残仓按末价平
            cl = bars[n - 1][4]
            ex = (cl - op["entry"]) / op["R"] if op["d"] > 0 else (op["entry"] - cl) / op["R"]
            bk["trades"] += 1; bk["rsum"] += ex
            if ex > 0:
                bk["wins"] += 1; bk["rwins"] += ex
            else:
                bk["rloss"] += -ex
            bk["eq"].append(bk["eq"][-1] + ex)
        return bk

    def report(tag, bk):
        t = bk["trades"]
        if t == 0:
            print(f"  {tag:<26} 交易 {0:>6}  胜率   -    avgR   -    PF   -   最大回撤   -")
            return
        wr = bk["wins"] / t
        avgR = bk["rsum"] / t
        pf = bk["rwins"] / bk["rloss"] if bk["rloss"] > 0 else float("inf")
        peak = 0.0; mdd = 0.0
        for e in bk["eq"]:
            peak = max(peak, e); mdd = min(mdd, e - peak)
        print(f"  {tag:<26} 交易 {t:>6}  胜率 {wr*100:>5.1f}%  avgR {avgR:>6.3f}  PF {pf:>6.2f}  最大回撤 {mdd:>6.2f}")

    print(f"=== 分状态 P&L 回测（{interval}, {n}根, {time.time()-t0:.2f}s）===")
    print("[主回测：各状态采用 PRD 对应策略]")
    report("SMOOTH_TREND(趋势跟随)", run("SMOOTH_TREND", lambda i: results[i]["regime"] == "SMOOTH_TREND"))
    report("CHOPPY_TREND(轻仓趋势)", run("CHOPPY_TREND", lambda i: results[i]["regime"] == "CHOPPY_TREND"))
    report("REGULAR_RANGE(均值回归)", run("REGULAR_RANGE", lambda i: results[i]["regime"] == "REGULAR_RANGE"))
    report("IRREGULAR_RANGE(观望)", run("REGULAR_RANGE", lambda i: False))   # 纪律：不交易
    report("TRANSITIONAL(不交易)", run("REGULAR_RANGE", lambda i: False))
    print("\n[对照：把规则震荡的均值回归策略，硬套到不规则震荡上]")
    report("→IRREGULAR上跑均值回归", run("REGULAR_RANGE", lambda i: results[i]["regime"] == "IRREGULAR_RANGE"))


# ============================== MTF 回测：1h 定 bias + 5m 触发确认层 ==============================
HOUR_MS = 3600000


def _m5_window(m5_bars, h1_open_time):
    """返回下一整小时内的 5m 根索引列表（严格因果：只用 1h bar 收盘之后的 5m）。"""
    lo = int(h1_open_time) + HOUR_MS
    hi = lo + HOUR_MS
    out = []
    for j, b in enumerate(m5_bars):
        t = int(b[0])
        if t >= hi:
            break
        if t >= lo:
            out.append(j)
    return out


def _m5_confirm(m5_bars, h1_bar, win, direction, regime_type):
    """5m 确认层（过滤型），返回触发/确认根索引或 None（5m 否决该 1h 信号）。
    TREND：下一小时 5m 未反向击穿 1h 实体极值 → 结构完好，确认（返回窗口首根作占位）。
    RANGE：下一小时 5m 触及 1h 实体极值后回转 → 均值回归入场点（返回该根）。
    这是「5m 仅作入场触发确认层」：过滤掉 1h 信号发出后 5m 立即反向否决的噪声。"""
    if not win:
        return None
    o1, h1, l1, c1 = h1_bar[1], h1_bar[2], h1_bar[3], h1_bar[4]
    body_lo, body_hi = min(o1, c1), max(o1, c1)
    if regime_type in TRENDS:
        lo_min = min(m5_bars[j][3] for j in win)
        hi_max = max(m5_bars[j][2] for j in win)
        if direction > 0 and lo_min >= body_lo:
            return win[0]
        if direction < 0 and hi_max <= body_hi:
            return win[0]
        return None
    else:  # REGULAR_RANGE 均值回归
        for j in win:
            if direction > 0 and m5_bars[j][3] < body_lo and m5_bars[j][4] > m5_bars[j][1]:
                return j
            if direction < 0 and m5_bars[j][2] > body_hi and m5_bars[j][4] < m5_bars[j][1]:
                return j
        return None


def pnl_backtest_mtf(h1_bars, m5_bars, interval_h1="1h"):
    """MTF 回测：1h 定 regime/方向(bias) 并用 1h atr 定 R；5m 作确认/触发层（过滤型）。
    - TREND：1h 收盘入场，但仅当下一小时 5m 结构未破（未反向击穿 1h 实体极值）才执行；5m 否决则跳过该 1h 信号。
    - RANGE：1h 给出均值回归方向，5m 触及 1h 实体极值后回转的具体那根作为入场。
    出场统一用 5m 真实价格判定（标准回测，零未来函数）。
    对照纯 1h（pnl_backtest）：验证「5m 确认层」作为过滤器能否提升实盘可用性。"""
    t0 = time.time()
    h1_metrics = compute_indicators(h1_bars)
    h1_res = classify_series(h1_bars, interval_h1)
    n1, n5 = len(h1_bars), len(m5_bars)

    def opens_params(res, k):
        if res["confidence"] < 0.30:
            return None
        atr = h1_metrics[k]["atr"]
        if atr is None or atr <= 0:
            return None
        if res["regime"] in TRENDS:
            d = 1 if res["direction"] == "BULL" else (-1 if res["direction"] == "BEAR" else 0)
            if d == 0:
                return None
            tpR, maxhold_1h = 2.0, 20
        elif res["regime"] == "REGULAR_RANGE":
            d = -1 if h1_bars[k][4] > h1_bars[k][1] else 1
            tpR, maxhold_1h = 1.0, 10
        else:
            return None
        return d, tpR, maxhold_1h, atr

    bk = {"trades": 0, "wins": 0, "rwins": 0.0, "rloss": 0.0, "rsum": 0.0, "eq": [0.0]}
    for k in range(WARMUP, n1):
        res = h1_res[k]
        if res["regime"] not in ("SMOOTH_TREND", "CHOPPY_TREND", "REGULAR_RANGE"):
            continue
        p = opens_params(res, k)
        if not p:
            continue
        d, tpR, maxhold_1h, atr = p
        win = _m5_window(m5_bars, h1_bars[k][0])
        tj = _m5_confirm(m5_bars, h1_bars[k], win, d, res["regime"])
        if tj is None:
            continue  # 5m 否决 -> 跳过该 1h 信号
        # 入场价：TREND 用 1h 收盘；RANGE 用 5m 触发根收盘
        if res["regime"] in TRENDS:
            entry = h1_bars[k][4]
            start_j = win[0]
        else:
            entry = m5_bars[tj][4]
            start_j = tj + 1
        R = max(0.5, 1.0 * atr)
        sl, tp = entry - d * R, entry + d * tpR * R
        maxhold_5m = maxhold_1h * 12
        ex = None
        for j in range(start_j, n5):
            hi, lo, cl = m5_bars[j][2], m5_bars[j][3], m5_bars[j][4]
            if d > 0:
                if lo <= sl: ex = -1.0; break
                if hi >= tp: ex = tpR; break
                if (j - start_j) >= maxhold_5m: ex = (cl - entry) / R; break
            else:
                if hi >= sl: ex = -1.0; break
                if lo <= tp: ex = tpR; break
                if (j - start_j) >= maxhold_5m: ex = (entry - cl) / R; break
        if ex is None:
            cl = m5_bars[n5 - 1][4]
            ex = (cl - entry) / R if d > 0 else (entry - cl) / R
        bk["trades"] += 1; bk["rsum"] += ex
        if ex > 0:
            bk["wins"] += 1; bk["rwins"] += ex
        else:
            bk["rloss"] += -ex
        bk["eq"].append(bk["eq"][-1] + ex)

    t = bk["trades"]
    print(f"=== MTF 回测（1h定bias + 5m确认层, {n1}根1h / {n5}根5m, {time.time()-t0:.2f}s）===")
    if t == 0:
        print("  无交易（5m 确认层过滤掉全部 1h 信号）")
        return
    wr = bk["wins"] / t; avgR = bk["rsum"] / t
    pf = bk["rwins"] / bk["rloss"] if bk["rloss"] > 0 else float("inf")
    peak = 0.0; mdd = 0.0
    for e in bk["eq"]:
        peak = max(peak, e); mdd = min(mdd, e - peak)
    print(f"  MTF(1h+5m)  交易 {t:>6}  胜率 {wr*100:>5.1f}%  avgR {avgR:>6.3f}  PF {pf:>6.2f}  最大回撤 {mdd:>6.2f}")


# ============================== CLI ==============================
def main():
    ap = argparse.ArgumentParser(description=" 四维市场状态分类（PRD V1.0）")
    ap.add_argument("--backtest", metavar="PATH", help="历史数据路径(json klines / csv)，做整段分类统计")
    ap.add_argument("--pnl", metavar="PATH", help="历史数据路径，做分状态 P&L 回测（PRD §9.2）。默认 1h（自动重采样）")
    ap.add_argument("--pnl-mtf", metavar="PATH", help="5m 数据路径，做 MTF 回测（1h定bias + 5m触发确认）")
    ap.add_argument("--monitor", action="store_true", help="常驻实时监控+飞书切换告警")
    ap.add_argument("--once", action="store_true", help="单次分类最新状态")
    ap.add_argument("--mtf", action="store_true", help="MTF 模式：1h 定状态 + 5m 触发确认门控（配合 --monitor/--once）")
    ap.add_argument("--interval", default="1h", help="周期，默认 1h（5m 噪声过大，见文件头设计决策）")
    args = ap.parse_args()

    if args.backtest:
        bars = load_bars(args.backtest)
        if args.interval == "1h":
            bars = resample_to_1h(bars)
        print(f"[load] {args.backtest} -> {len(bars)} 根 ({args.interval})")
        backtest_report(bars, args.interval)
    elif args.pnl:
        bars = load_bars(args.pnl)
        if args.interval == "1h":
            bars = resample_to_1h(bars)
        print(f"[load] {args.pnl} -> {len(bars)} 根 ({args.interval})")
        pnl_backtest(bars, args.interval)
    elif args.pnl_mtf:
        m5 = load_bars(args.pnl_mtf)
        h1 = resample_to_1h(m5)
        print(f"[load] {args.pnl_mtf} -> 5m {len(m5)} 根｜重采样 1h {len(h1)} 根")
        pnl_backtest_mtf(h1, m5)
    elif args.monitor:
        run_live(args.interval, mtf=args.mtf)
    elif args.once:
        bars = fetch_klines(SYMBOL, args.interval, WARMUP + 60)
        if not bars:
            print("[once] 取数失败")
            return
        res = classify_series(bars, args.interval)[-1]
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if args.mtf:
            m5 = fetch_klines(SYMBOL, "5m", WARMUP + 60)
            if m5:
                win = _m5_window(m5, bars[-1][0])
                d = 1 if res["direction"] == "BULL" else (-1 if res["direction"] == "BEAR" else 0)
                tj = _m5_confirm(m5, bars[-1], win, d, res["regime"]) if res["regime"] in TRENDS | {"REGULAR_RANGE"} else None
                print(f"[mtf] 5m 触发确认: {'已确认@' + str(m5[tj][0]) if tj is not None else '未确认（本小时 5m 暂未同向确认，观望）'}")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
