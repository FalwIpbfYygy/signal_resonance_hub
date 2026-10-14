# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 风控管理模块

理论依据：
- 区间篇P282: "交易完全基于数学"
- 区间篇第29章: 保护性止损(信号棒极值±0.3×ATR)
- 区间篇第30章: 获利了结(1R了结50%, 1.5R了结50%)
- 区间篇第31章: 逐步入场(仅顺势加仓,禁止逆势加仓)
"""
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime, date

import config
from bar_patterns import TradingSignal


@dataclass
class Position:
    """持仓信息"""
    signal: TradingSignal
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    size: float                 # 仓位大小(合约数)
    opened_at: datetime
    tp1_closed: bool = False    # TP1是否已平仓
    tp2_closed: bool = False    # TP2是否已平仓


class RiskManager:
    """风控管理器

    管理交易者方程、仓位大小、止损止盈、加仓规则、日亏损限额
    """

    def __init__(self, account_balance: float = 10000.0):
        self.account_balance = account_balance
        self.positions: List[Position] = []
        self.daily_pnl: float = 0.0
        self.daily_pnl_date: Optional[date] = None
        self.is_trading_halted = False

    def check_trader_equation(self, signal: TradingSignal) -> bool:
        """检查交易者方程

        胜率 × 回报 > 败率 × 风险 才可交易
        """
        eq_params = config.TRADER_EQUATION.get(
            signal.setup_type, {"win_rate": 0.5, "min_rr": 1.5}
        )
        win_rate = eq_params["win_rate"]
        loss_rate = 1.0 - win_rate
        reward = signal.risk_reward

        # 交易者方程: win_rate * reward > loss_rate * 1.0
        expected_value = win_rate * reward - loss_rate * 1.0
        return expected_value > 0

    def calc_position_size(self, signal: TradingSignal) -> float:
        """计算仓位大小

        单笔风险 = 账户余额 × 风险比例
        仓位 = 单笔风险 / 单合约风险
        """
        risk_amount = self.account_balance * config.RISK_PER_TRADE
        per_contract_risk = abs(signal.entry_price - signal.stop_price)

        if per_contract_risk <= 0:
            return 0.0

        # 逐步入场: 初始头寸为正常的1/2
        if signal.setup_type in ("h2_trend", "l2_trend", "h1_strong", "l1_strong"):
            # 趋势中可逐步入场,初始1/2
            position_size = (risk_amount / per_contract_risk) * 0.5
        else:
            position_size = risk_amount / per_contract_risk

        return round(position_size, 2)

    def check_position_limits(self, direction: str) -> bool:
        """检查持仓限制

        - 最大同时持仓: 3笔
        - 单方向最大持仓: 2笔
        """
        if len(self.positions) >= config.MAX_POSITIONS:
            return False

        same_direction = sum(
            1 for p in self.positions if p.signal.direction == direction
        )
        if same_direction >= config.MAX_SAME_DIRECTION:
            return False

        return True

    def check_daily_loss(self) -> bool:
        """检查日亏损限额

        日亏损达3%→停止交易
        """
        # 检查日期是否变更
        today = date.today()
        if self.daily_pnl_date != today:
            self.daily_pnl_date = today
            self.daily_pnl = 0.0
            self.is_trading_halted = False

        if self.is_trading_halted:
            return False

        daily_loss_limit = self.account_balance * config.DAILY_LOSS_LIMIT
        if self.daily_pnl <= -daily_loss_limit:
            self.is_trading_halted = True
            return False

        return True

    def can_open_position(self, signal: TradingSignal) -> bool:
        """综合检查是否可以开仓"""
        # 交易者方程
        if not self.check_trader_equation(signal):
            return False

        # 日亏损限额
        if not self.check_daily_loss():
            return False

        # 持仓限制
        if not self.check_position_limits(signal.direction):
            return False

        return True

    def get_stop_loss(self, signal: TradingSignal, atr: float) -> float:
        """获取止损价格

        初始止损: 信号棒极值 ± 0.3×ATR
        信号棒异常大时: 信号棒高度×60%
        """
        # 信号棒高度
        bar_height = signal.signal_high - signal.signal_low
        avg_bar_height = atr * 2.0  # 估计平均棒高

        # 信号棒异常大时使用资金管理止损
        if bar_height > avg_bar_height * 2.0:
            if signal.direction == "long":
                return signal.entry_price - bar_height * config.SL_LARGE_BAR_RATIO
            else:
                return signal.entry_price + bar_height * config.SL_LARGE_BAR_RATIO

        # 标准止损: 信号棒极值±0.3×ATR
        return signal.stop_price

    def get_take_profit(self, signal: TradingSignal, is_strong_trend: bool = False):
        """获取止盈价格

        标准: 1R了结50%, 1.5R了结50%
        强趋势: 2R了结50%, 3R了结25%, 剩余奔跑
        """
        if is_strong_trend:
            # 强趋势动态止盈
            if signal.direction == "long":
                risk = signal.entry_price - signal.stop_price
                tp1 = signal.entry_price + risk * config.TP_STRONG_TP1_R
                tp2 = signal.entry_price + risk * config.TP_STRONG_TP2_R
            else:
                risk = signal.stop_price - signal.entry_price
                tp1 = signal.entry_price - risk * config.TP_STRONG_TP1_R
                tp2 = signal.entry_price - risk * config.TP_STRONG_TP2_R
            return tp1, tp2, 0.5, 0.25  # tp1_ratio, tp2_ratio
        else:
            # 标准止盈
            return signal.tp1_price, signal.tp2_price, config.TP1_CLOSE_RATIO, config.TP2_CLOSE_RATIO

    def can_add_position(self, existing_position: Position,
                          new_signal: TradingSignal) -> bool:
        """检查是否可以加仓

        允许加仓的场景:
        1. 强尖峰后的小幅回撤
        2. 通道中顺势方向的回撤
        3. 突破回撤位置

        禁止: 逆势加仓
        """
        # 方向必须一致
        if existing_position.signal.direction != new_signal.direction:
            return False

        # 新信号价格不能比现有仓位差太多(顺势加仓)
        if new_signal.direction == "long":
            if new_signal.entry_price < existing_position.entry_price - 2 * existing_position.signal.atr:
                return False  # 价格远离=可能逆势
        else:
            if new_signal.entry_price > existing_position.entry_price + 2 * existing_position.signal.atr:
                return False

        # 持仓限制
        if not self.check_position_limits(new_signal.direction):
            return False

        return True

    def update_trailing_stop(self, position: Position, current_price: float,
                              latest_swing_low: float = 0.0,
                              latest_swing_high: float = 0.0) -> float:
        """更新跟踪止损

        规则:
        1. 入场棒后移动≥1R → 止损移至盈亏平衡
        2. 创新高/低后 → 止损跟踪至最近波段点
        3. 利润达1R → 了结50%后,止损移至盈亏平衡
        """
        if position.signal.direction == "long":
            risk = position.entry_price - position.stop_price
            if risk <= 0:
                return position.stop_price

            # 利润≥1R → 止损移至盈亏平衡
            if current_price - position.entry_price >= risk:
                new_stop = position.entry_price
                if new_stop > position.stop_price:
                    position.stop_price = new_stop

            # 创新高后跟踪波段低点
            if latest_swing_low > 0:
                new_stop = latest_swing_low - config.SL_ATR_MULT * position.signal.atr
                if new_stop > position.stop_price:
                    position.stop_price = new_stop

        else:  # short
            risk = position.stop_price - position.entry_price
            if risk <= 0:
                return position.stop_price

            if position.entry_price - current_price >= risk:
                new_stop = position.entry_price
                if new_stop < position.stop_price:
                    position.stop_price = new_stop

            if latest_swing_high > 0:
                new_stop = latest_swing_high + config.SL_ATR_MULT * position.signal.atr
                if new_stop < position.stop_price:
                    position.stop_price = new_stop

        return position.stop_price

    def get_risk_summary(self) -> str:
        """获取风控摘要(用于日志)"""
        return (
            f"账户余额:{self.account_balance:.2f} | "
            f"持仓数:{len(self.positions)}/{config.MAX_POSITIONS} | "
            f"日盈亏:{self.daily_pnl:.2f} | "
            f"交易状态:{'停止' if self.is_trading_halted else '正常'}"
        )
