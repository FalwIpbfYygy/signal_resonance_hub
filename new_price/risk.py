# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 风控模块

职责: 在交易计划(entry.TradePlan)之上补全「风险三要素」:
  1. 仓位建议     — 单笔风险固定比例, 由止损距离反推数量(PRD §8.3)
  2. 加仓合规检查 — 唯一合规加仓 = 顺势 + 趋势早期 + 盈利中(PRD §8.4)
  3. 强制退出判定 — 趋势反向破推动起点 / 区间放量真突破(PRD §8.5)

锁定定义:
  · 止损止盈在入场时即定                      [太妃 L674]
  · 期望 0.2 基准: 60%×1R(剥) / 40%×2R(波)   [太妃 L62/L683, 学委 L1185]
  · 止损较大→小仓位, 回调/有把握时再加仓      [学委 L24/L491/L630]
  · 不要死扛, 更不要亏损(kui)加仓             [学委 L1349]
  · 顺势仓位盈利中, 见三推先减仓一部分         [学委 L1365]
  · 最合理止损=突破起涨点(推动起点)            [学委 L127]
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import config
from market_state import MarketStateResult
from entry import TradePlan, _swing_anchors, _ds


# ====================== 数据结构 ======================

@dataclass
class PositionAdvice:
    """仓位建议(仅建议, 不下单)。"""
    risk_pct: float = 0.0         # 单笔风险占账户 %
    risk_amount: float = 0.0      # 风险金额(USD)
    sl_distance: float = 0.0      # 止损距离(价格美元)
    quantity: float = 0.0         # 建议数量(XAU 单位, 现货 1:1 等价)
    notional: float = 0.0         # 名义金额(USD)


# ====================== 风控引擎 ======================

class RiskManager:
    """风险三要素计算器(仓位/加仓/无效化)。"""

    # ---------- 1. 仓位建议 ----------

    def suggest_position(self, plan: TradePlan,
                         equity: float = None) -> PositionAdvice:
        """按 SL 距离反推数量: 数量 = 风险金额 / 止损距离。"""
        equity = equity if equity is not None else config.ACCOUNT_EQUITY
        risk_pct = config.RISK_PCT
        risk_amount = equity * risk_pct / 100.0
        sl_distance = abs(plan.entry_price - plan.stop_price)
        if sl_distance <= 0:
            return PositionAdvice(risk_pct=risk_pct, risk_amount=risk_amount)
        quantity = risk_amount / sl_distance
        notional = quantity * plan.entry_price
        return PositionAdvice(
            risk_pct=risk_pct, risk_amount=round(risk_amount, 2),
            sl_distance=round(sl_distance, 2), quantity=round(quantity, 4),
            notional=round(notional, 2))

    # ---------- 2. 加仓合规检查 ----------

    def evaluate_add(self, plan: TradePlan, mstate: MarketStateResult,
                     entry_price: float, current_price: float,
                     count_level: int) -> Tuple[bool, str]:
        """唯一合规加仓 = 顺势 + 趋势早期 + 盈利中; 禁止摊平亏损(PRD §8.4)。

        返回 (是否允许加仓, 原因)。count_level 为当前回调数推等级。
        """
        # 方向须顺势(趋势态)
        if not hasattr(mstate, "state") or mstate.state not in (
                "trend_up", "trend_down", "broad_up", "broad_down"):
            return False, "非趋势态, 不顺势加仓"
        want_long = mstate.direction > 0
        is_long = plan.direction == "long"
        if want_long != is_long:
            return False, "逆势, 禁止加仓"

        # 趋势早期(回调等级低)
        if count_level > config.ADD_LEVEL_MAX:
            return False, f"趋势中后段(高/低{count_level}), 不加仓"

        # 盈利中(禁止摊平亏损)
        ds = _ds(plan.direction)
        floating = (current_price - entry_price) * ds
        min_profit = config.ADD_PROFIT_BUFFER_ATR * mstate.atr
        if floating <= min_profit:
            return False, (f"未脱离成本/浮盈不足({floating:.2f}<{min_profit:.2f}), "
                           f"禁止摊平亏损")
        return True, f"顺势早期盈利中(浮盈{floating:.2f}), 可回调小仓加"

    # ---------- 3. 强制退出(无效化)判定 ----------

    def invalidation(self, plan: TradePlan, mstate: MarketStateResult,
                     current_price: float, current_volume: float = None,
                     avg_volume: float = None) -> Optional[str]:
        """返回无效化原因(须强制离场), 无则 None。PRD §8.5。"""
        state = mstate.state

        # 趋势态: 反向突破推动起点 → 逻辑证伪
        if state in ("trend_up", "trend_down", "broad_up", "broad_down"):
            _, wave_start, _ = _swing_anchors(mstate, plan.direction)
            if wave_start is None:
                return None
            if plan.direction == "long" and current_price < wave_start:
                return f"反向跌破推动起点{wave_start:.2f}, 趋势逻辑证伪"
            if plan.direction == "short" and current_price > wave_start:
                return f"反向突破推动起点{wave_start:.2f}, 趋势逻辑证伪"
            return None

        # 区间态(fade): 放量真突破边线 → 离场
        if state == "range":
            rl, rh = mstate.range_low, mstate.range_high
            if plan.direction == "long":
                if current_price < rl:
                    brek = self._volume_surge(current_volume, avg_volume)
                    return f"放量下破区间下缘{rl:.2f}" if brek else f"跌破区间下缘{rl:.2f}"
            else:
                if current_price > rh:
                    brek = self._volume_surge(current_volume, avg_volume)
                    return f"放量上破区间上缘{rh:.2f}" if brek else f"突破区间上缘{rh:.2f}"
            return None

        return None

    @staticmethod
    def _volume_surge(volume: float, avg_volume: float) -> bool:
        """区间真突破: 量能 ≥ 2×近N棒均量。"""
        if volume is None or avg_volume is None or avg_volume <= 0:
            return False
        return volume >= config.INVALIDATE_VOLUME_MULT * avg_volume