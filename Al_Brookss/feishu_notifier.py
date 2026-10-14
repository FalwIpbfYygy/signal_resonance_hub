# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 飞书推送模块（卡片版 + 调试增强 + 修正分割线）
"""
import threading
import requests
from datetime import datetime

import config
from bar_patterns import TradingSignal
from market_state import MarketStateResult


SETUP_TYPE_CN = {
    "second_entry":      "二次入场",
    "failed_failure":    "失败的失败",
    "h2_trend":          "H2回调买进",
    "l2_trend":          "L2回调卖出",
    "h1_strong":         "H1强趋势买进",
    "l1_strong":         "L1强趋势卖出",
    "mtr_conservative":  "MTR保守反转",
    "mtr_aggressive":    "MTR激进反转",
    "wedge_flag":        "楔形旗形",
    "tr_reversal":       "区间反转",
    "climax_reversal":   "高潮反转",
    "final_flag":        "最终旗形反转",
}

STATE_CN = {
    "trend_up":   "多头趋势",
    "trend_down": "空头趋势",
    "tr":         "交易区间",
    "ttr":        "紧凑区间",
    "unknown":    "未知",
}


class FeishuNotifier:
    def __init__(self):
        self.webhook = config.FEISHU_WEBHOOK

    def send_signal(self, signal: TradingSignal,
                    market_state: MarketStateResult,
                    filter_desc: str,
                    position_size: float = 0.0):
        msg = self._format_message(signal, market_state, filter_desc, position_size)
        threading.Thread(target=self._send, args=(msg,), daemon=True).start()


    def _format_message(self, signal: TradingSignal,
                        market_state: MarketStateResult,
                        filter_desc: str,
                        position_size: float) -> dict:
        setup_cn = SETUP_TYPE_CN.get(signal.setup_type, signal.setup_type)
        state_cn = STATE_CN.get(
            signal.state if hasattr(signal, 'state') else market_state.state,
            market_state.state
        )

        slope_threshold = config.TREND_SLOPE_THRESHOLD * market_state.ema20
        if market_state.ema_slope > slope_threshold:
            ema_dir = "向上"
        elif market_state.ema_slope < -slope_threshold:
            ema_dir = "向下"
        else:
            ema_dir = "水平"

        # ----- 改动开始 -----
        direction_cn = "做多" if signal.direction == "long" else "做空"
        color = "green" if signal.direction == "long" else "red"
        direction_display = f"<font color='{color}'><b>{direction_cn}</b></font>"
        # ----- 改动结束 -----

        if signal.bar_close_time > 0:
            close_time_str = datetime.fromtimestamp(
                signal.bar_close_time / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
        else:
            close_time_str = "未知"

        session = self._get_session()
        resonance_str = "、".join(signal.resonance) if signal.resonance else "无"
        reasons_str = "\n".join(f"  • {r}" for r in signal.reasons) if signal.reasons else "  无"
        size_str = f"{position_size:.2f}合约" if position_size > 0 else "未计算"

        title = f"{signal.interval}  {setup_cn}信号"

        info_lines = [
            f"**扫描时间**：{signal.timestamp}",
            f"**K线收盘**：{close_time_str}(北京时间)",
            f"**开仓方向**：{direction_display}",  # ← 使用带颜色的变量
            f"**开仓价格**：${signal.entry_price:.2f}",
            f"**信号强度**：{signal.grade}",
            f"**市场状态**：{state_cn}(EMA20 {ema_dir})",
            f"**触发架构**：{setup_cn}({signal.setup_type})"
        ]
        info_text = "\n".join(info_lines)

        risk_text = "\n".join([
            f"**止损**：${signal.stop_price:.2f} (信号棒极值±{config.SL_ATR_MULT}×ATR)",
            f"**TP1**：${signal.tp1_price:.2f} → 了结{signal.tp1_ratio * 100:.0f}%",
            f"**TP2**：${signal.tp2_price:.2f} → 了结{signal.tp2_ratio * 100:.0f}%",
            f"**风险回报比**：1:{signal.risk_reward:.1f}",
            f"**建议仓位**：{size_str}"
        ])

        filter_text = f"**过滤确认**\n{filter_desc}"

        aux_text = "\n".join([
            f"**ATR(14)**：{market_state.atr:.2f}",
            f"**Hurst**：{market_state.hurst:.2f}",
            f"**ATR比率**：{market_state.atr_ratio:.2f}",
            f"**趋势持续**：{market_state.trend_bars}棒",
            f"**当前时段**：{session}",
            f"**共振周期**：{resonance_str}",
            f"**识别理由**：\n{reasons_str}"
        ])

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red" if signal.direction == "short" else "green",
                "title": {"tag": "plain_text", "content": title}
            },
            "elements": [
                {"tag": "markdown", "content": info_text},
                {"tag": "hr"},
                {"tag": "markdown", "content": risk_text},
                {"tag": "hr"},
                {"tag": "markdown", "content": filter_text},
                {"tag": "hr"},
                {"tag": "markdown", "content": aux_text}
            ]
        }
        return {"msg_type": "interactive", "card": card}

    def _get_session(self) -> str:
        hour = datetime.now().hour
        for session_name, (start, end) in config.SESSIONS.items():
            if start <= hour < end:
                names = {
                    "asian":   "亚盘",
                    "eu_open": "欧盘开盘",
                    "eu":      "欧盘",
                    "us_open": "美盘开盘",
                    "us":      "美盘",
                }
                return names.get(session_name, session_name)
        return "休市"

    def _send(self, msg: dict):
        send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        try:
            response = requests.post(self.webhook, json=msg, timeout=50)
            if response.status_code == 200:
                try:
                    resp_data = response.json()
                    if resp_data.get('code') == 0:
                        print(f"[{send_time}] ✅ 飞书卡片发送成功")
                    else:
                        print(f"[{send_time}] ❌ 飞书业务错误: {resp_data}")
                except:
                    print(f"[{send_time}] ⚠️ 飞书返回非JSON响应: {response.text[:200]}")
            else:
                print(f"[{send_time}] ❌ HTTP错误 {response.status_code}: {response.text[:200]}")
        except Exception as e:
            print(f"[{send_time}] ❌ 请求异常: {str(e)}")

    def send_text(self, text: str):
        msg = {"msg_type": "text", "content": {"text": text}}
        threading.Thread(target=self._send, args=(msg,), daemon=True).start()

    def test_card(self):
        """测试卡片（也使用 hr）"""
        test_msg = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "blue",
                    "title": {"tag": "plain_text", "content": "🧪 飞书卡片测试"}
                },
                "elements": [
                    {"tag": "markdown", "content": "**连接成功**！如果你看到这条消息，说明卡片格式和webhook均正常。"},
                    {"tag": "hr"},   # 分割线测试
                    {"tag": "markdown", "content": "这是第二条信息"}
                ]
            }
        }
        print("[测试] 正在发送测试卡片...")
        self._send(test_msg)


