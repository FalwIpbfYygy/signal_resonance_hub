# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 过滤规则模块

理论依据：
- 区间篇P144: "紧凑TR内不应做任何交易"
- 区间篇P183: "H1入场需强趋势+强尖峰+非高潮"
- 趋势篇P92: "反转棒重叠过多→降级为不可交易"
- 区间篇第26章: "做一笔交易需要两个理由"
"""
from typing import List, Optional, Dict
from datetime import datetime

import config
from bar_patterns import TradingSignal
from market_state import MarketStateResult


class SignalFilter:
    """信号过滤器

    应用Brooks体系的全部过滤规则:
    1. 全局硬过滤(市场状态/信号强度/EMA距离/ATR比率/Hurst)
    2. 趋势方向过滤(30分钟EMA20方向)
    3. 多周期共振(可选,增强信号)
    4. "两个理由"原则
    5. 排除场景
    """

    def filter_signal(self, signal: TradingSignal,
                      market_state: MarketStateResult,
                      market_state_30m: Optional[MarketStateResult] = None,
                      all_market_states: Optional[Dict[str, MarketStateResult]] = None
                      ) -> Optional[TradingSignal]:
        """过滤单个信号(简化版,无原因返回)"""
        filtered, _ = self.filter_signal_with_reason(
            signal, market_state, market_state_30m, all_market_states
        )
        return filtered

    def filter_signal_with_reason(self, signal: TradingSignal,
                      market_state: MarketStateResult,
                      market_state_30m: Optional[MarketStateResult] = None,
                      all_market_states: Optional[Dict[str, MarketStateResult]] = None
                      ) -> tuple:
        """过滤单个信号(带详细失败原因)

        Args:
            signal: 待过滤的交易信号
            market_state: 当前周期的市场状态
            market_state_30m: 30分钟市场状态(用于趋势方向过滤)
            all_market_states: 所有周期的市场状态(用于共振)

        Returns:
            (通过过滤的信号, 失败原因) - 通过时失败原因为None
            (None, 失败原因) - 未通过时返回具体原因
        """
        # === 1. 全局硬过滤 ===
        ok, reason = self._check_hard_filters_with_reason(signal, market_state)
        if not ok:
            return None, reason

        # === 2. 趋势方向过滤(30分钟EMA20) ===
        ok, reason = self._check_trend_direction_with_reason(signal, market_state_30m)
        if not ok:
            return None, reason

        # === 3. 排除场景检查 ===
        ok, reason = self._check_exclusions_with_reason(signal, market_state)
        if not ok:
            return None, reason

        # === 4. 多周期共振(可选,增强信号) ===
        if config.RESONANCE_ENABLED and all_market_states:
            resonance = self._check_resonance(signal, all_market_states)
            signal.resonance = resonance

        # === 5. "两个理由"原则检查 ===
        ok, reason = self._check_two_reasons_with_reason(signal)
        if not ok:
            return None, reason

        return signal, None

    def _check_hard_filters_with_reason(self, signal: TradingSignal,
                            market_state: MarketStateResult) -> tuple:
        """全局硬过滤(带原因)"""
        # 市场状态: 非TTR
        if market_state.state == "ttr":
            return False, "紧凑交易区间(TTR)"

        # 信号强度: strong或strongest
        if signal.grade not in config.SIGNAL_GRADES_ALLOWED:
            return False, f"信号强度={signal.grade}(需strong/strongest)"

        # EMA距离: |close-EMA20| > 0.5×ATR
        ema_dist = abs(signal.entry_price - market_state.ema20)
        ema_threshold = config.EMA_DIST_ATR_MULT * market_state.atr
        if ema_dist <= ema_threshold:
            # 对于MTR和反转类信号,EMA距离可以放宽
            if signal.setup_type not in ("mtr_conservative", "mtr_aggressive",
                                          "climax_reversal", "final_flag"):
                return False, f"EMA距离={ema_dist:.2f}≤{ema_threshold:.2f}(0.5×ATR)"

        # ATR比率: ≥0.8(低波动压缩时不交易)
        if market_state.atr_ratio < config.ATR_RATIO_THRESHOLD:
            return False, f"ATR比率={market_state.atr_ratio:.2f}<{config.ATR_RATIO_THRESHOLD}(低波动)"

        # Hurst指数: ≥0.55(均值回归市场不交易)
        if market_state.hurst < config.HURST_THRESHOLD:
            if signal.setup_type != "tr_reversal":
                return False, f"Hurst={market_state.hurst:.2f}<{config.HURST_THRESHOLD}(均值回归)"

        return True, None

    def _check_trend_direction_with_reason(self, signal: TradingSignal,
                                market_state_30m: Optional[MarketStateResult]) -> tuple:
        """趋势方向过滤(带原因)"""
        # 反转类信号不需要30m趋势确认
        reversal_types = ("mtr_conservative", "mtr_aggressive",
                          "climax_reversal", "final_flag", "failed_failure")
        if signal.setup_type in reversal_types:
            return True, None

        if market_state_30m is None:
            return True, None

        slope_threshold = config.TREND_SLOPE_THRESHOLD * market_state_30m.ema20
        is_30m_bull = market_state_30m.ema_slope > slope_threshold
        is_30m_bear = market_state_30m.ema_slope < -slope_threshold

        if signal.direction == "long":
            if is_30m_bear and market_state_30m.state == "trend_down":
                return False, "30m趋势向下(做多需非向下)"
        elif signal.direction == "short":
            if is_30m_bull and market_state_30m.state == "trend_up":
                return False, "30m趋势向上(做空需非向上)"

        return True, None

    def _check_exclusions_with_reason(self, signal: TradingSignal,
                          market_state: MarketStateResult) -> tuple:
        """排除场景检查(带原因)"""
        if market_state.state == "ttr":
            return False, "紧凑交易区间"

        if market_state.atr_ratio < config.ATR_RATIO_THRESHOLD:
            return False, f"ATR比率={market_state.atr_ratio:.2f}<{config.ATR_RATIO_THRESHOLD}"

        return True, None

    def _check_two_reasons_with_reason(self, signal: TradingSignal) -> tuple:
        """'两个理由'原则检查(带原因)"""
        # 所有已定义的架构类型都自带两个理由
        auto_pass_types = (
            "second_entry", "failed_failure", "mtr_conservative", "mtr_aggressive",
            "climax_reversal", "final_flag", "h2_trend", "l2_trend",
            "wedge_flag", "tr_reversal", "h1_strong", "l1_strong"
        )
        if signal.setup_type in auto_pass_types:
            return True, None

        if len(signal.reasons) >= 2:
            return True, None

        return False, f"仅有{len(signal.reasons)}个理由(需≥2个)"

    def _check_hard_filters(self, signal: TradingSignal,
                            market_state: MarketStateResult) -> bool:
        """全局硬过滤(兼容旧接口,内部委托给_with_reason版本)"""
        ok, _ = self._check_hard_filters_with_reason(signal, market_state)
        return ok

    def _check_trend_direction(self, signal: TradingSignal,
                                market_state_30m: Optional[MarketStateResult]) -> bool:
        """趋势方向过滤(兼容旧接口)"""
        ok, _ = self._check_trend_direction_with_reason(signal, market_state_30m)
        return ok

    def _check_exclusions(self, signal: TradingSignal,
                          market_state: MarketStateResult) -> bool:
        """排除场景检查(兼容旧接口)"""
        ok, _ = self._check_exclusions_with_reason(signal, market_state)
        return ok

    def _check_two_reasons(self, signal: TradingSignal) -> bool:
        """'两个理由'原则检查(兼容旧接口)"""
        ok, _ = self._check_two_reasons_with_reason(signal)
        return ok

    def _check_resonance(self, signal: TradingSignal,
                         all_market_states: Dict[str, MarketStateResult]) -> List[str]:
        """多周期共振检查(可选,增强信号)

        检查更高时间框架是否同方向趋势
        """
        resonance = []
        higher_intervals = config.RESONANCE_MAP.get(signal.interval, [])

        for interval in higher_intervals:
            ms = all_market_states.get(interval)
            if ms is None:
                continue

            # 检查更高周期趋势方向是否一致
            if signal.direction == "long":
                if ms.state in ("trend_up", "tr"):
                    if ms.state == "trend_up":
                        resonance.append(f"{interval}↑")
                    else:
                        resonance.append(f"{interval}→")
            elif signal.direction == "short":
                if ms.state in ("trend_down", "tr"):
                    if ms.state == "trend_down":
                        resonance.append(f"{interval}↓")
                    else:
                        resonance.append(f"{interval}→")

        return resonance

    # ===== 以下旧方法已合并到_with_reason版本,保留壳以兼容 =====

    def get_filter_description(self, signal: TradingSignal,
                               market_state: MarketStateResult) -> str:
        """获取过滤结果的描述(用于飞书推送)"""
        checks = []

        # EMA距离
        ema_dist = abs(signal.entry_price - market_state.ema20)
        checks.append(f"EMA距离:{ema_dist:.2f}>{config.EMA_DIST_ATR_MULT * market_state.atr:.2f} ✓")

        # 信号强度
        checks.append(f"信号强度:{signal.grade} ✓")

        # ATR比率
        checks.append(f"ATR比率:{market_state.atr_ratio:.2f}≥{config.ATR_RATIO_THRESHOLD} ✓")

        # Hurst
        checks.append(f"Hurst:{market_state.hurst:.2f}")

        return " | ".join(checks)
