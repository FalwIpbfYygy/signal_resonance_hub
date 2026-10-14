

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
INTERVALS = ["5m", "15m", "30m"]
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
# 斐波那契扩展常用档位
FIB_EXT = [1.272, 2.0, 2.24, 2.618, 3.0, 3.618]
# ===================================================

# 防重复通知：key=周期, value=已推送K线时间戳
notified = {interval: set() for interval in INTERVALS}
# 记录反转区测试次数：{interval: {"range_key": 次数}}
prz_test_count = {interval: dict() for interval in INTERVALS}


def send_feishu(msg_text):
    """发送飞书消息"""
    msg = {"msg_type": "text", "content": {"text": msg_text.strip()}}
    send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        response = requests.post(FEISHU_WEBHOOK, json=msg)
        print(f"[{send_time}] 飞书发送成功，状态码: {response.status_code}")
    except Exception as e:
        print(f"[{send_time}] 飞书发送失败: {str(e)}")


def get_klines_retry(interval, limit=120, retries=3):
    """获取币安K线，带重试"""
    url = f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}?symbol={SYMBOL}&interval={interval}&limit={limit}"
    for attempt in range(1, retries + 1):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            print(f"[{now}] [{interval}] [尝试 {attempt}/{retries}] 请求K线...")
            resp = requests.get(url)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"[{now}] [{interval}] HTTP {resp.status_code}")
        except Exception as e:
            print(f"[{now}] [{interval}] 请求异常：{str(e)}")
        time.sleep(1)
    print(f"[{datetime.now()}] [{interval}] 请求全部失败")
    return None


def swing_high_low(klines, start_idx, lookback):
    """简易波段极值搜索，寻找连续波段A、B、C极值（影线高低）"""
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    points = []
    i = start_idx
    while i >= lookback:
        # 寻找波段高点
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            points.append({"idx": i, "price": highs[i], "type": "H"})
        # 寻找波段低点
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            points.append({"idx": i, "price": lows[i], "type": "L"})
        i -= 1
    # 按时间先后排序（早→晚）
    points.sort(key=lambda x: x["idx"])
    return points


def find_abcd_structure(klines):
    """
    识别AB=CD谐波结构（熊猫教练规则）
    返回: 结构信息 or None
    看涨ABCD: A(L) → B(H) → C(L回调) → D(底部反转做多)
    看跌ABCD: A(H) → B(L) → C(H回调) → D(顶部反转做空)
    """
    swing_points = swing_high_low(klines, len(klines)-2, 10)
    if len(swing_points) < 3:
        return None

    # 遍历连续三段波段尝试匹配 A-B-C
    for n in range(len(swing_points)-2):
        A = swing_points[n]
        B = swing_points[n+1]
        C = swing_points[n+2]
        # 过滤必须交替高低
        if A["type"] == B["type"] or B["type"] == C["type"]:
            continue

        A_price = A["price"]
        B_price = B["price"]
        C_price = C["price"]

        # ========== 计算AB斐波回撤，校验C点 [0.382,0.886] ==========
        ab_len = abs(B_price - A_price)
        if ab_len < 0.1:
            continue
        retrace = abs(C_price - B_price) / ab_len

        if not (0.382 <= retrace <= 0.886):
            continue

        # 区分看涨 / 看跌ABCD
        if A["type"] == "L" and B["type"] == "H" and C["type"] == "L":
            pattern_direction = "BUY"  # 看涨ABCD，D区域底部做多
        elif A["type"] == "H" and B["type"] == "L" and C["type"] == "H":
            pattern_direction = "SELL"  # 看跌ABCD，D区域顶部做空
        else:
            continue

        # ========== 腿二线：AB等长平移得到理论CD目标 ==========
        if pattern_direction == "BUY":
            leg2_target = C_price - ab_len
        else:
            leg2_target = C_price + ab_len

        # ========== BC斐波扩展，寻找距离腿二线最近档位，生成PRZ反转区 ==========
        bc_len = abs(C_price - B_price)
        if bc_len < 0.1:
            continue
        extend_values = [B_price + fac * (C_price - B_price) for fac in FIB_EXT]
        # 找到距离腿二线最近的扩展价位
        closest_val = min(extend_values, key=lambda x: abs(x - leg2_target))
        # 反转区间：closest_val ± 小幅缓冲区（黄金品种默认0.8，可自行微调）
        buffer = 0.8
        prz_low = min(closest_val, leg2_target) - buffer
        prz_high = max(closest_val, leg2_target) + buffer

        # ========== 止盈止损点位（视频规则） ==========
        # AD波段
        if pattern_direction == "BUY":
            D_ref = min(prz_low, prz_high)
        else:
            D_ref = max(prz_low, prz_high)
        ad_range = abs(D_ref - A_price)
        tp1 = D_ref + ad_range * 0.382 if pattern_direction == "BUY" else D_ref - ad_range * 0.382
        tp2 = D_ref + ad_range * 0.618 if pattern_direction == "BUY" else D_ref - ad_range * 0.618
        sl_line = D_ref - ad_range * 1.272 if pattern_direction == "BUY" else D_ref + ad_range * 1.272

        structure = {
            "dir": pattern_direction,
            "A": A_price,
            "B": B_price,
            "C": C_price,
            "leg2_target": leg2_target,
            "prz_low": prz_low,
            "prz_high": prz_high,
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "sl": round(sl_line, 2),
            "C_retrace": round(retrace, 3),
            "A_idx": A["idx"],
            "C_idx": C["idx"]
        }
        return structure
    return None


def check_reversal_candle(klines, idx, direction):
    """
    反转K线确认（裸K验证，视频：裸K入场、谐波止盈）
    BUY：锤子、看涨吞没；SELL：流星、看跌吞没
    """
    if idx < 2:
        return False
    k0 = klines[idx]
    k1 = klines[idx-1]
    o0, h0, l0, c0 = float(k0[1]), float(k0[2]), float(k0[3]), float(k0[4])
    o1, h1, l1, c1 = float(k1[1]), float(k1[2]), float(k1[3]), float(k1[4])

    body0 = abs(c0 - o0)
    wick_down = o0 - l0 if c0 >= o0 else c0 - l0
    wick_up = h0 - c0 if c0 >= o0 else h0 - o0

    if direction == "BUY":
        # 看涨条件：长下影反转K
        if wick_down > body0 * 1.8 and c0 > o0:
            return True
        # 看涨吞没
        if c0 > o0 and o1 > c1 and c0 > h1 and o0 < l1:
            return True
    else:
        # 看跌条件：长上影流星
        if wick_up > body0 * 1.8 and c0 < o0:
            return True
        # 看跌吞没
        if c0 < o0 and c1 > o1 and c0 < l1 and o0 > h1:
            return True
    return False


def scan(interval):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    klines = get_klines_retry(interval, limit=120)
    if not klines or len(klines) < 15:
        return

    latest_k = klines[-1]
    close_price = float(latest_k[4])
    k_ts = latest_k[0]
    # 信号时间 = 该根 K 线的 close_time（接口第 7 字段，毫秒）
    signal_time_str = datetime.fromtimestamp(int(latest_k[6]) / 1000).strftime("%Y-%m-%d %H:%M:%S")

    # 同一个K线只推送一次
    if k_ts in notified[interval]:
        return

    abcd = find_abcd_structure(klines)
    if not abcd:
        return

    # 当前价格是否进入PRZ反转区间
    in_prz = abcd["prz_low"] <= close_price <= abcd["prz_high"]
    if not in_prz:
        return

    # 反转区唯一标识，限制最多2次测试
    prz_key = f"{abcd['prz_low']:.1f}_{abcd['prz_high']:.1f}"
    test_times = prz_test_count[interval].get(prz_key, 0)
    if test_times >= 2:
        print(f"[{now_str}] [{interval}] 反转区已测试2次，不再开仓")
        return
    prz_test_count[interval][prz_key] = test_times + 1

    # 裸K反转信号确认
    rev_signal = check_reversal_candle(klines, -1, abcd["dir"])
    if not rev_signal:
        print(f"[{now_str}] [{interval}] 进入反转区，但无反转K，等待...")
        return

    # 组装推送消息
    if abcd["dir"] == "BUY":
        title = "【看涨 AB=CD 谐波做多信号】"
        side = "做多"
    else:
        title = "【看跌 AB=CD 谐波做空信号】"
        side = "做空"

    msg = f"""
{title}
品种：{SYMBOL} 周期：{interval}
当前价格：{close_price:.2f}
结构点位
A:{abcd['A']:.2f}  B:{abcd['B']:.2f}  C:{abcd['C']:.2f}
C点AB回撤比例：{abcd['C_retrace']}
📌 PRZ反转区间：[{abcd['prz_low']:.2f}, {abcd['prz_high']:.2f}]
🎯 T1止盈：{abcd['tp1']:.2f}
🎯 T2止盈：{abcd['tp2']:.2f}
⛔ 终极失效止损：{abcd['sl']:.2f}
操作思路：裸K确认入场，谐波测算目标
策略来源：熊猫教练 AB=CD谐波交易法
方向：{side}
触发时间：{now_str}
"""
    send_feishu(msg)
    # 统一入库（入场价用当前价格，止盈止损来自谐波测算）
    signal_store.record_signal(
        signal_time=signal_time_str, symbol=SYMBOL, interval=interval,
        pattern=("AB=CD做多" if abcd["dir"] == "BUY" else "AB=CD做空"),
        direction=abcd["dir"],
        entry=close_price, sl=abcd["sl"], tp1=abcd["tp1"], tp2=abcd["tp2"],
    )
    notified[interval].add(k_ts)


# ====================== 主循环 ======================
if __name__ == "__main__":
    print("=" * 70)
    print("AB=CD谐波形态监测程序 |  5m/15m/30m")
    print("策略：熊猫教练谐波交易AB=CD模型 | 震荡行情优先，单边谨慎使用")
    print("=" * 70)
    last_check_sec = -1

    while True:
        t = datetime.now()
        sec = t.second
        # 每10秒扫描一轮
        if sec % 10 == 0 and sec != last_check_sec:
            for tf in INTERVALS:
                scan(tf)
            last_check_sec = sec
        time.sleep(0.1)