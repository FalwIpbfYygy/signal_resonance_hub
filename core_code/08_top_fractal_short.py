"""
顶分型做空策略 v2  (P0 + P1 优化版)
====================================================================
P0 改造：
  ① Bill Williams 5 根 K 标准顶分型（替换原 3 根 K 简化版）
  ② 第 6 根 K 右侧确认：必须收盘跌破中间 K 实体下沿才开仓
  ③ 砍掉"一般"等级，只保留"次强" / "最强"
  ④ 信号强度按"实体大小 / 20 根 K 实体均值"重算

P1 改造：
  ① Hurst 指数过滤：H < 0.55 一律不开仓（震荡市噪音过滤）
  ② ATR 波动率过滤：当前 ATR < 0.8×均 ATR 视为低波动压缩，跳过
  ③ 时段过滤：仅在北京时间 15:00-20:00（伦敦+纽约重叠盘）开仓
  ④ 大周期趋势：30m 周期 MA20 朝下才允许做空

止损 SL：顶分型高点 + 0.5×ATR
止盈 TP1：1R（建议减半仓）
止盈 TP2：2R（剩余半仓）   → 综合赔率 1:1.5 以上

收线驱动（v3）：
  ① 每周期只在 K 线完整收盘后判断一次（对齐周期边界 + 8 秒缓冲）
  ② 拉取后剔除最后一根未收盘 K 线，全部逻辑只用已收盘 K 线
     （原版拿进行中的 K 线当"第6根确认K"，会随价格反复变化）
"""

import requests
import time
import os
import sys
from datetime import datetime
import numpy as np

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store


# ====================== 基础配置 ======================
SYMBOL = ""
INTERVALS = ["5m", "15m"]          # 触发周期
HIGHER_TF = "30m"                   # 大周期背景（趋势过滤用）
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
# ===================================================

# ====================== P1 过滤阈值 ======================
HURST_LOOKBACK     = 100            # 计算 Hurst 用的 K 线根数
HURST_THRESHOLD    = 0.55           # H 必须 > 此值才视为趋势市
ATR_PERIOD         = 14
ATR_RATIO_MIN      = 0.8            # 当前 ATR / 50 根均 ATR  < 此值视为低波动，跳过
MA_TREND_PERIOD    = 20             # 30m 大周期 MA 周期
SESSION_START_H    = 15             # 北京时间开始（伦敦+NY 重叠）
SESSION_END_H      = 20             # 北京时间结束
# ==========================================================

# 防重复推送
notified = {interval: set() for interval in INTERVALS}

# ====================== 收线驱动配置 ======================
INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
CLOSE_LAG = 8                                # 收线后等待秒数，确保 Binance 已生成新K线
next_scan = {interval: 0 for interval in INTERVALS}   # 各周期下次应扫描的时间戳


def drop_forming_bar(klines):
    """剔除最后一根未收盘 K 线（t_close 在未来 = 进行中），返回仅含已收盘K的列表"""
    now_ms = int(time.time() * 1000)
    if klines and klines[-1][6] > now_ms:
        return klines[:-1]
    return klines
# =========================================================


# ---------------------- 飞书推送 ----------------------
def send_feishu(interval, dt_str, entry, sl, tp1, tp2, strength, hurst, atr_ratio):
    text = f"""
【{interval} XAU 顶分型v2 做空信号｜{strength}】
开仓时间：{dt_str}
入场价：{entry:.2f}
止损 SL：{sl:.2f}
止盈 TP1：{tp1:.2f}（1R）
止盈 TP2：{tp2:.2f}（2R）
综合赔率：1:1.5
—————
市场状态过滤
Hurst(100)：{hurst:.2f}  {'✅趋势市' if hurst>0.55 else '❌'}
ATR 比率：{atr_ratio:.2f}    {'✅' if atr_ratio>0.8 else '❌'}
大周期 30m：MA20 朝下   ✅
时段：伦敦+NY 重叠盘   ✅
""".strip()
    msg = {"msg_type": "text", "content": {"text": text}}
    send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        response = requests.post(FEISHU_WEBHOOK, json=msg)
        print(f"[{send_time}] 飞书发送成功，状态码: {response.status_code}")
    except Exception as e:
        print(f"[{send_time}] 飞书发送失败: {str(e)}")


# ---------------------- K线拉取 ----------------------
def get_klines_retry(interval, limit=200, retries=3):
    url = f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}?symbol={SYMBOL}&interval={interval}&limit={limit}"
    for attempt in range(1, retries + 1):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            print(f"[{now}] [尝试 {attempt}/{retries}] 正在请求{interval}K线...")
            resp = requests.get(url)
            if resp.status_code == 200:
                print(f"[{now}] [{interval}] 请求成功 ({len(resp.json())} 根)")
                return resp.json()
            else:
                print(f"[{now}] [{interval}] 状态码：{resp.status_code}")
        except Exception as e:
            print(f"[{now}] [{interval}] 异常：{e}，1秒后重试...")
        time.sleep(1)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{interval}] 全部重试失败")
    return None


# ============================================================
# P1 过滤器
# ============================================================
def calc_hurst(prices, max_lag=20):
    """
    R/S (Rescaled Range) 法计算 Hurst 指数（比方差时间法更稳健）
      H > 0.55: 趋势市（适合做空反转）
      H ≈ 0.5:  随机漫步
      H < 0.5:  均值回归（震荡市，跳过）
    """
    if len(prices) < max_lag * 3:
        return 0.5
    p   = np.asarray(prices, dtype=float)
    rets = np.diff(np.log(p))
    lags = range(2, min(max_lag, len(rets) // 2))
    rs_pts = []
    for lag in lags:
        n_chunks = len(rets) // lag
        if n_chunks < 1:
            continue
        chunk_rs = []
        for i in range(n_chunks):
            seg = rets[i * lag: (i + 1) * lag]
            seg = seg - np.mean(seg)
            cum = np.cumsum(seg)
            r = float(np.max(cum) - np.min(cum))
            s = float(np.std(seg, ddof=1))
            if s > 0:
                chunk_rs.append(r / s)
        if chunk_rs:
            rs_pts.append((lag, float(np.mean(chunk_rs))))
    if len(rs_pts) < 2:
        return 0.5
    log_lags = np.log([x[0] for x in rs_pts])
    log_rs   = np.log([x[1] for x in rs_pts])
    slope, _ = np.polyfit(log_lags, log_rs, 1)
    # 数值上偶尔会出现 slope<0（数据极短/全平），统一夹紧到 [0,1]
    return float(max(0.0, min(1.0, slope)))


def calc_atr(klines, period=14):
    """Wilder ATR"""
    if len(klines) < period + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h  = float(klines[i][2])
        l  = float(klines[i][3])
        pc = float(klines[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return float(np.mean(trs[-period:]))


def in_session():
    """仅在伦敦+NY 重叠盘（北京 15:00-20:00）允许开仓"""
    h = (datetime.utcnow().hour + 8) % 24
    return SESSION_START_H <= h < SESSION_END_H


def check_higher_tf_trend():
    """30m 大周期 MA20 朝下 才允许做空（仅用已收盘K线）"""
    k30 = get_klines_retry(HIGHER_TF, limit=MA_TREND_PERIOD + 5, retries=2)
    if not k30:
        return None  # 拉取失败 → 不开仓
    k30 = drop_forming_bar(k30)
    if len(k30) < MA_TREND_PERIOD + 2:
        return None  # 数据不足 → 不开仓
    closes = [float(k[4]) for k in k30]
    ma20   = float(np.mean(closes[-MA_TREND_PERIOD:]))
    last   = closes[-1]
    return last < ma20


# ============================================================
# P0 核心：5 根 K 顶分型 + 第 6 根 K 确认
# ============================================================
def find_top_fractal_5bar(klines):
    """
    在已闭合的 K 线中，定位最近一个 5 根 K 顶分型。
    返回中间 K 的下标，找不到返回 None。
    """
    n = len(klines)
    if n < 6:
        return None
    # 中间 K 取 klines[-4]（即倒数第 4 根），其右侧 2 根是 klines[-3], klines[-2]
    # 第 6 根 K 确认使用 klines[-1]（最新已闭合 K）
    mid = n - 4
    h_mid   = float(klines[mid][2])
    h_l2    = float(klines[mid - 2][2])
    h_l1    = float(klines[mid - 1][2])
    h_r1    = float(klines[mid + 1][2])
    h_r2    = float(klines[mid + 2][2])
    if h_mid > h_l2 and h_mid > h_l1 and h_mid > h_r1 and h_mid > h_r2:
        return mid
    return None


def check_confirmation_break(klines, mid_idx):
    """
    第 6 根 K 确认：klines[-1] 收盘必须跌破中间 K 实体下沿
    返回确认 K 的下标，否则 None
    """
    if mid_idx is None:
        return None
    confirm_idx = len(klines) - 1
    o   = float(klines[mid_idx][1])
    c   = float(klines[mid_idx][4])
    body_bottom = min(o, c)
    confirm_low   = float(klines[confirm_idx][3])
    confirm_close = float(klines[confirm_idx][4])
    if confirm_close < body_bottom or confirm_low < body_bottom:
        return confirm_idx
    return None


def grade_strength(klines, mid_idx):
    """
    信号强度分级（砍掉"一般"）：
      最强：当前 K 实体 ≥ 20 根 K 实体均值的 1.5 倍
      次强：当前 K 实体 ≥ 20 根 K 实体均值的 0.8 倍
    不达标返回 None（不开仓）
    """
    body = abs(float(klines[mid_idx][4]) - float(klines[mid_idx][1]))
    start = max(0, mid_idx - 20)
    bodies = [abs(float(klines[j][4]) - float(klines[j][1])) for j in range(start, mid_idx)]
    avg_body = float(np.mean(bodies)) if bodies else 1.0
    if avg_body <= 0:
        return None
    if body >= avg_body * 1.5:
        return "最强"
    if body >= avg_body * 0.8:
        return "次强"
    return None


def build_short_signal(klines):
    """
    完整信号生成：5 根 K 顶分型 + 第 6 根 K 确认 + 强度分级
    返回 dict 或 None
    """
    if len(klines) < 60:
        return None
    mid_idx = find_top_fractal_5bar(klines)
    if mid_idx is None:
        return None
    confirm_idx = check_confirmation_break(klines, mid_idx)
    if confirm_idx is None:
        return None
    strength = grade_strength(klines, mid_idx)
    if strength is None:
        return None

    # 动态止盈止损（ATR 自适应）
    atr = calc_atr(klines, ATR_PERIOD)
    if atr is None or atr <= 0:
        return None
    fractal_high = float(klines[mid_idx][2])
    entry   = float(klines[confirm_idx][4])     # 第 6 根 K 收盘价 = 入场价
    sl      = fractal_high + atr * 0.5           # 止损：顶分型高点 + 0.5 ATR
    risk    = sl - entry
    if risk <= 0:
        return None
    tp1 = entry - risk * 1.0
    tp2 = entry - risk * 2.0
    return {
        "strength": strength,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "risk": risk,
        "atr": atr,
        "mid_idx": mid_idx,
        "confirm_idx": confirm_idx,
    }


# ============================================================
# 单周期扫描
# ============================================================
def scan(interval):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    # ===== 收线门控：未到本周期收线后的判断时刻，直接返回 =====
    now_ts = time.time()
    if now_ts < next_scan[interval]:
        return
    iv_sec = INTERVAL_SEC[interval]
    # 下次扫描 = 下一根K线收盘时刻 + CLOSE_LAG
    next_scan[interval] = (int(now_ts) // iv_sec + 1) * iv_sec + CLOSE_LAG

    # P1-③ 时段过滤
    if not in_session():
        return

    # P1-④ 大周期趋势
    higher_trend_ok = check_higher_tf_trend()
    if higher_trend_ok is None:
        print(f"[{now}] [{interval}] 30m 大周期数据不足，跳过本轮")
        next_scan[interval] = time.time() + 20   # 20秒后补一次
        return
    if not higher_trend_ok:
        return  # 30m 趋势向上，不做空

    # 拉取本周期 K 线
    klines = get_klines_retry(interval, limit=200)
    if not klines:
        next_scan[interval] = time.time() + 20   # 拉取失败20秒后重试
        return
    # 剔除未收盘的最后一根，只用已收盘K线判断
    klines = drop_forming_bar(klines)
    if len(klines) < 60:
        return

    closes = [float(k[4]) for k in klines]

    # P1-① Hurst 过滤
    hurst = calc_hurst(closes[-HURST_LOOKBACK:], max_lag=20)
    if hurst < HURST_THRESHOLD:
        return  # 震荡市，跳过

    # P1-② ATR 过滤
    atr_now = calc_atr(klines, ATR_PERIOD)
    # 用过去 50 个 ATR 滚动均值
    atr_window = []
    for end in range(ATR_PERIOD * 3, len(klines)):
        sub = klines[:end + 1]
        a = calc_atr(sub, ATR_PERIOD)
        if a:
            atr_window.append(a)
        if len(atr_window) >= 50:
            break
    if not atr_window or atr_now is None:
        return
    atr_avg = float(np.mean(atr_window))
    atr_ratio = atr_now / atr_avg if atr_avg > 0 else 0
    if atr_ratio < ATR_RATIO_MIN:
        return  # 低波动压缩，跳过

    # P0 信号生成
    sig = build_short_signal(klines)
    if not sig:
        return

    # 去重
    kid = klines[sig["confirm_idx"]][0]
    if kid in notified[interval]:
        return
    notified[interval].add(kid)

    dt_str = datetime.fromtimestamp(int(klines[sig["confirm_idx"]][6]) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [{interval}] {sig['strength']} 顶分型v2 做空 | "
          f"入场 {sig['entry']:.2f} SL {sig['sl']:.2f} TP1 {sig['tp1']:.2f} TP2 {sig['tp2']:.2f}")
    send_feishu(interval, dt_str, sig["entry"], sig["sl"], sig["tp1"], sig["tp2"],
                sig["strength"], hurst, atr_ratio)
    # 统一入库（字段齐全，直通；方向固定做空）
    signal_store.record_signal(
        signal_time=dt_str, symbol=SYMBOL, interval=interval,
        pattern="顶分型做空", direction="做空",
        entry=sig["entry"], sl=sig["sl"], tp1=sig["tp1"], tp2=sig["tp2"],
    )


# ====================== 主程序 ======================
if __name__ == "__main__":
    print("=" * 60)
    print("顶分型v3 做空策略（收线驱动 + Hurst+ATR+时段+大周期） ")
    print(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
    print(f"监控周期: {INTERVALS} | 大周期: {HIGHER_TF}")
    print(f"开仓时段: 北京 {SESSION_START_H}:00 - {SESSION_END_H}:00")
    print("判断时机: 每根K线完整收盘后（收线 +8秒），只用已收盘K线")
    print("=" * 60)

    while True:
        for interval in INTERVALS:
            scan(interval)   # 内部有收线门控，未到收线时刻立即返回，不发请求
        time.sleep(1)
