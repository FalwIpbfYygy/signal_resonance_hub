'''

5分钟别用

2026/8/6 11:23 这个指标半小时比较稳一点


'''


import requests
import time
import os
import sys
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store

# ====================== 【策略参数｜严格对照原文】 ======================
SYMBOL = ""
# 小周期扫描周期（找入场）
# SCAN_INTERVALS = ["5m", "15m", "30m"]
# SCAN_INTERVALS = ["30m"]
SCAN_INTERVALS = ["15m", "30m"]
# 大周期：用来定整体趋势
TREND_TIMEFRAME = "4h"

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

# ====================== QQ邮箱推送（双周期共振） ======================
QQ_EMAIL_SENDER = os.environ.get("QQ_EMAIL_SENDER", "")      # 发件邮箱
# 授权码从环境变量读取（QQ_EMAIL_AUTH_CODE），避免明文硬编码进代码/仓库
QQ_EMAIL_AUTH_CODE = os.environ.get("QQ_EMAIL_AUTH_CODE", "")    # QQ邮箱SMTP授权码
QQ_EMAIL_RECEIVER = os.environ.get("QQ_EMAIL_RECEIVER", "")    # 收件邮箱
QQ_SMTP_SERVER = "smtp.qq.com"
QQ_SMTP_PORT = 465
QQ_EMAIL_TIMEOUT = 30
# 双周期共振判定窗口（秒）：15m与30m信号触发时间差小于该值视为“同一时间”
DOUBLE_SIGNAL_WINDOW = 60

# Pinbar标准：影线至少实体2倍
PINBAR_WICK_MULTI = 2.0
# 吞没形态阈值：吞没实体至少覆盖前一根实体比例
ENGULF_RATIO = 1.0
# 判断趋势回溯K线数量
TREND_LOOKBACK = 8
# 寻找局部高低点（支撑阻力）回看K线数量
SR_LOOKBACK = 30
# 价格距离关键位容忍比例（形态需要靠近支撑阻力）
SR_TOLERANCE_PCT = 0.002
# 是否开启“等待下一K确认信号”(True=减少毛刺，False=K线收盘立即告警)
WAIT_NEXT_CANDLE_CONFIRM = False
# ======================================================================

# 防重复告警 {周期: {时间戳}}
notified = {interval: set() for interval in SCAN_INTERVALS}
# 缓存大周期K线，减少频繁请求
cache_4h_kline = None
cache_4h_expire = 0

# 最近一次信号 {周期: {"detect_time": float, "kline_ts": int, "msg": str}}
recent_signal = {}
# 已发送QQ邮箱的共振信号对，避免重复推送 {(15m kline_ts, 30m kline_ts)}
qq_sent_pairs = set()


def send_feishu(msg_text):
    """统一飞书发送函数"""
    msg = {"msg_type": "text", "content": {"text": msg_text.strip()}}
    send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        resp = requests.post(FEISHU_WEBHOOK, json=msg)
        print(f"[{send_time}] 飞书推送 | code:{resp.status_code}")
    except Exception as e:
        print(f"[{send_time}] 推送异常: {str(e)}")


def send_qq_email(subject, body):
    """QQ邮箱发送（SSL直连，失败仅打印日志）"""
    send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = Header("量化告警系统", "utf-8").encode() + f" <{QQ_EMAIL_SENDER}>"
    msg["To"] = Header("交易者", "utf-8").encode() + f" <{QQ_EMAIL_RECEIVER}>"
    msg["Subject"] = Header(subject, "utf-8")
    try:
        server = smtplib.SMTP_SSL(QQ_SMTP_SERVER, QQ_SMTP_PORT, timeout=QQ_EMAIL_TIMEOUT)
        server.login(QQ_EMAIL_SENDER, QQ_EMAIL_AUTH_CODE)
        server.sendmail(QQ_EMAIL_SENDER, [QQ_EMAIL_RECEIVER], msg.as_string())
        try:
            server.quit()
        except Exception:
            pass
        print(f"[{send_time}] QQ邮箱推送 | {subject}")
    except Exception as e:
        print(f"[{send_time}] QQ邮箱发送异常: {str(e)}")


def try_send_double_signal(interval, msg, kline_ts):
    """双周期共振：15m与30m同一时间都触发信号时，合并推送QQ邮箱"""
    recent_signal[interval] = {"detect_time": time.time(), "kline_ts": kline_ts, "msg": msg}
    if "15m" not in recent_signal or "30m" not in recent_signal:
        return
    s15 = recent_signal["15m"]
    s30 = recent_signal["30m"]
    if abs(s15["detect_time"] - s30["detect_time"]) > DOUBLE_SIGNAL_WINDOW:
        return
    pair = (s15["kline_ts"], s30["kline_ts"])
    if pair in qq_sent_pairs:
        return
    qq_sent_pairs.add(pair)
    body = s15["msg"].strip() + "\n\n" + "=" * 30 + "\n\n" + s30["msg"].strip()
    subject = " 双Pinbar共振信号 | 15m+30m"
    send_qq_email(subject, body)


def get_klines_retry(interval, limit=100, retries=3):
    """获取币安合约K线 重试机制"""
    url = f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}?symbol={SYMBOL}&interval={interval}&limit={limit}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url)
            if resp.status_code == 200:
                print(f"[{now_str}] 请求成功 | 周期:{interval} 尝试次数:{attempt} status:{resp.status_code}")
                return resp.json()
            else:
                print(f"[{now_str}] 请求异常状态码 | 周期:{interval} 尝试次数:{attempt} status:{resp.status_code}")
        except Exception as e:
            print(f"[{now_str}] 请求抛出异常 | 周期:{interval} 尝试次数:{attempt} error:{str(e)}")
        time.sleep(1)
    print(f"[{now_str}] 请求最终失败 | 周期:{interval} 已用尽重试次数")
    return None


def parse_kline_list(raw_klines):
    """原始币安K线转结构化列表 [timestamp, o,h,l,c,vol] float"""
    data = []
    for k in raw_klines:
        ts = k[0]
        o = float(k[1])
        h = float(k[2])
        l = float(k[3])
        c = float(k[4])
        vol = float(k[5])
        data.append([ts, o, h, l, c, vol])
    return data


def find_support_resistance(k_data, lookback):
    """
    寻找近期局部高低点（支撑阻力）
    return: [support_list, resistance_list]
    """
    supports = []
    resistances = []
    length = len(k_data)
    window = 3  # 左右各3根对比判定局部极值
    for i in range(window, length - window):
        low_i = k_data[i][3]
        high_i = k_data[i][2]
        # 局部低点
        left_low = min([k_data[i - w][3] for w in range(1, window + 1)])
        right_low = min([k_data[i + w][3] for w in range(1, window + 1)])
        if low_i <= left_low and low_i <= right_low:
            supports.append(low_i)
        # 局部高点
        left_high = max([k_data[i - w][2] for w in range(1, window + 1)])
        right_high = max([k_data[i + w][2] for w in range(1, window + 1)])
        if high_i >= left_high and high_i >= right_high:
            resistances.append(high_i)
    return supports, resistances


def price_near_level(price, levels, tolerance_pct):
    """判断价格是否靠近任意支撑/阻力位"""
    for lv in levels:
        diff = abs(price - lv) / lv
        if diff <= tolerance_pct:
            return True
    return False


def judge_trend(k_data, end_idx, lookback):
    """
    趋势判定
    return bull / bear / side
    """
    if end_idx < lookback:
        return "side"
    closes = [k_data[i][4] for i in range(end_idx - lookback, end_idx + 1)]
    up = 0
    down = 0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            up += 1
        elif closes[i] < closes[i - 1]:
            down += 1
    ratio = lookback * 0.65
    if up >= ratio:
        return "bull"
    elif down >= ratio:
        return "bear"
    else:
        return "side"


def check_pinbar(o, h, l, c, wick_min_mult):
    """
    Pinbar识别
    返回 None / ("bull",等级) / ("bear",等级)
    """
    body_top = max(o, c)
    body_bottom = min(o, c)
    body = body_top - body_bottom
    if body <= 1e-7:
        return None

    upper_wick = h - body_top
    lower_wick = body_bottom - l

    # 看涨Pinbar：长下影
    if lower_wick >= upper_wick * 1.2 and lower_wick >= body * wick_min_mult:
        r = lower_wick / body
        if r >= 3.0:
            lv = "极强Pinbar"
        elif r >= 2.5:
            lv = "标准Pinbar"
        else:
            lv = "偏弱Pinbar"
        return ("bull", lv)
    # 看跌Pinbar：长上影
    if upper_wick >= lower_wick * 1.2 and upper_wick >= body * wick_min_mult:
        r = upper_wick / body
        if r >= 3.0:
            lv = "极强Pinbar"
        elif r >= 2.5:
            lv = "标准Pinbar"
        else:
            lv = "偏弱Pinbar"
        return ("bear", lv)
    return None


def check_engulfing(prev_o, prev_c, curr_o, curr_c, min_ratio):
    """
    吞没形态识别
    return None / bull_engulf / bear_engulf
    """
    prev_body_top = max(prev_o, prev_c)
    prev_body_bottom = min(prev_o, prev_c)
    curr_body_top = max(curr_o, curr_c)
    curr_body_bottom = min(curr_o, curr_c)

    prev_body_len = prev_body_top - prev_body_bottom
    curr_body_len = curr_body_top - curr_body_bottom
    if prev_body_len < 1e-7 or curr_body_len < 1e-7:
        return None

    # 看涨吞没：阳线实体完全包住前面阴线实体
    if curr_o < curr_c and curr_body_bottom <= prev_body_bottom and curr_body_top >= prev_body_top:
        if curr_body_len / prev_body_len >= min_ratio:
            return "bull_engulf"
    # 看跌吞没：阴线包住前面阳线
    if curr_o > curr_c and curr_body_bottom <= prev_body_bottom and curr_body_top >= prev_body_top:
        if curr_body_len / prev_body_len >= min_ratio:
            return "bear_engulf"
    return None


def get_big_trend():
    """获取4H大周期趋势，带缓存减少请求"""
    global cache_4h_kline, cache_4h_expire
    now_ts = time.time()
    # 缓存120秒，避免疯狂请求4H
    if cache_4h_kline is not None and now_ts < cache_4h_expire:
        kdata = cache_4h_kline
    else:
        raw = get_klines_retry(TREND_TIMEFRAME, limit=80)
        if not raw:
            return "side", None
        kdata = parse_kline_list(raw)
        cache_4h_kline = kdata
        cache_4h_expire = now_ts + 120
    trend = judge_trend(kdata, len(kdata)-1, TREND_LOOKBACK)
    return trend, kdata


def scan_interval(interval):
    """单周期扫描主逻辑"""
    big_trend, _ = get_big_trend()
    raw_k = get_klines_retry(interval, limit=SR_LOOKBACK + 10)
    if not raw_k or len(raw_k) < 15:
        return
    kdata = parse_kline_list(raw_k)
    last_idx = len(kdata) - 1
    last_ts, o, h, l, c, vol = kdata[last_idx]

    # 防重复推送
    if last_ts in notified[interval]:
        return

    # 获取支撑阻力
    s_list, r_list = find_support_resistance(kdata, SR_LOOKBACK)
    price_near_s = price_near_level(c, s_list, SR_TOLERANCE_PCT)
    price_near_r = price_near_level(c, r_list, SR_TOLERANCE_PCT)

    # ========== 形态检测 ==========
    pin_signal = check_pinbar(o, h, l, c, PINBAR_WICK_MULTI)
    # 吞没需要前一根K
    prev_ts, prev_o, prev_h, prev_l, prev_c, prev_vol = kdata[last_idx - 1]
    engulf_signal = check_engulfing(prev_o, prev_c, o, c, ENGULF_RATIO)

    signal_text = None
    signal_type = ""
    strength = ""

    # --------------------------
    # 规则：只允许顺势、靠近支撑阻力的信号
    # 场景1：大周期下跌(bear) + 价格靠近支撑 → 看涨形态
    # 场景2：大周期上涨(bull) + 价格靠近阻力 → 看跌形态
    # --------------------------
    # 看涨Pinbar / 看涨吞没
    if (pin_signal and pin_signal[0] == "bull") or engulf_signal == "bull_engulf":
        if big_trend == "bear" and price_near_s:
            if pin_signal:
                sig_type = "看涨Pinbar(做多)"
                strength = pin_signal[1]
            else:
                sig_type = "看涨吞没(做多)"
                strength = "标准吞没形态"
            signal_text = sig_type
            sl_ref = f"{l - 1.5:,.2f}"
            logic = "大周期下跌趋势，回调至支撑区域出现反转形态"
            tp_ref = "最近一级阻力位"

    # 看跌Pinbar / 看跌吞没
    elif (pin_signal and pin_signal[0] == "bear") or engulf_signal == "bear_engulf":
        if big_trend == "bull" and price_near_r:
            if pin_signal:
                sig_type = "看跌Pinbar(做空)"
                strength = pin_signal[1]
            else:
                sig_type = "看跌吞没(做空)"
                strength = "标准吞没形态"
            signal_text = sig_type
            sl_ref = f"{h + 1.5:,.2f}"
            logic = "大周期上涨趋势，反弹至阻力区域出现反转形态"
            tp_ref = "最近一级支撑位"

    if signal_text is not None:
        # 信号时间 = 该根 K 线的 close_time（接口第 7 字段，毫秒）
        dt_str = datetime.fromtimestamp(int(raw_k[last_idx][6]) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        # 统一入库（入场=当前价，止损=极值∓1.5，TP=最近阻力/支撑位）
        if "做多" in signal_text:
            _dir = "做多"
            _sl = l - 1.5
            # 做多目标：最近一级阻力位（r_list 中最小且大于当前价的）
            _above = [rv for rv in r_list if rv > c]
            _tp = min(_above) if _above else None
        else:
            _dir = "做空"
            _sl = h + 1.5
            _below = [sv for sv in s_list if sv < c]
            _tp = max(_below) if _below else None
        signal_store.record_signal(
            signal_time=dt_str, symbol=SYMBOL, interval=interval,
            pattern=signal_text, direction=_dir,
            entry=c, sl=_sl, tp1=_tp, tp2=None,
        )
        msg = f"""
【{SYMBOL} {interval} 信号触发】
形态：{signal_text}
形态强度：{strength}
当前价格：{c:.2f}
大周期({TREND_TIMEFRAME})趋势：{big_trend}
触发逻辑：{logic}
风控参考
👉止损参考：{sl_ref}
👉第一目标参考：{tp_ref}
策略原则：等待K线确认再择机入场，分批减仓，单笔风险≤2%资金
【学习交流，不作为交易依据】
        """.strip()
        send_feishu(msg)
        notified[interval].add(last_ts)
        print(f"✅ [{dt_str}] {interval} | {signal_text} | 价格:{c:.2f}")
        # 双周期共振：15m与30m同一时间都触发时推送QQ邮箱
        try_send_double_signal(interval, msg, last_ts)


if __name__ == "__main__":
    print("="*75)
    print("Pinbar+吞没 价格行为监控｜大周期4H定方向｜位置优先过滤")
    print(f"监控周期: {SCAN_INTERVALS} | 大周期:{TREND_TIMEFRAME}")
    print(f"Pinbar影线倍数:{PINBAR_WICK_MULTI} | 等待确认:{WAIT_NEXT_CANDLE_CONFIRM}")
    print("="*75)

    last_check_sec = -1
    while True:
        now = datetime.now()
        sec = now.second
        # 每10秒一轮扫描
        if sec % 10 == 0 and sec != last_check_sec:
            for tf in SCAN_INTERVALS:
                scan_interval(tf)
            last_check_sec = sec
        time.sleep(0.1)

