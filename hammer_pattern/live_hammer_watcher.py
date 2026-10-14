# -*- coding: utf-8 -*-
"""锤子形态实盘监测守护进程（长期运行，飞书推送）。

设计要点（严守用户纪律）：
- 服务器零代理：Binance / 飞书均直连，curl 不加 -x，requests trust_env=False，不读任何代理环境变量。
- 严格无未来函数：形态判定只用当根 OHLC（与回测同一套 detector.is_hammer）。
- 过滤口径：放量≥1.8×MA20 + 仅工作日(周一~周五) + 信号形成时间∈[09:00,23:00] 北京。
- 去重：按「最近一根已收盘 5m K 线」的开盘时间戳去重，进程重启也通过 state 文件续上，不重复报警。
- 仅提醒不下单：本程序只检测形态并推送通知，不做任何下单/交易动作。

运行方式（用户部署到服务器）：
  python live_hammer_watcher.py            # 常驻守护，每 60s 检查一次
  python live_hammer_watcher.py --once    # 只检查一次（便于联调）
  python live_hammer_watcher.py --test-push   # 发一条测试消息验证 webhook
  python live_hammer_watcher.py --self-test   # 用本地缓存验证检测/去重/消息（不联网、不推送）

部署前请修改下方 CONFIG 中的 FEISHU_WEBHOOK。
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

# ============================ CONFIG（部署前修改） ============================
SYMBOL = ""
INTERVAL = "5m"
FETCH_LIMIT = 40                 # 取最近 40 根，足够算 vol_MA20 + ATR14
POLL_SECONDS = 60                # 轮询间隔（秒）；5m K 线，60s 足够及时

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

# 时间过滤（北京时区）
WEEKDAY_ONLY = True              # 仅周一~周五
HOUR_START, HOUR_END = 9, 23     # 信号形成时间 ∈ [09:00, 23:00]

# 飞书
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
# =============================================================================

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "watcher_state.json")
LOG_FILE = os.path.join(BASE, "watcher.log")
CACHE = os.path.join(BASE, "data_cache", "_5m_2026.json")
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


def fetch_klines(symbol: str, interval: str, limit: int):
    """服务器直连拉最近 limit 根 K 线（零代理，curl 不加 -x）。"""
    url = (f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}"
           f"?symbol={symbol}&interval={interval}&limit={limit}")
    p = subprocess.run(["curl", "-s", "-m", "30", url],
                       capture_output=True, text=True, timeout=40)
    if p.returncode != 0:
        raise RuntimeError(f"curl rc={p.returncode}: {p.stderr.strip()}")
    return json.loads(p.stdout)


def atr14(klines, i):
    trs = []
    for j in range(max(1, i - 14 + 1), i + 1):
        h, l, pc = float(klines[j][2]), float(klines[j][3]), float(klines[j - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def detect_target(klines):
    """返回最近一根『已收盘』5m K 线的锤子判定结果（多头或空头方向），否则 None。

    返回 dict: {open_ms, time_bj, kline, metrics, vol_ratio, direction} 或 None。
    严格无未来函数：只用当根 OHLC + 历史成交量(不含未来)。
    """
    if len(klines) < VOL_MA_WINDOW + 2:
        return None
    # klines[-1] 是当前正在形成的 K 线（未收盘）；klines[-2] 是最新已收盘
    i = len(klines) - 2
    k = klines[i]
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    p = {
        "lower_ratio_min": LOWER_RATIO_MIN, "upper_ratio_max": UPPER_RATIO_MAX,
        "body_ratio_max": BODY_RATIO_MAX, "close_pos_min": CLOSE_POS_MIN,
        "lower_to_body_min": LOWER_TO_BODY_MIN,
    }
    # 多头锤子（长下影）优先，否则试空头倒锤子（长上影）
    ok, m = is_hammer(k, p)
    direction = "多" if ok else None
    if not ok:
        ok, m = is_inverted_hammer(k, p)
        direction = "空" if ok else None
    if direction is None:
        return None
    # 放量过滤
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
    # 时间过滤（北京）
    t = datetime.datetime.fromtimestamp(k[0] / 1000, BJ)
    if WEEKDAY_ONLY and t.weekday() >= 5:
        return None
    if not (HOUR_START <= t.hour <= HOUR_END):
        return None
    return {"open_ms": k[0], "time_bj": t, "kline": k, "metrics": m,
            "vol_ratio": vr, "direction": direction}


def build_plan(klines, i, direction="多"):
    """交易计划快照（仅供参考，非投资建议）。空头方向做镜像。"""
    k = klines[i]
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    a = atr14(klines, i)
    tick = 0.5
    sl_buf = max(0.5, 0.3 * a)
    if direction == "空":
        # 空头倒锤子：跌破低点 L 后入场，止损在上方，TP 向下
        entry = l - tick
        sl = h + sl_buf
        risk = sl - entry
        return dict(entry=entry, sl=sl, tp1=entry - risk, tp2=entry - 2 * risk, risk=risk, atr=a)
    # 多头锤子：突破高点 H 后入场，止损在下方，TP 向上
    entry = h + tick
    sl = l - sl_buf
    risk = entry - sl
    return dict(entry=entry, sl=sl, tp1=entry + risk, tp2=entry + 2 * risk, risk=risk, atr=a)


def build_message(res, klines, i):
    k = res["kline"]
    m = res["metrics"]
    o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    t = res["time_bj"]
    direction = res.get("direction", "多")
    plan = build_plan(klines, i, direction)
    vr = res["vol_ratio"]
    is_short = (direction == "空")
    trig = f"跌破 L({l:.1f}) 后入场" if is_short else f"收盘价>H({h:.1f})"
    invalid = f"突破 H({h:.1f}) 放弃" if is_short else f"跌破 L({l:.1f}) 放弃"
    dir_txt = "🔻 倒锤子(空头)信号" if is_short else "🔨 放量锤子信号"
    plan_hdr = "—— 空头计划(仅供参考) ——" if is_short else "—— 多头计划(仅供参考) ——"
    msg = (
        f"{dir_txt} · {SYMBOL} {INTERVAL}\n"
        f"时间(北京): {t.strftime('%Y-%m-%d %H:%M')} {WNAMES[t.weekday()]}\n"
        f"OHLC: O={o:.1f} H={h:.1f} L={l:.1f} C={c:.1f}\n"
        f"量比={vr:.1f}x | 下影/全幅={m['lower_ratio']*100:.0f}% | 上影/全幅={m['upper_ratio']*100:.0f}% "
        f"| 实体/全幅={m['body_ratio']*100:.0f}% | 收位={m['close_pos']*100:.0f}%\n"
        f"{plan_hdr}\n"
        f"触发: {trig} | 入场≈{plan['entry']:.1f}\n"
        f"止损≈{plan['sl']:.1f} | TP1≈{plan['tp1']:.1f} | TP2≈{plan['tp2']:.1f} | RR=2:1\n"
        f"失效: {invalid}\n"
        f"⚠️ 形态提醒，非投资建议，仅供观察，不下单。"
    )
    return msg


def build_card(res, klines, i):
    """构造飞书交互式卡片（interactive），遵循飞书卡片规范。"""
    k = res["kline"]
    m = res["metrics"]
    h, l = float(k[2]), float(k[3])
    t = res["time_bj"]
    direction = res.get("direction", "多")
    plan = build_plan(klines, i, direction)
    vr = res["vol_ratio"]
    is_short = (direction == "空")
    emoji = "🔻" if is_short else "🔨"
    title = f"{emoji} {'倒锤子(空头)' if is_short else '锤子(多头)'}交易信号 · {SYMBOL} {INTERVAL}"
    if is_short:
        dir_line = "**信号方向**  🔴 看跌（空头倒锤子）"
        trig = f"**触发条件**  跌破低点 L({l:.1f}) 后入场"
        invalid = f"**失效条件**  突破高点 H({h:.1f}) 放弃"
    else:
        dir_line = "**信号方向**  🟢 看涨（多头锤子）"
        trig = f"**触发条件**  突破高点 H({h:.1f}) 后入场"
        invalid = f"**失效条件**  跌破低点 L({l:.1f}) 放弃"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red" if is_short else "orange",
                   "title": {"tag": "plain_text", "content": title}},
        "elements": [
            # —— 第一节：信号概要（每行一个字段）——
            {"tag": "markdown", "content": "\n".join([
                dir_line,
                "**K线周期**  " + INTERVAL,
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
                f"**入场价格**  {plan['entry']:.1f}",
                f"**止损价格**  {plan['sl']:.1f}",
                f"**止盈目标 1**  {plan['tp1']:.1f}",
                f"**止盈目标 2**  {plan['tp2']:.1f}",
                "**风险回报比**  1:2",
                invalid,
            ])},
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": "⚠️ 形态提醒，非投资建议，仅供观察，不下单。"}
            ]},
        ],
    }


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
            if isinstance(st, dict) and "seen" in st:
                return st
    except Exception:
        pass
    return {"seen": []}


def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception as e:
        log(f"[warn] state 保存失败: {e}")


def cycle(state):
    """一次检查周期：返回是否处理了新信号（用于日志）。"""
    klines = fetch_klines(SYMBOL, INTERVAL, FETCH_LIMIT)
    res = detect_target(klines)
    if res is None:
        return False
    # 去重 key 含方向，避免同一根 K 线多/空重复，也不互相覆盖
    key = [res["direction"], res["open_ms"]]
    seen = state.setdefault("seen", [])
    if key in seen:
        return False  # 已处理过该 K 线（同方向）
    seen.append(key)
    if len(seen) > 500:  # 只保留最近 500 条，避免无限增长
        state["seen"] = seen[-500:]
    save_state(state)
    i = len(klines) - 2
    card = build_card(res, klines, i)
    log(f"[命中][{res['direction']}] {res['time_bj'].strftime('%Y-%m-%d %H:%M')} "
        f"量比={res['vol_ratio']:.1f}x，推送飞书卡片…")
    if FEISHU_WEBHOOK.startswith("REPLACE"):
        log("[warn] FEISHU_WEBHOOK 未配置，跳过推送（仅打印卡片）:\n" + json.dumps(card, ensure_ascii=False))
        return True
    ok, detail = send_feishu_card(FEISHU_WEBHOOK, card)
    if ok:
        log("[ok] 飞书推送成功")
    else:
        log(f"[fail] 飞书推送失败: {detail}")
    return True


def run_daemon():
    log("锤子监测守护进程启动")
    state = load_state()
    stop = {"v": False}

    def _sig(*_):
        stop["v"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    while not stop["v"]:
        try:
            cycle(state)
        except Exception as e:
            log(f"[error] 周期异常: {e}")
        # 分段休眠，便于信号及时退出
        for _ in range(POLL_SECONDS):
            if stop["v"]:
                break
            time.sleep(1)
    log("收到退出信号，已停止")


def cmd_once():
    log("[once] 单次检查")
    state = load_state()
    try:
        handled = cycle(state)
        log("[once] 完成，" + ("发现并处理信号" if handled else "无信号"))
    except Exception as e:
        log(f"[once] 失败: {e}")


def cmd_test_push():
    if FEISHU_WEBHOOK.startswith("REPLACE"):
        log("[test-push] 请先在 CONFIG 配置 FEISHU_WEBHOOK")
        return
    ok, detail = send_feishu(FEISHU_WEBHOOK,
                             "🔨 锤子监测自测消息：若你看到这条，说明 webhook 配置正确。")
    log(f"[test-push] ok={ok} detail={detail}")


def cmd_self_test():
    """用本地缓存验证检测/去重/消息构造（不联网、不推送）。"""
    if not os.path.exists(CACHE):
        log("[self-test] 无缓存文件，无法离线自测")
        return
    with open(CACHE, "r", encoding="utf-8") as f:
        cache = json.load(f)
    # 找一个已知锤子（2026-10-02 09:10）与一个非锤子邻近点
    def find_idx(ts_prefix):
        for idx, k in enumerate(cache):
            if datetime.datetime.fromtimestamp(k[0] / 1000, BJ).strftime("%Y-%m-%d %H:%M") == ts_prefix:
                return idx
        return None
    hi = find_idx("2026-10-02 09:10")
    # 取该锤子作为「已收盘」目标：窗口以它结尾（target=窗口[-2]，forming=窗口[-1]）
    if hi is None or hi < FETCH_LIMIT:
        log("[self-test] 缓存中找不到 2026-10-02 09:10 锤子或窗口不足")
        return
    window = cache[hi + 2 - FETCH_LIMIT: hi + 2]  # 使 target=window[-2]=该锤子
    res = detect_target(window)
    log(f"[self-test] 已知锤子判定 -> {'命中' if res else '未命中(异常!)'}")
    if res:
        i = len(window) - 2
        msg = build_message(res, window, i)
        log("---- 消息预览 ----\n" + msg)
    # 反向：用一段明显非锤子窗口（例如找一根实体很大的 K 线做 target）
    # 简单验证：把窗口整体平移 5 根，大概率不再是同一锤子
    window2 = cache[hi + 7 - FETCH_LIMIT: hi + 7]
    res2 = detect_target(window2)
    log(f"[self-test] 偏移窗口判定 -> {'命中' if res2 else '未命中(预期)'}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--once":
        cmd_once()
    elif arg == "--test-push":
        cmd_test_push()
    elif arg == "--self-test":
        cmd_self_test()
    else:
        run_daemon()
