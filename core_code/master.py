"""
master.py
Unified launcher that monitors the real-time strategy scripts under core_code/.

Design:
- The individual strategy scripts are left untouched; master.py never modifies a single line of them
- Uses runpy.run_path() to execute each .py as __main__, thereby triggering each file's
  `if __name__ == "__main__":` block (the user's "main" method)
- Each script runs in its own daemon thread, non-blocking
- The main thread handles health checks and a unified Ctrl+C shutdown

Strategy scripts launched (named by strategy):
  1. 01_OOPS_long.py           Larry Williams OOPS gap reversal (long)
  2. 02_Pinbar_reversal.py     naked-bar Pinbar reversal
     02_Pinbar_v2.py
  3. 03_OBV_MA21.py            OBV divergence + MA21 golden/death cross
  4. 04_RSI21背离.py           RSI(21) top/bottom divergence
  5. 05_harmonic_AB_CD.py      AB=CD harmonic pattern
  6. 06_TD_nine_turn.py        TD Sequential + MACD histogram filter
  7. 07_EMA100_HA.py           Heikin Ashi + EMA100
  8. 08_top_fractal_short.py   top fractal short
  9. 09_bottom_fractal_long.py bottom fractal long
 10. 10_harami_realtime.py     gold harami realtime
"""

import os
import sys
import time
import threading
import runpy
from datetime import datetime
from contextlib import redirect_stdout, redirect_stderr


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 与 10 个文件一一对应（按策略内容命名）
SCRIPTS = [
    "01_OOPS_long.py",

    "02_Pinbar_reversal.py",

    "05_harmonic_AB_CD.py",
    "06_TD_nine_turn.py",

    "07_EMA100_HA.py",
    "07_EMA100_HA_v2.py",

    "08_top_fractal_short.py",
    "09_bottom_fractal_long.py",

    "10_harami_realtime.py",
    "13_WM.py",
    "11_paxg_engulfing.py",
    "14_setup_signal.py",
    "02_Pinbar_v2.py",
]

class TaggedWriter:
    """把子脚本的输出统一加 [脚本名] 前缀，便于排查"""

    def __init__(self, tag, stream):
        self.tag = tag
        self.stream = stream
        self._lock = threading.Lock()

    def write(self, text):
        if not text:
            return
        with self._lock:
            for line in text.splitlines(keepends=True):
                self.stream.write(f"[{self.tag}] {line}")

    def flush(self):
        with self._lock:
            try:
                self.stream.flush()
            except Exception:
                pass


def run_main(script_name: str):
    """在线程中以 __main__ 身份执行单个脚本"""
    path = os.path.join(BASE_DIR, script_name)
    if not os.path.exists(path):
        print(f"[{datetime.now():%H:%M:%S}] [FAIL] 文件不存在: {script_name}", flush=True)
        return

    print(f"[{datetime.now():%H:%M:%S}] [GO  ] 启动: {script_name}", flush=True)

    tag = script_name.replace(".py", "")
    tagged_out = TaggedWriter(tag, sys.stdout)
    tagged_err = TaggedWriter(tag, sys.stderr)

    # 关键：以 run_name="__main__" 执行，从而命中各文件里的
    #       `if __name__ == "__main__":` 块（也就是用户说的 main 方法）
    try:
        with redirect_stdout(tagged_out), redirect_stderr(tagged_err):
            runpy.run_path(path, run_name="__main__")
    except SystemExit as e:
        print(f"[{datetime.now():%H:%M:%S}] [EXIT] {script_name} SystemExit({e.code})", flush=True)
    except BaseException as e:  # 包括 KeyboardInterrupt、Exception 等
        print(f"[{datetime.now():%H:%M:%S}] [ERR ] {script_name} 异常: {type(e).__name__}: {e}", flush=True)


def main():
    print("=" * 72)
    print(" 10 策略联合监控 master 启动")
    print(f"启动时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"工作目录: {BASE_DIR}")
    print(f"计划并发运行 {len(SCRIPTS)} 个监控线程")
    print("说明: 每个子脚本以 __main__ 身份跑在独立线程中，原文件 0 修改")
    print("=" * 72)

    threads = []
    for name in SCRIPTS:
        t = threading.Thread(
            target=run_main,
            args=(name,),
            daemon=True,            # 主线程退出时自动杀
            name=f"thr-{name.replace('.py','')}",
        )
        t.start()
        threads.append(t)
        time.sleep(0.3)               # 错峰启动，避免瞬时打爆代理

    print(f"\n已派发 {len(threads)} 个线程，进入常驻监听。Ctrl+C 退出。\n", flush=True)

    try:
        while True:
            time.sleep(5)
            for t in threads:
                if not t.is_alive():
                    print(f"[{datetime.now():%H:%M:%S}] [WARN] 线程已退出: {t.name}", flush=True)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，主线程退出；所有守护线程随之结束。", flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
