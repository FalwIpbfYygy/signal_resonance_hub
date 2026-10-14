"""飞书推送：每日交割单 + 决策提醒。

关键纪律（沿用 S2 修复结论）：HTTP 200 不代表成功，必须校验响应体 code==0，
否则视为推送失败（避免「推送成功」假象）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import requests


def send_feishu(webhook: str, text: str, proxy: Optional[str] = None) -> Tuple[bool, str]:
    """推送文本消息到飞书。返回 (ok, detail)。严格校验业务码 code==0。"""
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        resp = session.post(webhook, json=payload, timeout=30)
    except Exception as e:
        return False, f"ERR:{e}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        body = resp.json()
    except Exception:
        return False, f"响应非 JSON: {resp.text[:200]}"
    if not isinstance(body, dict) or body.get("code") != 0:
        code = body.get("code") if isinstance(body, dict) else "?"
        return False, f"业务码 {code}（HTTP 200 但业务未成功）: {str(body)[:200]}"
    return True, "ok"


def format_daily_report(
    date: str,
    decisions: List[Dict[str, any]],
    kill_switch_info: str,
    experience_changes: List[str],
    est_pnl_pct: float,
) -> str:
    """把当日决策汇总为飞书可读的交割单文本。"""
    total = len(decisions)
    from collections import Counter

    dist = Counter(d.get("decision", "OBSERVE") for d in decisions)
    lines = []
    lines.append(f"【 Agent 交割单 {date}】")
    lines.append(f"信号总数: {total}")
    lines.append("决策分布: " + ", ".join(f"{k}={v}" for k, v in dist.items()) or "无")
    lines.append(f"当日已录入盈亏(实际): {est_pnl_pct:+.2%}")
    # 名义敞口占比（与真实盈亏区分，避免混淆）— 仅当存在正仓位占比时展示
    nominal = sum((d.get("position_size_pct") or 0) for d in decisions)
    if nominal:
        lines.append(f"当日名义敞口占比: {nominal:+.2%}")
    lines.append(f"KillSwitch: {kill_switch_info}")
    if experience_changes:
        lines.append("经验库变更: " + "; ".join(experience_changes))
    else:
        lines.append("经验库变更: 无")
    # 关键建议明细（前 10 条）
    if decisions:
        lines.append("--- 关键建议 ---")
        for d in decisions[:10]:
            lines.append(
                f"  {d.get('signal_id')} {d.get('direction')} "
                f"{d.get('decision')} SL={d.get('stop_loss')} TP={d.get('take_profit')} "
                f"size={d.get('position_size_pct')}"
            )
    return "\n".join(lines)
