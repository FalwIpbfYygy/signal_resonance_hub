# -*- coding: utf-8 -*-
"""
================================================================================
PAXGUSDT 高胜算形态 —— 回测脚本（基于 x80_回测框架 的骨架）

设计原则：
1) 回测框架骨架沿用：周分片遍历 / 冷却去重 / 盈亏模拟 / 分周报告 + 分周期 stat_*.txt
2) 数据接口沿用实盘本来的：币安现货 klines（与 data.py 同源），crypto 24/7 全量，
   不过滤周末（实盘 data.py 也不过滤）。
3) 真正的信号逻辑与 high_winss_S1 实盘代码一致：
   build_ctx + run_all_detectors（patterns.py）
   + 高一级周期趋势过滤 + 分级（engine.scan_interval 同款）
   + 冷却去重（COOLDOWN_BARS * 单根秒数）

仅做回测，不推送飞书/邮件，不入库。
================================================================================
"""

import os
import sys
import time
import gc
from datetime import datetime, timedelta, timezone

# ===================== 复用实盘模块（同目录） =====================
import config
from indicators import trend_direction
from patterns import build_ctx, run_all_detectors


# utils.py
from functools import wraps
from datetime import datetime


def timer(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_dt = datetime.now()
        result = func(*args, **kwargs)
        end_dt = datetime.now()
        cost_sec = (end_dt - start_dt).total_seconds()

        # 换算 时:分:秒.毫秒
        hours = int(cost_sec // 3600)
        minutes = int((cost_sec % 3600) // 60)
        remain_sec = cost_sec % 60

        print(f"【开始执行】{func.__name__}")
        print(f"开始北京时间：{start_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
        print(f"【执行结束】{func.__name__}")
        print(f"结束北京时间：{end_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
        print(f"【运行耗时】{func.__name__}: {hours:02d}时{minutes:02d}分{remain_sec:.3f}秒（总计 {cost_sec:.6f} 秒）\n")
        return result
    return wrapper

# ===================== 代理配置（与 high_main.py 一致） =====================
BACKTEST_PROXY = os.environ.get("BACKTEST_PROXY", "")
if BACKTEST_PROXY:
    os.environ["HTTP_PROXY"] = BACKTEST_PROXY
    os.environ["HTTPS_PROXY"] = BACKTEST_PROXY

# ===================== 回测全局配置 =====================
SYMBOL = config.SYMBOL
BINANCE_URL = config.BASE_URL

BACKTEST_START_DATE = "2025-01-01 00:00:00"   # 可自行调整回测起点
PRINT_EVERY_SIGNAL = True
TRACK_PNL = True
LOT_SIZE = 0.05
CONTRACT_SIZE = 100
MAX_FUTURE_BARS = 100               # 观测窗口（根），用于模拟止盈止损
DEDUP_COOLDOWN_SEC = None           # 实际用 config.COOLDOWN_BARS * 单根秒数，这里仅占位

# 信号逻辑相关（直接取自实盘 config，保证一致）
SIGNAL_INTERVALS = config.SIGNAL_INTERVALS      # ["30m", "1h", "4h"]
TREND_INTERVALS = config.TREND_INTERVALS        # 各周期对应的趋势背景周期
LIMIT = config.LIMIT                            # 每根扫描回看根数（与实盘一致）
COOLDOWN_BARS = config.COOLDOWN_BARS            # 同方向冷却根数

# 回测需要拉取的全部周期 = 信号周期 ∪ 趋势周期
_NEEDED_INTERVALS = list(set(SIGNAL_INTERVALS) | set(TREND_INTERVALS.values()))

OUTPUT_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_output")
SIGNAL_LOG_FILE = os.path.join(OUTPUT_FOLDER, "all_signals_log.txt")
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# 分周期统计容器（按实盘信号周期动态生成）
global_stats = {
    iv: {"total": 0, "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0}
    for iv in SIGNAL_INTERVALS
}


def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}")


# ==============================================================================
# 时间工具
# ==============================================================================
def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000)


def get_next_monday(dt: datetime) -> datetime:
    days_ahead = (7 - dt.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (dt + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)


def get_week_filename(week_start: datetime, week_end: datetime) -> str:
    year = week_start.year
    _, week_num, _ = week_start.isocalendar()
    s_str = week_start.strftime("%Y%m%d")
    e_str = week_end.strftime("%Y%m%d")
    name = f"backtest_week_{year}_W{week_num:02d}_{s_str}-{e_str}.txt"
    return os.path.join(OUTPUT_FOLDER, name)


def _bar_seconds(interval: str) -> int:
    """周期字符串转秒数。"""
    unit = interval[-1]
    num = int(interval[:-1])
    return num * {"m": 60, "h": 3600, "d": 86400}.get(unit, 60)


# ==============================================================================
# 数据接口（沿用实盘：币安 klines，全量，不过滤周末）
# ==============================================================================
def fetch_historical_klines_range(interval: str, range_start_ms: int, range_end_ms: int, retries: int = 5):
    """按时间区间分页拉取历史 K 线，返回 bar 字典列表（含 volume）。

    与实盘 data.py 同源：币安现货接口、字段一致；crypto 24/7 全量不过滤周末。
    """
    all_klines = []
    current_start = range_start_ms
    import requests

    while current_start < range_end_ms:
        url = (f"{BINANCE_URL}?symbol={SYMBOL}&interval={interval}"
               f"&startTime={current_start}&limit=1000")
        done = False
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(url, timeout=config.REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    log(f"[{interval}] HTTP {resp.status_code} 请求失败，重试 {attempt}")
                    time.sleep(1)
                    continue
                data = resp.json()
                if not data:
                    done = True
                    break
                for k in data:
                    all_klines.append({
                        "open_time": int(k[0]),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "close_time": int(k[6]),
                    })
                last_close_ms = data[-1][6]
                next_start = last_close_ms + 1
                if next_start >= range_end_ms:
                    done = True
                    break
                current_start = next_start
                time.sleep(0.22)
                break
            except Exception as e:
                log(f"[{interval}] 拉取异常: {e}，重试 {attempt}")
                time.sleep(1)
        if done:
            break

    # 去重 + 按时间排序
    seen = set()
    unique = []
    for k in all_klines:
        ot = k["open_time"]
        if ot not in seen:
            seen.add(ot)
            unique.append(k)
    unique.sort(key=lambda x: x["open_time"])
    return unique


# ==============================================================================
# 趋势背景（与 engine._trend 一致：取高一级周期，窗口内线性回归斜率符号）
# ==============================================================================
def _build_trend_series(bars):
    """对某一周期每根 K 线，预计算其收盘时的趋势方向（1/-1/0）。"""
    series = []
    n = len(bars)
    for j in range(n):
        start = max(0, j + 1 - LIMIT)
        window = bars[start:j + 1]
        t = 0
        if len(window) >= 50:
            closes = [b["close"] for b in window][-50:]
            t = trend_direction(closes, 50)
        series.append((bars[j]["close_time"], t))
    return series


def _trend_lookup(series, curr_time_ms, ptr_holder):
    """取 curr_time_ms 之前最近一次的趋势方向（指针推进，O(1)）。"""
    ptr = ptr_holder[0]
    while ptr < len(series) and series[ptr][0] <= curr_time_ms:
        ptr += 1
    ptr_holder[0] = ptr
    if ptr > 0:
        return series[ptr - 1][1]
    return 0


# ==============================================================================
# 分级（与 engine._grade 完全一致）
# ==============================================================================
def _grade(sig, trend_ok):
    aligned = trend_ok and sig.get("vol_ok", False)
    touches = sig.get("touches", 0)
    if aligned and touches >= 4:
        return config.GRADE_STRONGEST
    if aligned:
        return config.GRADE_STRONG
    return config.GRADE_GENERAL


def _trend_ok(direction, trend):
    return trend != 0 and ((direction == 1 and trend == 1) or (direction == -1 and trend == -1))


# ==============================================================================
# 信号方向校验（修复「TP/止损方向写反」导致的假盈利）
# ==============================================================================
def validate_signal(sig):
    """校验入场/止损/TP1 的方向关系是否自洽。

    做多必须  tp1 > 入场 > 止损；做空必须 tp1 < 入场 < 止损。
    返回 (is_valid, reason)。
    """
    entry = sig["entry"]
    sl = sig["stop_loss"]
    tp1 = sig["tp1"]
    is_buy = sig["direction"] == "BUY"
    if is_buy:
        if not (sl < entry):
            return False, "止损高于入场"
        if not (tp1 > entry):
            return False, "TP1 低于或等于入场"
    else:
        if not (sl > entry):
            return False, "止损低于入场"
        if not (tp1 < entry):
            return False, "TP1 高于或等于入场"
    return True, ""


# ==============================================================================
# 盈亏模拟（真实分批盈亏：70% 走 TP1，30% 跟踪 TP2/止损，修复「盈亏恒为 ±1R」）
# ==============================================================================
def simulate_trade(signal, future_bars):
    """按「推 70% TP1 / 留 30% TP2」真实核算盈亏，不再把每次止盈记成 +1R。

    返回 (result, pnl)：
        win   = 70% 在 TP1 止盈 + 30% 在 TP2 止盈（或回撤止损/到期保本）
        loss  = 全仓止损
        timeout = 观测窗口内未触碰
        invalid = 方向自洽校验失败（上层应跳过，不计入统计）
    """
    entry = signal["entry"]
    sl = signal["stop_loss"]
    tp1 = signal["tp1"]
    tp2 = signal.get("tp2")
    sd = signal["stop_distance"]
    is_buy = signal["direction"] == "BUY"
    unit = LOT_SIZE * CONTRACT_SIZE  # 每点美元价值（0.05 手 × 100 盎司 = $5/点）

    ok, reason = validate_signal(signal)
    if not ok:
        return "invalid", 0.0

    if not future_bars:
        return "timeout", 0.0

    # 1) 找第一个触发事件（止损或 TP1），同根 K 线同时触发时按止损优先（保守）
    first_kind, first_idx = None, -1
    for idx, bar in enumerate(future_bars):
        if is_buy:
            hit_sl = bar["low"] <= sl
            hit_tp1 = bar["high"] >= tp1
        else:
            hit_sl = bar["high"] >= sl
            hit_tp1 = bar["low"] <= tp1
        if hit_sl and hit_tp1:
            first_kind, first_idx = "sl", idx  # 同根触发，保守按止损
            break
        if hit_sl:
            first_kind, first_idx = "sl", idx
            break
        if hit_tp1:
            first_kind, first_idx = "tp1", idx
            break

    if first_kind is None:
        return "timeout", 0.0

    # 2) 先触止损 → 全仓止损
    if first_kind == "sl":
        return "loss", -sd * unit

    # 3) 先触 TP1 → 70% 在 TP1 止盈，30% 继续跟踪 TP2 / 止损 / 到期
    pnl70 = (tp1 - entry) * 0.70 * unit if is_buy else (entry - tp1) * 0.70 * unit

    pnl30 = 0.0
    exited30 = False
    for bar in future_bars[first_idx + 1:]:
        if is_buy:
            hit_sl30 = bar["low"] <= sl
            hit_tp2 = (tp2 is not None) and (bar["high"] >= tp2)
        else:
            hit_sl30 = bar["high"] >= sl
            hit_tp2 = (tp2 is not None) and (bar["low"] <= tp2)
        if hit_sl30:
            pnl30 = -sd * 0.30 * unit
            exited30 = True
            break
        if hit_tp2:
            pnl30 = (tp2 - entry) * 0.30 * unit if is_buy else (entry - tp2) * 0.30 * unit
            exited30 = True
            break
    # 30% 部分未再触发任何事件 → 按到期保本（0 收益）处理，不再虚增
    return "win", pnl70 + pnl30


# ==============================================================================
# 文本输出（沿用框架格式）
# ==============================================================================
def build_signal_text(sig, gen_iv, sig_time, pnl_result=None):
    score = sig["score"]
    star = "⭐⭐⭐" if score >= 90 else "⭐⭐" if score >= 80 else "⭐"
    direction_cn = "做多 BUY" if sig["direction"] == "BUY" else "做空 SELL"

    pattern_name = sig.get("pattern") or "未知形态"
    detail_text = sig.get("detail") or ""
    quality_tag = sig.get("quality") or ""

    lines = []
    lines.append(f"\n{'=' * 70}")
    lines.append(f"🚨 [{gen_iv}] {sig_time.strftime('%Y-%m-%d %H:%M')} | "
                 f"{sig['direction']} | 价格 {sig['current_price']:.2f} | "
                 f"评分 {score} {star} | 形态: {pattern_name}")
    lines.append(f"   方向: {direction_cn}")
    lines.append(f"   形态: {pattern_name}（{quality_tag}）{('— ' + detail_text) if detail_text else ''}")
    lines.append(f"   放量: {'是' if sig.get('vol_ok') else '否'}  接触点: {sig.get('touches', 0)}")
    lines.append(f"   入场: {sig['entry']:.2f}  止损: {sig['stop_loss']:.2f}  "
                 f"距离: {sig['stop_distance']:.2f}")
    lines.append(f"   TP1:  {sig['tp1']:.2f} (推 70%)   TP2: {sig['tp2']:.2f} (留 30%)")

    if pnl_result is not None:
        result, pnl = pnl_result
        if result == "win":
            lines.append(f"   💰 结果: ✅ 触及TP1  盈亏 +${pnl:.2f}")
        elif result == "loss":
            lines.append(f"   💸 结果: ❌ 触及止损  盈亏 -${abs(pnl):.2f}")
        else:
            lines.append(f"   ⏳ 结果: 观测周期内未触碰止盈止损({MAX_FUTURE_BARS}根K线)")
    return "\n".join(lines)


def build_summary_text(signals, total_pnl, wins, losses, timeouts, week_start, week_end,
                       dedup_skipped=0, skip_count=0, invalid_count=0):
    completed = wins + losses
    win_rate_display = (wins / completed * 100) if completed > 0 else 0.0
    lines = []
    lines.append(f"\n{'=' * 70}")
    lines.append(f"📊 单周回测汇总")
    lines.append(f"周区间: {week_start.strftime('%Y-%m-%d')} ~ {week_end.strftime('%Y-%m-%d')}")
    lines.append(f"{'=' * 70}")
    lines.append(f"  有效信号总数(去重后): {len(signals)} 条")
    lines.append(f"  信号去重拦截次数:     {dedup_skipped} 次")
    lines.append(f"  无信号跳过:           {skip_count} 次")
    lines.append(f"  方向自洽校验拦截:     {invalid_count} 次(TP/止损方向写反)")
    lines.append(f"  完整成交笔数:         {completed} 笔 (盈利 {wins} / 亏损 {losses})")
    lines.append(f"  未触发止盈止损:       {timeouts} 次")
    lines.append(f"  胜率(仅完整成交):     {win_rate_display:.2f}%")
    lines.append(f"  本周累计盈亏:         ${total_pnl:+.2f}")
    lines.append(f"{'=' * 70}")
    return "\n".join(lines)


def _to_fw_sig(sig, curr_bar):
    """把实盘信号 dict 转成框架需要的信号 dict（字段对齐 build_signal_text）。"""
    direction = "BUY" if sig["direction"] == 1 else "SELL"
    entry = sig["entry"]
    stop = sig["stop"]
    score = 95 if sig.get("quality") == config.GRADE_STRONGEST else 85
    return {
        "direction": direction,
        "current_price": entry,
        "entry": entry,
        "stop_loss": stop,
        "stop_distance": abs(entry - stop),
        "tp1": sig["target1"],
        "tp2": sig["target2"],
        "score": score,
        # 以下附带实盘原始字段，便于扩展
        "pattern": sig.get("pattern"),
        "quality": sig.get("quality"),
        "detail": sig.get("detail"),
        "touches": sig.get("touches"),
        "vol_ok": sig.get("vol_ok"),
    }


# ==============================================================================
# 单周回测主逻辑
# ==============================================================================
def run_single_week(week_start_dt: datetime, week_end_dt: datetime, cooldown: dict = None):
    if week_start_dt >= week_end_dt:
        log(f"无效时间区间 {week_start_dt} ~ {week_end_dt}，直接跳过")
        return

    log(f"\n==================== 开始回测本周 ====================")
    log(f"时间窗口 {week_start_dt} ~ {week_end_dt}")
    week_start_ms = dt_to_ms(week_start_dt)
    week_end_ms = dt_to_ms(week_end_dt)

    # 1) 拉取本周期需要的全部周期历史 K 线（向前多拉 WARMUP_BARS 根做预热，消除周边界冷启动）
    klines_data = {}
    for interval in _NEEDED_INTERVALS:
        log(f"拉取 [{interval}] K线...")
        warmup_ms = config.WARMUP_BARS * _bar_seconds(interval) * 1000
        klines_data[interval] = fetch_historical_klines_range(interval, week_start_ms - warmup_ms, week_end_ms)
        log(f"[{interval}] 获取K线 {len(klines_data[interval])} 根")

    # 2) 预计算各趋势周期的方向序列（指针查找，O(1)）
    trend_series = {}
    trend_ptrs = {}
    for tiv in set(TREND_INTERVALS.values()):
        trend_series[tiv] = _build_trend_series(klines_data[tiv])
        trend_ptrs[tiv] = [0]

    report_content = []
    report_content.append(f"PAXGUSDT周度回测报告【高胜算形态·实盘逻辑】")
    report_content.append(f"回测区间：{week_start_dt.strftime('%Y-%m-%d')} ~ {week_end_dt.strftime('%Y-%m-%d')}")
    report_content.append("=" * 70)

    signals = []
    wins, losses, timeouts = 0, 0, 0
    total_pnl = 0.0
    dedup_skipped = 0
    skip_count = 0
    invalid_count = 0
    if cooldown is None:
        cooldown = {}                   # {(iv, pattern, direction): 最近信号毫秒}（跨周共享，避免周界重复触发）

    # 3) 遍历每个信号周期，逐根 K 线扫描（与实盘 scan_interval 一致）
    for gen_iv in SIGNAL_INTERVALS:
        src = klines_data[gen_iv]
        if len(src) < 50:
            log(f"[{gen_iv}] K线数量不足，跳过该周期信号扫描")
            continue

        trend_iv = TREND_INTERVALS.get(gen_iv, gen_iv)
        bar_seconds = _bar_seconds(gen_iv)
        cooldown_ms = COOLDOWN_BARS * bar_seconds * 1000

        for i in range(len(src)):
            curr = src[i]
            curr_time = curr["close_time"]

            # 预热段（week_start 之前）不产生信号：仅让指标/趋势进入热状态
            if curr_time < week_start_ms:
                continue

            # 只取当前根及之前已收盘的 K 线，回看上限 LIMIT（防未来函数，与实盘一致）
            start = max(0, i + 1 - LIMIT)
            window = src[start:i + 1]
            if len(window) < 50:
                skip_count += 1
                continue

            ctx = build_ctx(window)
            if ctx["cur_atr"] <= 0:
                skip_count += 1
                continue

            # 趋势背景：高一级周期在 curr_time 之前的趋势方向
            trend = _trend_lookup(trend_series[trend_iv], curr_time, trend_ptrs[trend_iv])

            # 跑全部检测器，逐条按实盘逻辑过滤 + 分级 + 冷却去重
            results = run_all_detectors(ctx)
            if not results:
                skip_count += 1
                continue

            for sig in results:
                trend_ok = _trend_ok(sig["direction"], trend)
                grade = _grade(sig, trend_ok)
                if grade == config.GRADE_GENERAL:
                    continue

                # 形态黑名单：历史回测胜率过低的形态直接跳过
                if sig["pattern"] in config.PATTERN_BLACKLIST:
                    skip_count += 1
                    continue

                # 做空降权：仅允许 >= SHORT_MIN_GRADE 的做空信号
                if sig["direction"] == -1 and config.SHORT_MIN_GRADE != "off":
                    grade_order = {config.GRADE_STRONG: 1, config.GRADE_STRONGEST: 2}
                    min_lv = grade_order.get(config.SHORT_MIN_GRADE, 0)
                    cur_lv = grade_order.get(grade, 0)
                    if cur_lv < min_lv:
                        skip_count += 1
                        continue

                sig["quality"] = grade

                cd_key = (gen_iv, sig["pattern"], sig["direction"])
                last = cooldown.get(cd_key, 0)
                if curr_time - last < cooldown_ms:
                    dedup_skipped += 1
                    continue
                cooldown[cd_key] = curr_time

                fw_sig = _to_fw_sig(sig, curr)

                # 方向自洽校验：TP/止损方向写反的信号跳过，不计入统计（修复假盈利）
                ok, _reason = validate_signal(fw_sig)
                if not ok:
                    invalid_count += 1
                    continue

                signals.append(fw_sig)

                pnl_result = None
                if TRACK_PNL:
                    future = src[i + 1:i + 1 + MAX_FUTURE_BARS]
                    pnl_result = simulate_trade(fw_sig, future)
                    res_tag, pnl = pnl_result
                    total_pnl += pnl
                    g = global_stats[gen_iv]
                    g["total"] += 1
                    g["pnl"] += pnl
                    if res_tag == "win":
                        wins += 1
                        g["win"] += 1
                    elif res_tag == "loss":
                        losses += 1
                        g["loss"] += 1
                    else:
                        timeouts += 1
                        g["timeout"] += 1

                sig_text = build_signal_text(fw_sig, gen_iv, ms_to_dt(curr_time), pnl_result)
                report_content.append(sig_text)
                with open(SIGNAL_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(sig_text + "\n")
                if PRINT_EVERY_SIGNAL:
                    print(sig_text)

    summary_text = build_summary_text(signals, total_pnl, wins, losses, timeouts,
                                      week_start_dt, week_end_dt, dedup_skipped, skip_count, invalid_count)
    report_content.append(summary_text)
    full_report = "\n".join(report_content)

    file_path = get_week_filename(week_start_dt, week_end_dt)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(full_report)
    log(f"\n✅ 本周回测完成，报告保存路径：{file_path}")
    log("=" * 60)

    del klines_data, report_content, signals
    gc.collect()


# ==============================================================================
# 分周期统计输出
# ==============================================================================
def export_global_statistics():
    for iv in SIGNAL_INTERVALS:
        s = global_stats[iv]
        total = s["total"]
        win = s["win"]
        loss = s["loss"]
        timeout = s["timeout"]
        net_pnl = s["pnl"]
        completed = win + loss
        win_rate = (win / completed * 100) if completed > 0 else 0.0

        content = f"""==================== {iv} 全局完整统计 ====================
总信号数量：{total}
盈利笔数：{win}
亏损笔数：{loss}
未触发(观测期无触碰)：{timeout}
完整成交笔数：{completed}
胜率(仅统计完成止盈止损)：{win_rate:.2f} %
累计盈亏：${net_pnl:+.2f}
===========================================================
"""
        filename = os.path.join(OUTPUT_FOLDER, f"stat_{iv}.txt")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"✅ 已生成 {filename}")


# ==============================================================================
# 主循环
# ==============================================================================
@timer
def main():
    log("PAXGUSDT 高胜算形态 回测框架启动（信号逻辑复用实盘代码）")
    start_dt = datetime.strptime(BACKTEST_START_DATE, "%Y-%m-%d %H:%M:%S")
    end_dt = datetime.now()

    # 启动时清空信号汇总日志，避免多次运行重复累计（历史数据可从周报重建）
    if config.CLEAR_SIGNAL_LOG_ON_START and os.path.exists(SIGNAL_LOG_FILE):
        with open(SIGNAL_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
        log(f"已清空信号汇总日志：{SIGNAL_LOG_FILE}")

    current_week_start = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    week_count = 0
    cooldown = {}   # 跨周共享冷却，避免周界处同形态重复触发

    while current_week_start < end_dt:
        current_week_end = get_next_monday(current_week_start)
        if current_week_end > end_dt:
            current_week_end = end_dt

        week_count += 1
        try:
            run_single_week(current_week_start, current_week_end, cooldown)
        except Exception as e:
            log(f"【{current_week_start} ~ {current_week_end}】单周回测发生异常 {e}")
            import traceback
            traceback.print_exc()

        current_week_start = current_week_end

    log(f"\n==================== 全部周期执行完毕 ====================")
    log(f"总共处理 {week_count} 个周")
    log(f"所有周报存放目录: {os.path.abspath(OUTPUT_FOLDER)}")
    log(f"全部信号汇总日志文件: {os.path.abspath(SIGNAL_LOG_FILE)}")
    export_global_statistics()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("用户手动终止程序")
    except Exception as e:
        log(f"全局异常:{e}")
        import traceback
        traceback.print_exc()
