# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 信号去重模块

理论依据：
- Brooks: "好的执行=不好的交易"(不要追逐已错过的信号)
- 信号去重确保同一架构不会重复推送
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime

import config
from bar_patterns import TradingSignal


@dataclass
class NotifiedRecord:
    """已通知信号记录"""
    setup_type: str              # 架构类型
    direction: str               # 方向
    entry_price: float           # 入场价
    signal_high: float           # 信号棒最高价
    signal_low: float            # 信号棒最低价
    bar_index: int               # 信号棒索引
    bar_close_time: int          # K线收盘时间
    notified_at: datetime        # 通知时间
    interval: str                # 时间周期
    bar_count_since: int = 0     # 距离通知时的棒数


class SignalDeduplicator:
    """信号去重器

    去重规则:
    1. 同周期同架构,K线未收盘前不重复推送
    2. 同K线同周期,只推送最高优先级架构
    3. 入场价相差<0.3×ATR=重复
    4. 信号棒极值未突破前不重复推送同向信号
    5. 超过10根K线 或 价格远离>2×ATR=过期清除
    """

    def __init__(self):
        # 每个周期维护一个已通知记录列表
        self.notified: Dict[str, List[NotifiedRecord]] = {
            interval: [] for interval in config.INTERVALS
        }

    def is_duplicate(self, signal: TradingSignal, atr: float) -> bool:
        """检查信号是否重复

        Args:
            signal: 待检查的信号
            atr: 当前ATR(用于价格相近判断)

        Returns:
            True=重复(应跳过), False=新信号(应推送)
        """
        interval = signal.interval
        records = self.notified.get(interval, [])

        for record in records:
            # 规则1: 同架构类型+同方向
            if record.setup_type != signal.setup_type:
                continue
            if record.direction != signal.direction:
                continue

            # 规则3: 入场价相差<0.3×ATR=重复
            price_diff = abs(signal.entry_price - record.entry_price)
            if price_diff < config.DEDUP_PRICE_ATR_MULT * atr:
                return True

            # 规则4: 信号棒极值未突破前不重复
            if signal.direction == "long":
                # 做多信号: 如果价格未突破前信号棒高点,视为重复
                if signal.entry_price <= record.signal_high + 0.01:
                    return True
            else:
                # 做空信号: 如果价格未跌破前信号棒低点,视为重复
                if signal.entry_price >= record.signal_low - 0.01:
                    return True

        return False

    def is_lower_priority_duplicate(self, signal: TradingSignal,
                                     existing_signals: List[TradingSignal]) -> bool:
        """检查是否是同K线同周期的低优先级重复

        同K线同周期,只推送最高优先级架构
        """
        for existing in existing_signals:
            if existing.interval != signal.interval:
                continue
            if existing.bar_close_time != signal.bar_close_time:
                continue
            if existing.direction != signal.direction:
                continue

            # 比较优先级
            existing_priority = config.SETUP_PRIORITY.get(existing.setup_type, 99)
            signal_priority = config.SETUP_PRIORITY.get(signal.setup_type, 99)

            if existing_priority <= signal_priority:
                return True  # 已有更高或同等优先级的信号

        return False

    def add_signal(self, signal: TradingSignal):
        """记录已通知的信号"""
        interval = signal.interval
        record = NotifiedRecord(
            setup_type=signal.setup_type,
            direction=signal.direction,
            entry_price=signal.entry_price,
            signal_high=signal.signal_high,
            signal_low=signal.signal_low,
            bar_index=signal.bar_index,
            bar_close_time=signal.bar_close_time,
            notified_at=datetime.now(),
            interval=interval,
        )
        self.notified[interval].append(record)

    def cleanup(self, current_prices: Dict[str, float], atrs: Dict[str, float]):
        """清理过期的去重记录

        清除条件:
        1. 超过10根K线(时间衰减)
        2. 价格远离>2×ATR(架构过期)
        3. 信号棒极值已被突破(架构失效)
        """
        now = datetime.now()

        for interval in self.notified:
            records = self.notified[interval]
            if not records:
                continue

            current_price = current_prices.get(interval, 0)
            atr = atrs.get(interval, 0)
            if current_price <= 0 or atr <= 0:
                continue

            kept = []
            for record in records:
                # 条件1: 超过10根K线
                # 简化: 超过DEDUP_EXPIRY_BARS * 周期分钟数的时间
                interval_minutes = config.INTERVAL_MINUTES.get(interval, 5)
                max_age_seconds = config.DEDUP_EXPIRY_BARS * interval_minutes * 60
                age = (now - record.notified_at).total_seconds()
                if age > max_age_seconds:
                    continue  # 过期,不保留

                # 条件2: 价格远离>2×ATR
                price_diff = abs(current_price - record.entry_price)
                if price_diff > config.DEDUP_PRICE_EXPIRE_MULT * atr:
                    continue  # 价格远离,不保留

                # 条件3: 信号棒极值已被突破
                if record.direction == "long":
                    # 做多信号: 价格跌破信号棒低点=架构失效
                    if current_price < record.signal_low:
                        continue
                else:
                    # 做空信号: 价格涨破信号棒高点=架构失效
                    if current_price > record.signal_high:
                        continue

                kept.append(record)

            self.notified[interval] = kept

    def get_notified_count(self, interval: str) -> int:
        """获取指定周期已通知的信号数量"""
        return len(self.notified.get(interval, []))

    def get_summary(self) -> str:
        """获取去重状态摘要"""
        parts = []
        for interval in config.INTERVALS:
            count = len(self.notified.get(interval, []))
            if count > 0:
                parts.append(f"{interval}:{count}")
        return " | ".join(parts) if parts else "无缓存"
