# -*- coding: utf-8 -*-
"""
report.py
信号日报表定时推送脚本。
职责：
1. 常驻运行，监测本地时间
2. 每日 17:00 生成第一份报表（统计 07:00 ~ 17:00）
3. 每日 23:00 生成第二份报表（统计 07:00 ~ 23:00）
4. 读取 signal_store 入库的 JSON，按时段筛选汇总
5. 生成 Markdown 表格，通过飞书【折叠卡片】推送
说明：凌晨 0-7 点黄金信号噪音大，不计入统计（见 _filter_signals_by_range）。
表格仅含基础字段，不输出任何行情分析、胜率、盈亏复盘。
"""
import os
import sys
import json
import time
import requests
from datetime import datetime
# 复用统一入库模块的数据目录与读取逻辑
import signal_store
# 日报表专用飞书 Webhook
REPORT_WEBHOOK = os.environ.get("REPORT_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
# 两个报表时段（小时）：17:00 与 23:00
REPORT_HOURS = [17, 23]
# 每 30 秒检测一次是否到点
CHECK_INTERVAL = 30
# 表头（与入库字段一一对应）
HEADERS = ["信号时间", "品种", "K线周期", "形态/架构", "方向",
           "入场价", "止损价", "止盈TP1", "止盈TP2"]


def _parse_signal_time(ts):
    """把信号时间解析为 datetime，兼容带秒/不带秒格式，失败返回 None。"""
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _filter_signals_by_range(signals, start_dt, end_dt):
    """按时间窗 [start_dt, end_dt] 筛选信号列表（含边界）。"""
    out = []
    for s in signals:
        t = _parse_signal_time(s.get("time", ""))
        if t is None:
            continue
        # 过滤凌晨 0-7 点（hour < 7），这部分不计入统计
        if t.hour < 7:
            continue
        if start_dt <= t <= end_dt:
            out.append(s)
    # 按信号时间升序排列
    out.sort(key=lambda s: s.get("time", ""))
    return out


def _fmt_price(v):
    """价格格式化：None → "-"，否则保留 2 位小数。"""
    if v is None or v == "":
        return "-"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "-"


def _build_markdown_table(signals):
    """把信号列表渲染成 Markdown 表格字符串（不附加任何分析文字）。"""
    lines = []
    lines.append("| " + " | ".join(HEADERS) + " |")
    lines.append("|" + "|".join(["---"] * len(HEADERS)) + "|")
    if not signals:
        # 无信号时给一行占位，避免表格为空
        lines.append("| " + " | ".join(["-"] * len(HEADERS)) + " |")
    else:
        for s in signals:
            row = [
                s.get("time", "-"),
                s.get("symbol", "-"),
                s.get("interval", "-"),
                s.get("pattern", "-"),
                s.get("direction", "-"),
                _fmt_price(s.get("entry")),
                _fmt_price(s.get("sl")),
                _fmt_price(s.get("tp1")),
                _fmt_price(s.get("tp2")),
            ]
            lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _build_card(title, table_md):
    """构造飞书卡片，表格放入折叠面板，默认收起"""
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": "🔻 点击展开信号明细表格"
                        }
                    },
                    "elements": [
                        {"tag": "markdown", "content": table_md}
                    ]
                }
            ],
        },
    }


def _send_report(title, table_md):
    """推送报表到飞书，失败打印日志但不抛出。"""
    payload = _build_card(title, table_md)
    try:
        resp = requests.post(REPORT_WEBHOOK, json=payload, timeout=60)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if resp.status_code != 200:
            print(f"[{now}] ❌ 报表推送失败 HTTP {resp.status_code}: {resp.text[:200]}")
        else:
            try:
                body = resp.json()
            except Exception:
                print(f"[{now}] ❌ 报表推送失败：响应非 JSON: {resp.text[:200]}")
            else:
                if not isinstance(body, dict) or body.get("code") != 0:
                    print(f"[{now}] ❌ 报表推送失败 业务码 {body.get('code') if isinstance(body, dict) else '?'}（HTTP 200 但业务未成功）: {str(body)[:200]}")
                else:
                    print(f"[{now}] ✅ 报表推送成功: {title}")
    except Exception as e:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] ❌ 报表推送异常: {e}")


def generate_daily_report(now=None):
    """生成并推送当日报表（读取当天 JSON 数据）。"""
    if now is None:
        now = datetime.now()
    date_key = now.strftime("%Y%m%d")
    signals = signal_store.load_signals(date_key)
    # 统计起点 07:00（凌晨 0-7 点信号不计入，见 _filter_signals_by_range）
    day_start = datetime(now.year, now.month, now.day, 7, 0, 0)
    # 全天结束（含 23:59:59）
    day_end = datetime(now.year, now.month, now.day, 23, 59, 59)
    hour = now.hour
    if hour == 17:
        end = datetime(now.year, now.month, now.day, 17, 0, 0)
        title = f"XAU/PAX 信号日报 · {now.strftime('%Y-%m-%d')} 07:00~17:00"
    elif hour == 23:
        end = day_end
        title = f"XAU/PAX 信号日报 · {now.strftime('%Y-%m-%d')} 07:00~23:00"
    else:
        return
    filtered = _filter_signals_by_range(signals, day_start, end)
    table_md = _build_markdown_table(filtered)
    _send_report(title, table_md)


def main():
    print("=" * 60)
    print("信号日报表定时推送脚本启动")
    print(f"推送时段: {REPORT_HOURS}")
    print(f"扫描间隔: {CHECK_INTERVAL} 秒")
    print(f"Webhook: {REPORT_WEBHOOK[:50]}...")
    print("=" * 60)
    # 记录每个时段当天是否已推送，避免同一小时重复推送
    last_pushed = {}  # {date_key: {hour: True}}
    while True:
        now = datetime.now()
        hour = now.hour
        date_key = now.strftime("%Y%m%d")
        if hour in REPORT_HOURS:
            pushed_map = last_pushed.setdefault(date_key, {})
            if not pushed_map.get(hour):
                try:
                    generate_daily_report(now)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                pushed_map[hour] = True
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()


