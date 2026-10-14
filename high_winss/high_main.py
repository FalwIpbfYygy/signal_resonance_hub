# -*- coding: utf-8 -*-
"""PAXGUSDT 高胜算形态交易系统 —— 主程序（每 10 秒扫描一次）。"""

import time
from datetime import datetime

import config
from engine import SignalEngine


import os



def main():
    print("=" * 62)
    print("PAXGUSDT 高胜算形态交易系统")
    print(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
    print(f"主信号周期: {config.SIGNAL_INTERVALS}")
    print(f"信号等级: 仅推送 {config.GRADE_STRONG} / {config.GRADE_STRONGEST}")
    print(f"斐波纳契信号: {'启用' if config.FIB_ENABLED else '观察模式(不推送)'}")
    print("=" * 62)

    engine = SignalEngine()
    last_scan_second = -1

    while True:
        now = datetime.now()
        current_second = now.second
        if current_second % config.SCAN_INTERVAL_SECONDS == 0 and current_second != last_scan_second:
            last_scan_second = current_second
            for interval in config.SIGNAL_INTERVALS:
                try:
                    engine.scan_interval(interval)
                except Exception as e:
                    print(f"[{now:%H:%M:%S}] 扫描 {interval} 异常：{e}")
        time.sleep(0.1)


if __name__ == "__main__":
    main()