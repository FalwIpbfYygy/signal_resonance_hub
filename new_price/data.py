# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 数据模块
Binance fapi K线获取 + 按 open_time 去重增量缓存。
仅基于已收盘K线(minus close_time)参与计算。
"""
import time
from datetime import datetime

import requests

import config


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_klines(interval, limit=None, retries=None):
    """拉取K线(带重试)。返回原始列表，失败返回 None。"""
    limit = limit or config.KLINE_LIMIT
    retries = retries or config.REQUEST_RETRIES
    url = f"{config.KLINE_URL}?symbol={config.SYMBOL}&interval={interval}&limit={limit}"
    for attempt in range(1, retries + 1):
        try:
            print(f"[{_now()}] [尝试 {attempt}/{retries}] 请求 {interval} K线...")
            resp = requests.get(url, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            print(f"[{_now()}] [尝试 {attempt}] 状态码 {resp.status_code}")
        except Exception as e:
            print(f"[{_now()}] [尝试 {attempt}] 异常 {e}，1秒后重试")
            time.sleep(1)
    print(f"[{_now()}] {interval} 全部 {retries} 次重试失败")
    return None


class KlineCache:
    """按 interval 维护去重后的K线缓存。

    原始K线字段(已核验):
      0 open_time  1 open  2 high  3 low  4 close
      5 volume     6 close_time  7 quote_vol  8 trades
      9 taker_buy_base  10 taker_buy_quote  11 ignore
    """

    def __init__(self):
        self._store = {}      # interval -> {open_time: kline}

    def update(self, interval, raw):
        """合并新K线，按 open_time 去重，返回本周期新增收盘K线数。"""
        if not raw:
            return 0
        bucket = self._store.setdefault(interval, {})
        new_closed = 0
        now_ms = int(time.time() * 1000)
        for k in raw:
            open_time = int(k[0])
            close_time = int(k[6])
            if open_time not in bucket:
                bucket[open_time] = k
                if close_time <= now_ms:          # 已收盘才算新增信号触发
                    new_closed += 1
        return new_closed

    def get_arrays(self, interval):
        """按时间排序返回 OHLCV + 主动买入量 数组(仅已收盘K线)。"""
        bucket = self._store.get(interval, {})
        if not bucket:
            return None
        now_ms = int(time.time() * 1000)
        # 排除尚未收盘的最后一根(close_time > now), 保证只算已收盘K线
        klines = [bucket[t] for t in sorted(bucket) if int(bucket[t][6]) <= now_ms]
        if not klines:
            return None
        opens = [float(k[1]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        taker_buys = [float(k[9]) for k in klines]
        close_times = [int(k[6]) for k in klines]
        return {
            "opens": opens, "highs": highs, "lows": lows, "closes": closes,
            "volumes": volumes, "taker_buys": taker_buys,
            "close_times": close_times, "last_close_time": close_times[-1],
        }

    def is_ready(self, interval, min_bars):
        """缓存是否达到最少棒数。"""
        return len(self._store.get(interval, {})) >= min_bars