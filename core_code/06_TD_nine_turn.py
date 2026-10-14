
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
INTERVALS = ["1m","5m","15m","30m"]
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
# ===================================================

# 信号去重：key=K线时间戳，避免重复推送
notified = {interval: set() for interval in INTERVALS}


def send_feishu(interval, dt_str, price, signal_type, filter_note):
    """发送飞书通知
    signal_type: 低九(做多) / 高九(做空)
    filter_note: MACD过滤状态
    """
    if signal_type == "低九":
        direction = "【做多预警】潜在底部"
    else:
        direction = "【做空预警】潜在顶部"

    text = f"""
【{interval} XAU {signal_type}{direction}
触发时间：{dt_str}
当前价格：{price:.2f}
周期：{interval}
过滤条件：{filter_note}
策略逻辑：神奇九转+MACD柱衰竭过滤（左侧反转信号）
⚠️ 九转左侧信号，单边趋势行情容易失效，仅供观测！
""".strip()
    msg = {"msg_type": "text", "content": {"text": text}}
    send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        response = requests.post(FEISHU_WEBHOOK, json=msg)
        print(f"[{send_time}] 飞书发送成功，状态码: {response.status_code}")
    except Exception as e:
        print(f"[{send_time}] 飞书发送失败: {str(e)}")


def get_klines_retry(interval, limit=120, retries=3):
    """获取币安K线，增加limit长度满足九转+MACD计算需求"""
    url = f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}?symbol={SYMBOL}&interval={interval}&limit={limit}"

    for attempt in range(1, retries + 1):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            print(f"[{now}] [尝试 {attempt}/{retries}] 请求{interval} K线...")
            resp = requests.get(url)
            if resp.status_code == 200:
                print(f"[{now}] [尝试 {attempt}] {interval} K线请求成功")
                return resp.json()
            else:
                print(f"[{now}] [尝试 {attempt}] 请求失败，状态码：{resp.status_code}")
        except Exception as e:
            print(f"[{now}] [尝试 {attempt}] 请求异常：{str(e)}，1秒后重试...")
        time.sleep(1)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {interval} 全部重试失败")
    return None


def calc_macd(close_list, fast=12, slow=26, signal=9):
    """计算MACD：返回 MACD柱序列（DIF-DEA），仅需要红绿柱数值
    close_list: 收盘价list
    return bar_list
    """
    def ema(series, length):
        res = []
        alpha = 2 / (length + 1)
        prev = series[0]
        res.append(prev)
        for val in series[1:]:
            prev = val * alpha + prev * (1 - alpha)
            res.append(prev)
        return res

    if len(close_list) < slow + signal:
        return []
    ema_fast = ema(close_list, fast)
    ema_slow = ema(close_list, slow)
    dif = [ema_fast[i] - ema_slow[i] for i in range(len(ema_fast))]
    dea = ema(dif, signal)
    bar = [dif[i] - dea[i] for i in range(len(dif))]
    return bar


def check_td_9seq(klines):
    """
    神奇九转TD Setup判定（视频原版规则）
    return: signal, seq_num
    signal: None / "高九" / "低九"
    """
    close = [float(k[4]) for k in klines]
    n = len(close)
    sell_seq = 0  # 上涨序列 高1~高9
    buy_seq = 0   # 下跌序列 低1~低9

    signal = None

    for i in range(n):
        if i < 4:
            continue
        curr_c = close[i]
        ref_c = close[i - 4]

        # 上涨序列(高序列): 当前收盘 > 前第4根收盘
        if curr_c > ref_c:
            sell_seq += 1
            buy_seq = 0
        # 下跌序列(低序列): 当前收盘 < 前第4根收盘
        elif curr_c < ref_c:
            buy_seq += 1
            sell_seq = 0
        # 等于，序列直接中断清零
        else:
            sell_seq = 0
            buy_seq = 0

    # 最后一根K形成9序列
    if sell_seq == 9:
        signal = "高九"
    elif buy_seq == 9:
        signal = "低九"
    return signal


def macd_filter_check(klines, signal_type):
    """
    老猫视频MACD过滤规则：只看红绿柱收敛，不使用信号线交叉
    高九（做空）：MACD绿柱逐步萎缩
    低九（做多）：MACD红柱逐步萎缩
    返回 (是否满足过滤, 说明文本)
    """
    close_list = [float(k[4]) for k in klines]
    bar_list = calc_macd(close_list)
    if len(bar_list) < 5:
        return False, "MACD数据不足，放弃信号"

    # 取末尾最近4根柱子对比，判断是否收敛
    last4 = bar_list[-4:]
    if signal_type == "高九":
        # 高九做空：当前处于绿柱（负数），柱子绝对值缩小（萎缩）
        if last4[-1] < 0 and abs(last4[-1]) <= abs(last4[-2]):
            return True, "✅绿柱萎缩，满足反转过滤条件"
        else:
            return False, "❌绿柱未萎缩，过滤信号"
    elif signal_type == "低九":
        # 低九做多：当前处于红柱（正数），柱子高度缩小（萎缩）
        if last4[-1] > 0 and last4[-1] <= last4[-2]:
            return True, "✅红柱萎缩，满足反转过滤条件"
        else:
            return False, "❌红柱未萎缩，过滤信号"
    return False, "未知信号类型"


def scan(interval):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    klines = get_klines_retry(interval)
    if not klines or len(klines) < 35:
        return

    latest_k = klines[-1]
    kid = latest_k[0]  # K线时间戳，唯一标识
    if kid in notified[interval]:
        return

    # 1. 判断九转信号
    td_signal = check_td_9seq(klines)
    if not td_signal:
        return

    # 2. MACD柱过滤
    pass_filter, filter_text = macd_filter_check(klines, td_signal)
    price = float(latest_k[4])
    # 信号时间 = 该根 K 线的 close_time（接口第 7 字段，毫秒）
    dt_str = datetime.fromtimestamp(int(latest_k[6]) / 1000).strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{now}] [{interval}] 检测到{td_signal} | {filter_text} 价格：{price:.2f}")

    # 只有满足MACD过滤条件才推送告警
    if pass_filter:
        send_feishu(interval, dt_str, price, td_signal, filter_text)
        # 统一入库（九转为左侧观测信号，止盈止损缺失→留空）
        signal_store.record_signal(
            signal_time=dt_str, symbol=SYMBOL, interval=interval,
            pattern=f"TD九转{td_signal}", direction=td_signal,
            entry=price, sl=None, tp1=None, tp2=None,
        )
        notified[interval].add(kid)


# ====================== 主程序 ======================
if __name__ == "__main__":
    print("=" * 60)
    print("神奇九转监控程序（老猫视频策略：九转+MACD柱过滤）")
    print(f"监控周期: {INTERVALS} | 品种: {SYMBOL}")
    print(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
    print("=" * 60)

    last_scan_second = -1

    while True:
        now = datetime.now()
        current_second = now.second

        # 每10秒扫描一次全部周期
        if current_second % 10 == 0 and current_second != last_scan_second:
            for cycle in INTERVALS:
                scan(cycle)
            last_scan_second = current_second

        time.sleep(0.1)


