# -*- coding: utf-8 -*-
"""飞书推送模块：异步发送，不阻塞主循环。"""

import threading
import time
import smtplib
import requests
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timezone, timedelta

import config


def _post(payload):
    """POST 到飞书，校验 HTTP 200 且业务码 code==0。返回 (ok, detail)。"""
    try:
        resp = requests.post(config.FEISHU_WEBHOOK, json=payload)
    except Exception as e:
        return False, f"ERR:{e}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        body = resp.json()
    except Exception:
        return False, f"响应非 JSON: {resp.text[:200]}"
    if not isinstance(body, dict) or body.get("code") != 0:
        return False, f"业务码 {body.get('code') if isinstance(body, dict) else '?'}（HTTP 200 但业务未成功）: {str(body)[:200]}"
    return True, "ok"


def send_feishu_async(text):
    """异步发送飞书文本消息（后台线程）。"""
    def worker():
        ok, detail = _post({"msg_type": "text", "content": {"text": text}})
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        if ok:
            print(f"[{ts}] ✅ 飞书发送成功")
        else:
            print(f"[{ts}] ❌ 飞书发送失败 {detail}")
    t = threading.Thread(target=worker, daemon=True)
    t.start()


def format_signal(interval, sig, trend_text):
    """按 PRD §10.2 格式化信号文本。

    sig["bar_time"] 为接口 K 线 close_time（毫秒，GMT/UTC），
    用作信号时间，避免取程序运行时刻。
    """
    direction = "多" if sig["direction"] == 1 else "空"
    quality = sig.get("quality", "strong")

    # 信号时间：来自接口 K 线 close_time（毫秒），GMT 与 北京时间（UTC+8）由同一时刻派生
    time_lines = []
    ts = sig.get("bar_time")
    if ts:
        gmt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        bj = gmt.astimezone(timezone(timedelta(hours=8)))
        time_lines = [
            f"GMT时间: {gmt.strftime('%Y-%m-%d %H:%M:%S')}",
            f"北京时间: {bj.strftime('%Y-%m-%d %H:%M:%S')}",
        ]

    lines = time_lines + [
        f"【{interval} PAXGUSDT {sig['pattern']} · {direction} · {quality}】",
        f"入场价：{sig['entry']}",
        f"止损价：{sig['stop']}",
        f"目标1：{sig['target1']}",
        f"目标2：{sig['target2']}",
        f"趋势背景：{trend_text}",
        f"放量确认：{'是' if sig.get('vol_ok') else '否'}",
        f"形态质量：接触点 {sig.get('touches', 0)}",
        f"信号说明：{sig.get('detail', '')}",
    ]
    return "\n".join(lines)


def _send_email_once(subject: str, body: str):
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = Header("量化告警系统", "utf-8").encode() + f" <{config.QQ_EMAIL_SENDER}>"
    msg["To"] = Header("交易者", "utf-8").encode() + f" <{config.QQ_EMAIL_RECEIVER}>"
    msg["Subject"] = Header(subject, "utf-8")

    server = smtplib.SMTP_SSL(
        config.QQ_SMTP_SERVER, config.QQ_SMTP_PORT, timeout=config.QQ_SMTP_TIMEOUT
    )
    server.login(config.QQ_EMAIL_SENDER, config.QQ_EMAIL_AUTH_CODE)
    server.sendmail(config.QQ_EMAIL_SENDER, config.QQ_EMAIL_RECEIVER, msg.as_string())
    try:
        server.quit()
    except Exception:
        pass


def send_email_async(subject: str, body: str):
    """异步发送QQ邮箱（后台线程，带重试与飞书备用告警）。"""
    if not config.QQ_EMAIL_ENABLED:
        return

    def worker():
        for attempt in range(1, config.QQ_EMAIL_RETRIES + 1):
            try:
                _send_email_once(subject, body)
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                print(f"[{ts}] QQ邮箱发送成功：{subject}")
                return
            except Exception as e:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                print(f"[{ts}] QQ邮箱发送失败(第{attempt}/{config.QQ_EMAIL_RETRIES}次)：{e}")
                if attempt < config.QQ_EMAIL_RETRIES:
                    time.sleep(config.QQ_EMAIL_RETRY_DELAY)

        # 全部重试失败 → 飞书备用告警
        try:
            send_feishu_async(
                f"⚠️ QQ邮箱推送失败，已重试{config.QQ_EMAIL_RETRIES}次仍无法发送。\n"
                f"主题：{subject}\n请检查邮箱配置或SMTP授权码。"
            )
        except Exception as e:
            print(f"备用通知发送失败：{e}")

    threading.Thread(target=worker, daemon=True).start()