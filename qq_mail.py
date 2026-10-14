# -*- coding: utf-8 -*-
"""
qq_mail.py
QQ 邮箱推送通道（纯标准库，供根目录模块如 resonance.py 复用）。

与 Al_Brookss/config.py 使用同一套账号口径，避免出现两个发信配置：
    发件 <YOUR_QQ_SENDER> → 收件 <YOUR_QQ_RECEIVER>，smtp.qq.com:465 (SSL)

配置优先级：环境变量 > 本文件默认值
    QQ_EMAIL_ENABLED  1/0        是否启用
    QQ_EMAIL_SENDER   发件邮箱
    QQ_EMAIL_AUTH_CODE  SMTP 授权码（不是登录密码）
    QQ_EMAIL_RECEIVER 收件邮箱
    QQ_SMTP_SERVER / QQ_SMTP_PORT

用法：
    import qq_mail
    qq_mail.send_async("主题", "正文")     # 异步 daemon 线程，不阻塞主循环
    qq_mail.send("主题", "正文")           # 同步（自检/调试用）
"""
import os
import smtplib
import threading
import time
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText

# ====================== 配置 ======================
QQ_EMAIL_ENABLED = os.environ.get("QQ_EMAIL_ENABLED", "1").strip().lower() \
    not in ("0", "false", "no", "off")
QQ_EMAIL_SENDER = os.environ.get("QQ_EMAIL_SENDER", "")
QQ_EMAIL_AUTH_CODE = os.environ.get("QQ_EMAIL_AUTH_CODE", "")
QQ_EMAIL_RECEIVER = os.environ.get("QQ_EMAIL_RECEIVER", "")
QQ_SMTP_SERVER = os.environ.get("QQ_SMTP_SERVER", "smtp.qq.com")
QQ_SMTP_PORT = int(os.environ.get("QQ_SMTP_PORT", "465"))

SMTP_TIMEOUT = 30           # 连接/发送超时（秒）
RETRIES = 3                 # 失败重试次数
RETRY_DELAY = 5             # 重试间隔（秒）

SENDER_NAME = "信号共振检测"


def is_enabled() -> bool:
    """是否具备发信条件：开关打开且发件人/授权码/收件人均非空。"""
    return bool(QQ_EMAIL_ENABLED and QQ_EMAIL_SENDER
                and QQ_EMAIL_AUTH_CODE and QQ_EMAIL_RECEIVER)


def _build_message(subject: str, body: str) -> MIMEText:
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = Header(SENDER_NAME, "utf-8").encode() + f" <{QQ_EMAIL_SENDER}>"
    msg["To"] = Header("交易者", "utf-8").encode() + f" <{QQ_EMAIL_RECEIVER}>"
    msg["Subject"] = Header(subject, "utf-8")
    return msg


def send(subject: str, body: str) -> bool:
    """同步发送邮件，失败重试 RETRIES 次。返回是否成功。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not is_enabled():
        print(f"[{ts}] [qq_mail] 未启用（开关关闭或收/发件人、授权码为空），跳过")
        return False

    msg = _build_message(subject, body)
    for attempt in range(1, RETRIES + 1):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        server = None
        try:
            server = smtplib.SMTP_SSL(QQ_SMTP_SERVER, QQ_SMTP_PORT, timeout=SMTP_TIMEOUT)
            server.login(QQ_EMAIL_SENDER, QQ_EMAIL_AUTH_CODE)
            server.sendmail(QQ_EMAIL_SENDER, [QQ_EMAIL_RECEIVER], msg.as_string())
            print(f"[{ts}] ✅ QQ邮箱发送成功: {subject}")
            return True
        except Exception as e:
            print(f"[{ts}] ❌ QQ邮箱发送失败(第{attempt}/{RETRIES}次): {e}")
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY)
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
    return False


def send_async(subject: str, body: str):
    """异步发送（daemon 线程），失败只打日志，不阻塞调用方主循环。"""
    if not is_enabled():
        return
    threading.Thread(target=send, args=(subject, body), daemon=True).start()


if __name__ == "__main__":
    # 连通性自检：python qq_mail.py
    print(f"enabled = {is_enabled()}")
    print(f"sender  = {QQ_EMAIL_SENDER} → receiver = {QQ_EMAIL_RECEIVER}")
    ok = send("【测试】信号共振 QQ 邮箱通道",
              "这是一封来自 qq_mail.py 的连通性测试邮件。\n"
              f"发送时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
              "【学习交流，不作为交易依据】")
    print(f"send ok = {ok}")
