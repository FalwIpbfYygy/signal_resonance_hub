# -*- coding: utf-8 -*-
"""锤子 / 倒锤子（多头 / 空头）形态实盘监测 · 飞书卡片推送 · 单文件版。

把检测器、飞书推送、多周期守护、去重全部内联到一个文件，开箱即用：
  python live_hammer_all_in_one.py            # 常驻守护，每 10s 扫 5m/15m/30m/1h/4h
  python live_hammer_all_in_one.py --once    # 单次扫描（联调）
  python live_hammer_all_in_one.py --test-push  # 发一张自测卡片验证 webhook

纪律（严守）：
- 严格无未来函数：形态判定只用当根 OHLC + 历史成交量（不含未来）。
- 服务器零代理：Binance / 飞书均直连（curl 不加 -x；requests trust_env=False，不读任何代理环境变量）。
- 过滤口径：放量≥1.8×MA20 + 仅工作日(周一~周五) + 信号时间∈[09:00,23:00] 北京。
- 去重：按 (周期, 方向, K线开盘时间) 去重，state 文件持久化并自动清理 3 天前记录，重启不重报。
- 仅提醒不下单：卡片底部固定“非投资建议，仅供观察，不下单”。
- 多头=🔨 orange；空头(倒锤子)=🔻 red。
"""

from __future__ import annotations

import os
import sys
import json
import time
import signal
import subprocess
import datetime
import requests

# ============================ CONFIG（部署前确认 webhook） ============================
SYMBOL = ""
INTERVALS = ["5m", "15m", "30m", "1h", "4h"]
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
POLL_SECONDS = 10

# 锤子判定阈值（与回测一致）
LOWER_RATIO_MIN = 0.55      # 长下影占比下限（多头）/ 长上影占比下限（空头，镜像）
UPPER_RATIO_MAX = 0.15      # 上影占比上限（多头）/ 下影占比上限（空头，镜像）
BODY_RATIO_MAX = 0.30       # 实体占比上限
CLOSE_POS_MIN = 0.60        # 收位下限（多头收在上部）/ 镜像：空头收在下部(close_pos<=1-该值)
LOWER_TO_BODY_MIN = 2.0     # 下影/实体 或 上影/实体 下限

# 放量过滤
VOLUME_FILTER = True
VOL_MA_WINDOW = 20
VOL_MULT = 1.8

# 时间过滤（北京时区）
WEEKDAY_ONLY = True          # 仅周一~周五
HOUR_START, HOUR_END = 9, 23  # 信号形成时间 ∈ [09:00, 23:00]

# 文件位置
BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "watcher_single_state.json")
LOG_FILE = os.path.join(BASE, "watcher_single.log")
BJ = datetime.timezone(datetime.timedelta(hours=8))
WNAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ============================== 检测器（多/空，零未来函数） ==============================
def _metrics(o, h, l, c):
    """从 OHLC 算结构比例。range<=0 时比例返回 0。"""
    rng = h - l
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    close_pos = (c - l) / rng if rng > 0 else 0.0
    return {
        "range": rng, "body": body, "upper": upper, "lower": lower,
        "close_pos": close_pos,
        "body_ratio": body / rng if rng > 0 else 0.0,
        "upper_ratio": upper / rng if rng > 0 else 0.0,
        "lower_ratio": lower / rng if rng > 0 else 0.0,
        "lower_to_body": (lower / body) if body > 0 else 99.0,
        "upper_to_body": (upper / body) if body > 0 else 99.0,
    }


def is_hammer(kline, p):
    """多头锤子：长下影、实体小、收在上部、上影极小。"""
    o, h, l, c = float(kline[1]), float(kline[2]), float(kline[3]), float(kline[4])
    m = _metrics(o, h, l, c)
    rng = m["range"]
    if rng <= 0:
        return False, m
    if m["lower_ratio"] < p["lower_ratio_min"]:
        return False, m
    if m["upper_ratio"] > p["upper_ratio_max"]:
        return False, m
    if m["body_ratio"] > p["body_ratio_max"]:
        return False, m
    if m["close_pos"] < p["close_pos_min"]:
        return False, m
    if m["lower_to_body"] < p["lower_to_body_min"]:
        return False, m
    return True, m


def is_inverted_hammer(kline, p):
    """空头倒锤子（is_hammer 的严格镜像）：长上影、实体小、收在下部、下影极小。"""
    o, h, l, c = float(kline[1]), float(kline[2]), float(kline[3]), float(kline[4])
    m = _metrics(o, h, l, c)
    rng = m["range"]
    if rng <= 0:
        return False, m
    if m["upper_ratio"] < p["lower_ratio_min"]:
        return False, m
    if m["lower_ratio"] > p["upper_ratio_max"]:
        return False, m
    if m["body_ratio"] > p["body_ratio_max"]:
        return False, m
    if m["close_pos"] > (1 - p["close_pos_min"]):
        return False, m
    if m["upper_to_body"] < p["lower_to_body_min"]:
        return False, m
    return True, m


# ============================== 飞书推送（校验业务码 code==0） ==============================
def send_feishu_card(webhook, card, timeout=30):
    """推送交互式卡片；HTTP 200 ≠ 成功，必须校验响应体 code==0。零代理直连。"""
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.post(webhook, json={"msg_type": "interactive", "card": card}, timeout=timeout)
    except Exception as e:
        return False, f"ERR:{e}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        body = resp.json()
    except Exception:
        return False, f"响应非 JSON: {resp.text[:200]}"
    if not isinstance(body, dict) or body.get("code") != 0:
        code = body.get("code") if isinstance(body, dict) else "?"
        return False, f"业务码 {code}（HTTP 200 但业务未成功）: {str(body)[:200]}"
    return True, "ok"


# ============================== 行情 / 指标 / 判定 ==============================
def fetch_klines(symbol, interval, limit, retries=3):
    """服务器直连拉最近 limit 根 K 线（零代理，curl 不加 -x）。带重试。"""
    url = (f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}"
           f"?symbol={symbol}&interval={interval}&limit={limit}")
    for attempt in range(1, retries + 1):
        try:
            p = subprocess.run(["curl", "-s", "-m", "30", url],
                               capture_output=True, text=True, timeout=40)
            if p.returncode != 0:
                time.sleep(1)
                continue
            data = json.loads(p.stdout)
            if isinstance(data, list) and data:
                return data
            time.sleep(1)
        except Exception:
            time.sleep(1)
    return None


def atr14(klines, i):
    trs = []
    for j in range(max(1, i - 14 + 1), i + 1):
        h, l, pc = float(klines[j][2]), float(klines[j][3]), float(klines[j - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def detect_target(klines):
    """返回最近一根『已收盘』K 线的锤子判定（多头或空头方向），否则 None。严格无未来函数。"""
    if len(klines) < VOL_MA_WINDOW + 2:
        return None
    i = len(klines) - 2  # klines[-1] 为正在形成的未收盘 K 线
    k = klines[i]
    p = dict(lower_ratio_min=LOWER_RATIO_MIN, upper_ratio_max=UPPER_RATIO_MAX,
             body_ratio_max=BODY_RATIO_MAX, close_pos_min=CLOSE_POS_MIN,
             lower_to_body_min=LOWER_TO_BODY_MIN)
    ok, m = is_hammer(k, p)
    direction = "多" if ok else None
    if not ok:
        ok, m = is_inverted_hammer(k, p)
        direction = "空" if ok else None
    if direction is None:
        return None
    vols = [float(x[5]) for x in klines]
    if VOLUME_FILTER:
        if i < VOL_MA_WINDOW:
            return None
        ma = sum(vols[i - VOL_MA_WINDOW + 1:i + 1]) / VOL_MA_WINDOW
        vr = (vols[i] / ma) if ma > 0 else None
        if vr is None or vr < VOL_MULT:
            return None
    else:
        vr = None
    t = datetime.datetime.fromtimestamp(k[0] / 1000, BJ)
    if WEEKDAY_ONLY and t.weekday() >= 5:
        return None
    if not (HOUR_START <= t.hour <= HOUR_END):
        return None
    return dict(open_ms=k[0], time_bj=t, kline=k, metrics=m, vol_ratio=vr, direction=direction)


def build_plan(klines, i, direction="多"):
    """交易计划快照（仅供参考）。空头做镜像。"""
    k = klines[i]
    h, l = float(k[2]), float(k[3])
    a = atr14(klines, i)
    tick = 0.5
    sl_buf = max(0.5, 0.3 * a)
    if direction == "空":
        entry = l - tick
        sl = h + sl_buf
        risk = sl - entry
        return dict(entry=entry, sl=sl, tp1=entry - risk, tp2=entry - 2 * risk, atr=a)
    entry = h + tick
    sl = l - sl_buf
    risk = entry - sl
    return dict(entry=entry, sl=sl, tp1=entry + risk, tp2=entry + 2 * risk, atr=a)


def build_card(interval, res, klines, i):
    """构造飞书交互式卡片（interactive），遵循飞书卡片规范：
    - card 含 config/header/elements 三层；分割线用 hr（无 divider）；
    - 多头 orange、空头 red（均为规范允许的 header.template）；单段 markdown < 4096 字符。
    """
    k = res["kline"]
    m = res["metrics"]
    h, l = float(k[2]), float(k[3])
    t = res["time_bj"]
    direction = res.get("direction", "多")
    plan = build_plan(klines, i, direction)
    vr = res["vol_ratio"]
    is_short = (direction == "空")
    emoji = "🔻" if is_short else "🔨"
    title = f"{emoji} {'倒锤子(空头)' if is_short else '锤子(多头)'}交易信号 · {SYMBOL} {interval}"
    if is_short:
        dir_line = "**信号方向**  🔴 看跌（空头倒锤子）"
        trig = f"**触发条件**  跌破低点 L({l:.2f}) 后入场"
        invalid = f"**失效条件**  突破高点 H({h:.2f}) 放弃"
    else:
        dir_line = "**信号方向**  🟢 看涨（多头锤子）"
        trig = f"**触发条件**  突破高点 H({h:.2f}) 后入场"
        invalid = f"**失效条件**  跌破低点 L({l:.2f}) 放弃"
    elements = [
        {"tag": "markdown", "content": "\n".join([
            dir_line,
            "**K线周期**  " + interval,
            "**信号时间**  " + t.strftime('%Y-%m-%d %H:%M') + "（北京）",
            f"**量能放大**  {vr:.1f} 倍（≥{VOL_MULT} 倍触发）",
        ])},
        {"tag": "hr"},
        {"tag": "markdown", "content": "\n".join([
            f"**下影占比**  {m['lower_ratio']*100:.0f}%",
            f"**上影占比**  {m['upper_ratio']*100:.0f}%",
            f"**实体占比**  {m['body_ratio']*100:.0f}%",
            f"**收盘位置**  {m['close_pos']*100:.0f}%（100%=收在最高价）",
        ])},
        {"tag": "hr"},
        {"tag": "markdown", "content": "\n".join([
            trig,
            f"**入场价格**  {plan['entry']:.2f}",
            f"**止损价格**  {plan['sl']:.2f}",
            f"**止盈目标 1**  {plan['tp1']:.2f}",
            f"**止盈目标 2**  {plan['tp2']:.2f}",
            "**风险回报比**  1:2",
            invalid,
        ])},
        {"tag": "note", "elements": [
            {"tag": "plain_text", "content": "⚠️ 形态提醒，非投资建议，仅供观察，不下单。"}
        ]},
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red" if is_short else "orange",
                   "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


# ============================== 去重 state ==============================
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = set(tuple(x) for x in json.load(f))
            return s
    except Exception:
        return set()


def save_state(s):
    cutoff = int(time.time() * 1000) - 3 * 86400 * 1000
    s = {k for k in s if k[2] >= cutoff}
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump([list(x) for x in s], f)
    except Exception:
        pass


# ============================== 日志 ==============================
def log(msg):
    line = f"{datetime.datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ============================== 扫描主循环 ==============================
def scan_once(state):
    for interval in INTERVALS:
        klines = fetch_klines(SYMBOL, interval, limit=40)
        if not klines:
            continue
        res = detect_target(klines)
        if res is None:
            continue
        key = (interval, res["direction"], res["open_ms"])
        if key in state:
            continue
        state.add(key)
        save_state(state)
        i = len(klines) - 2
        card = build_card(interval, res, klines, i)
        log(f"[命中][{interval}/{res['direction']}] "
            f"{res['time_bj'].strftime('%Y-%m-%d %H:%M')} 量比={res['vol_ratio']:.1f}x，推送飞书卡片…")
        ok, detail = send_feishu_card(FEISHU_WEBHOOK, card)
        log(f"[{'ok' if ok else 'fail'}] 飞书: {detail}")


def run_daemon():
    log("单文件多周期锤子监测守护进程启动")
    state = load_state()
    stop = {"v": False}

    def _sig(*_):
        stop["v"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    while not stop["v"]:
        try:
            scan_once(state)
        except Exception as e:
            log(f"[error] 周期异常: {e}")
        for _ in range(POLL_SECONDS):
            if stop["v"]:
                break
            time.sleep(1)
    log("收到退出信号，已停止")


def cmd_test_push():
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": "🔨 锤子监测自测卡片"}},
        "elements": [
            {"tag": "markdown", "content": "若你看到这张卡片，说明 webhook 卡片推送正常。"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "非投资建议。"}]},
        ],
    }
    ok, detail = send_feishu_card(FEISHU_WEBHOOK, card)
    log(f"[test-push] ok={ok} detail={detail}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--test-push":
        cmd_test_push()
    elif arg == "--once":
        log("[once] 单次扫描")
        scan_once(load_state())
    else:
        run_daemon()
