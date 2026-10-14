"""
ICT  交易策略 - 实时监控主程序
基于ICT全合一策略（All-in-One Strategy）
每10秒轮询Binance K线数据，检测信号并推送飞书
"""
import time
import os
import sys
from datetime import datetime

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store

from config import MIN_PUSH_STRENGTH
from data.binance_api import fetch_all_timeframes
from data.candle import Candle
from levels.time_levels import get_all_levels
from levels.level_manager import LevelManager
from signals.engine import SignalEngine
from notify.feishu import push_signals
from utils.time_utils import get_session_gmt, get_session_display, format_cst

# ---- 全局去重状态 ----
# 信号引擎（全局单例，保持去重状态跨扫描）
_engine = SignalEngine()
# 级别冷却追踪：{(level_name, direction): last_signal_timestamp}
_level_cooldown = {}
# 冷却时间：同一级别+方向，30分钟内不重复推送
LEVEL_COOLDOWN_SEC = 1800
# 全局信号间隔：任意信号推送后，至少间隔N秒才允许推送下一个信号
_last_push_time = None
GLOBAL_SIGNAL_GAP_SEC = 30


def scan():
    """主扫描逻辑：拉取数据 → 计算指标 → 标记级别 → 检测信号 → 推送"""
    now = datetime.now()
    print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] ========== 开始扫描 ==========")

    # 1. 拉取所有时间框架K线数据
    data = fetch_all_timeframes()
    if not data or "1m" not in data or "15m" not in data:
        print("⚠️ 关键时间框架数据缺失，跳过本轮扫描")
        return

    current_price = data["1m"][-1].close
    print(f"当前价格: ${current_price:.2f}")

    # 2. 获取当前时段
    session_name, session_weight = get_session_gmt(data["1m"][-1].close_time)
    print(f"当前时段: {get_session_display(session_name)} (权重={session_weight})")

    # 3. 标记所有关键级别
    levels = get_all_levels(data)
    print(f"已标记 {len(levels)} 个关键级别")

    # 4. 更新级别状态
    level_mgr = LevelManager(levels)
    level_mgr.update(current_price)

    touched = level_mgr.get_touched()
    if touched:
        print(f"已触及级别: {len(touched)} 个")
        for lv in touched:
            print(f"  - {lv.name} @ ${lv.price:.2f}")
    else:
        print("已触及级别: 0 个")

    # 5. 信号检测
    raw_signals = _engine.scan(data, levels)
    bias = _engine._get_market_bias(data)
    bias_display = {"LONG": "📈 做多偏向", "SHORT": "📉 做空偏向", "NEUTRAL": "⚖ 中性"}
    print(f"市场偏向: {bias_display.get(bias, bias)}")

    # 5.5 强度门槛过滤：只保留 >= MIN_PUSH_STRENGTH 星的信号
    #     低星信号彻底丢弃（不入库/不推送/不占级别冷却/不占全局间隔）
    strong_signals = [s for s in raw_signals if s.strength >= MIN_PUSH_STRENGTH]
    weak_count = len(raw_signals) - len(strong_signals)
    if weak_count > 0:
        print(f"强度门槛：丢弃 {weak_count} 个低星信号（<{MIN_PUSH_STRENGTH}⭐）")
    if not strong_signals:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ========== 扫描完成 ==========")
        return

    # 6. 全局信号间隔检查（同一波行情，只推送第一批信号）
    global _last_push_time
    now_ts = datetime.now()
    if _last_push_time is not None:
        gap = (now_ts - _last_push_time).total_seconds()
        if gap < GLOBAL_SIGNAL_GAP_SEC:
            print(f"全局信号间隔中 ({gap:.0f}s/{GLOBAL_SIGNAL_GAP_SEC}s)，跳过本轮信号推送")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ========== 扫描完成 ==========")
            return

    # 7. 级别冷却过滤（核心去重：同一级别+方向30分钟内只推送一次）
    filtered_signals = []
    for s in strong_signals:
        key = (s.level.name, s.direction)
        last_ts = _level_cooldown.get(key)
        if last_ts is not None:
            elapsed = (now_ts - last_ts).total_seconds()
            if elapsed < LEVEL_COOLDOWN_SEC:
                continue  # 冷却中，跳过
        _level_cooldown[key] = now_ts
        filtered_signals.append(s)

    # 清理过期冷却记录（超过1小时未触发的清理掉）
    stale_keys = [k for k, v in _level_cooldown.items()
                  if (now_ts - v).total_seconds() > 3600]
    for k in stale_keys:
        del _level_cooldown[k]

    # 8. 推送信号
    if filtered_signals:
        _last_push_time = datetime.now()  # 记录推送时间，触发全局间隔
        skipped = len(strong_signals) - len(filtered_signals)
        print(f"\n🔔 检测到 {len(filtered_signals)} 个有效信号"
              + (f" (冷却拦截 {skipped} 个)" if skipped > 0 else ""))
        for s in filtered_signals:
            print(f"  [{s.direction}] {s.level.name} @ ${s.level.price:.2f} "
                  f"| 类型: {s.signal_type} | 强度: {s.strength}/5")
            # 统一入库（信号时间 / 品种 / 周期 / 形态 / 方向 / 入场 / 止损 / TP1 / TP2）
            signal_store.record_signal(
                signal_time=format_cst(s.timestamp),
                symbol="",
                interval="1m/15m",
                pattern=s.signal_type,
                direction=s.direction,
                entry=s.entry_price,
                sl=s.stop_loss,
                tp1=s.take_profit,  # ICT 仅单一止盈值，填入 TP1
                tp2=None,
            )
        push_signals(filtered_signals)
    else:
        skipped = len(strong_signals) - len(filtered_signals)
        if skipped > 0:
            print(f"无有效信号 (冷却拦截 {skipped} 个)")
        else:
            print("无有效信号")

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ========== 扫描完成 ==========")


if __name__ == "__main__":
    print("=" * 60)
    print("ICT  交易策略 实时监控系统")
    print(f"品种:  (Binance Futures)")
    print(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} CST")
    print("策略: 关键级别 + 确认信号（CSD/FVG/SFP/流动性清扫）")
    print("全时段监控 | 级别冷却去重(30分钟) | 时段感知权重")
    print("=" * 60)

    last_scan_second = -1

    while True:
        now = datetime.now()
        current_second = now.second

        # 每10秒检测一次
        if current_second % 10 == 0 and current_second != last_scan_second:
            try:
                scan()
            except Exception as e:
                print(f"❌ 扫描异常: {e}")
                import traceback
                traceback.print_exc()
            last_scan_second = current_second

        time.sleep(0.1)

