# -*- coding: utf-8 -*-
"""
support_resistance_report.py
 支撑阻力定时报告。

职责：
1. 复用 ICT 模块计算 4 类支撑阻力（前日高低 / 前周高低 / Swing 波段高低 / 亚洲伦敦盘高低）
2. 每小时第 50 分钟推送一次飞书
3. 跳过北京时间 0-7 点 与 周末（周六周日）

部署：独立常驻进程，与 ICT_main.py 等脚本平级，单独启停、单独日志。
"""
import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta

# ====================== 复用 ICT 模块 ======================
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_BASE_DIR, "ICT"))

from data.binance_api import fetch_klines
from indicators.swing import find_swings
from levels.time_levels import (
    mark_asia_london_levels,
    mark_previous_daily_levels,
    mark_weekly_levels,
)

# ====================== 配置 ======================
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
SYMBOL = ""

PUSH_MINUTE = 50        # 每小时第 50 分钟推送（如 12:50、13:50）
SCAN_INTERVAL = 20      # 扫描间隔（秒）
SKIP_HOUR_BEFORE = 7    # 北京时间 0-7 点不推送（hour < 7）

CST = timezone(timedelta(hours=8))  # 北京时间

# 价位去重阈值（美元）：两个来源价位差小于此值视为同一位，只保留优先级更高的来源
DEDUP_TOLERANCE = 1.0

# 来源优先级（数字越小越优先，去重时保留优先级高的）
_SOURCE_PRIORITY = {
    "前日": 0,
    "前周": 1,
    "Swing": 2,
    "亚洲盘": 3,
    "伦敦盘": 4,
}


# ====================== 数据采集 ======================
def _collect_levels():
    """拉取 K 线并汇总 4 类支撑阻力，返回 (res_list, sup_list, current_price)。

    res_list / sup_list 元素为 (name, price, source)，price 保留 2 位小数。
    """
    candles_15m = fetch_klines("15m")
    candles_1h = fetch_klines("1h")
    candles_4h = fetch_klines("4h")
    candles_1d = fetch_klines("1d")

    if not candles_15m or not candles_1d:
        return None, None, None

    current_price = candles_15m[-1].close

    # 统一收集所有价位（先不分阻力/支撑），元素 (name, price, source)
    levels = []

    # 1. 前日高低点
    for lv in mark_previous_daily_levels(candles_1d):
        levels.append((lv.name, round(lv.price, 2), "前日"))

    # 2. 前周高低点（mark_weekly_levels 返回当周+前周，仅保留前周）
    for lv in mark_weekly_levels(candles_1d):
        if "前周" in lv.name:
            levels.append((lv.name, round(lv.price, 2), "前周"))

    # 3. 亚洲盘 / 伦敦盘高低（只取最近 24 小时 = 96 根 15m，避免混入多天数据）
    for lv in mark_asia_london_levels(candles_15m[-96:]):
        src = "亚洲盘" if "亚洲" in lv.name else "伦敦盘"
        levels.append((lv.name, round(lv.price, 2), src))

    # 4. Swing 波段高低点（1h / 4h / 1d 各取最近一个 swing high 和 swing low）
    for tf, candles in (("1h", candles_1h), ("4h", candles_4h), ("1d", candles_1d)):
        if not candles:
            continue
        swings = find_swings(candles)
        swing_highs = [s for s in swings if s.is_high]
        swing_lows = [s for s in swings if not s.is_high]
        if swing_highs:
            levels.append((f"{tf} Swing高", round(swing_highs[-1].price, 2), "Swing"))
        if swing_lows:
            levels.append((f"{tf} Swing低", round(swing_lows[-1].price, 2), "Swing"))

    # 去重
    levels = _dedup(levels)

    # 按相对当前价分组：上方=阻力，下方=支撑
    resistances = [lv for lv in levels if lv[1] > current_price]
    supports = [lv for lv in levels if lv[1] < current_price]
    # 阻力从近到远（升序），支撑从近到远（降序）
    resistances.sort(key=lambda x: x[1])
    supports.sort(key=lambda x: -x[1])

    return resistances, supports, current_price


def _dedup(items):
    """价位去重：价格差 < DEDUP_TOLERANCE 视为同一位，保留来源优先级更高的。"""
    out = []
    for name, price, source in items:
        dup_idx = None
        for i, (n2, p2, s2) in enumerate(out):
            if abs(price - p2) < DEDUP_TOLERANCE:
                dup_idx = i
                break
        if dup_idx is None:
            out.append((name, price, source))
        else:
            # 保留优先级更高的来源
            old = out[dup_idx]
            if _SOURCE_PRIORITY.get(source, 9) < _SOURCE_PRIORITY.get(old[2], 9):
                out[dup_idx] = (name, price, source)
    return out


# ====================== 报告文本 ======================
def _build_text(resistances, supports, current_price, now_cst):
    lines = [f"📐 {SYMBOL} 支撑阻力报告 · {now_cst.strftime('%Y-%m-%d %H:%M')}", ""]

    lines.append("【阻力】上方")
    if resistances:
        for name, price, _ in resistances:
            dist = price - current_price
            lines.append(f"• {name}  {price:.2f}  (+{dist:.2f})")
    else:
        lines.append("• 无")
    lines.append("")

    lines.append("【支撑】下方")
    if supports:
        for name, price, _ in supports:
            dist = price - current_price
            lines.append(f"• {name}  {price:.2f}  ({dist:.2f})")
    else:
        lines.append("• 无")
    lines.append("")

    lines.append(f"当前价 {current_price:.2f}")
    return "\n".join(lines)


# ====================== 推送 ======================
def _push(text):
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=30)
        now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        if resp.status_code != 200:
            print(f"[{now}] ❌ 支撑阻力报告推送失败 HTTP {resp.status_code}: {resp.text[:200]}")
            return False
        try:
            body = resp.json()
        except Exception:
            print(f"[{now}] ❌ 支撑阻力报告推送失败：响应非 JSON: {resp.text[:200]}")
            return False
        if not isinstance(body, dict) or body.get("code") != 0:
            print(f"[{now}] ❌ 支撑阻力报告推送失败 业务码 {body.get('code') if isinstance(body, dict) else '?'}（HTTP 200 但业务未成功）: {str(body)[:200]}")
            return False
        print(f"[{now}] ✅ 支撑阻力报告推送成功")
        return True
    except Exception as e:
        now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] ❌ 支撑阻力报告推送异常: {e}")
    return False


# ====================== 推送时机判断 ======================
def _should_push(now_cst, last_pushed_hour):
    """判断是否该推送：非周末、非 0-7 点、分钟 == 50、且本小时未推过。"""
    if now_cst.weekday() >= 5:  # 周六(5)、周日(6)
        return False
    if now_cst.hour < SKIP_HOUR_BEFORE:  # 0-7 点
        return False
    if now_cst.minute != PUSH_MINUTE:
        return False
    if last_pushed_hour == now_cst.hour:
        return False
    return True


# ====================== 主循环 ======================
def main():
    print("=" * 60)
    print(" 支撑阻力定时报告进程启动")
    print(f"推送: 每小时第 {PUSH_MINUTE} 分钟（北京时间）")
    print(f"过滤: 跳过 0-{SKIP_HOUR_BEFORE} 点、周末")
    print("=" * 60)

    last_pushed_hour = None

    while True:
        try:
            now_cst = datetime.now(CST)
            if _should_push(now_cst, last_pushed_hour):
                resistances, supports, current_price = _collect_levels()
                if resistances is not None and supports is not None:
                    text = _build_text(resistances, supports, current_price, now_cst)
                    if _push(text):
                        last_pushed_hour = now_cst.hour
        except Exception as e:
            import traceback
            traceback.print_exc()

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
