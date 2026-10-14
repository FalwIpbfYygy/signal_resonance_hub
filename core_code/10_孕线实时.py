
'''
<BINANCE_FUTURES_KLINE_URL>?symbol=&interval=1m&limit=1000

孕线半个小时需要等12个左右


'''



import requests
import time
import threading
import os
import sys
from datetime import datetime

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store

# ====================== 配置 ======================
SYMBOL = ""
INTERVALS = ["5m", "15m", "30m"]
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
STOP_LOSS_OFFSET = 0.3
CHECK_INTERVAL = 10
# ===================================================



notified = {interval: set() for interval in INTERVALS}


# ---------------------- 异步飞书（绝不卡信号！） ----------------------
def send_feishu_async(interval, signal_time, direction, sl):
    def task():
        # ✅ 只保留：第三根K线信号时间！！！
        text = f"""
【黄金孕线 即时信号】
周期：{interval}
信号出现（第三根K线）：{signal_time}
方向：{direction}
止损价格：{sl:.2f}
""".strip()
        send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        msg = {"msg_type": "text", "content": {"text": text}}
        try:
            requests.post(FEISHU_WEBHOOK, json=msg)
            print(f"[{send_time}] ✅ 飞书发送成功")
        except Exception as e:
            print(f"[{send_time}] ❌ 飞书发送失败: {str(e)}")

    threading.Thread(target=task, daemon=True).start()


# ---------------------- 获取K线 ----------------------
def get_klines_retry(interval, limit=50, retries=3):
    """获取币安合约K线数据（带重试）"""
    url = f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}?symbol={SYMBOL}&interval={interval}&limit={limit}"

    for attempt in range(1, retries + 1):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            print(f"[{now}] [尝试 {attempt}/{retries}] 获取{interval} K线...")
            resp = requests.get(url)
            if resp.status_code == 200:
                print(f"[{now}] ✅ {interval} K线获取成功")
                return resp.json()
            else:
                print(f"[{now}] ❌ 请求失败，状态码：{resp.status_code}")
        except Exception as e:
            print(f"[{now}] ❌ 请求异常：{str(e)}")
        time.sleep(1)

    print(f"❌ {interval} K线全部重试失败")
    return None

# ---------------------- 孕线识别 ----------------------
def is_pregnant_line(k1, k2):
    m_open = float(k1[1])
    m_close = float(k1[4])
    m_high = float(k1[2])
    m_low = float(k1[3])

    c_open = float(k2[1])
    c_close = float(k2[4])
    c_high = float(k2[2])
    c_low = float(k2[3])

    m_top = max(m_open, m_close)
    m_bot = min(m_open, m_close)
    c_top = max(c_open, c_close)
    c_bot = min(c_open, c_close)

    if not (c_bot >= m_bot and c_top <= m_top):
        return False
    if not (c_low >= m_low and c_high <= m_high):
        return False
    return True


def check_break(k1, k3):
    m_top = max(float(k1[1]), float(k1[4]))
    m_bot = min(float(k1[1]), float(k1[4]))

    k3_top = max(float(k3[1]), float(k3[4]))
    k3_bot = min(float(k3[1]), float(k3[4]))

    if k3_top > m_top:
        return "做多"
    if k3_bot < m_bot:
        return "做空"
    return None


# ---------------------- 实时扫描（真正第三根K线） ----------------------
def scan_real_time(interval):
    klines = get_klines_retry(interval)
    if not klines or len(klines) < 3:
        return

    end = len(klines) - 1
    k1 = klines[end - 2]  # 母线
    k2 = klines[end - 1]  # 子线
    k3 = klines[end]  # ✅ 第三根K线（刚走完！）

    signal_id = k3[0]
    if signal_id in notified[interval]:
        return

    # 孕线形态
    if not is_pregnant_line(k1, k2):
        return

    # 第三根K线直接出方向
    direction = check_break(k1, k3)
    if not direction:
        return

    # ✅ 只取第三根K线的 close_time（接口第 7 字段，毫秒）
    signal_time = datetime.fromtimestamp(int(k3[6]) / 1000).strftime("%Y-%m-%d %H:%M:%S")

    # 止损
    m_high = float(k1[2])
    m_low = float(k1[3])
    sl = m_low - STOP_LOSS_OFFSET if direction == "做多" else m_high + STOP_LOSS_OFFSET

    # ✅ 立刻发飞书！！！
    print(f"✅【即时信号】{interval} | {signal_time} | {direction}")
    send_feishu_async(interval, signal_time, direction, sl)

    # 统一入库（入场=第三根K收盘，SL 已有，TP 补 1R/2R）
    _entry = float(k3[4])
    _risk = abs(_entry - sl)
    _tp1 = _entry + _risk if direction == "做多" else _entry - _risk
    _tp2 = _entry + _risk * 2 if direction == "做多" else _entry - _risk * 2
    signal_store.record_signal(
        signal_time=signal_time, symbol=SYMBOL, interval=interval,
        pattern="黄金孕线", direction=direction,
        entry=_entry, sl=sl, tp1=_tp1, tp2=_tp2,
    )

    notified[interval].add(signal_id)


# ---------------------- 主循环 ----------------------
if __name__ == "__main__":
    print("=" * 60)
    print(" 黄金孕线 极速监控（第三根K线走完 立即推送）")
    print(" 无第四根K线 | 无开仓时间 | 纯信号")
    print("=" * 60)

    last_check = 0
    while True:
        now = time.time()
        if now - last_check >= CHECK_INTERVAL:
            for interval in INTERVALS:
                scan_real_time(interval)
            last_check = now
        time.sleep(0.2)



