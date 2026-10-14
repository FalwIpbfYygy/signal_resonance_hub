# -*- coding: utf-8 -*-
"""DXY 美元指数合成模块（SMT 对标）

币安期货无 DXY 合约，改从 TwelveData 拉取 6 个外汇对，按 ICE 美元指数公式
合成 DXY 序列，供 SMT 背离检测使用。

ICE 美元指数公式：
    DXY = 50.14348112
          * EURUSD^(-0.576) * USDJPY^(0.136) * GBPUSD^(-0.119)
          * USDCAD^(0.091)  * USDSEK^(0.042)  * USDCHF^(0.036)

限流约束（TwelveData 免费版）：
    - 每分钟 ≤8 次请求，每天 ≤800 次
    - 因此串行请求 6 个品种，并做长缓存（默认 15 分钟）
    - 周末外汇休市返回空数据，需做空判断
"""
import time
import requests
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

from config import TWELVEDATA_API_KEY, SMT_CACHE_SECONDS, SMT_LOOKBACK


# 合成 DXY 的 6 个外汇对及其指数幂
_FX_PAIRS = ["EUR/USD", "USD/JPY", "GBP/USD", "USD/CAD", "USD/SEK", "USD/CHF"]
_FX_POWERS = {
    "EUR/USD": -0.576,
    "USD/JPY": 0.136,
    "GBP/USD": -0.119,
    "USD/CAD": 0.091,
    "USD/SEK": 0.042,
    "USD/CHF": 0.036,
}
_DXY_COEFF = 50.14348112

# 缓存：{ts: 最近一次拉取时间戳, series: [(ts_ms, dxy_value), ...] 升序}
_cache: Dict = {"ts": 0.0, "series": []}


def _parse_td_dt(dt_str: str) -> Optional[int]:
    """TwelveData 的 'YYYY-MM-DD HH:MM:SS'（UTC）→ 毫秒时间戳。失败返回 None。"""
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _fetch_fx(symbol: str, outputsize: int) -> Dict[int, float]:
    """拉取单个外汇对 1min 收盘序列，返回 {ts_ms: close}（升序）。失败/空返回 {}。"""
    url = ("https://api.twelvedata.com/time_series"
           f"?symbol={symbol}&interval=1min&outputsize={outputsize}"
           f"&apikey={TWELVEDATA_API_KEY}")
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
    except Exception:
        return {}
    if data.get("status") != "ok" or "values" not in data:
        return {}
    out: Dict[int, float] = {}
    for v in data["values"]:
        ts = _parse_td_dt(v.get("datetime", ""))
        if ts is None:
            continue
        try:
            out[ts] = float(v["close"])
        except (TypeError, ValueError):
            continue
    return out


def _synthesize(fx_data: Dict[str, Dict[int, float]]) -> List[Tuple[int, float]]:
    """按时间戳对齐 6 个外汇对，逐分钟合成 DXY 序列（升序）。"""
    if not fx_data:
        return []
    # 取所有品种时间戳交集，保证每个时间点 6 个品种都有值
    common_ts = None
    for sym, d in fx_data.items():
        ts_set = set(d.keys())
        common_ts = ts_set if common_ts is None else (common_ts & ts_set)
    if not common_ts:
        return []
    series = []
    for ts in sorted(common_ts):
        product = 1.0
        ok = True
        for sym, power in _FX_POWERS.items():
            c = fx_data.get(sym, {}).get(ts)
            if c is None or c <= 0:
                ok = False
                break
            product *= c ** power
        if ok:
            series.append((ts, _DXY_COEFF * product))
    return series


def fetch_dxy_series(force: bool = False) -> List[Tuple[int, float]]:
    """获取合成 DXY 序列 [(ts_ms, dxy_value), ...]（升序，带缓存）。

    缓存未过期时直接返回缓存，避免频繁请求触发限流。

    关键：无论拉取成功还是失败（空数据/限流），都更新时间戳进入冷却期，
    避免「失败 → 缓存未生效 → 下一轮扫描又重试」的重试风暴导致限流。
    """
    now = time.time()
    if not force and (now - _cache["ts"]) < SMT_CACHE_SECONDS:
        return _cache["series"]

    fx_data: Dict[str, Dict[int, float]] = {}
    for sym in _FX_PAIRS:
        fx_data[sym] = _fetch_fx(sym, SMT_LOOKBACK)
        time.sleep(0.3)  # 串行 + 间隔，避免触发分钟限流

    series = _synthesize(fx_data)
    # 成功/失败都记录冷却时间戳，失败也进入 15 分钟冷却，防止重试风暴
    _cache["ts"] = now
    if series:
        _cache["series"] = series
    return series


def get_latest_dxy(force: bool = False) -> Optional[float]:
    """获取最新合成 DXY 值（当前分钟收盘），无数据返回 None。"""
    series = fetch_dxy_series(force)
    return series[-1][1] if series else None
