"""Binance API 数据拉取"""
import requests
import time
from datetime import datetime
from typing import List, Optional, Dict

from config import SYMBOL, BINANCE_FUTURES_URL, KLINE_LIMIT, ALL_TIMEFRAMES
from data.candle import Candle
from utils.time_utils import ms_to_datetime


def parse_kline(raw: list) -> Candle:
    """将Binance API原始K线数组解析为Candle对象"""
    return Candle(
        open_time=ms_to_datetime(raw[0]),
        close_time=ms_to_datetime(raw[6]),
        open=float(raw[1]),
        high=float(raw[2]),
        low=float(raw[3]),
        close=float(raw[4]),
        volume=float(raw[5]),
        quote_volume=float(raw[7]),
        trades=int(raw[8]),
        taker_buy_volume=float(raw[9]),
    )


def fetch_klines(interval: str, limit: int = None, retries: int = 3) -> Optional[List[Candle]]:
    """拉取单时间框架K线数据，带重试。

    收盘驱动：剔除未收盘的最后一根 K 线（close_time 未到），避免未来函数。
    """
    if limit is None:
        limit = KLINE_LIMIT.get(interval, 300)
    url = f"{BINANCE_FUTURES_URL}?symbol={SYMBOL}&interval={interval}&limit={limit}"

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url)
            if resp.status_code == 200:
                raw_data = resp.json()
                candles = [parse_kline(r) for r in raw_data]
                return _drop_unclosed(candles)
            print(f"[{interval}] 请求失败，状态码: {resp.status_code}")
        except Exception as e:
            print(f"[{interval}] 请求异常: {e}，{1}秒后重试...")
            time.sleep(1)

    print(f"[{interval}] 全部 {retries} 次重试均失败")
    return None


def _drop_unclosed(candles: List[Candle]) -> List[Candle]:
    """剔除未收盘的最后一根 K 线（close_time 未到当前时刻）。"""
    if not candles:
        return candles
    now = datetime.now(candles[-1].close_time.tzinfo)
    return [c for c in candles if c.close_time <= now]


def fetch_all_timeframes() -> Dict[str, List[Candle]]:
    """拉取所有时间框架的K线数据"""
    result = {}
    for tf in ALL_TIMEFRAMES:
        candles = fetch_klines(tf)
        if candles:
            result[tf] = candles
        else:
            print(f"⚠️ [{tf}] 数据拉取失败，跳过")
    return result


def get_latest_candle(candles: List[Candle]) -> Candle:
    """获取最新一根K线"""
    return candles[-1] if candles else None


def is_candle_closed(candle: Candle) -> bool:
    """判断K线是否已完成（收盘时间已过）"""
    now = datetime.now(candle.close_time.tzinfo)
    return now >= candle.close_time