import requests
import time
from datetime import datetime
import pandas as pd
import numpy as np

import os
import sys

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store

# ====================== 配置区（只改这里）======================
SYMBOL = ""
INTERVAL = "1m"
LIMIT = 1000
# API_URL = f"https://coinpred.com/api/weatherapi/chart-data?symbol={SYMBOL}&interval={INTERVAL}&limit={LIMIT}"
API_URL = f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}?symbol=&interval=1m&limit=1000"

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

# 策略固定参数
EMA_LENGTH = 100
MIN_PULLBACK_BARS = 2
DOJI_MAX_BODY_RATIO = 0.15
SL_BUFFER = 0.01
TP_RR = 1.0
SCAN_INTERVAL = 10
NOTIFIED_TS = set()

# 平台价差：接口价格 -7 = 你的盘面真实价格
PRICE_OFFSET = 7

# ====================== 通用工具函数 ======================
def send_feishu_msg(text: str):
    headers = {"Content-Type": "application/json"}
    payload = {
        "msg_type": "text",
        "content": {"text": text}
    }
    try:
        res = requests.post(FEISHU_WEBHOOK, json=payload, headers=headers, timeout=60)
        if res.status_code == 200 and res.json().get("code") == 0:
            print(f"[{datetime.now()}] 飞书推送成功")
        else:
            print(f"[{datetime.now()}] 飞书推送失败，返回：{res.text}")
    except Exception as e:
        print(f"[{datetime.now()}] 飞书请求异常：{str(e)}")

def fetch_klines(retries=3):
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(API_URL)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    rows = []
                    for item in data:
                        ts = item[6]  # close_time（毫秒），统一用收盘时间
                        o = float(item[1])
                        h = float(item[2])
                        l = float(item[3])
                        c = float(item[4])
                        vol = 0.0
                        rows.append([ts, o, h, l, c, vol])
                    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df = df.sort_values("timestamp").reset_index(drop=True)
                    return df
            print(f"接口异常，状态码：{resp.status_code}")
        except Exception as e:
            print(f"拉取重试{attempt}：{e}")
        time.sleep(1)
    return None

def calc_heikin_ashi(df):
    ha = df.copy()
    ha["HAC"] = (ha["open"] + ha["high"] + ha["low"] + ha["close"]) / 4
    ha["HAO"] = np.nan
    ha.loc[0, "HAO"] = ha.loc[0, "open"]
    for i in range(1, len(ha)):
        ha.loc[i, "HAO"] = (ha.loc[i-1, "HAO"] + ha.loc[i-1, "HAC"]) / 2
    ha["HAH"] = ha[["high", "HAO", "HAC"]].max(axis=1)
    ha["HAL"] = ha[["low", "HAO", "HAC"]].min(axis=1)
    ha["ha_bull"] = ha["HAC"] > ha["HAO"]
    return ha

def calc_ema(series, length):
    ema = pd.Series(np.nan, index=series.index)
    init_sma = series.iloc[:length].mean()
    ema.iloc[length - 1] = init_sma
    alpha = 2 / (length + 1)
    for i in range(length, len(series)):
        ema.iloc[i] = series.iloc[i] * alpha + ema.iloc[i-1] * (1 - alpha)
    return ema

def check_clean_pullback(ha_df, curr_idx, trend):
    count = 0
    start_back = curr_idx - 1
    end_back = max(-1, curr_idx - 20)
    for i in range(start_back, end_back, -1):
        row = ha_df.iloc[i]
        if trend == "up":
            if not row["ha_bull"] and abs(row["HAH"] - row["HAO"]) < 1e-6:
                count += 1
            else:
                break
        else:
            if row["ha_bull"] and abs(row["HAL"] - row["HAO"]) < 1e-6:
                count += 1
            else:
                break
    return count >= MIN_PULLBACK_BARS, count

def is_doji(ha_row):
    body = abs(ha_row["HAC"] - ha_row["HAO"])
    price_range = ha_row["HAH"] - ha_row["HAL"]
    if price_range < 1e-8:
        return False
    return (body / price_range) <= DOJI_MAX_BODY_RATIO

# ====================== 单次全量检测 ======================
def run_check():
    df_raw = fetch_klines()
    if df_raw is None or len(df_raw) < EMA_LENGTH + 10:
        print(f"[{datetime.now()}] K线长度不足，跳过")
        return

    df_ha = calc_heikin_ashi(df_raw)
    df_ha["ema_100"] = calc_ema(df_ha["close"], EMA_LENGTH)
    signal_list = []

    for idx in range(EMA_LENGTH, len(df_ha)):
        row = df_ha.iloc[idx]
        ts = int(row["timestamp"])
        if ts in NOTIFIED_TS:
            continue

        close_price = float(row["close"])
        ema_val = float(row["ema_100"])
        trend = None
        if close_price > ema_val:
            trend = "up"
        elif close_price < ema_val:
            trend = "down"
        else:
            continue

        pull_ok, pull_cnt = check_clean_pullback(df_ha, idx, trend)
        if not pull_ok:
            continue
        if not is_doji(row):
            continue

        dt_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
        entry_price = close_price
        ha_h = float(row["HAH"])
        ha_l = float(row["HAL"])

        if trend == "up":
            side = "做多"
            sl = ha_l - SL_BUFFER
            risk = entry_price - sl
            tp = entry_price + risk * TP_RR
        else:
            side = "做空"
            sl = ha_h + SL_BUFFER
            risk = sl - entry_price
            tp = entry_price - risk * TP_RR

        # 统一所有价格扣减7个点，适配你的平台
        entry_real = entry_price - PRICE_OFFSET
        sl_real = sl - PRICE_OFFSET
        tp_real = tp - PRICE_OFFSET

        NOTIFIED_TS.add(ts)
        signal_list.append({
            "time": dt_str,
            "side": side,
            "pull_count": pull_cnt,
            "entry": round(entry_real, 2),
            "sl": round(sl_real, 2),
            "tp": round(tp_real, 2),
            "src_entry": round(entry_price, 2)
        })

    if len(signal_list) > 0:
        msg = "====  1分钟策略触发信号（已扣减7点）====\n"
        for sig in signal_list:
            msg += f"""
K线时间：{sig['time']}
交易方向：{sig['side']}
回调K线数：{sig['pull_count']}
接口原始入场：{sig['src_entry']}
你的平台入场：{sig['entry']}
止损SL：{sig['sl']}
止盈TP(1:1)：{sig['tp']}
----------------------------------------------------
"""
        print(msg)
        send_feishu_msg(msg)
        # 统一入库（TP1=原tp，TP2 补 2R）
        for sig in signal_list:
            _risk = abs(sig["entry"] - sig["sl"])
            _tp2 = sig["entry"] + _risk * 2 if sig["side"] == "做多" else sig["entry"] - _risk * 2
            signal_store.record_signal(
                signal_time=sig["time"], symbol=SYMBOL, interval=INTERVAL,
                pattern="EMA100+HA回调", direction=sig["side"],
                entry=sig["entry"], sl=sig["sl"], tp1=sig["tp"], tp2=_tp2,
            )
    else:
        print(f"[{datetime.now()}] 本轮无信号")

# ====================== 常驻实时循环 ======================
if __name__ == "__main__":
    print("========  1min HA+EMA100 实时监控启动 ========")
    print(f"数据源：{API_URL}")
    print(f"轮询间隔：{SCAN_INTERVAL}秒")
    print(f"价格修正：接口价格-{PRICE_OFFSET}适配你的交易平台")
    print(f"飞书推送已开启\n")

    last_execute = 0
    while True:
        current = time.time()
        if current - last_execute >= SCAN_INTERVAL:
            run_check()
            last_execute = current
        time.sleep(0.5)
