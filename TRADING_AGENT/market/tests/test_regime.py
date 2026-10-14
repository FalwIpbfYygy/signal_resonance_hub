"""market.regime 自检。"""
from __future__ import annotations

from market.regime import detect_regime, KNOWN_REGIMES


def test_news_risk_dominates():
    reg, conf = detect_regime(adx=40, atr=10, atr_ma=10, news_risk=True)
    assert reg == "NEWS_RISK" and conf == 0.9


def test_liquidity_sweep_dominates():
    reg, conf = detect_regime(adx=40, atr=10, atr_ma=10, liquidity_sweep=True)
    assert reg == "LIQUIDITY_SWEEP"


def test_trending_up():
    reg, conf = detect_regime(adx=35, atr=10, atr_ma=10, trend_slope=0.5)
    assert reg == "TRENDING_UP" and conf > 0.5


def test_trending_down():
    reg, _ = detect_regime(adx=30, atr=10, atr_ma=10, trend_slope=-0.4)
    assert reg == "TRENDING_DOWN"


def test_high_volatility():
    reg, _ = detect_regime(adx=10, atr=20, atr_ma=10)
    assert reg == "HIGH_VOLATILITY"


def test_low_volatility():
    reg, _ = detect_regime(adx=10, atr=5, atr_ma=10)
    assert reg == "LOW_VOLATILITY"


def test_ranging_default():
    reg, _ = detect_regime(adx=10, atr=10, atr_ma=10, trend_slope=0.0)
    assert reg == "RANGING"


def test_known_regimes_set():
    assert "TRENDING_UP" in KNOWN_REGIMES
    assert "RANGING" in KNOWN_REGIMES
