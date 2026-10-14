import requests
import time
import os
import sys
from datetime import datetime

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store

# ====================== 配置 ======================
SYMBOL = ""
INTERVALS = ["5m", "15m"]
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
LOOKBACK_TREND = 5       # 前置下跌趋势回溯K线数量
# ===================================================

# 防止同一根K线重复推送信号
notified = {interval: set() for interval in INTERVALS}


def send_feishu(interval, dt_str, price, pattern_type, open_k, prev_low):
    """飞书消息推送"""
    text = f"""
【{interval} XAU Larry OOPS 做多信号】
触发时间：{dt_str}
K线开盘价：{open_k:.2f}
前一根最低价：{prev_low:.2f}
触发价格：{price:.2f}
形态等级：{pattern_type} OOPS（空头陷阱反转）
策略逻辑：跳空低开后快速收回前低，衰竭恐慌抛盘
""".strip()
    msg = {"msg_type": "text", "content": {"text": text}}
    send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        response = requests.post(FEISHU_WEBHOOK, json=msg)
        print(f"[{send_time}] 飞书发送成功，状态码: {response.status_code}")
    except Exception as e:
        print(f"[{send_time}] 飞书发送失败: {str(e)}")


def get_klines_retry(interval, limit=100, retries=3):
    """获取币安K线，带重试机制，limit不需要1000，减少流量"""
    url = f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}?symbol={SYMBOL}&interval={interval}&limit={limit}"

    for attempt in range(1, retries + 1):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            print(f"[{now}] [尝试 {attempt}/{retries}] 请求 {interval} K线...")
            resp = requests.get(url)
            if resp.status_code == 200:
                print(f"[{now}] [{interval}] 请求成功")
                return resp.json()
            else:
                print(f"[{now}] [{interval}] 请求失败，状态码：{resp.status_code}")
        except Exception as e:
            print(f"[{now}] [{interval}] 请求异常：{str(e)}，1s重试")
        time.sleep(1)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [{interval}] {retries}次重试全部失败")
    return None


def check_down_trend(klines, idx, lookback=5):
    """前置条件：检查是否处于持续下跌趋势
    idx: 当前K线下标，需要前面lookback根K连续收盘价走低
    """
    if idx < lookback:
        return False
    for j in range(idx - lookback + 1, idx):
        close_curr = float(klines[j][4])
        close_prev = float(klines[j - 1][4])
        if close_curr > close_prev:
            return False
    return True


def check_oops_pattern(klines, idx):
    """
    OOPS 形态判断（做多，老猫视频版本）
    klines[i] = [timestamp, open, high, low, close, volume,...]
    返回 None / "标准OOPS" / "强势OOPS"
    """
    if idx < 1:
        return None
    # 当前K
    curr_open = float(klines[idx][1])
    curr_high = float(klines[idx][2])
    curr_low = float(klines[idx][3])
    curr_close = float(klines[idx][4])
    # 前一根K
    prev_high = float(klines[idx - 1][2])
    prev_low = float(klines[idx - 1][3])

    # 条件1：开盘 < 前一根最低价（跳空低开）
    if curr_open >= prev_low:
        return None

    # 条件2：K线最高价突破前一根最低价（盘中收回缺口）
    if curr_high <= prev_low:
        return None

    # 条件3：前置下跌趋势
    if not check_down_trend(klines, idx, LOOKBACK_TREND):
        return None

    # 分级：强势OOPS=收盘站稳prev_low之上；标准仅盘中刺破
    if curr_close > prev_low:
        return "强势OOPS", curr_open, prev_low
    else:
        return "标准OOPS", curr_open, prev_low


def scan(interval):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    klines = get_klines_retry(interval)
    if not klines or len(klines) < 5:
        return

    # 使用倒数第2根【已经闭合完成的K线】，避免未走完的实时K未来函数风险！
    i = len(klines) - 2
    k_timestamp = klines[i][0]

    if k_timestamp in notified[interval]:
        return

    result = check_oops_pattern(klines, i)
    if result:
        pattern_type, open_k, prev_low = result
        close_price = float(klines[i][4])
        print(f"[{now}] ✅ {interval} 捕捉 {pattern_type} | 开盘:{open_k:.2f} 前低:{prev_low:.2f} 收盘:{close_price:.2f}")
        # 信号时间 = 该根已收盘 K 线的 close_time（接口第 7 字段，毫秒）
        dt_str = datetime.fromtimestamp(int(klines[i][6]) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        # 统一入库（入场=触发价，止损=前低，TP1/TP2=1R/2R）
        _risk = close_price - prev_low
        signal_store.record_signal(
            signal_time=dt_str, symbol=SYMBOL, interval=interval,
            pattern=f"{pattern_type} OOPS做多", direction="做多",
            entry=close_price, sl=prev_low,
            tp1=(close_price + _risk) if _risk > 0 else None,
            tp2=(close_price + _risk * 2) if _risk > 0 else None,
        )
        send_feishu(interval, dt_str, close_price, pattern_type, open_k, prev_low)
        notified[interval].add(k_timestamp)


# ====================== 主程序 ======================
if __name__ == "__main__":
    print("=" * 60)
    print("Larry Williams OOPS【哎呀形态】监控程序")
    print("监控周期：5m、15m  做多信号")
    print(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
    print("⚠️ 策略说明：下跌末端跳空低开，盘中收回前低 = OOPS多头陷阱")
    print("=" * 60)

    last_scan_second = -1
    while True:
        now = datetime.now()
        current_second = now.second
        # 每10秒扫描一次
        if current_second % 10 == 0 and current_second != last_scan_second:
            scan("5m")
            scan("15m")
            last_scan_second = current_second
        time.sleep(0.1)