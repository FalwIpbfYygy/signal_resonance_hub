# -*- coding: utf-8 -*-
"""数据获取模块：币安现货 K 线，带重试与代理，并按收盘时间过滤未收盘 K 线。"""

import time
import requests
from datetime import datetime, timezone

import config


def fetch_klines(interval, limit=None, retries=None, timeout=None):
    """拉取 K 线，返回「已收盘」K 线的字典列表。

    返回每根 K 线为 dict：
        open_time, open, high, low, close, volume, quote_volume,
        trades, close_time, taker_buy_base, taker_buy_quote
    """
    limit = limit or config.LIMIT
    retries = retries or config.REQUEST_RETRIES
    timeout = timeout or config.REQUEST_TIMEOUT

    url = f"{config.BASE_URL}?symbol={config.SYMBOL}&interval={interval}&limit={limit}"

    for attempt in range(1, retries + 1):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                bars = _parse_klines(data)
                bars = _drop_unclosed(bars)
                if bars:
                    print(f"[{now}][{interval}] 数据成功：{len(bars)} 根已收盘 K 线")
                return bars
            else:
                print(f"[{now}][{interval}] 请求失败，状态码 {resp.status_code}")
        except Exception as e:
            print(f"[{now}][{interval}] 请求异常：{e}，1 秒后重试")
        time.sleep(1)

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{interval}] 重试 {retries} 次均失败")
    return []


def _parse_klines(data):
    bars = []
    for k in data:
        bars.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": int(k[6]),
            "quote_volume": float(k[7]),
            "trades": int(k[8]),
            "taker_buy_base": float(k[9]),
            "taker_buy_quote": float(k[10]),
        })
    return bars


def _drop_unclosed(bars, now_ms=None):
    """收盘驱动：剔除未收盘的最后一根 K 线（close_time 未到）。"""
    if not bars:
        return bars
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    # 反转数组便于从尾部删除未收盘 K 线
    kept = [b for b in bars if b["close_time"] <= now_ms]
    return kept