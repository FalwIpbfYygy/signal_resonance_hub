"""
================================================================================
 实时信号推送系统 v1.0
================================================================================
数据源  : PAXGUSDT (Binance Futures)  →   = PAXGUSDT + 12
输出    : 飞书 Webhook 信号推送
策略    : 小白扫盲课 7 模块 PA 体系
      ① 交易员守则 (3 铁律)
      ② 两臂战法 (吞没 + 缺口)
      ③ 成交量分布 (Volume Profile)
      ④ 绿线系统 (Hull MA 9 / 16)
      ⑤ 多周期共振 (M5 + M15 + M30)
      ⑥ 仓位管理 (ATR 自适应止损)
      ⑦ 飞书推送 + 去重
运行    : 本地 24h 实时扫描
================================================================================
"""

import time
import json
import os
import sys
import requests
from datetime import datetime

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store



# ==============================================================================
# Section 1: 全局配置 (所有可调参数)
# ==============================================================================

# ---------- 数据源 ----------
SYMBOL = "PAXGUSDT"
BINANCE_URL = os.environ.get("BINANCE_FUTURES_KLINE_URL")
INTERVALS = ["5m", "15m", "30m"]
KLINES_LIMIT = 200
PRICE_OFFSET = 12.0              # PAXGUSDT + 12 = 

# ---------- 飞书 ----------
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

# ---------- 绿线 (Hull MA) ----------
HULL_FAST_PERIOD = 9              # 亮绿线
HULL_SLOW_PERIOD = 16             # 暗绿线

# ---------- 止损 (ATR 自适应) ----------
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 1.5
MIN_STOP_DISTANCE = 1.0
MAX_STOP_DISTANCE = 6.0              # 放宽: 4.0 → 6.0 (避免 M30 高波动时止损被截太紧)

# ---------- Volume Profile ----------
VP_LOOKBACK_BARS = 100
VP_BINS = 50
VP_GAP_RATIO = 0.3                # 密度 < 中位数 × (1 - 0.3) 视为缺口

# ---------- 信号 ----------
SCAN_INTERVAL_SEC = 10            # 扫描间隔
SIGNAL_COOLDOWN_SEC = 1800        # 同信号冷却 30 分钟
SIGNAL_SCORE_THRESHOLD = 80       # 评分阈值
PRICE_BUCKET_SIZE = 2.0           # 入场价分档 (每 2 美元一档)
SINGLE_INTERVAL_SCORE_BOOST = 10  # 单 interval 推送需 ≥ 90 分

# ---------- 运行时状态 ----------
kline_cache = {iv: [] for iv in INTERVALS}
notified_signals = {}             # key: (interval, direction, bucket) → timestamp


# ==============================================================================
# Section 2: 工具函数
# ==============================================================================

def log(msg: str):
    """统一日志输出"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}", flush=True)


def wma_at(values, end_idx: int, period: int):
    """计算在 end_idx 位置的 WMA(period)"""
    if end_idx < period - 1:
        return None
    window = values[end_idx - period + 1:end_idx + 1]
    weights = list(range(1, period + 1))
    return sum(v * w for v, w in zip(window, weights)) / sum(weights)


def wma_list(values, period: int):
    """对列表计算 WMA"""
    if len(values) < period:
        return None
    recent = values[-period:]
    weights = list(range(1, period + 1))
    return sum(v * w for v, w in zip(recent, weights)) / sum(weights)


# ==============================================================================
# Section 3: 数据层
# ==============================================================================

def fetch_klines(interval: str, retries: int = 3):
    """从 Binance 拉取 K 线 (含重试)"""
    url = f"{BINANCE_URL}?symbol={SYMBOL}&interval={interval}&limit={KLINES_LIMIT}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url)
            if resp.status_code == 200:
                return resp.json()
            log(f"[{interval}] 拉取失败 状态码:{resp.status_code}")
        except Exception as e:
            log(f"[{interval}] 拉取异常:{e} (尝试 {attempt}/{retries})")
        time.sleep(1)
    return None


def parse_klines(raw):
    """解析 K 线 + 应用 PRICE_OFFSET 换算"""
    parsed = []
    for k in raw:
        parsed.append({
            "open_time": int(k[0]),
            "open":   float(k[1]) + PRICE_OFFSET,
            "high":   float(k[2]) + PRICE_OFFSET,
            "low":    float(k[3]) + PRICE_OFFSET,
            "close":  float(k[4]) + PRICE_OFFSET,
            "volume": float(k[5]),
            "close_time": int(k[6]),
            "trades": int(k[8]),
        })
    return parsed


# ==============================================================================
# Section 4: 指标层
# ==============================================================================

def calc_hull_series(prices, period: int):
    """
    Hull MA 完整序列
    HMA(n) = WMA(2*WMA(n/2) - WMA(n), sqrt(n))
    """
    half_p = max(1, period // 2)
    sqrt_p = max(1, int(period ** 0.5))
    n = len(prices)
    result = [None] * n

    if n < period + sqrt_p:
        return result

    # 第一步: 计算 raw = 2*WMA(n/2) - WMA(n)
    for i in range(period, n):
        wma_half = wma_at(prices, i, half_p)
        wma_full = wma_at(prices, i, period)
        if wma_half is not None and wma_full is not None:
            result[i] = 2 * wma_half - wma_full

    # 第二步: 对 raw 序列再做 WMA(sqrt(n))
    hull = [None] * n
    for i in range(sqrt_p - 1, n):
        raw_window = [r for r in result[i - sqrt_p + 1:i + 1] if r is not None]
        if len(raw_window) >= sqrt_p:
            hull[i] = wma_list(raw_window, sqrt_p)
    return hull


def calc_volume_profile(klines, lookback: int = VP_LOOKBACK_BARS):
    """
    构建最近 N 根 K 线的 Volume Profile
    返回: (profile_dict 或 None, min_price)
    """
    if len(klines) < 10:
        return None, None
    recent = klines[-lookback:] if len(klines) >= lookback else klines

    all_lows = [k["low"] for k in recent]
    all_highs = [k["high"] for k in recent]
    min_p = min(all_lows)
    max_p = max(all_highs)

    if max_p <= min_p:
        return None, None

    bin_size = (max_p - min_p) / VP_BINS
    bins = [0.0] * VP_BINS

    for k in recent:
        mid_p = (k["low"] + k["high"]) / 2
        bin_idx = int((mid_p - min_p) / bin_size)
        bin_idx = max(0, min(VP_BINS - 1, bin_idx))
        bins[bin_idx] += k["volume"]

    return {"min": min_p, "max": max_p, "bin_size": bin_size, "bins": bins}, min_p


def classify_volume_position(profile, current_price: float) -> str:
    """
    判断当前价在 VP 中的位置
    返回: "gap" | "thick" | "boundary" | "out_of_range" | "unknown"
    """
    if not profile:
        return "unknown"
    min_p = profile["min"]
    max_p = profile["max"]
    bin_size = profile["bin_size"]
    bins = profile["bins"]

    if current_price < min_p or current_price > max_p:
        return "out_of_range"

    bin_idx = int((current_price - min_p) / bin_size)
    bin_idx = max(0, min(len(bins) - 1, bin_idx))
    current_density = bins[bin_idx]

    valid = [b for b in bins if b > 0]
    if not valid:
        return "unknown"
    sorted_d = sorted(valid)
    median_d = sorted_d[len(sorted_d) // 2]

    if median_d <= 0:
        return "unknown"
    ratio = current_density / median_d

    if ratio < (1 - VP_GAP_RATIO):
        return "gap"
    elif ratio > (1 + VP_GAP_RATIO):
        return "thick"
    return "boundary"


def calc_atr(klines, period: int = ATR_PERIOD):
    """ATR (Average True Range)"""
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        prev = klines[i - 1]
        curr = klines[i]
        tr = max(
            curr["high"] - curr["low"],
            abs(curr["high"] - prev["close"]),
            abs(curr["low"] - prev["close"]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def detect_engulfing(klines) -> str | None:
    """
    检测最近一根 K 线的吞没形态
    返回: "bullish" | "bearish" | None
    """
    if len(klines) < 2:
        return None
    prev = klines[-2]
    curr = klines[-1]

    # 阳包阴
    if (prev["close"] < prev["open"] and
        curr["close"] > curr["open"] and
        curr["open"] < prev["close"] and
        curr["close"] > prev["open"]):
        return "bullish"

    # 阴包阳
    if (prev["close"] > prev["open"] and
        curr["close"] < curr["open"] and
        curr["open"] > prev["close"] and
        curr["close"] < prev["open"]):
        return "bearish"

    return None


def check_direction_only(interval: str, klines) -> str | None:
    """
    M15/M30 方向确认 (不需要吞没形态)
    课程原话: "十五分钟K和三十分钟K来去帮你把握行情的方向"
    返回: "BUY" | "SELL" | None
    """
    if not klines or len(klines) < HULL_SLOW_PERIOD + 5:
        return None

    closes = [k["close"] for k in klines]
    current_price = closes[-1]

    hull_fast = calc_hull_series(closes, HULL_FAST_PERIOD)
    hull_slow = calc_hull_series(closes, HULL_SLOW_PERIOD)

    if hull_fast[-1] is None or hull_slow[-1] is None:
        return None

    hf = hull_fast[-1]
    hs = hull_slow[-1]

    if current_price > hf > hs:
        return "BUY"
    if current_price < hf < hs:
        return "SELL"
    return None


# ==============================================================================
# Section 5: 信号评估
# ==============================================================================

def evaluate_interval(interval: str, klines, require_engulfing: bool = True):
    """
    评估单个 interval 是否产生信号
    require_engulfing=True (M5 主信号): 必须有吞没
    require_engulfing=False (M15/M30 方向确认): 仅检查绿线方向
    返回: dict 或 None
    """
    if len(klines) < max(50, HULL_SLOW_PERIOD + 10):
        return None

    closes = [k["close"] for k in klines]
    current_price = closes[-1]

    hull_fast = calc_hull_series(closes, HULL_FAST_PERIOD)
    hull_slow = calc_hull_series(closes, HULL_SLOW_PERIOD)

    if hull_fast[-1] is None or hull_slow[-1] is None:
        return None

    atr = calc_atr(klines)
    if atr is None or atr <= 0:
        return None

    profile, _ = calc_volume_profile(klines)
    vp_pos = classify_volume_position(profile, current_price)

    # 必须不在厚区
    if vp_pos == "thick":
        return None

    # 绿线方向
    hf = hull_fast[-1]
    hs = hull_slow[-1]

    if current_price > hf > hs:
        direction = "BUY"
    elif current_price < hf < hs:
        direction = "SELL"
    else:
        return None

    # 吞没检查 (仅 M5 需要)
    engulfing = None
    if require_engulfing:
        engulfing = detect_engulfing(klines)
        if engulfing is None:
            return None
        # 方向必须与吞没一致
        if (engulfing == "bullish" and direction != "BUY") or \
           (engulfing == "bearish" and direction != "SELL"):
            return None

    return {
        "interval": interval,
        "direction": direction,
        "current_price": current_price,
        "hull_fast": hf,
        "hull_slow": hs,
        "atr": atr,
        "vp_position": vp_pos,
        "engulfing": engulfing,
    }


def calc_signal_score(signal, m15_confirmed: bool, m30_confirmed: bool) -> int:
    """
    7 维评分 (满分 100)
    """
    score = 0

    # 1. 吞没形态 (25)
    score += 25

    # 2. 缺口位置 (15)
    if signal["vp_position"] == "gap":
        score += 15
    elif signal["vp_position"] == "boundary":
        score += 8

    # 3. 绿线方向 (15)  (基础条件已过滤,这里加分)
    score += 15

    # 4. M15 共振 (15)
    if m15_confirmed:
        score += 15

    # 5. M30 共振 (15)
    if m30_confirmed:
        score += 15

    # 6. 多周期形态共振 (10)
    if m15_confirmed and m30_confirmed:
        score += 10
    elif m15_confirmed or m30_confirmed:
        score += 5

    # 7. 位置健康度 (5)
    if signal["vp_position"] == "gap":
        score += 5

    return min(score, 100)


def calc_trade_levels(signal):
    """
    计算入场价、止损、止盈
    止损: ATR × 1.5, clamp(1.0, 4.0)
    止盈: 1R / 2R
    """
    entry = signal["current_price"]
    if signal["direction"] == "BUY":
        entry = round(entry + 0.5, 2)
    else:
        entry = round(entry - 0.5, 2)

    raw_stop = signal["atr"] * ATR_STOP_MULTIPLIER
    stop_dist = max(MIN_STOP_DISTANCE, min(MAX_STOP_DISTANCE, raw_stop))
    stop_dist = round(stop_dist, 2)

    if signal["direction"] == "BUY":
        stop_loss = round(entry - stop_dist, 2)
        tp1 = round(entry + stop_dist, 2)
        tp2 = round(entry + stop_dist * 2, 2)
    else:
        stop_loss = round(entry + stop_dist, 2)
        tp1 = round(entry - stop_dist, 2)
        tp2 = round(entry - stop_dist * 2, 2)

    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "stop_distance": stop_dist,
    }


# ==============================================================================
# Section 6: 飞书推送
# ==============================================================================

def push_feishu(text: str):
    """推送消息到飞书"""
    msg = {"msg_type": "text", "content": {"text": text}}
    try:
        resp = requests.post(FEISHU_WEBHOOK, json=msg, timeout=5)
        log(f"飞书推送 状态码:{resp.status_code}")
    except Exception as e:
        log(f"飞书推送异常:{e}")


def format_signal_msg(signal, levels, m15_ok: bool, m30_ok: bool) -> str:
    """格式化信号消息"""
    score = signal["_score"]
    star = "⭐⭐⭐" if score >= 90 else "⭐⭐" if score >= 80 else "⭐"

    m15_mark = "✅" if m15_ok else "❌"
    m30_mark = "✅" if m30_ok else "❌"
    direction_cn = "做多 BUY" if signal["direction"] == "BUY" else "做空 SELL"
    vp_cn = {
        "gap": "缺口区",
        "boundary": "边界区",
        "thick": "厚区",
        "out_of_range": "超出范围",
        "unknown": "未知",
    }.get(signal["vp_position"], signal["vp_position"])
    engulfing_cn = "阳包阴" if signal["engulfing"] == "bullish" else "阴包阳"

    raw_stop = signal["atr"] * ATR_STOP_MULTIPLIER
    stop_clamped = "限制为" if raw_stop > MAX_STOP_DISTANCE or raw_stop < MIN_STOP_DISTANCE else "实际为"

    msg = f"""══════════PAXG 吞没+位置 共振策略（美盘时段信号较强）════════════════════
🚨 【{signal['direction']} 信号 {star}】

📊 基础信息
   品种:
   触发周期:{signal['interval']} (M15:{m15_mark} M30:{m30_mark})
   当前价:{signal['current_price']:.2f}
   信号时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
   数据源:PAXGUSDT + 12

📈 交易方案
   方向:{direction_cn}
   入场价:{levels['entry']:.2f}
   止损价:{levels['stop_loss']:.2f} (距离 {levels['stop_distance']:.2f})
   止盈 1:{levels['tp1']:.2f} (1R,推 70%)
   止盈 2:{levels['tp2']:.2f} (2R,留 30%)

🔍 信号依据
   ✅ {signal['interval']} {engulfing_cn}形态
   ✅ 当前位置在{vp_cn}
   ✅ 价格{'站稳' if signal['direction'] == 'BUY' else '跌破'}双绿线
   {m15_mark} M15 同向
   {m30_mark} M30 同向
   📊 评分:{score}/100

📐 关键参数
   ATR(14):{signal['atr']:.2f} 美元
   止损倍数:1.5× ATR = {raw_stop:.2f} → {stop_clamped} {levels['stop_distance']:.2f}
   Hull MA(9):{signal['hull_fast']:.2f}
   Hull MA(16):{signal['hull_slow']:.2f}

⚠️ 风险提示
   • 请确认未亏损达账户 10%
   • 请确认未连续 3 单亏损
   • 重大数据日请自行暂停交易
══════════════════════════════"""
    return msg


# ==============================================================================
# Section 7: 信号去重
# ==============================================================================

def is_duplicate(interval: str, direction: str, price: float) -> bool:
    """同 interval + 方向 + 价格档 + 30 分钟内 不重复"""
    bucket = int(price / PRICE_BUCKET_SIZE) * PRICE_BUCKET_SIZE
    key = (interval, direction, bucket)
    if key in notified_signals:
        last_time = notified_signals[key]
        if (datetime.now() - last_time).total_seconds() < SIGNAL_COOLDOWN_SEC:
            return True
    return False


def mark_notified(interval: str, direction: str, price: float):
    bucket = int(price / PRICE_BUCKET_SIZE) * PRICE_BUCKET_SIZE
    key = (interval, direction, bucket)
    notified_signals[key] = datetime.now()


# ==============================================================================
# Section 8: 主扫描循环
# ==============================================================================

def scan():
    """每 SCAN_INTERVAL_SEC 秒执行一次
    核心逻辑 (修复后):
      M5 是主信号生成器 (必须有吞没)
      M15/M30 仅做方向确认 (绿线方向一致即可)
    """
    # 步骤 1: 拉取所有 interval 数据
    for interval in INTERVALS:
        raw = fetch_klines(interval)
        if raw is None:
            log(f"[{interval}] 拉取失败,跳过")
            continue
        kline_cache[interval] = parse_klines(raw)

    if "5m" not in kline_cache or not kline_cache["5m"]:
        return

    # 步骤 2: M5 主信号 (必须有吞没)
    m5_signal = evaluate_interval("5m", kline_cache["5m"], require_engulfing=True)
    if m5_signal is None:
        return

    # 步骤 3: M15 / M30 方向确认 (不需要吞没)
    m15_dir = check_direction_only("15m", kline_cache.get("15m", []))
    m30_dir = check_direction_only("30m", kline_cache.get("30m", []))

    m5_dir = m5_signal["direction"]
    m15_ok = (m15_dir == m5_dir)
    m30_ok = (m30_dir == m5_dir)

    # 步骤 4: 计算评分
    score = calc_signal_score(m5_signal, m15_ok, m30_ok)
    m5_signal["_score"] = score

    # 步骤 5: 阈值判断 (无 M15/M30 共振时要求更高分)
    if not m15_ok and not m30_ok:
        threshold = SIGNAL_SCORE_THRESHOLD + SINGLE_INTERVAL_SCORE_BOOST
    else:
        threshold = SIGNAL_SCORE_THRESHOLD

    if score < threshold:
        return

    # 步骤 6: 信号去重 (30 分钟 + 价格档)
    if is_duplicate("5m", m5_dir, m5_signal["current_price"]):
        return

    # 步骤 7: 计算交易位 + 推送飞书
    levels = calc_trade_levels(m5_signal)
    msg = format_signal_msg(m5_signal, levels, m15_ok, m30_ok)
    push_feishu(msg)
    # 统一入库（字段齐全，直通）；信号时间 = 5m K 线 close_time（毫秒）
    _close_time = kline_cache["5m"][-1].get("close_time")
    _signal_time = datetime.fromtimestamp(_close_time / 1000).strftime("%Y-%m-%d %H:%M:%S") if _close_time else datetime.now()
    signal_store.record_signal(
        signal_time=_signal_time, symbol=SYMBOL, interval="5m",
        pattern="PAXG-吞没+位置", direction=m5_signal["direction"],
        entry=levels["entry"], sl=levels["stop_loss"],
        tp1=levels["tp1"], tp2=levels["tp2"],
    )
    mark_notified("5m", m5_dir, m5_signal["current_price"])
    log(f"✅ M5 {m5_dir} 信号已推送 (评分 {score}, M15:{m15_ok} M30:{m30_ok})")


# ==============================================================================
# Section 9: 主入口
# ==============================================================================

def main():
    log("=" * 60)
    log(" 实时信号推送系统 v1.0 启动")
    log(f"扫描周期:{INTERVALS}")
    log(f"扫描间隔:{SCAN_INTERVAL_SEC} 秒")
    log(f"数据源:{SYMBOL} + {PRICE_OFFSET} = ")
    log(f"绿线参数:Hull MA({HULL_FAST_PERIOD}, {HULL_SLOW_PERIOD})")
    log(f"止损区间:{MIN_STOP_DISTANCE} ~ {MAX_STOP_DISTANCE} 美元")
    log("=" * 60)

    push_feishu(
        f"✅ 信号系统已启动\n"
        f"扫描周期:{INTERVALS}\n"
        f"数据源:{SYMBOL} + {PRICE_OFFSET} = \n"
        f"绿线:Hull MA({HULL_FAST_PERIOD}, {HULL_SLOW_PERIOD})"
    )

    while True:
        try:
            scan()
        except KeyboardInterrupt:
            log("用户中断,系统退出")
            break
        except Exception as e:
            log(f"主循环异常:{e}")
            import traceback
            traceback.print_exc()

        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
