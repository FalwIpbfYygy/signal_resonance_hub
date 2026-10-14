# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 信号去重模块

职责: 防止同一信号在同一根/相邻K线刷屏(PRD §9)。

去重链:
  1. K线级去重     — 以 bar_id(open_time) 为主键, 同一根收盘K线同一形态只发一次。
  2. 区间级冷却     — 同一周期同一方向, 冷却 N 根K线内不重复推送。
  3. 形态级去重     — 同一极值点反复触发的同一形态, 仅首发一次;
                     后续仅当信号「升级」(strong→strongest) 才再推。
  4. 未收盘拦截     — 由数据层/引擎层完成(仅新收盘K线触发重算), 本模块不介入。

设计: 本模块为纯状态机, 无网络/无副作用, 由引擎在推送前调用。
"""
from typing import Tuple

import config


def _grade_rank(grade: str) -> int:
    """分级排序(用于升级判断): strongest > strong > 其他。"""
    return {"strongest": 2, "strong": 1}.get(grade, 0)


class SignalDedup:
    """信号去重状态机。"""

    def __init__(self, cooldown_bars: int = None):
        self.cooldown = cooldown_bars if cooldown_bars is not None \
            else config.DEDUP_COOLDOWN_BARS
        self._last_by_dir: dict = {}    # (interval, direction) -> bar_index
        self._sent: dict = {}           # (interval, bar_id, pattern) -> grade

    def should_send(self, interval: str, direction: str, pattern: str,
                    bar_index: int, bar_id, grade: str) -> Tuple[bool, str]:
        """判断当前信号是否放行。返回 (是否放行, 拦截原因)。

        顺序: 先判「同K线同形态」(支持升级), 再判「新K线冷却」。
        升级(strong→strongest)属同一根K线重估, 不受冷却拦截。
        """
        # 1)(3) K线级 + 形态级: 同一 bar_id 同一形态
        ek = (interval, bar_id, pattern)
        if ek in self._sent:
            prev = self._sent[ek]
            # 仅当升级(strong→strongest)才再推
            if _grade_rank(grade) <= _grade_rank(prev):
                return False, f"同K线同形态已发(等级{prev}, 无升级)"
            return True, f"信号升级({prev}→{grade})重推"

        # 2) 区间级冷却: 同周期同方向, 距上次发送不足 N 根 → 拦
        dk = (interval, direction)
        if dk in self._last_by_dir:
            gap = bar_index - self._last_by_dir[dk]
            if gap < self.cooldown:
                return False, f"冷却中(距上次{max(gap, 0)}根<{self.cooldown})"
        return True, ""

    def mark_sent(self, interval: str, direction: str, pattern: str,
                  bar_index: int, bar_id, grade: str):
        """推送成功后记录, 更新冷却与已发状态。"""
        self._last_by_dir[(interval, direction)] = bar_index
        self._sent[(interval, bar_id, pattern)] = grade


def default_dedup() -> SignalDedup:
    return SignalDedup()