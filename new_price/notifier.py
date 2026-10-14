# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 飞书推送模块

职责: 把交易计划(entry.TradePlan)格式化为文本卡片, 异步推送到飞书(PRD §10)。

规范(PRD §10):
  · 通道: webhook(项目已验证地址 586c2871)
  · 异步发送(daemon 线程), 失败仅记日志、不阻塞主循环、不重试刷屏
  · 仅信号放行时推送(去重由引擎层 dedup 负责)
"""
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

import config
from entry import TradePlan
from risk import PositionAdvice


BJT = timezone(timedelta(hours=8))

# 市场状态中文映射(与 market_state.describe 对齐)
_STATE_CN = {
    "trend_up": "急速多头",
    "trend_down": "急速空头",
    "broad_up": "宽通道多头",
    "broad_down": "宽通道空头",
    "range": "交易区间",
    "tight_range": "紧凑区间",
    "unknown": "数据不足",
}


def _fmt_ts(ms: int) -> str:
    """毫秒时间戳 → BJT 时间字符串。"""
    return datetime.fromtimestamp(ms / 1000.0, tz=BJT).strftime("%Y-%m-%d %H:%M")


class Notifier:
    """飞书异步推送器。"""

    def __init__(self, webhook: str = None):
        self.webhook = webhook or config.FEISHU_WEBHOOK

    def format_signal(self, plan: TradePlan, close_time: int,
                      advice: Optional[PositionAdvice] = None) -> str:
        """按 PRD §10.2 格式化信号文本卡片。"""
        direction = "多" if plan.direction == "long" else "空"
        state = _STATE_CN.get(plan.market_state, plan.market_state)
        mode = "波段" if plan.mode == "swing" else "剥头皮"
        if advice is not None:
            size = f"{advice.risk_pct}%风险(建议{advice.quantity}手, 名义{advice.notional}USD)"
        else:
            size = "-"
        return "\n".join([
            f"【{plan.interval}  {plan.setup_type}信号】",
            f"开仓时间：{_fmt_ts(close_time)}",
            f"开仓价格：{plan.entry_price}    周期：{plan.interval}",
            f"状态：{state}    方向：{direction}",
            f"入场方案：{plan.setup_type}    信号分级：{plan.grade}",
            f"止损SL：{plan.stop_price}    止盈TP1：{plan.tp1_price}    TP2：{plan.tp2_price}",
            f"目标依据：{plan.target_basis}",
            f"仓位建议：{size}    模式：{mode}",
        ])

    def send_signal(self, plan: TradePlan, close_time: int,
                    advice: Optional[PositionAdvice] = None):
        """格式化并异步推送一笔交易信号。"""
        self.send_async(self.format_signal(plan, close_time, advice))

    def send_async(self, text: str):
        """异步(daemon 线程)发送; 失败仅记日志, 不阻塞主循环。"""

        def _do():
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            try:
                resp = requests.post(
                    self.webhook,
                    json={"msg_type": "text", "content": {"text": text}},
                    timeout=config.REQUEST_TIMEOUT,
                )
                if resp.status_code != 200:
                    print(f"[{now}] ❌ 飞书发送失败 HTTP {resp.status_code}: {resp.text[:200]}")
                    return
                try:
                    body = resp.json()
                except Exception:
                    print(f"[{now}] ❌ 飞书发送失败：响应非 JSON: {resp.text[:200]}")
                    return
                if not isinstance(body, dict) or body.get("code") != 0:
                    print(f"[{now}] ❌ 飞书发送失败 业务码 {body.get('code') if isinstance(body, dict) else '?'}（HTTP 200 但业务未成功）: {str(body)[:200]}")
                    return
                print(f"[{now}] ✅ 飞书发送成功, 状态码 {resp.status_code}")
            except Exception as e:
                print(f"[{now}] 飞书发送失败: {e}")

        threading.Thread(target=_do, daemon=True).start()