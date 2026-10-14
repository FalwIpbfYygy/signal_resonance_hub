# -*- coding: utf-8 -*-
"""信号引擎：获取数据 → 检测 → 趋势过滤 → 分级 → 去重 → 推送。"""

import os
import sys
from datetime import datetime, timedelta, timezone

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store

import config
from data import fetch_klines
from patterns import build_ctx, run_all_detectors
from indicators import trend_direction
from notifier import send_feishu_async, send_email_async, format_signal


class SignalEngine:
    def __init__(self):
        # 冷却：{(interval, pattern, direction): 最后推送时间戳}
        self._cooldown = {}
        # 趋势缓存：{interval: direction}
        self._trend_cache = {}

    # ---------------- 趋势 ----------------
    def _trend(self, interval):
        if interval in self._trend_cache:
            return self._trend_cache[interval]
        bars = fetch_klines(interval)
        if not bars:
            return 0
        closes = [b["close"] for b in bars]
        d = trend_direction(closes, period=50)
        self._trend_cache[interval] = d
        return d

    def _trend_text(self, d):
        return {1: "上升", -1: "下降", 0: "无趋势"}.get(d, "无趋势")

    # ---------------- 分级 ----------------
    def _grade(self, sig, trend_ok):
        aligned = trend_ok and sig.get("vol_ok", False)
        touches = sig.get("touches", 0)
        if aligned and touches >= 4:
            return config.GRADE_STRONGEST
        if aligned:
            return config.GRADE_STRONG
        return config.GRADE_GENERAL

    def _trend_ok(self, direction, trend):
        return trend != 0 and ((direction == 1 and trend == 1) or (direction == -1 and trend == -1))

    # ---------------- 去重 ----------------
    def _persisted_dup(self, interval, sig, cooldown_seconds):
        """跨进程/重启去重：冷却窗口内是否已入库过同周期同形态同方向信号。"""
        direction_text = "做多" if sig["direction"] == 1 else "做空"
        now = datetime.now()
        days = {now.strftime("%Y%m%d"), (now - timedelta(days=1)).strftime("%Y%m%d")}
        for day in days:
            for row in signal_store.load_signals(day):
                if row.get("interval") != interval:
                    continue
                if row.get("pattern") != sig["pattern"]:
                    continue
                if row.get("direction") != direction_text:
                    continue
                try:
                    t = datetime.strptime(row.get("time", ""), "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    continue
                if (now - t).total_seconds() < cooldown_seconds:
                    return True
        return False

    # ---------------- 主扫描 ----------------
    def scan_interval(self, interval):
        bars = fetch_klines(interval)
        if not bars:
            return
        ctx = build_ctx(bars)
        if ctx["cur_atr"] <= 0:
            return

        # 趋势背景（高一级周期）
        trend_interval = config.TREND_INTERVALS.get(interval, interval)
        trend = self._trend(trend_interval)

        results = run_all_detectors(ctx)
        for sig in results:
            trend_ok = self._trend_ok(sig["direction"], trend)
            grade = self._grade(sig, trend_ok)
            if grade == config.GRADE_GENERAL:
                continue
            # 形态黑名单：历史胜率过低的形态直接跳过
            if sig["pattern"] in config.PATTERN_BLACKLIST:
                continue
            # 做空降权：仅允许 >= SHORT_MIN_GRADE 的做空信号
            if sig["direction"] == -1 and config.SHORT_MIN_GRADE != "off":
                grade_order = {config.GRADE_STRONG: 1, config.GRADE_STRONGEST: 2}
                if grade_order.get(grade, 0) < grade_order.get(config.SHORT_MIN_GRADE, 0):
                    continue
            sig["quality"] = grade
            cd_key = (interval, sig["pattern"], sig["direction"])
            cooldown_seconds = config.COOLDOWN_BARS * _bar_seconds(interval)
            now = datetime.now().timestamp()
            if now - self._cooldown.get(cd_key, 0) < cooldown_seconds:
                continue
            if self._persisted_dup(interval, sig, cooldown_seconds):
                self._cooldown[cd_key] = now
                continue
            self._cooldown[cd_key] = now
            text = format_signal(interval, sig, self._trend_text(trend))
            print(f"[信号] {interval} {sig['pattern']} {sig['direction']} {grade} @ {sig['entry']}")
            # 统一入库（信号时间 / 品种 / 周期 / 形态 / 方向 / 入场 / 止损 / TP1 / TP2）
            signal_store.record_signal(
                signal_time=sig.get("bar_time"),
                symbol=config.SYMBOL,
                interval=interval,
                pattern=sig["pattern"],
                direction=sig["direction"],
                entry=sig.get("entry"),
                sl=sig.get("stop"),
                tp1=sig.get("target1"),
                tp2=sig.get("target2"),
            )
            send_feishu_async(text)
            send_email_async(f"PAXGUSDT {interval} {sig['pattern']}信号提醒", text)


def _bar_seconds(interval):
    """把周期字符串转换为秒。"""
    unit = interval[-1]
    num = int(interval[:-1])
    mult = {"m": 60, "h": 3600, "d": 86400}.get(unit, 60)
    return num * mult