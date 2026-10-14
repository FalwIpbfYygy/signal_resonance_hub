import os
import sys

import requests
import time
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store

# ====================== 配置参数 实盘可调 ======================
API_URL = os.environ.get("BINANCE_FUTURES_KLINE_URL")
SYMBOL = ""
INTERVAL = "4h"
LIMIT = 200

# W/M形态参数
MAX_BAR_GAP = 24
MIN_BAR_GAP = 4
ATR_MULT_TOL = 0.30
NECK_MARGIN_ATR = 0.45

POLL_SEC = 60
SIGNAL_DEDUPLICATE_SET = set()
BOOT_DT = datetime.now()  # 程序启动时刻，早于此时间的历史信号全部忽略


FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

#=============================================================

def feishu_send_card(card_payload: dict):
    try:
        headers = {"Content‑Type": "application/json"}
        resp = requests.post(FEISHU_WEBHOOK, json=card_payload, timeout=100)
        resp_json = resp.json()
        return resp_json.get("code") == 0, resp_json
    except Exception as e:
        print(f"[飞书异常] {str(e)}")
        return False, {}

def feishu_build_wm_card(sig: dict):
    sig_type = sig["type"]
    if "W底" in sig_type:
        tpl = "green"
        title_text = "🔥检测到 W底(双底) -4H"
    else:
        tpl = "red"
        title_text = "🔥检测到 M顶(双顶) -4H"

    elements = [
        {
            "tag": "markdown",
            "content": f"**信号产生时间：** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                       f"- 确认K时间：`{sig['confirm_time']}`\n"
                       f"- 颈线价位：`{sig['neck_price']}`"
        },
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "⚠️量化策略信号，仅供参考，不构成投资建议"}]
        }
    ]

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": tpl, "title": {"tag": "plain_text", "content": title_text}},
        "elements": elements
    }
    payload = {"msg_type": "interactive", "card": card}
    return payload

def fetch_kline() -> pd.DataFrame:
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    resp = requests.get(API_URL, params=params, timeout=120)
    raw = resp.json()
    df = pd.DataFrame(raw, columns=[
        "openTime","open","high","low","close","volume",
        "closeTime","quoteVol","tradeCount","takerBase","takerQuote","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["openTime"] = pd.to_datetime(df["openTime"], unit="ms")
    df["closeTime"] = pd.to_datetime(df["closeTime"], unit="ms")
    return df

def calc_atr(df:pd.DataFrame, period=14):
    df = df.copy()
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = abs(df['high'] - df['close'].shift(1))
    df['tr3'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['tr1','tr2','tr3']].max(axis=1)
    df['atr'] = df['tr'].rolling(window=period).mean()
    return df['atr']

def get_local_extremum(df:pd.DataFrame, win=2):
    high_idx = []
    low_idx = []
    arr_high = df["high"].values
    arr_low = df["low"].values
    for i in range(win, len(df)-win):
        is_high = True
        is_low = True
        for d in range(-win, win+1):
            if d == 0:
                continue
            if arr_high[i] <= arr_high[i+d]:
                is_high = False
            if arr_low[i] >= arr_low[i+d]:
                is_low = False
        if is_high:
            high_idx.append(i)
        if is_low:
            low_idx.append(i)
    return high_idx, low_idx

def scan_wm_pattern(df_all:pd.DataFrame):
    df = df_all.iloc[:-1].copy()
    if len(df) < 40:
        return []
    df["atr"] = calc_atr(df)
    high_points, low_points = get_local_extremum(df, win=2)
    sig_list = []

    # W底 双底
    for i, idx_L1 in enumerate(low_points):
        L1_price = df.loc[idx_L1, "low"]
        atr_val = df.loc[idx_L1, "atr"]
        if pd.isna(atr_val):
            continue
        for idx_L2 in low_points:
            gap = idx_L2 - idx_L1
            if not (MIN_BAR_GAP <= gap <= MAX_BAR_GAP):
                continue
            if idx_L2 <= idx_L1:
                continue
            L2_price = df.loc[idx_L2, "low"]
            if abs(L1_price - L2_price) > atr_val * ATR_MULT_TOL:
                continue
            neck_high = df.loc[idx_L1:idx_L2, "high"].max()
            after = df.loc[idx_L2+1:]
            cond = after["close"] > neck_high
            if not np.any(cond):
                continue
            confirm_idx = after[cond].index[0]
            key = f"W|{idx_L1}|{idx_L2}|{confirm_idx}"
            sig_list.append({
                "sig_key":key,
                "type":"W底(双底)",
                "L1_time":str(df.loc[idx_L1,"openTime"]),
                "L1_price":round(L1_price,2),
                "L2_time":str(df.loc[idx_L2,"openTime"]),
                "L2_price":round(L2_price,2),
                "neck_price":round(neck_high,2),
                "confirm_time":str(df.loc[confirm_idx,"openTime"])
            })

    # M顶 双顶
    for i, idx_H1 in enumerate(high_points):
        H1_price = df.loc[idx_H1, "high"]
        atr_val = df.loc[idx_H1, "atr"]
        if pd.isna(atr_val):
            continue
        for idx_H2 in high_points:
            gap = idx_H2 - idx_H1
            if not (MIN_BAR_GAP <= gap <= MAX_BAR_GAP):
                continue
            if idx_H2 <= idx_H1:
                continue
            H2_price = df.loc[idx_H2, "high"]
            if abs(H1_price - H2_price) > atr_val * ATR_MULT_TOL:
                continue
            neck_low = df.loc[idx_H1:idx_H2, "low"].min()
            after = df.loc[idx_H2+1:]
            cond = after["close"] < neck_low
            if not np.any(cond):
                continue
            confirm_idx = after[cond].index[0]
            key = f"M|{idx_H1}|{idx_H2}|{confirm_idx}"
            sig_list.append({
                "sig_key":key,
                "type":"M顶(双顶)",
                "H1_time":str(df.loc[idx_H1,"openTime"]),
                "H1_price":round(H1_price,2),
                "H2_time":str(df.loc[idx_H2,"openTime"]),
                "H2_price":round(H2_price,2),
                "neck_price":round(neck_low,2),
                "confirm_time":str(df.loc[confirm_idx,"openTime"])
            })
    return sig_list

def _record_signal(sig: dict):
    """把 W/M 形态信号写入统一信号库（供信号共振统计）。

    时间口径说明：`confirm_time` 由 pd.to_datetime(openTime_ms, unit="ms") 得来，
    是 **UTC 裸时间**；而其他系统（10_harami_realtime / 11_paxg_engulfing）用的是
    datetime.fromtimestamp() 的"系统本地时间"。直接入库会差 8 小时，
    所以这里显式按 UTC 解析成 epoch 秒再入库，由 signal_store 统一按本地时区渲染。

    该脚本只产出颈线价（突破入场位），本身不计算止损/止盈，SL/TP 留空。
    """
    try:
        dt_utc = datetime.strptime(str(sig.get("confirm_time", "")),
                                   "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as e:
        print(f"[入库跳过] confirm_time 无法解析: {sig.get('confirm_time')} ({e})")
        return
    is_w = "W底" in sig.get("type", "")
    try:
        signal_store.record_signal(
            signal_time=dt_utc.timestamp(), symbol=SYMBOL, interval=INTERVAL,
            pattern=sig.get("type", ""), direction="做多" if is_w else "做空",
            entry=sig.get("neck_price"), sl=None, tp1=None, tp2=None,
        )
    except Exception as e:
        print(f"[入库异常] {sig.get('type')}: {e}")


def main_loop():
    print(f"======== W/M检测器启动 {BOOT_DT.strftime('%Y-%m-%d %H:%M:%S')} 周期{INTERVAL} ========")
    # 已移除：feishu_test_connect 连通测试，不再发送启动测试卡片，也不打印✅连通测试信息

    while True:
        try:
            df_k = fetch_kline()
            sigs = scan_wm_pattern(df_k)
            for sig in sigs:
                sk = sig["sig_key"]
                if sk not in SIGNAL_DEDUPLICATE_SET:
                    confirm_dt = datetime.fromisoformat(sig["confirm_time"])
                    # 过滤历史旧信号，只保留程序启动之后新确认的形态
                    if confirm_dt < BOOT_DT:
                        SIGNAL_DEDUPLICATE_SET.add(sk)
                        continue

                    SIGNAL_DEDUPLICATE_SET.add(sk)
                    print("\n" + "="*70)
                    print(f"🔥【检测到形态信号】 {datetime.now()}")
                    for k,v in sig.items():
                        print(f"{k}: {v}")
                    print("="*70+"\n")

                    card_payload = feishu_build_wm_card(sig)
                    ok,resp = feishu_send_card(card_payload)
                    if not ok:
                        print(f"⚠️飞书卡片推送失败！返回:{resp}")
                    _record_signal(sig)
            time.sleep(POLL_SEC)
        except Exception as e:
            print(f"异常捕获 {datetime.now()} error:{str(e)}")
            time.sleep(5)

if __name__ == "__main__":
    main_loop()



