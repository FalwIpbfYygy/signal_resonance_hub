"""飞书通知推送（卡片样式 + 信号去重 + 交易时段标签）"""
import requests
from datetime import datetime
from typing import List

from config import FEISHU_WEBHOOK
from signals.engine import TradeSignal
from utils.time_utils import get_session_display, format_cst


_LAST_SIGNAL_KEYS: set = set()


def build_signal_card(signal: TradeSignal) -> dict:
    """构造飞书交互式卡片"""
    direction_emoji = "🔴 SELL 做空" if signal.direction == "SELL" else "🟢 BUY 做多"
    strength_stars = "⭐" * signal.strength
    strength_label = "高概率" if signal.strength >= 4 else ("中概率" if signal.strength >= 3 else "低概率")
    # 卡片标题配色：做多蓝色 / 做空红色
    header_template = "blue" if signal.direction == "BUY" else "red"

    confirm_lines = "\n".join([f"✅ {c}" for c in signal.confirmations])

    risk = abs(signal.stop_loss - signal.entry_price)
    reward = abs(signal.take_profit - signal.entry_price)
    rr = reward / risk if risk > 0 else 0

    card = {
        "header": {
            "title": {
                "content": "📈 ICT  交易信号"
            },
            "template": header_template
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "content": f"""
**信号方向**: {direction_emoji}
**交易时段**: {get_session_display(signal.session)}
**信号类型**: {signal.signal_type}
**信号强度**: {strength_stars}（{strength_label}）
**触发价位**: {signal.level.name} ${signal.level.price:.2f}

**GMT时间**: {signal.timestamp.strftime('%Y-%m-%d %H:%M:%S')}
**北京时间**: {format_cst(signal.timestamp)}
                    """.strip(),
                    "tag": "lark_md"
                }
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "content": f"""
入场价格：${signal.entry_price:.2f}
止损价格：${signal.stop_loss:.2f}
止盈目标：${signal.take_profit:.2f}
风险回报比：1:{rr:.1f}
周期级别：15m结构 / 1m执行
                    """.strip(),
                    "tag": "lark_md"
                }
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "content": f"**确认条件**\n{confirm_lines}",
                    "tag": "lark_md"
                }
            }
        ]
    }
    return card


def push_signals(signals: List[TradeSignal]):
    """推送信号卡片至飞书，内置信号去重"""
    global _LAST_SIGNAL_KEYS

    for signal in signals:
        key = dedup_key(signal)
        if key in _LAST_SIGNAL_KEYS:
            continue
        _LAST_SIGNAL_KEYS.add(key)

        # 限制缓存数量，防止内存持续上涨
        if len(_LAST_SIGNAL_KEYS) > 100:
            _LAST_SIGNAL_KEYS = set(list(_LAST_SIGNAL_KEYS)[-100:])

        card = build_signal_card(signal)
        payload = {
            "msg_type": "interactive",
            "card": card
        }
        send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        try:
            resp = requests.post(FEISHU_WEBHOOK, json=payload)
            if resp.status_code != 200:
                print(f"[{send_time}] ❌ 飞书卡片推送失败 HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                try:
                    body = resp.json()
                except Exception:
                    print(f"[{send_time}] ❌ 飞书卡片推送失败：响应非 JSON: {resp.text[:200]}")
                else:
                    if not isinstance(body, dict) or body.get("code") != 0:
                        print(f"[{send_time}] ❌ 飞书卡片推送失败 业务码 {body.get('code') if isinstance(body, dict) else '?'}（HTTP 200 但业务未成功）: {str(body)[:200]}")
                    else:
                        print(f"[{send_time}] ✅ 飞书卡片推送成功，状态码: {resp.status_code}")
        except Exception as e:
            print(f"[{send_time}] 飞书推送异常: {str(e)}")


def dedup_key(signal: TradeSignal) -> str:
    """生成信号去重Key：5分钟桶，同方向同级别合并抑制重复推送"""
    bucket = signal.timestamp.replace(second=0, microsecond=0)
    bucket = bucket.replace(minute=(bucket.minute // 5) * 5)
    return f"{signal.level.name}_{signal.direction}_{bucket.strftime('%Y%m%d_%H%M')}"
