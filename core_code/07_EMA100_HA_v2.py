"""
EMA100 + Heikin-Ashi 策略 v2   (P0-A + P0-B + P1 优化版)
====================================================================
源文件 07_EMA100_HA.py 不动；本文件为并行运行的 v2 升级版。
P0-A 改造（趋势判定 3 重确认）：
  ① 距离过滤：|HA_close − EMA100| > 0.5×ATR 才承认趋势（远离 EMA±3 噪音区）
  ② 斜率过滤：EMA[t] − EMA[t-5] 必须与趋势同向
  ③ K线实体过滤：HA 实体方向与趋势一致
  ④ HA / Raw 统一：全程使用 HA 体系（HAC / HAO / HAH / HAL / ha_bull）
P0-B 改造（Doji 突破确认）：
  ⑤ Doji 出现后，必须等下一根 K（idx+1）收盘价突破 Doji 实体上沿(做多)
     或跌破实体下沿(做空) 才入场 —— 单根 Doji 即入场是最大假突破来源
P1 改造（信号质量 + 仓位）：
  ⑥ Pullback 双重确认：颜色数 ≥ 2 根 + 回撤极值距 EMA ≤ 0.5×ATR
  ⑦ Doji 阈值分级：一级 ≤ 0.10｜二级 ≤ 0.20｜> 0.20 直接砍掉
  ⑧ 1:1.5 赔率[<16;63;16m：TP1 = 1R（减半仓），TP2 = 2R（剩半仓）
  ⑨ SL buffer：HA_low − 0.3×ATR（多）/ HA_high + 0.3×ATR（空）
  ⑩ 价格修正：接口价格 − 7 适配平台
未启用（保留到 v3）：P2 市场状态过滤（Hurst / ATR 比率 / 时段 / 30m 大周期）
====================================================================
"""
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

# ====================== 基础配置 ======================
SYMBOL = ""
INTERVAL = "1m"
LIMIT = 1000
API_URL = f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}?symbol={SYMBOL}&interval={INTERVAL}&limit={LIMIT}"

# 策略参数
EMA_LENGTH             = 100
ATR_PERIOD             = 14
EMA_DISTANCE_ATR_K     = 0.5
EMA_SLOPE_LOOKBACK     = 5
MIN_PULLBACK_BARS      = 2
PULLBACK_DEPTH_ATR_K   = 0.5
DOJI_STRICT_RATIO      = 0.10
DOJI_LOOSE_RATIO       = 0.20
SL_BUFFER_ATR          = 0.3
TP1_R                  = 1.0
TP2_R                  = 2.0
PRICE_OFFSET           = 7
SCAN_INTERVAL          = 10

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

NOTIFIED_TS = set()
BOOT_TS_MS = int(time.time() * 1000)   # 程序启动毫秒时间戳，历史早于此的信号全部忽略
# =====================================================

def send_feishu_msg(text: str):
    headers = {"Content-Type": "application/json"}
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        res = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        if res.status_code == 200 and res.json().get("code") == 0:
            print(f"[{datetime.now()}] 飞书推送成功")
        else:
            print(f"[{datetime.now()}] 飞书推送失败：{res.text}")
    except Exception as e:
        print(f"[{datetime.now()}] 飞书请求异常: {e}")


def fetch_klines(retries=2):
    for _ in range(retries):
        try:
            resp = requests.get(API_URL, timeout=8)
            raw = resp.json()
            rows = []
            for item in raw:
                rows.append([
                    int(item[6]),
                    float(item[1]),
                    float(item[2]),
                    float(item[3]),
                    float(item[4]),
                    float(item[5])
                ])
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            return df
        except Exception as e:
            print(f"K线拉取异常 {e}")
            time.sleep(1)
    return None


def calc_heikin_ashi(df_raw):
    ha = df_raw.copy()
    ha["HAC"] = (ha["open"] + ha["high"] + ha["low"] + ha["close"]) / 4
    ha["HAO"] = np.nan
    ha.loc[0, "HAO"] = ha.loc[0, "open"]
    for i in range(1, len(ha)):
        ha.loc[i, "HAO"] = (ha.loc[i-1, "HAO"] + ha.loc[i-1, "HAC"]) / 2
    ha["HAH"] = ha[["high", "HAO", "HAC"]].max(axis=1)
    ha["HAL"] = ha[["low",  "HAO", "HAC"]].min(axis=1)
    ha["ha_bull"] = ha["HAC"] > ha["HAO"]
    return ha


def calc_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def calc_atr(df, period=14):
    df = df.copy()
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = abs(df["high"] - df["close"].shift(1))
    df["tr3"] = abs(df["low"]  - df["close"].shift(1))
    df["tr"] = df[["tr1","tr2","tr3"]].max(axis=1)
    atr = df["tr"].rolling(window=period).mean()
    return float(atr.iloc[-1])


def check_trend_3confirm(ha_df, idx, ema_now, ema_prev5, atr_now):
    row = ha_df.iloc[idx]
    hac = float(row["HAC"])
    # ①距离过滤
    if abs(hac - ema_now) < EMA_DISTANCE_ATR_K * atr_now:
        return None
    # ②ema斜率
    ema_slope = ema_now - ema_prev5
    # ③HA实体方向
    is_bull = bool(row["ha_bull"])

    up_votes = 0
    down_votes = 0
    if hac > ema_now: up_votes +=1
    if ema_slope > 0: up_votes +=1
    if is_bull: up_votes +=1

    if hac < ema_now: down_votes +=1
    if ema_slope < 0: down_votes +=1
    if not is_bull: down_votes +=1

    if up_votes >=2 and ema_slope>0:
        return "up"
    if down_votes >=2 and ema_slope<0:
        return "down"
    return None


def check_pullback(ha_df, idx, trend, ema_now, atr_now):
    count = 0
    pullback_low = float("inf")
    pullback_high = -float("inf")
    end_i = max(0, idx - 20)
    for i in range(idx-1, end_i-1, -1):
        r = ha_df.iloc[i]
        if trend == "up":
            if not r["ha_bull"]:
                count +=1
                pullback_low = min(pullback_low, float(r["HAL"]))
            else:
                break
        else:
            if r["ha_bull"]:
                count +=1
                pullback_high = max(pullback_high, float(r["HAH"]))
            else:
                break
    if count < MIN_PULLBACK_BARS:
        return False, count, None
    if trend == "up":
        if abs(pullback_low - ema_now) > PULLBACK_DEPTH_ATR_K * atr_now:
            return False, count, None
        return True, count, pullback_low
    else:
        if abs(pullback_high - ema_now) > PULLBACK_DEPTH_ATR_K * atr_now:
            return False, count, None
        return True, count, pullback_high


def classify_doji(ha_row):
    body = abs(float(ha_row["HAC"]) - float(ha_row["HAO"]))
    rng = float(ha_row["HAH"]) - float(ha_row["HAL"])
    if rng < 1e-8:
        return None
    ratio = body / rng
    if ratio <= DOJI_STRICT_RATIO:
        return "一级"
    elif ratio <= DOJI_LOOSE_RATIO:
        return "二级"
    return None


def check_breakout_confirm(ha_df, idx, trend, body_top, body_bottom):
    if idx+1 >= len(ha_df):
        return None
    nx = ha_df.iloc[idx+1]
    hac_nx = float(nx["HAC"])
    ts = int(nx["timestamp"])
    if trend == "up" and hac_nx > body_top:
        return ts
    if trend == "down" and hac_nx < body_bottom:
        return ts
    return None


def run_check():
    df_raw = fetch_klines()
    if df_raw is None or len(df_raw) < EMA_LENGTH + EMA_SLOPE_LOOKBACK + 30:
        print(f"[{datetime.now()}] K线长度不足，跳过")
        return
    df_ha = calc_heikin_ashi(df_raw)
    df_ha["ema100"] = calc_ema(df_ha["close"], EMA_LENGTH)
    atr_now = calc_atr(df_raw, ATR_PERIOD)

    start_idx = EMA_LENGTH + EMA_SLOPE_LOOKBACK
    sig_list = []

    for idx in range(start_idx, len(df_ha)-1):
        row = df_ha.iloc[idx]
        ema_now = float(row["ema100"])
        ema_prev5 = float(df_ha.iloc[idx - EMA_SLOPE_LOOKBACK]["ema100"])
        if np.isnan(ema_now) or np.isnan(ema_prev5):
            continue

        trend = check_trend_3confirm(df_ha, idx, ema_now, ema_prev5, atr_now)
        if trend is None:
            continue

        pull_ok, pull_cnt, _ = check_pullback(df_ha, idx, trend, ema_now, atr_now)
        if not pull_ok:
            continue

        doji_grade = classify_doji(row)
        if doji_grade is None:
            continue

        hao = float(row["HAO"])
        hac = float(row["HAC"])
        b_top = max(hao, hac)
        b_bot = min(hao, hac)
        confirm_ts = check_breakout_confirm(df_ha, idx, trend, b_top, b_bot)
        if confirm_ts is None:
            continue

        # =========核心修复：过滤历史信号=========
        if confirm_ts < BOOT_TS_MS:
            NOTIFIED_TS.add(confirm_ts)
            continue
        if confirm_ts in NOTIFIED_TS:
            continue
        # =======================================

        NOTIFIED_TS.add(confirm_ts)
        confirm_row = df_ha.iloc[idx+1]
        entry_price = float(confirm_row["HAC"])
        ha_h = float(row["HAH"])
        ha_l = float(row["HAL"])

        if trend == "up":
            side = "做多"
            sl = ha_l - SL_BUFFER_ATR * atr_now
            risk = entry_price - sl
            tp1 = entry_price + risk * TP1_R
            tp2 = entry_price + risk * TP2_R
        else:
            side = "做空"
            sl = ha_h + SL_BUFFER_ATR * atr_now
            risk = sl - entry_price
            tp1 = entry_price - risk * TP1_R
            tp2 = entry_price - risk * TP2_R

        if risk <= 1e-6:
            continue

        entry_real = entry_price - PRICE_OFFSET
        sl_real    = sl - PRICE_OFFSET
        tp1_real   = tp1 - PRICE_OFFSET
        tp2_real   = tp2 - PRICE_OFFSET
        ema_real   = ema_now - PRICE_OFFSET

        sig_list.append({
            "time": datetime.fromtimestamp(confirm_ts/1000).strftime("%Y-%m-%d %H:%M"),
            "side": side,
            "doji": doji_grade,
            "pull": pull_cnt,
            "dist": round(abs(entry_price-ema_now)/atr_now,2) if atr_now>0 else 0,
            "entry": round(entry_real,2),
            "sl": round(sl_real,2),
            "tp1": round(tp1_real,2),
            "tp2": round(tp2_real,2),
            "ema": round(ema_real,2),
            "src": round(entry_price,2),
        })

    if sig_list:
        msg = f"====  1min EMA100+HA v2 触发（已扣减{PRICE_OFFSET}点）====\n"
        for sig in sig_list:
            msg += f"""
K线时间：{sig['time']}
交易方向：{sig['side']}｜Doji{sig['doji']}
回调K线数：{sig['pull']}｜EMA距离={sig['dist']}×ATR
EMA 平台价：{sig['ema']}
接口入场：{sig['src']}｜平台入场：{sig['entry']}
止损SL：{sig['sl']}
止盈TP1：{sig['tp1']}（1R · 减半）
止盈TP2：{sig['tp2']}（2R · 剩半）
--------------------------------
"""
        print(msg)
        send_feishu_msg(msg)
        for s in sig_list:
            signal_store.record_signal(
                signal_time=s["time"], symbol=SYMBOL, interval=INTERVAL,
                pattern="EMA100+HA回调v2", direction=s["side"],
                entry=s["entry"], sl=s["sl"], tp1=s["tp1"], tp2=s["tp2"]
            )
    else:
        print(f"[{datetime.now()}] 本轮无信号")


if __name__ == "__main__":
    print("=" * 64)
    print(" 1min EMA100+HA v2 实时监控启动")
    print(f"BOOT_TS_MS={BOOT_TS_MS}｜只识别启动之后新K产生的信号")
    print("P0-A:距离+斜率+实体3重确认｜P0-B:Doji突破确认｜P1:1:1.5赔率")
    print(f"轮询间隔：{SCAN_INTERVAL}秒｜价格修正：接口价-{PRICE_OFFSET}")
    print("=" * 64)
    last_run = 0
    while True:
        now = time.time()
        if now - last_run >= SCAN_INTERVAL:
            run_check()
            last_run = now
        time.sleep(0.5)


