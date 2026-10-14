# -*- coding: utf-8 -*-
"""命令行入口：拉数 -> 扫描 -> 打印。手动运行，不挂后台。

用法示例：
  python cli.py                                    # 默认  5m 1000根，放量1.8x
  python cli.py --symbol  --interval 15m    # 换周期
  python cli.py --no-volume                        # 关闭放量过滤
  python cli.py --proxy ""                         # 服务器零代理
  python cli.py --lower-ratio-min 0.6 --upper-ratio-max 0.1   # 调严格度
"""
from __future__ import annotations

import argparse
import os
import sys

# 让 cli 能 import 同目录模块（无论从哪里运行）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import merged
from detector import scan
from fetcher import fetch_klines


def _build_parser():
    p = argparse.ArgumentParser(description=" 5m 锤子/针形形态扫描器")
    p.add_argument("--symbol", default=None, help="交易对，默认 ")
    p.add_argument("--interval", default=None, help="K线周期，默认 5m")
    p.add_argument("--limit", type=int, default=None, help="拉取根数，默认 1000")
    p.add_argument("--lower-ratio-min", type=float, default=None, help="下影占全幅下限，默认0.55")
    p.add_argument("--upper-ratio-max", type=float, default=None, help="上影占全幅上限(十字星闸门)，默认0.15")
    p.add_argument("--body-ratio-max", type=float, default=None, help="实体占全幅上限，默认0.30")
    p.add_argument("--close-pos-min", type=float, default=None, help="收盘位置下限，默认0.60")
    p.add_argument("--lower-to-body-min", type=float, default=None, help="下影/实体下限，默认2.0")
    p.add_argument("--no-volume", action="store_true", help="关闭放量过滤")
    p.add_argument("--vol-ma-window", type=int, default=None, help="成交量MA窗口，默认20")
    p.add_argument("--vol-mult", type=float, default=None, help="放量倍数，默认1.8")
    p.add_argument("--proxy", default=None, help="代理地址；传空串\"\"=零代理；不传则用 config 默认")
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)
    overrides = {
        "symbol": args.symbol,
        "interval": args.interval,
        "limit": args.limit,
        "lower_ratio_min": args.lower_ratio_min,
        "upper_ratio_max": args.upper_ratio_max,
        "body_ratio_max": args.body_ratio_max,
        "close_pos_min": args.close_pos_min,
        "lower_to_body_min": args.lower_to_body_min,
        "volume_filter": (False if args.no_volume else None),
        "vol_ma_window": args.vol_ma_window,
        "vol_mult": args.vol_mult,
        # proxy: 显式传 "" 表示零代理；传其他值覆盖默认；不传用默认
        "proxy": ("" if args.proxy == "" else (args.proxy if args.proxy is not None else None)),
    }
    p = merged(overrides)

    print(f"[拉取] {p['symbol']} {p['interval']} limit={p['limit']} "
          f"proxy={'零代理' if not p['proxy'] else p['proxy']}")
    klines = fetch_klines(p["symbol"], p["interval"], p["limit"], p["proxy"])
    print(f"[数据] 共 {len(klines)} 根，覆盖 "
          f"{_bt(klines[0])} ~ {_bt(klines[-1])} (北京)")

    hits = scan(klines, p)
    vol_tag = f"放量≥{p['vol_mult']}xMA{p['vol_ma_window']}" if p["volume_filter"] else "不放开量"
    print(f"\n[结果] 锤子形态命中 {len(hits)} 根（{vol_tag}）")
    print(f"{'时间(北京)':<17}{'下影/实体':>10}{'下影/全幅':>10}{'上影/全幅':>10}{'收位':>8}{'量/MA20':>9}")
    for h in hits:
        vr = h["vol_ratio"] if h["vol_ratio"] is not None else 0.0
        print(f"{h['time_bj']:<17}"
              f"{h['lower_to_body']:>9.1f}x"
              f"{h['lower_ratio']*100:>9.0f}%"
              f"{h['upper_ratio']*100:>9.0f}%"
              f"{h['close_pos']*100:>7.0f}%"
              f"{vr:>8.1f}x")


def _bt(k):
    from datetime import datetime, timezone, timedelta
    return datetime.fromtimestamp(float(k[0]) / 1000, timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")


if __name__ == "__main__":
    main()
