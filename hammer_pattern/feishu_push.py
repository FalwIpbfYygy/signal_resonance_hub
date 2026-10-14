# -*- coding: utf-8 -*-
"""飞书推送（文本 + 交互式卡片），服务器零代理版。

复用 TRAE/journal/feishu.py 的纪律：
- HTTP 200 不代表成功，必须校验响应体 business code == 0，否则视为推送失败。
- 服务器直连：trust_env=False 且不设任何 proxies，不读 HTTP_PROXY/HTTPS_PROXY，
  不做“代理失败转直连”兜底。本模块不接受任何 proxy 参数。
- 卡片规范（见飞书开放平台）：card 必须有 config/header/elements 三层；
  分割线用 hr（没有 divider）；header.template 仅允许 blue/green/red/yellow/purple/orange/grey；
  单段 markdown < 4096 字符（否则 99991664）。
"""
from __future__ import annotations

import requests


def _post(webhook: str, payload: dict, timeout: int = 30) -> tuple[bool, str]:
    """统一 POST 并校验业务码 code==0。返回 (ok, detail)。"""
    session = requests.Session()
    session.trust_env = False  # 拒绝环境代理，服务器直连
    try:
        resp = session.post(webhook, json=payload, timeout=timeout)
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


def send_feishu(webhook: str, text: str, timeout: int = 30) -> tuple[bool, str]:
    """推送纯文本消息（msg_type=text）。"""
    return _post(webhook, {"msg_type": "text", "content": {"text": text}}, timeout)


def send_feishu_card(webhook: str, card: dict, timeout: int = 30) -> tuple[bool, str]:
    """推送交互式卡片（msg_type=interactive）。card 为飞书 card 对象。"""
    return _post(webhook, {"msg_type": "interactive", "card": card}, timeout)
