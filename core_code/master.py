"""
master.py
统一启动并管理 core_code/ 下 10 个  实时监控脚本。

设计：
- 10 个文件保持原样，master.py 不修改其中任何一行
- 用 runpy.run_path() 把每个 .py 当作 __main__ 执行，从而触发
  各文件里的 `if __name__ == "__main__":` 块（即用户所谓的 main 方法）
- 每个脚本独立开一个守护线程，互不阻塞
- 主线程负责巡检 + 接收 Ctrl+C 统一退出

子进程列表（按策略内容命名）：
  1. 01_OOPS做多.py          Larry Williams OOPS 跳空反转
  2. 02_Pinbar反转.py        裸K Pinbar 反转
     02_Pinbar_v2.py
  
  3. 03_OBV_MA21.py          OBV 背离 + MA21 金叉/死叉
  4. 04_RSI21背离.py         RSI(21) 顶/底背离
  5. 05_谐波AB_CD.py         AB=CD 谐波形态
  6. 06_TD九转.py            神奇九转 + MACD 柱过滤
  7. 07_EMA100_HA.py         Heikin Ashi + EMA100
  8. 08_顶分型做空.py        顶分型做空
  9. 09_底分型做多.py        底分型做多
 10. 10_孕线实时.py          黄金孕线实时
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
    "01_OOPS做多.py",

    "02_Pinbar反转_升级.py",

    "05_谐波AB_CD.py",
    "06_TD九转.py",

    "07_EMA100_HA.py",
    "07_EMA100_HA_v2.py",

    "08_顶分型做空.py",
    "09_底分型做多.py",

    "10_孕线实时.py",
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
