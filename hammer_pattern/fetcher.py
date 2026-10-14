# -*- coding: utf-8 -*-
"""K 线拉取适配层（网络只在此处出现；检测层纯函数，不依赖此模块）。

实现说明：
  - 本地实测：币安被墙，经本地代理(127.0.0.1:10808)用 Python(urllib/requests) 的
    HTTPS-CONNECT 隧道握手超时；但 `curl -x <proxy>` 稳定可用（仅较慢）。
  - 故 fetcher 直接用 curl 子进程拉数（Windows Git Bash / Ubuntu 均有 curl）。
  - 零代理纪律：部署到服务器时 proxy 置空，curl 不传 -x 即直连。
"""
from __future__ import annotations

import os
import json
import subprocess
from typing import Optional


def fetch_klines(symbol: str, interval: str, limit: int, proxy: Optional[str] = None) -> list:
    """拉取 Binance fapi klines。返回原始 list（[open_time,o,h,l,c,vol,...]）。"""
    url = (
        f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}"
        f"?symbol={symbol}&interval={interval}&limit={limit}"
    )
    cmd = ["curl", "-s", "-m", "120"]
    if proxy:
        cmd += ["-x", proxy]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=130)
    if proc.returncode != 0:
        raise RuntimeError(f"curl 失败 rc={proc.returncode}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"返回非 JSON（代理/网络异常？）：{proc.stdout[:200]}") from e
