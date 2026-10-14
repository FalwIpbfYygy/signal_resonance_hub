# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - QQ邮箱推送模块

与飞书推送并行，将交易信号以纯文本邮件形式发送到QQ邮箱。
SMTP配置集中在 config.py 中。
"""
import smtplib
import threading
import time
from email.mime.text import MIMEText
from email.header import Header
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


class QqEmailNotifier:
    def __init__(self):
        self.sender = config.QQ_EMAIL_SENDER
        self.auth_code = config.QQ_EMAIL_AUTH_CODE
        self.receiver = config.QQ_EMAIL_RECEIVER
        self.smtp_server = config.QQ_SMTP_SERVER
        self.smtp_port = config.QQ_SMTP_PORT
        self.retries = config.QQ_EMAIL_RETRIES
        self.retry_delay = config.QQ_EMAIL_RETRY_DELAY
        self.smtp_timeout = config.QQ_SMTP_TIMEOUT

    def send_signal(self, signal: TradingSignal,
                    market_state: MarketStateResult,
                    filter_desc: str,
                    position_size: float = 0.0):
        """异步发送交易信号邮件"""
        subject = self._format_subject(signal)
        body = self._format_message(signal, market_state, filter_desc, position_size)
        threading.Thread(target=self._send, args=(subject, body), daemon=True).start()

    def _format_subject(self, signal: TradingSignal) -> str:
        setup_cn = SETUP_TYPE_CN.get(signal.setup_type, signal.setup_type)
        direction_cn = "做多" if signal.direction == "long" else "做空"
        return f" {signal.interval} {setup_cn}{direction_cn}信号提醒"

    def _format_message(self, signal: TradingSignal,
                        market_state: MarketStateResult,
                        filter_desc: str,
                        position_size: float) -> str:
        setup_cn = SETUP_TYPE_CN.get(signal.setup_type, signal.setup_type)
        state_cn = STATE_CN.get(
            signal.state if hasattr(signal, 'state') else market_state.state,
            market_state.state
        )
        direction_cn = "做多 ⟶ LONG" if signal.direction == "long" else "做空 ⟶ SHORT"

        if signal.bar_close_time > 0:
            close_time_str = datetime.fromtimestamp(
                signal.bar_close_time / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
        else:
            close_time_str = "未知"

        resonance_str = "、".join(signal.resonance) if signal.resonance else "无"
        reasons_str = "\n".join(f"  • {r}" for r in signal.reasons) if signal.reasons else "  无"
        size_str = f"{position_size:.2f}合约" if position_size > 0 else "未计算"

        lines = [
            f"【 {signal.interval} {setup_cn}信号触发】",
            f"开仓方向：{direction_cn}",
            f"触发架构：{setup_cn}({signal.setup_type})",
            f"信号强度：{signal.grade}",
            f"市场状态：{state_cn}",
            "",
            f"开仓价格：${signal.entry_price:.2f}",
            f"止损价格：${signal.stop_price:.2f}",
            f"TP1：${signal.tp1_price:.2f} → 了结{signal.tp1_ratio * 100:.0f}%",
            f"TP2：${signal.tp2_price:.2f} → 了结{signal.tp2_ratio * 100:.0f}%",
            f"风险回报比：1:{signal.risk_reward:.1f}",
            f"建议仓位：{size_str}",
            "",
            "信息参考：",
            f"  ATR(14)：{market_state.atr:.2f}",
            f"  Hurst：{market_state.hurst:.2f}",
            f"  趋势持续：{market_state.trend_bars}棒",
            f"  共振周期：{resonance_str}",
            f"  识别理由：",
            reasons_str,
            "",
            "过滤确认：",
            f"  {filter_desc}" if filter_desc else "  无",
            "",
            f"扫描时间：{signal.timestamp}",
            f"K线收盘：{close_time_str}(北京时间)",
            "",
            "【学习交流，不作为交易依据】",
        ]
        return "\n".join(lines)

    def _send(self, subject: str, body: str):
        send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        msg = self._build_message(subject, body)

        for attempt in range(1, self.retries + 1):
            try:
                server = smtplib.SMTP_SSL(
                    self.smtp_server, self.smtp_port, timeout=self.smtp_timeout
                )
                server.login(self.sender, self.auth_code)
                server.sendmail(self.sender, self.receiver, msg.as_string())
                try:
                    server.quit()
                except Exception:
                    pass
                print(f"[{send_time}] ✅ QQ邮箱发送成功: {subject}")
                return
            except Exception as e:
                print(f"[{send_time}] ❌ QQ邮箱发送失败(第{attempt}/{self.retries}次): {str(e)}")
                if attempt < self.retries:
                    time.sleep(self.retry_delay)

        # 全部重试失败 → 触发备用通知(飞书)
        self._fallback_notify(subject)

    def _build_message(self, subject: str, body: str) -> MIMEText:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = Header("量化告警系统", "utf-8").encode() + f" <{self.sender}>"
        msg["To"] = Header("交易者", "utf-8").encode() + f" <{self.receiver}>"
        msg["Subject"] = Header(subject, "utf-8")
        return msg

    def _fallback_notify(self, subject: str):
        """QQ邮箱全部重试失败后，通过飞书发送备用告警"""
        try:
            from feishu_notifier import FeishuNotifier
            text = (f"⚠️ QQ邮箱推送失败，已重试{self.retries}次仍无法发送。\n"
                    f"主题：{subject}\n请检查邮箱配置或SMTP授权码。")
            FeishuNotifier().send_text(text)
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ❌ 备用通知发送失败: {str(e)}")