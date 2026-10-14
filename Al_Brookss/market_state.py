# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 市场状态判断模块
实现Brooks三态判定：趋势(多头/空头) / 交易区间(TR) / 紧凑交易区间(TTR)

理论依据：
- 趋势篇P55: "市场任何时候只存在两种状态——趋势或交易区间"
- 趋势篇P55: "市场经常表现出惯性，倾向于继续做刚刚在做的事情"
- 区间篇P144: "紧凑交易区间内不应做任何交易"
"""
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional

from indicators import (
    calc_ema, calc_atr, calc_ema_slope, calc_avg_entity,
    calc_atr_ratio, calc_hurst, calc_overlap_ratio,
    calc_close_series_direction, calc_price_above_ema_ratio,
    calc_ema_cross_count, find_range_bounds, find_swing_highs,
    find_swing_lows, calc_taker_buy_ratio
)
import config


@dataclass
class MarketStateResult:
    """市场状态判断结果"""
    state: str = "unknown"                  # trend_up / trend_down / tr / ttr
    ema20: float = 0.0                      # 最新EMA20值
    ema_slope: float = 0.0                  # EMA20斜率
    atr: float = 0.0                        # ATR(14)
    atr_ratio: float = 1.0                  # ATR比率(平均实体/ATR)
    hurst: float = 0.5                      # Hurst指数
    overlap_ratio: float = 0.5              # 棒线重叠率
    close_direction: int = 0                # 收盘价序列方向(正=上升主导)
    price_above_ema_ratio: float = 0.5      # 价格在EMA上方比例
    ema_cross_count: int = 0                # 穿越EMA次数
    range_high: float = 0.0                 # 区间上边界
    range_low: float = 0.0                  # 区间下边界
    taker_buy_ratio: float = 0.5            # 主动买入占比(买压指标)
    is_tradeable: bool = False              # 是否可交易(非TTR)
    trend_bars: int = 0                     # 当前趋势持续棒数
    swing_highs: list = field(default_factory=list)  # 波段高点列表
    swing_lows: list = field(default_factory=list)   # 波段低点列表
    ema_series: np.ndarray = field(default_factory=lambda: np.array([]))
    avg_entity: float = 0.0                 # 20棒平均实体大小


class MarketStateJudge:
    """市场状态判断器

    进场前必须先判断市场处于频谱的哪个位置：
    - 极端趋势 → 顺势止损单入场+加仓
    - 强趋势(尖峰期) → H1/L1顺势入场
    - 通道趋势 → H2/L2顺势入场
    - 交易区间(TR) → 顶底反转交易
    - 紧凑TR(TTR) → 不交易
    """

    def judge(self, opens, highs, lows, closes, volumes=None, taker_buy_volumes=None):
        """判断市场状态

        Args:
            opens: 开盘价数组
            highs: 最高价数组
            lows: 最低价数组
            closes: 收盘价数组
            volumes: 成交量数组(可选)
            taker_buy_volumes: 主动买入量数组(可选)

        Returns:
            MarketStateResult
        """
        opens = np.array(opens, dtype=float)
        highs = np.array(highs, dtype=float)
        lows = np.array(lows, dtype=float)
        closes = np.array(closes, dtype=float)

        result = MarketStateResult()

        # 数据不足时返回未知状态
        min_bars = max(config.TREND_CLOSE_SERIES_PERIOD, config.EMA_PERIOD) + 5
        if len(closes) < min_bars:
            result.state = "unknown"
            result.is_tradeable = False
            return result

        # === 计算所有量化指标 ===
        # EMA20序列
        result.ema_series = calc_ema(closes, config.EMA_PERIOD)
        result.ema20 = float(result.ema_series[-1])

        # EMA20斜率(趋势方向+成熟度)
        result.ema_slope = calc_ema_slope(result.ema_series, config.EMA_SLOPE_PERIOD)

        # ATR(14) - 波动率+自适应止损基础
        result.atr = calc_atr(highs, lows, closes, config.ATR_PERIOD)

        # ATR比率 - 判定紧凑TR(<0.8=低波动压缩)
        result.atr_ratio = calc_atr_ratio(opens, closes, result.atr)

        # Hurst指数 - 趋势持续性判断(<0.55=均值回归)
        result.hurst = calc_hurst(closes, config.HURST_PERIOD)

        # 棒线重叠率 - TR/TTR判定(>0.6=紧凑)
        result.overlap_ratio = calc_overlap_ratio(highs, lows, 10)

        # 收盘价序列方向(上升/下降主导)
        result.close_direction = calc_close_series_direction(
            closes, config.TREND_CLOSE_SERIES_PERIOD
        )

        # 价格在EMA上方比例(>0.6=多头趋势)
        result.price_above_ema_ratio = calc_price_above_ema_ratio(
            closes, result.ema_series, config.TREND_CLOSE_SERIES_PERIOD
        )

        # 穿越EMA次数(多=交易区间)
        result.ema_cross_count = calc_ema_cross_count(
            closes, result.ema_series, config.TREND_CLOSE_SERIES_PERIOD
        )

        # 区间边界(最近50棒)
        result.range_high, result.range_low = find_range_bounds(highs, lows, 50)

        # 主动买入占比(买压/卖压)
        if taker_buy_volumes is not None and volumes is not None:
            result.taker_buy_ratio = calc_taker_buy_ratio(
                taker_buy_volumes, volumes, 10
            )

        # 平均实体大小
        result.avg_entity = calc_avg_entity(opens, closes, config.AVG_ENTITY_PERIOD)

        # 趋势持续棒数
        result.trend_bars = self._calc_trend_bars(closes, result.ema_series)

        # 波段高低点
        result.swing_highs = find_swing_highs(highs, lookback=2)
        result.swing_lows = find_swing_lows(lows, lookback=2)

        # === 核心三态判定 ===
        result.state = self._determine_state(result)
        result.is_tradeable = result.state != "ttr"

        return result

    def _calc_trend_bars(self, closes, ema):
        """计算当前趋势持续了多少棒

        从最后一根K线向前追溯，直到价格与EMA的关系发生反转
        """
        if len(closes) < 2 or len(ema) < 2:
            return 0
        bars = 0
        is_bull = closes[-1] > ema[-1]
        for i in range(len(closes) - 1, max(0, len(closes) - 300), -1):
            curr_bull = closes[i] > ema[i]
            if curr_bull != is_bull:
                break
            bars += 1
        return bars

    def _determine_state(self, r: MarketStateResult) -> str:
        """核心三态判定逻辑

        判定优先级：
        1. 先判定是否为交易区间(TR)
        2. 若是TR，进一步判定是否为紧凑TR(TTR) → 不交易
        3. 若非TR，判定趋势方向(多头/空头)
        4. 默认归类为TR
        """
        # 判定交易区间
        is_tr = self._is_trading_range(r)

        if is_tr:
            # TTR判定: TR + 低波动 + 高重叠 → 不交易
            if (r.atr_ratio < config.ATR_RATIO_THRESHOLD and
                    r.overlap_ratio > config.OVERLAP_RATIO_THRESHOLD):
                return "ttr"
            return "tr"

        # 判定趋势方向
        if self._is_bull_trend(r):
            return "trend_up"
        if self._is_bear_trend(r):
            return "trend_down"

        # 无法明确判定趋势，默认为交易区间
        return "tr"

    def _is_trading_range(self, r: MarketStateResult) -> bool:
        """判断是否为交易区间

        条件(满足2个以上=交易区间):
        1. EMA斜率近水平
        2. 价格在EMA上下穿梭(穿越次数≥4)
        3. 收盘方向不明显(上升/下降棒数接近)
        4. Hurst<0.55(均值回归)
        """
        # EMA斜率近水平(斜率绝对值小于阈值的百分比)
        slope_flat = abs(r.ema_slope) < config.TREND_SLOPE_THRESHOLD * r.ema20

        # 穿越EMA次数多(≥4次=交易区间)
        high_cross = r.ema_cross_count >= 4

        # 收盘方向不明显(|方向值|<4)
        neutral_direction = abs(r.close_direction) < 4

        # Hurst<0.55倾向均值回归
        low_hurst = r.hurst < config.HURST_THRESHOLD

        conditions = [slope_flat, high_cross, neutral_direction, low_hurst]
        return sum(conditions) >= 2

    def _is_bull_trend(self, r: MarketStateResult) -> bool:
        """判断多头趋势

        条件(满足3个以上=多头趋势):
        1. EMA斜率向上
        2. 收盘价序列上升(上升棒数明显多于下降)
        3. 价格大部分在EMA上方(≥60%)
        4. Hurst≥0.55(趋势持续)
        """
        # EMA斜率向上
        slope_up = r.ema_slope > config.TREND_SLOPE_THRESHOLD * r.ema20

        # 收盘价序列上升(方向值>4, 约等于上升棒数≥12)
        close_up = r.close_direction > 4

        # 价格大部分在EMA上方
        price_above = r.price_above_ema_ratio >= config.TREND_PRICE_ABOVE_EMA_RATIO

        # Hurst≥0.55(趋势持续性)
        hurst_ok = r.hurst >= config.HURST_THRESHOLD

        conditions = [slope_up, close_up, price_above, hurst_ok]
        return sum(conditions) >= 3

    def _is_bear_trend(self, r: MarketStateResult) -> bool:
        """判断空头趋势(与多头对称)"""
        # EMA斜率向下
        slope_down = r.ema_slope < -config.TREND_SLOPE_THRESHOLD * r.ema20

        # 收盘价序列下降
        close_down = r.close_direction < -4

        # 价格大部分在EMA下方
        price_below = r.price_above_ema_ratio <= (1 - config.TREND_PRICE_ABOVE_EMA_RATIO)

        # Hurst≥0.55
        hurst_ok = r.hurst >= config.HURST_THRESHOLD

        conditions = [slope_down, close_down, price_below, hurst_ok]
        return sum(conditions) >= 3

    def get_state_description(self, r: MarketStateResult) -> str:
        """获取市场状态的中文描述(用于飞书推送)"""
        desc_map = {
            "trend_up": "多头趋势",
            "trend_down": "空头趋势",
            "tr": "交易区间",
            "ttr": "紧凑区间(不交易)",
            "unknown": "数据不足",
        }
        state_desc = desc_map.get(r.state, "未知")

        # EMA方向
        if r.ema_slope > config.TREND_SLOPE_THRESHOLD * r.ema20:
            ema_dir = "向上"
        elif r.ema_slope < -config.TREND_SLOPE_THRESHOLD * r.ema20:
            ema_dir = "向下"
        else:
            ema_dir = "水平"

        return f"{state_desc}(EMA20 {ema_dir}, ATR={r.atr:.2f}, Hurst={r.hurst:.2f})"

    def get_detail_report(self, r: MarketStateResult, closes=None) -> str:
        """获取详细的市场结构分析报告(用于日志输出)

        完整展示系统对当前市场的分析结果，便于验证系统是否正常工作
        """
        desc_map = {
            "trend_up": "多头趋势 ↑",
            "trend_down": "空头趋势 ↓",
            "tr": "交易区间 ↔",
            "ttr": "紧凑区间(不交易) ⏸",
            "unknown": "数据不足",
        }
        state_desc = desc_map.get(r.state, "未知")

        # EMA方向
        if r.ema_slope > config.TREND_SLOPE_THRESHOLD * r.ema20:
            ema_dir = "向上 ↑"
        elif r.ema_slope < -config.TREND_SLOPE_THRESHOLD * r.ema20:
            ema_dir = "向下 ↓"
        else:
            ema_dir = "水平 →"

        # 收盘价序列方向
        if r.close_direction > 4:
            close_dir = f"上升主导(+{r.close_direction})"
        elif r.close_direction < -4:
            close_dir = f"下降主导({r.close_direction})"
        else:
            close_dir = f"中性({r.close_direction:+d})"

        # 买压/卖压
        if r.taker_buy_ratio > 0.55:
            pressure = f"买压主导({r.taker_buy_ratio*100:.0f}%)"
        elif r.taker_buy_ratio < 0.45:
            pressure = f"卖压主导({(1-r.taker_buy_ratio)*100:.0f}%)"
        else:
            pressure = f"平衡({r.taker_buy_ratio*100:.0f}%)"

        # 可交易性
        tradeable = "✓ 可交易" if r.is_tradeable else "✗ 不交易"

        lines = [
            f"  ┌─ 市场结构分析 ─────────────────────────",
            f"  │ 状态: {state_desc}  {tradeable}",
            f"  │ EMA20: ${r.ema20:.2f}  斜率: {ema_dir}  斜率值={r.ema_slope:.4f}",
            f"  │ ATR(14): {r.atr:.2f}  ATR比率: {r.atr_ratio:.2f}({'≥0.8正常' if r.atr_ratio >= 0.8 else '<0.8低波动'})",
            f"  │ Hurst: {r.hurst:.2f}({'≥0.55趋势' if r.hurst >= 0.55 else '<0.55均值回归'})",
            f"  │ 重叠率: {r.overlap_ratio:.2f}  穿越EMA: {r.ema_cross_count}次",
            f"  │ 收盘方向: {close_dir}  价格在EMA上: {r.price_above_ema_ratio*100:.0f}%",
            f"  │ 趋势持续: {r.trend_bars}棒  平均实体: {r.avg_entity:.2f}",
            f"  │ 买卖压力: {pressure}",
        ]

        # 区间边界
        if r.range_high > 0 and r.range_low > 0:
            range_size = r.range_high - r.range_low
            lines.append(f"  │ 区间边界: ${r.range_low:.2f} ~ ${r.range_high:.2f} (幅度{range_size:.2f})")

        # 波段点
        if r.swing_highs:
            recent_highs = r.swing_highs[-3:]
            highs_str = ", ".join(f"${p:.2f}" for _, p in recent_highs)
            lines.append(f"  │ 近期波段高: {highs_str}")
        if r.swing_lows:
            recent_lows = r.swing_lows[-3:]
            lows_str = ", ".join(f"${p:.2f}" for _, p in recent_lows)
            lines.append(f"  │ 近期波段低: {lows_str}")

        # 当前价格位置
        if closes and len(closes) > 0:
            current_price = closes[-1]
            lines.append(f"  │ 当前价格: ${current_price:.2f}")

        lines.append(f"  └─────────────────────────────────────────")

        return "\n".join(lines)
