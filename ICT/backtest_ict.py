"""
================================================================================
ICT  回测框架 v1.0
基于 x80_回测框架.py 架构，接入 ICT 策略信号引擎
================================================================================
"""
import os
import time
import gc
from datetime import datetime, timedelta, timezone
import requests

# 修复 Windows GBK 编码下的 emoji 输出问题（Python 3.7+）
import sys





sys.stdout.reconfigure(encoding='utf-8')

# ===================== 路径 & 代理 =====================
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BACKTEST_PROXY = os.environ.get("BACKTEST_PROXY", "")
if BACKTEST_PROXY:
    os.environ["HTTP_PROXY"] = BACKTEST_PROXY
    os.environ["HTTPS_PROXY"] = BACKTEST_PROXY

# ===================== ICT 模块导入 =====================
from data.candle import Candle, KeyLevel
from indicators.swing import find_swings
from indicators.fvg import find_fvgs, check_fvg_inversion
from indicators.csd import get_latest_csd, has_bearish_csd, has_bullish_csd
from indicators.atr import detect_sfp, detect_liquidity_sweep
from levels.time_levels import (
    mark_asia_london_levels, mark_previous_daily_levels,
    mark_weekly_levels, mark_fvg_swing_levels,
)
from utils.time_utils import get_session_gmt, get_min_confirmations

# ==============================================================================
# 【全局回测配置区】
# ==============================================================================
SYMBOL = ""
BINANCE_URL = os.environ.get("BINANCE_FUTURES_KLINE_URL")

BACKTEST_START_DATE = "2026-01-01 00:00:00"
PRINT_EVERY_SIGNAL = True
TRACK_PNL = True
LOT_SIZE = 0.05            # 0.05手
CONTRACT_SIZE = 100         # 100盎司/手
MAX_FUTURE_BARS = 100       # 信号后最多观测K线数
DEDUP_COOLDOWN_SEC = 7200   # 2小时去重（同一级别连续触发问题）
PRICE_BUCKET_SIZE = 2.0     # 保留兼容

# 回测时间框架：
#   gen_iv: 信号生成周期
#   确认用 5m（替代实盘 1m，减少数据量）
#   高级别用 1h / 4h / 1d
SIGNAL_GEN_INTERVALS = ["15m"]
INTERVALS = ["5m", "15m", "1h", "4h", "1d"]

# ---- 输出 ----
OUTPUT_FOLDER = os.path.join(os.path.dirname(__file__), "backtest_output")
SIGNAL_LOG_FILE = os.path.join(OUTPUT_FOLDER, "ict_all_signals_log.txt")
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ---- 全局统计 ----
global_stats = {}
for iv in SIGNAL_GEN_INTERVALS:
    global_stats[iv] = {"total": 0, "win": 0, "loss": 0, "timeout": 0, "pnl": 0.0}

# ---- 简易日志 ----
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

def is_weekend_ms(ts_ms: int) -> bool:
    dt = ms_to_dt(ts_ms)
    return dt.weekday() >= 5

def get_week_filename(week_start: datetime, week_end: datetime) -> str:
    year = week_start.year
    _, week_num, _ = week_start.isocalendar()
    s_str = week_start.strftime("%Y%m%d")
    e_str = week_end.strftime("%Y%m%d")
    name = f"ict_backtest_week_{year}_W{week_num:02d}_{s_str}-{e_str}.txt"
    return os.path.join(OUTPUT_FOLDER, name)

# ==============================================================================
# K线拉取 & 解析
# ==============================================================================
def parse_klines(raw_klines):
    """将原始K线数组转为 Candle 对象列表"""
    UTC = timezone.utc
    out = []
    for k in raw_klines:
        c = Candle(
            open_time=datetime.fromtimestamp(k[0] / 1000.0, tz=UTC),
            close_time=datetime.fromtimestamp(k[6] / 1000.0, tz=UTC),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
            quote_volume=float(k[7]),
            trades=int(k[8]),
            taker_buy_volume=float(k[9]),
        )
        out.append(c)
    return out


def fetch_historical_klines_range(interval: str, range_start_ms: int, range_end_ms: int):
    """拉取历史K线，返回原始数组"""
    all_klines = []
    current_start = range_start_ms

    while current_start < range_end_ms:
        url = (f"{BINANCE_URL}?symbol={SYMBOL}&interval={interval}"
               f"&startTime={current_start}&limit=1500")
        try:
            resp = requests.get(url)
            if resp.status_code != 200:
                log(f"[{interval}] HTTP {resp.status_code} 请求失败")
                break
            data = resp.json()
            if not data:
                break
            all_klines.extend(data)
            last_close_ms = data[-1][6]
            next_start = last_close_ms + 1
            if next_start >= range_end_ms:
                break
            current_start = next_start
            time.sleep(0.22)
        except Exception as e:
            log(f"[{interval}] 拉取异常: {e}")
            break

    # 去重 + 排序 + 过滤周末
    seen = set()
    unique = []
    for k in all_klines:
        ot = k[0]
        if ot not in seen:
            seen.add(ot)
            unique.append(k)
    unique.sort(key=lambda x: x[0])
    return [k for k in unique if not is_weekend_ms(k[0])]


def slice_candles_up_to(candles: list, close_time_ms: int) -> list:
    """截取Candle列表到指定时间（防未来泄漏）"""
    return [c for c in candles if c.close_time.timestamp() * 1000 <= close_time_ms]


# ==============================================================================
# 【ICT 信号生成 - 回测版】
# 对每个时间点，用当前窗口内数据计算指标，检测信号
# ==============================================================================
def generate_ict_signal(data: dict, bar_index: int, gen_iv: str):
    """
    回测信号生成（防未来泄漏）
    data: {interval: [Candle, ...]}  全部周期已解析的 Candle 列表
    bar_index: 当前 gen_iv 周期的K线索引
    gen_iv: 信号生成周期，如 "15m"

    返回: TradeSignal 或 None
    """
    candles_15m = data.get("15m", [])
    candles_5m = data.get("5m", [])
    candles_1h = data.get("1h", [])
    candles_4h = data.get("4h", [])
    candles_1d = data.get("1d", [])

    if bar_index >= len(candles_15m) or bar_index < 50:
        return None

    current_bar = candles_15m[bar_index]
    current_price = current_bar.close
    current_time = current_bar.close_time
    current_time_ms = int(current_time.timestamp() * 1000)

    # ---- 截取各周期当前可见数据（防未来泄漏） ----
    vis_15m = candles_15m[:bar_index + 1]
    vis_5m = slice_candles_up_to(candles_5m, current_time_ms)
    vis_1h = slice_candles_up_to(candles_1h, current_time_ms)
    vis_4h = slice_candles_up_to(candles_4h, current_time_ms)
    vis_1d = slice_candles_up_to(candles_1d, current_time_ms)

    if len(vis_5m) < 30 or len(vis_15m) < 30:
        return None

    # ---- 计算所有级别 ----
    levels = []

    # 时间级别：亚洲盘/伦敦盘
    levels.extend(mark_asia_london_levels(vis_15m))

    # 前日高低
    if vis_1d:
        levels.extend(mark_previous_daily_levels(vis_1d))
        levels.extend(mark_weekly_levels(vis_1d))

    # 高级别 FVG + Swing
    for tf, candles_tf in [("1h", vis_1h), ("4h", vis_4h), ("1d", vis_1d)]:
        if len(candles_tf) >= 3:
            swings_tf = find_swings(candles_tf)
            fvgs_tf = find_fvgs(candles_tf, swings_tf)
            levels.extend(mark_fvg_swing_levels(candles_tf, tf, swings_tf, fvgs_tf))

    if not levels:
        return None

    # ---- 级别触及检测 ----
    buffer_pct = 0.05
    buffer = current_price * buffer_pct / 100.0
    touched_levels = []
    for lv in levels:
        if abs(current_price - lv.price) <= buffer:
            lv.status = "TOUCHED"
            touched_levels.append(lv)

    if not touched_levels:
        return None

    # ---- 确认信号计算（用5m图替代1m图） ----
    swings_5m = find_swings(vis_5m)
    fvgs_5m = find_fvgs(vis_5m, swings_5m)
    latest_csd = get_latest_csd(vis_5m)
    sfp_result = detect_sfp(vis_5m, swings_5m)

    session_name, session_weight = get_session_gmt(current_time)
    min_confirm = get_min_confirmations(session_weight)

    # ---- 遍历已触及级别，生成信号 ----
    best_signal = None
    best_strength = 0

    for level in touched_levels:
        if not level.is_support:  # 阻力位 → 空头信号
            confirmations, strength = _check_sell_confirmations(
                vis_5m, fvgs_5m, latest_csd, sfp_result)
        else:  # 支撑位 → 多头信号
            confirmations, strength = _check_buy_confirmations(
                vis_5m, fvgs_5m, latest_csd, sfp_result)

        if len(confirmations) < min_confirm:
            continue

        if strength > best_strength:
            # 计算止盈止损
            entry_price = current_price
            stop_loss = _calc_stop_loss(vis_5m, level, entry_price)
            take_profit = _calc_take_profit(level, entry_price, stop_loss, levels, touched_levels)

            best_strength = strength
            # 转换为回测框架兼容的信号格式
            best_signal = {
                "direction": "SELL" if not level.is_support else "BUY",
                "current_price": current_price,
                "entry": entry_price,
                "stop_loss": stop_loss,
                "stop_distance": abs(entry_price - stop_loss),
                "tp1": take_profit,
                "tp2": take_profit * 1.02 if not level.is_support else take_profit * 0.98,
                "score": strength * 20,  # 1-5 → 20-100
                "signal_type": _build_signal_type(confirmations),
                "confirmations": confirmations,
                "level_name": level.name,
                "level_price": level.price,
                "session": session_name,
                "strength": strength,
            }

    return best_signal


def _check_sell_confirmations(candles_5m, fvgs_5m, latest_csd, sfp_result):
    """空头确认信号检查"""
    confirmations = []
    strength = 0

    if detect_liquidity_sweep(candles_5m, "bearish", lookback=15):
        confirmations.append("流动性清扫(扫过高点)")
        strength += 1

    if latest_csd and latest_csd.direction == -1:
        confirmations.append("空头CSD")
        strength += 1
    elif has_bearish_csd(candles_5m, lookback=3):
        confirmations.append("空头CSD(近期)")
        strength += 1

    inverted = check_fvg_inversion(fvgs_5m, candles_5m)
    if any(f.direction == 1 for f in inverted):
        confirmations.append("反转FVG(多头FVG被反转)")
        strength += 1

    if sfp_result.get("bearish_sfp"):
        confirmations.append("空头SFP")
        strength += 1

    for fvg in fvgs_5m[-3:]:
        if fvg.is_bsg and fvg.direction == -1:
            confirmations.append("BSG(突破结构缺口)")
            strength += 2
            break

    return confirmations, min(strength, 5)


def _check_buy_confirmations(candles_5m, fvgs_5m, latest_csd, sfp_result):
    """多头确认信号检查"""
    confirmations = []
    strength = 0

    if detect_liquidity_sweep(candles_5m, "bullish", lookback=15):
        confirmations.append("流动性清扫(扫过低点)")
        strength += 1

    if latest_csd and latest_csd.direction == 1:
        confirmations.append("多头CSD")
        strength += 1
    elif has_bullish_csd(candles_5m, lookback=3):
        confirmations.append("多头CSD(近期)")
        strength += 1

    inverted = check_fvg_inversion(fvgs_5m, candles_5m)
    if any(f.direction == -1 for f in inverted):
        confirmations.append("反转FVG(空头FVG被反转)")
        strength += 1

    if sfp_result.get("bullish_sfp"):
        confirmations.append("多头SFP")
        strength += 1

    for fvg in fvgs_5m[-3:]:
        if fvg.is_bsg and fvg.direction == 1:
            confirmations.append("BSG(突破结构缺口)")
            strength += 2
            break

    return confirmations, min(strength, 5)


def _calc_stop_loss(candles_5m, level, entry_price):
    """计算止损价格"""
    swings_5m = find_swings(candles_5m)
    if not level.is_support:  # 空头 → 止损在级别上方Swing High
        for s in reversed(swings_5m):
            if s.is_high and s.price > level.price:
                return s.price * 1.001
        return level.price * 1.003
    else:  # 多头 → 止损在级别下方Swing Low
        for s in reversed(swings_5m):
            if not s.is_high and s.price < level.price:
                return s.price * 0.999
        return level.price * 0.997


def _calc_take_profit(level, entry_price, stop_loss, all_levels, touched_levels):
    """计算止盈目标：找下一个未触及的相反方向级别"""
    risk = abs(entry_price - stop_loss)
    # 默认 1.5R
    if not level.is_support:
        return entry_price - risk * 1.5
    else:
        return entry_price + risk * 1.5


def _build_signal_type(confirmations):
    types = []
    for kw in ["BSG", "反转FVG", "CSD", "SFP", "流动性清扫"]:
        if any(kw in c for c in confirmations):
            types.append(kw)
    return " + ".join(types) if types else "未分类"


# ==============================================================================
# 盈亏模拟（同 x80 框架）
# ==============================================================================
def simulate_trade(signal, future_bars):
    if not future_bars:
        return "timeout", 0.0
    entry = signal["entry"]
    sl = signal["stop_loss"]
    tp1 = signal["tp1"]
    sd = signal["stop_distance"]
    is_buy = signal["direction"] == "BUY"
    pnl_unit = sd * 1.0 * LOT_SIZE * CONTRACT_SIZE

    for bar in future_bars:
        if is_buy:
            hit_sl = bar["low"] <= sl
            hit_tp1 = bar["high"] >= tp1
        else:
            hit_sl = bar["high"] >= sl
            hit_tp1 = bar["low"] <= tp1

        if hit_sl and hit_tp1:
            return "loss", -pnl_unit
        if hit_sl:
            return "loss", -pnl_unit
        if hit_tp1:
            return "win", pnl_unit
    return "timeout", 0.0


# ==============================================================================
# 文本输出格式化
# ==============================================================================
def build_signal_text(sig, gen_iv, sig_time, pnl_result=None):
    score = sig["score"]
    star = "⭐⭐⭐" if score >= 80 else "⭐⭐" if score >= 60 else "⭐"
    direction_cn = "做多 BUY" if sig["direction"] == "BUY" else "做空 SELL"
    session = sig.get("session", "N/A")

    lines = []
    lines.append(f"\n{'=' * 70}")
    lines.append(f"[ICT-{gen_iv}] {sig_time.strftime('%Y-%m-%d %H:%M')} | "
                 f"{sig['direction']} | ${sig['current_price']:.2f} | "
                 f"评分 {score} {star}")
    lines.append(f"   方向: {direction_cn}")
    lines.append(f"   时段: {session} | 级别: {sig['level_name']} @ ${sig['level_price']:.2f}")
    lines.append(f"   信号类型: {sig['signal_type']} | 强度: {sig['strength']}/5")
    lines.append(f"   入场: {sig['entry']:.2f}  止损: {sig['stop_loss']:.2f}  "
                 f"距离: {sig['stop_distance']:.2f}")
    lines.append(f"   TP1:  {sig['tp1']:.2f}")

    if pnl_result is not None:
        result, pnl = pnl_result
        if result == "win":
            lines.append(f"   [WIN] 触及TP1  盈亏 +${pnl:.2f}")
        elif result == "loss":
            lines.append(f"   [LOSS] 触及止损  盈亏 -${abs(pnl):.2f}")
        else:
            lines.append(f"   [TIMEOUT] 观测期内未触碰止盈止损({MAX_FUTURE_BARS}根K线)")
    return "\n".join(lines)


def build_summary_text(signals, total_pnl, wins, losses, timeouts,
                       week_start, week_end, dedup_skipped=0, skip_count=0,
                       level_consumed_skipped=0):
    completed = wins + losses
    win_rate_display = (wins / completed * 100) if completed > 0 else 0.0
    lines = []
    lines.append(f"\n{'=' * 70}")
    lines.append(f"ICT 单周回测汇总")
    lines.append(f"周区间: {week_start.strftime('%Y-%m-%d')} ~ {week_end.strftime('%Y-%m-%d')}")
    lines.append(f"{'=' * 70}")
    lines.append(f"  有效信号总数(去重后): {len(signals)} 条")
    lines.append(f"  去重策略: 级别名称+方向 | 冷却: {DEDUP_COOLDOWN_SEC // 3600}小时 | 交易完成后级别消耗")
    lines.append(f"  时间冷却拦截:         {dedup_skipped} 次")
    lines.append(f"  级别已消耗拦截:       {level_consumed_skipped} 次")
    lines.append(f"  无信号跳过:           {skip_count} 次")
    lines.append(f"  完整成交笔数:         {completed} 笔 (盈利 {wins} / 亏损 {losses})")
    lines.append(f"  未触发止盈止损:       {timeouts} 次")
    lines.append(f"  胜率(仅完整成交):     {win_rate_display:.2f}%")
    lines.append(f"  本周累计盈亏:         ${total_pnl:+.2f}")
    lines.append(f"{'=' * 70}")
    return "\n".join(lines)


def build_global_stats_text():
    """生成全局多周期统计"""
    lines = []
    lines.append(f"\n{'=' * 70}")
    lines.append(f"ICT 回测全局统计汇总")
    lines.append(f"{'=' * 70}")
    for iv in SIGNAL_GEN_INTERVALS:
        s = global_stats[iv]
        total = s["total"]
        win = s["win"]
        loss = s["loss"]
        timeout = s["timeout"]
        net_pnl = s["pnl"]
        completed = win + loss
        win_rate = (win / completed * 100) if completed > 0 else 0.0
        lines.append(f"\n--- {iv} ---")
        lines.append(f"  总信号: {total} | 盈利: {win} | 亏损: {loss} | "
                     f"未触发: {timeout} | 胜率: {win_rate:.1f}% | 盈亏: ${net_pnl:+.2f}")
    lines.append(f"{'=' * 70}")
    return "\n".join(lines)


# ==============================================================================
# 单周回测主逻辑
# ==============================================================================
def run_single_week(week_start_dt: datetime, week_end_dt: datetime):
    if week_start_dt >= week_end_dt:
        log(f"无效时间区间 {week_start_dt} ~ {week_end_dt}，跳过")
        return

    log(f"\n{'=' * 60}")
    log(f"开始回测: {week_start_dt} ~ {week_end_dt}")
    week_start_ms = dt_to_ms(week_start_dt)
    week_end_ms = dt_to_ms(week_end_dt)

    # ---- 拉取所有周期K线 ----
    raw_data = {}
    klines_data = {}  # 原始 dict 格式（用于盈亏模拟）
    candle_data = {}  # Candle 对象格式（用于ICT指标计算）

    for interval in INTERVALS:
        log(f"拉取 [{interval}] K线...")
        raw = fetch_historical_klines_range(interval, week_start_ms, week_end_ms)
        raw_data[interval] = raw
        klines_data[interval] = [{
            "open_time": k[0], "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]), "close_time": k[6],
        } for k in raw]
        candle_data[interval] = parse_klines(raw)
        log(f"  [{interval}] 获取 {len(raw)} 根K线")

    # ---- 报告初始化 ----
    report_content = []
    report_content.append(f"ICT  回测报告")
    report_content.append(f"回测区间：{week_start_dt.strftime('%Y-%m-%d')} ~ {week_end_dt.strftime('%Y-%m-%d')}")
    report_content.append("=" * 70)

    signals = []
    wins, losses, timeouts = 0, 0, 0
    total_pnl = 0.0
    dedup_skipped = 0
    level_consumed_skipped = 0
    skip_count = 0
    notified_in_backtest = {}
    consumed_levels = set()  # 已消耗的级别（交易完成后不再生成信号）

    # ---- 逐周期扫描 ----
    for gen_iv in SIGNAL_GEN_INTERVALS:
        src_candles = candle_data.get(gen_iv, [])
        src_klines = klines_data.get(gen_iv, [])

        if len(src_candles) < 50:
            log(f"[{gen_iv}] K线数量不足，跳过")
            continue

        # 遍历每根K线
        for i in range(50, len(src_candles)):
            sig = generate_ict_signal(candle_data, i, gen_iv)

            if sig is None:
                skip_count += 1
                continue

            # ---- 去重（基于级别名称+方向，而非价格桶）----
            dedup_key = (gen_iv, sig["level_name"], sig["direction"])
            curr_time = src_candles[i].close_time
            curr_time_ms = int(curr_time.timestamp() * 1000)

            # 检查级别是否已被消耗（之前交易已完成）
            if dedup_key in consumed_levels:
                level_consumed_skipped += 1
                continue

            if dedup_key in notified_in_backtest:
                last_ts = notified_in_backtest[dedup_key]
                if curr_time_ms - last_ts < DEDUP_COOLDOWN_SEC * 1000:
                    dedup_skipped += 1
                    continue
            notified_in_backtest[dedup_key] = curr_time_ms

            # ---- 盈亏模拟 ----
            sig_time = curr_time.replace(tzinfo=None)
            signals.append(sig)

            pnl_result = None
            if TRACK_PNL:
                future = src_klines[i + 1:i + 1 + MAX_FUTURE_BARS]
                pnl_result = simulate_trade(sig, future)
                res_tag, pnl = pnl_result
                total_pnl += pnl
                global_stats[gen_iv]["total"] += 1
                global_stats[gen_iv]["pnl"] += pnl
                if res_tag == "win":
                    wins += 1
                    global_stats[gen_iv]["win"] += 1
                    consumed_levels.add(dedup_key)  # 止盈 → 级别已消耗
                elif res_tag == "loss":
                    losses += 1
                    global_stats[gen_iv]["loss"] += 1
                    consumed_levels.add(dedup_key)  # 止损 → 级别已消耗
                else:
                    timeouts += 1
                    global_stats[gen_iv]["timeout"] += 1
                    # timeout 不消耗级别，价格可能还会回来

            sig_text = build_signal_text(sig, gen_iv, sig_time, pnl_result)
            report_content.append(sig_text)
            with open(SIGNAL_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(sig_text + "\n")
            if PRINT_EVERY_SIGNAL:
                print(sig_text)

    # ---- 周汇总 ----
    summary_text = build_summary_text(signals, total_pnl, wins, losses, timeouts,
                                      week_start_dt, week_end_dt, dedup_skipped, skip_count,
                                      level_consumed_skipped)
    report_content.append(summary_text)
    full_report = "\n".join(report_content)

    file_path = get_week_filename(week_start_dt, week_end_dt)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(full_report)
    log(f"本周回测完成 -> {file_path}")
    log(f"   信号: {len(signals)} | 盈利: {wins} | 亏损: {losses} | "
        f"超时: {timeouts} | 盈亏: ${total_pnl:+.2f}")
    log(f"   去重: 冷却拦截{dedup_skipped}次 | 级别消耗拦截{level_consumed_skipped}次")

    # 释放内存
    del raw_data, klines_data, candle_data, report_content, signals, notified_in_backtest
    gc.collect()


# ==============================================================================
# 主循环
# ==============================================================================
def get_next_monday(dt: datetime) -> datetime:
    days_ahead = (7 - dt.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (dt + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)



def main():
    log("=" * 60)
    log("ICT  回测系统启动")
    log(f"品种: {SYMBOL} | 信号周期: {SIGNAL_GEN_INTERVALS}")
    log(f"数据周期: {INTERVALS}")
    log(f"回测区间: {BACKTEST_START_DATE} ~ {datetime.now().strftime('%Y-%m-%d')}")
    log("=" * 60)

    start_dt = datetime.strptime(BACKTEST_START_DATE, "%Y-%m-%d %H:%M:%S")
    end_dt = datetime.now()

    current_week_start = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    week_count = 0

    while current_week_start < end_dt:
        current_week_end = get_next_monday(current_week_start)
        if current_week_end > end_dt:
            current_week_end = end_dt

        week_count += 1
        try:
            run_single_week(current_week_start, current_week_end)
        except Exception as e:
            log(f"【{current_week_start} ~ {current_week_end}】异常: {e}")
            import traceback
            traceback.print_exc()

        current_week_start = current_week_end

    # ---- 全局统计 ----
    log(f"\n{'=' * 60}")
    log(f"全部 {week_count} 周回测完成")
    log(f"周报目录: {os.path.abspath(OUTPUT_FOLDER)}")
    log(f"信号日志: {os.path.abspath(SIGNAL_LOG_FILE)}")

    global_stats_text = build_global_stats_text()
    print(global_stats_text)

    # 保存全局统计
    stat_file = os.path.join(OUTPUT_FOLDER, "ict_global_stats.txt")
    with open(stat_file, "w", encoding="utf-8") as f:
        f.write(global_stats_text)
    log(f"全局统计已保存: {stat_file}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("用户手动终止")
    except Exception as e:
        log(f"全局异常: {e}")
        import traceback
        traceback.print_exc()



