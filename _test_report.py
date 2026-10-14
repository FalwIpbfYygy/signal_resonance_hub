# -*- coding: utf-8 -*-
"""临时测试：模拟各系统信号，真实推送 17:00 / 23:00 两份日报表到飞书。"""
from datetime import datetime
import report

TODAY = "2026-08-20"

# 覆盖全部系统 + 全部字段情形（完整 / 补TP / 全缺 / 单止盈 / 多空 / 多周期双品种）
morning = [
    {"time": f"{TODAY} 09:15:00", "symbol": "", "interval": "1m/15m",
     "pattern": "ICT-BSG做多", "direction": "做多", "entry": 3210.5, "sl": 3205.0, "tp1": 3220.0, "tp2": None},
    {"time": f"{TODAY} 10:00:00", "symbol": "", "interval": "15m",
     "pattern": "H2回调买进", "direction": "做多", "entry": 3208.2, "sl": 3201.1, "tp1": 3220.0, "tp2": 3234.4},
    {"time": f"{TODAY} 11:30:00", "symbol": "PAXGUSDT", "interval": "1h",
     "pattern": "看跌Pinbar", "direction": "做空", "entry": 3285.0, "sl": 3292.0, "tp1": 3270.0, "tp2": 3255.0},
    {"time": f"{TODAY} 13:20:00", "symbol": "", "interval": "15m",
     "pattern": "AB=CD做多", "direction": "做多", "entry": 3215.0, "sl": 3206.3, "tp1": 3224.0, "tp2": 3230.0},
    {"time": f"{TODAY} 14:05:00", "symbol": "", "interval": "5m",
     "pattern": "强势OOPS做多", "direction": "做多", "entry": 3218.0, "sl": 3212.5, "tp1": 3223.5, "tp2": 3229.0},
    {"time": f"{TODAY} 15:40:00", "symbol": "", "interval": "30m",
     "pattern": "TD九转低九", "direction": "做多", "entry": 3219.7, "sl": None, "tp1": None, "tp2": None},
    {"time": f"{TODAY} 16:25:00", "symbol": "", "interval": "30m",
     "pattern": "看跌吞没(做空)", "direction": "做空", "entry": 3222.0, "sl": 3226.0, "tp1": 3218.0, "tp2": None},
]

# 23:00 全天报表 = 上午全部 + 17:00 之后新增
later = [
    {"time": f"{TODAY} 18:10:00", "symbol": "", "interval": "5m",
     "pattern": "顶分型做空", "direction": "做空", "entry": 3216.0, "sl": 3221.0, "tp1": 3206.0, "tp2": 3196.0},
    {"time": f"{TODAY} 19:45:00", "symbol": "", "interval": "5m",
     "pattern": "底分型做多", "direction": "做多", "entry": 3198.0, "sl": 3192.0, "tp1": 3210.0, "tp2": 3222.0},
    {"time": f"{TODAY} 20:30:00", "symbol": "", "interval": "1m",
     "pattern": "EMA100+HA回调v2", "direction": "做多", "entry": 3202.0, "sl": 3197.0, "tp1": 3207.0, "tp2": 3212.0},
    {"time": f"{TODAY} 21:15:00", "symbol": "PAXGUSDT", "interval": "5m",
     "pattern": "PAXG-吞没+位置", "direction": "做多", "entry": 3290.0, "sl": 3285.0, "tp1": 3300.0, "tp2": 3310.0},
    {"time": f"{TODAY} 22:00:00", "symbol": "", "interval": "1m",
     "pattern": "WM三要素", "direction": "做空", "entry": 3205.5, "sl": 3209.0, "tp1": 3200.5, "tp2": 3195.5},
    {"time": f"{TODAY} 22:30:00", "symbol": "", "interval": "15m",
     "pattern": "黄金孕线", "direction": "做空", "entry": 3207.0, "sl": 3210.2, "tp1": 3202.0, "tp2": 3197.0},
]

if __name__ == "__main__":
    print("=" * 60)
    print("开始模拟推送两份日报表到飞书")

    # 第一份：17:00 报表
    t1 = f"XAU/PAX 信号日报 · {TODAY} 00:00~17:00"
    report._send_report(t1, report._build_markdown_table(morning))
    print("→ 17:00 报表已发送")

    # 第二份：23:00 全天报表
    t2 = f"XAU/PAX 信号日报 · {TODAY} 00:00~23:00"
    full = morning + later
    report._send_report(t2, report._build_markdown_table(full))
    print("→ 23:00 报表已发送")

    print("完成。请到飞书查看两条卡片消息。")