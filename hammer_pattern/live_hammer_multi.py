# -*- coding: utf-8 -*-
"""锤子形态实盘监测（多周期 5m/15m/30m/1h/4h）· 飞书推送 · 服务器零代理。

设计要点（严守用户纪律）：
- 复用 detector.is_hammer（与回测同源，严格无未来函数，只用当根 OHLC）。
- 过滤：放量≥1.8×MA20 + 仅工作日(周一~周五) + 信号形成时间∈[09:00,23:00] 北京（均可配置）。
- 去重：按 (周期, 已收盘K线开盘时间) 去重，state 文件持久化并自动清理 3 天前的记录，重启不重报。
- 飞书：直接 POST 文本消息，校验业务码 code==0（HTTP 200 ≠ 成功）。
- 服务器零代理：Binance/飞书均直连（curl 不加 -x；requests trust_env=False，不读任何代理环境变量）。

运行：
  python live_hammer_multi.py              # 常驻，每 10s 扫所有周期
  python live_hammer_multi.py --once      # 单次扫描（联调用）
  python live_hammer_multi.py --test-push # 发测试消息验证 webhook（服务器侧用）
"""
from __future__ import annotations

import os
import sys
import json
import time
import signal
import subprocess
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from detector import is_hammer, is_inverted_hammer
from feishu_push import send_feishu, send_feishu_card

# ====================== 配置（部署前确认 webhook） ======================
SYMBOL = ""
INTERVALS = ["5m", "15m", "30m", "1h", "4h"]
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
POLL_SECONDS = 10

# 锤子判定阈值（与 PRD / 回测一致）
LOWER_RATIO_MIN = 0.55
UPPER_RATIO_MAX = 0.15
BODY_RATIO_MAX = 0.30
CLOSE_POS_MIN = 0.60
LOWER_TO_BODY_MIN = 2.0

# 放量过滤
VOLUME_FILTER = True
VOL_MA_WINDOW = 20
VOL_MULT = 1.8

# 时间过滤（北京时区）；高周期若想放宽把 HOUR_START/HOUR_END 改成 0/23 即可
WEEKDAY_ONLY = True
HOUR_START, HOUR_END = 9, 23
# =========================================================================

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "watcher_multi_state.json")
LOG_FILE = os.path.join(BASE, "watcher_multi.log")
BJ = datetime.timezone(datetime.timedelta(hours=8))
WNAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def log(msg: str):
    line = f"{datetime.datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fetch_klines(interval: str, limit: int = 40, retries: int = 3):
    """服务器直连拉最近 limit 根 K 线（零代理，curl 不加 -x）。带重试。"""
    url = (f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}"
           f"?symbol={SYMBOL}&interval={interval}&limit={limit}")
    for attempt in range(1, retries + 1):
        try:
            p = subprocess.run(["curl", "-s", "-m", "30", url],
                               capture_output=True, text=True, timeout=40)
            if p.returncode != 0:
                log(f"[{interval}] curl rc={p.returncode}，1s 后重试({attempt})")
                time.sleep(1)
                continue
            data = json.loads(p.stdout)
            if isinstance(data, list) and data:
                return data
            log(f"[{interval}] 返回空数据，重试({attempt})")
            time.sleep(1)
        except Exception as e:
            log(f"[{interval}] 请求异常: {e}，1s 后重试({attempt})")
            time.sleep(1)
    log(f"[{interval}] 全部 {retries} 次重试失败")
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
    # 多头锤子（长下影）优先，否则试空头倒锤子（长上影）
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
    k = klines[i]
    h, l = float(k[2]), float(k[3])
    a = atr14(klines, i)
    tick = 0.5
    if direction == "空":
        # 空头倒锤子：跌破低点 L 后入场，止损在上方，TP 向下
        entry = l - tick
        sl = h + max(0.5, 0.3 * a)
        risk = sl - entry
        return dict(entry=entry, sl=sl, tp1=entry - risk, tp2=entry - 2 * risk, atr=a)
    # 多头锤子：突破高点 H 后入场，止损在下方，TP 向上
    entry = h + tick
    sl = l - max(0.5, 0.3 * a)
    risk = entry - sl
    return dict(entry=entry, sl=sl, tp1=entry + risk, tp2=entry + 2 * risk, atr=a)


def build_message(interval: str, res: dict, klines, i: int) -> str:
    k = res["kline"]
    m = res["metrics"]
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    t = res["time_bj"]
    plan = build_plan(klines, i)
    vr = res["vol_ratio"]
    return (
        f"🔨 放量锤子信号 · {SYMBOL} {interval}\n"
        f"时间(北京): {t.strftime('%Y-%m-%d %H:%M')} {WNAMES[t.weekday()]}\n"
        f"OHLC: O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f}\n"
        f"量比={vr:.1f}x | 下影/全幅={m['lower_ratio']*100:.0f}% | 上影/全幅={m['upper_ratio']*100:.0f}% "
        f"| 实体/全幅={m['body_ratio']*100:.0f}% | 收位={m['close_pos']*100:.0f}%\n"
        f"—— 多头计划(仅供参考) ——\n"
        f"触发: 收盘价>H({h:.2f}) | 入场≈{plan['entry']:.2f}\n"
        f"止损≈{plan['sl']:.2f} | TP1≈{plan['tp1']:.2f} | TP2≈{plan['tp2']:.2f} | RR=2:1\n"
        f"失效: 跌破 L({l:.2f}) 放弃\n"
        f"⚠️ 形态提醒，非投资建议，仅供观察，不下单。"
    )


def build_card(interval: str, res: dict, klines, i: int) -> dict:
    """构造飞书交互式卡片（interactive），遵循飞书卡片规范：
    - card 含 config/header/elements 三层；分割线用 hr（无 divider）；
    - 多头用 orange、空头用 red（均为规范允许的 header.template）；单段 markdown < 4096 字符。
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
    title = f"{emoji} {'倒锤子(空头)' if is_short else '锤子(多头)'}交易信号 ·  {interval}"
    # 方向字段 + 头/底颜色
    if is_short:
        dir_line = "**信号方向**  🔴 看跌（空头倒锤子）"
        trig = f"**触发条件**  跌破低点 L({l:.2f}) 后入场"
        invalid = f"**失效条件**  突破高点 H({h:.2f}) 放弃"
    else:
        dir_line = "**信号方向**  🟢 看涨（多头锤子）"
        trig = f"**触发条件**  突破高点 H({h:.2f}) 后入场"
        invalid = f"**失效条件**  跌破低点 L({l:.2f}) 放弃"
    elements = [
        # —— 第一节：信号概要（每行一个字段）——
        {"tag": "markdown", "content": "\n".join([
            dir_line,
            "**K线周期**  " + interval,
            "**信号时间**  " + t.strftime('%Y-%m-%d %H:%M') + "（北京）",
            f"**量能放大**  {vr:.1f} 倍（≥{VOL_MULT} 倍触发）",
        ])},
        {"tag": "hr"},
        # —— 第二节：形态明细（逐行字段）——
        {"tag": "markdown", "content": "\n".join([
            f"**下影占比**  {m['lower_ratio']*100:.0f}%",
            f"**上影占比**  {m['upper_ratio']*100:.0f}%",
            f"**实体占比**  {m['body_ratio']*100:.0f}%",
            f"**收盘位置**  {m['close_pos']*100:.0f}%（100%=收在最高价）",
        ])},
        {"tag": "hr"},
        # —— 第三节：交易计划（逐行字段，方向镜像）——
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


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(tuple(x) for x in json.load(f))
    except Exception:
        return set()


def save_state(s):
    # 自动清理 3 天前的记录，避免无限增长
    cutoff = int(time.time() * 1000) - 3 * 86400 * 1000
    s = {k for k in s if k[1] >= cutoff}
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump([list(x) for x in s], f)
    except Exception as e:
        log(f"[warn] state 保存失败: {e}")


def scan_once(state):
    for interval in INTERVALS:
        klines = fetch_klines(interval, limit=40)
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
        log(f"[命中][{interval}] {res['time_bj'].strftime('%Y-%m-%d %H:%M')} "
            f"量比={res['vol_ratio']:.1f}x，推送飞书卡片…")
        ok, detail = send_feishu_card(FEISHU_WEBHOOK, card)
        log(f"[{'ok' if ok else 'fail'}] 飞书: {detail}")


def run_daemon():
    log("多周期锤子监测守护进程启动")
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
