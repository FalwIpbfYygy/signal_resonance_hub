"""Market Regime Detector（确定性启发式，无 LLM）。

输入：ADX（趋势强度）、ATR（当前波动）、ATR 滚动均值（波动基准）、趋势斜率（正/负）、
      liquidity_sweep / news_risk 标志。
输出：regime 标签 + 置信度。
"""
from __future__ import annotations

from typing import Tuple

ADX_TREND_THRESHOLD = 25.0


def detect_regime(
    adx: float,
    atr: float,
    atr_ma: float,
    trend_slope: float = 0.0,
    liquidity_sweep: bool = False,
    news_risk: bool = False,
) -> Tuple[str, float]:
    if news_risk:
        return "NEWS_RISK", 0.9
    if liquidity_sweep:
        return "LIQUIDITY_SWEEP", 0.8
    if adx > ADX_TREND_THRESHOLD:
        if trend_slope > 0:
            return "TRENDING_UP", min(1.0, adx / 50.0)
        if trend_slope < 0:
            return "TRENDING_DOWN", min(1.0, adx / 50.0)
    if atr_ma > 0 and atr > atr_ma * 1.5:
        return "HIGH_VOLATILITY", 0.7
    if atr_ma > 0 and atr < atr_ma * 0.7:
        return "LOW_VOLATILITY", 0.6
    return "RANGING", 0.6


# 主流程选用的全部 regime（供校验）
KNOWN_REGIMES = {
    "TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOLATILITY",
    "LOW_VOLATILITY", "LIQUIDITY_SWEEP", "NEWS_RISK", "UNCERTAIN",
}
