# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 主程序

基于Al Brooks《趋势篇》《区间篇》《反转篇》三书体系
实时监控多周期价格行为信号

系统流程:
1. 每10秒扫描5m/15m/30m/1h/4h五个周期
2. 判断市场状态(趋势/TR/TTR)
3. 识别信号棒形态(反转棒/双棒反转/内包棒等)
4. 回撤计数(H1/L1/H2/L2/H3/L3) + 反转架构(MTR/高潮/楔形/最终旗形/失败的失败)
5. 过滤规则 + 风控检查 + 信号去重
6. 飞书实时推送
"""
import time
import os
import sys
import requests
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store

# 导入config会自动设置代理环境变量
import config
from bar_patterns import BarData, TradingSignal
from market_state import MarketStateJudge, MarketStateResult
from pullback_counter import PullbackCounter
from reversal_patterns import ReversalPatternDetector
from filters import SignalFilter
from risk_manager import RiskManager
from signal_dedup import SignalDeduplicator
from feishu_notifier import FeishuNotifier
from qq_email_notifier import QqEmailNotifier


# ====================== 数据获取 ======================

def get_klines_retry(interval: str, limit: int = 1000, retries: int = 3) -> Optional[list]:
    """获取K线数据(带重试)

    Args:
        interval: K线周期(5m/15m/30m/1h/4h)
        limit: 获取数量
        retries: 重试次数

    Returns:
        原始K线数据列表, 失败返回None
    """
    url = f"{config.KLINE_URL}?symbol={config.SYMBOL}&interval={interval}&limit={limit}"

    for attempt in range(1, retries + 1):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            print(f"[{now}] [{interval}] [尝试 {attempt}/{retries}] 正在请求K线数据...")
            resp = requests.get(url, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                print(f"[{now}] [{interval}] [尝试 {attempt}] 请求成功, 获取{len(data)}根K线")
                return data
            else:
                print(f"[{now}] [{interval}] [尝试 {attempt}] 请求失败，状态码：{resp.status_code}")
        except Exception as e:
            print(f"[{now}] [{interval}] [尝试 {attempt}] 请求异常：{str(e)}，1秒后重试...")
        time.sleep(1)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [{interval}] 全部 {retries} 次重试均失败")
    return None


def parse_klines(raw_klines: list) -> List[BarData]:
    """解析币安K线数据为BarData列表

    币安返回格式: [open_time, open, high, low, close, volume, close_time, quote_volume, trade_count, taker_buy_volume, taker_buy_quote, ignore]
    """
    bars = []
    for k in raw_klines:
        bar = BarData(
            index=len(bars),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
            taker_buy=float(k[9]) if len(k) > 9 else 0.0,
            open_time=int(k[0]),
            close_time=int(k[6]),
        )
        bars.append(bar)
    return bars


# ====================== 系统核心 ======================

class TradingSystem:
    """Al Brooks价格行为交易系统"""

    def __init__(self):
        """初始化所有模块"""
        self.state_judge = MarketStateJudge()
        self.pullback_counter = PullbackCounter()
        self.reversal_detector = ReversalPatternDetector()
        self.signal_filter = SignalFilter()
        self.risk_manager = RiskManager(account_balance=10000.0)
        self.deduplicator = SignalDeduplicator()
        self.notifier = FeishuNotifier()
        self.email_notifier = QqEmailNotifier()

        # 缓存: 每个周期的市场状态
        self.market_states: Dict[str, MarketStateResult] = {}

        # 每个周期最后处理的K线收盘时间
        self.last_close_time: Dict[str, int] = {interval: 0 for interval in config.INTERVALS}

        # 启动时间
        self.start_time = datetime.now()

        print("=" * 60)
        print("Al Brooks 价格行为交易系统 启动")
        print(f"交易标的: {config.SYMBOL}")
        print(f"监控周期: {', '.join(config.INTERVALS)}")
        print(f"扫描间隔: {config.SCAN_INTERVAL_SECONDS}秒")
        print(f"启动时间: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

    def scan_interval(self, interval: str) -> List[TradingSignal]:
        """扫描单个周期

        Returns:
            该周期检测到的交易信号列表
        """
        # 1. 获取K线数据
        raw_klines = get_klines_retry(interval, config.KLINE_LIMIT, config.REQUEST_RETRIES)
        if raw_klines is None or len(raw_klines) < 50:
            print(f"[{interval}] K线数据不足, 跳过")
            return []

        # 2. 解析K线
        bars = parse_klines(raw_klines)

        # 3. 检查是否有新K线收盘
        latest_bar = bars[-1]
        current_time_ms = int(time.time() * 1000)

        # 最新K线的收盘时间
        latest_close_time = latest_bar.close_time

        # 如果最新K线未收盘, 使用倒数第二根(已收盘)
        if current_time_ms < latest_close_time:
            # 最新K线还在形成中, 检查倒数第二根是否是新收盘的
            if len(bars) < 2:
                return []
            closed_bar = bars[-2]
            if closed_bar.close_time <= self.last_close_time[interval]:
                return []  # 没有新收盘的K线
            # 使用到倒数第二根(已收盘)的数据进行分析
            bars = bars[:-1]
        else:
            # 最新K线已收盘
            if latest_close_time <= self.last_close_time[interval]:
                return []  # 已经处理过这根K线

        # 更新最后处理的收盘时间
        self.last_close_time[interval] = bars[-1].close_time

        # 4. 提取数据数组
        opens = [b.open for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        volumes = [b.volume for b in bars]
        taker_buys = [b.taker_buy for b in bars]

        # 5. 判断市场状态
        market_state = self.state_judge.judge(opens, highs, lows, closes, volumes, taker_buys)
        self.market_states[interval] = market_state

        # 输出详细的市场结构分析报告
        detail_report = self.state_judge.get_detail_report(market_state, closes)
        print(f"[{interval}]")
        print(detail_report)

        # TTR状态不交易
        if market_state.state == "ttr":
            print(f"[{interval}] 紧凑交易区间, 跳过信号检测")
            return []

        # 6. 信号检测
        signals = []

        # 回撤计数系统(H1/L1/H2/L2/H3/L3 + 二次入场)
        pullback_signals = self.pullback_counter.analyze(bars, market_state, interval)
        if pullback_signals:
            print(f"[{interval}] 回撤计数: 检测到 {len(pullback_signals)} 个信号")
            for s in pullback_signals:
                dir_cn = "多" if s.direction == "long" else "空"
                print(f"    → {s.setup_type} {dir_cn} @ ${s.entry_price:.2f} (强度:{s.grade})")
        else:
            # 输出回撤计数的分析过程(即使无信号)
            self._log_pullback_analysis(interval, bars, market_state)

        signals.extend(pullback_signals)

        # 反转架构(MTR/高潮/楔形/最终旗形/失败的失败)
        reversal_signals = self.reversal_detector.analyze(bars, market_state, interval)
        if reversal_signals:
            print(f"[{interval}] 反转架构: 检测到 {len(reversal_signals)} 个信号")
            for s in reversal_signals:
                dir_cn = "多" if s.direction == "long" else "空"
                print(f"    → {s.setup_type} {dir_cn} @ ${s.entry_price:.2f} (强度:{s.grade})")
        else:
            self._log_reversal_analysis(interval, bars, market_state)

        signals.extend(reversal_signals)

        if signals:
            print(f"[{interval}] ✅ 共检测到 {len(signals)} 个原始信号")
        else:
            print(f"[{interval}] 无合格信号")

        return signals

    def _log_pullback_analysis(self, interval: str, bars: List[BarData],
                                market_state: MarketStateResult):
        """输出回撤计数分析过程(无信号时)"""
        state = market_state.state

        if state == "trend_up":
            start = self.pullback_counter._find_pullback_start_bull(bars)
            counts = self.pullback_counter._count_highs(bars, start)
            if counts:
                latest = counts[-1]
                print(f"[{interval}] 回撤计数: H{latest.count_type[-1]} "
                      f"(无合格信号棒或未通过过滤)")
            else:
                print(f"[{interval}] 回撤计数: 无H计数(未出现回撤)")
        elif state == "trend_down":
            start = self.pullback_counter._find_pullback_start_bear(bars)
            counts = self.pullback_counter._count_lows(bars, start)
            if counts:
                latest = counts[-1]
                print(f"[{interval}] 回撤计数: L{latest.count_type[-1]} "
                      f"(无合格信号棒或未通过过滤)")
            else:
                print(f"[{interval}] 回撤计数: 无L计数(未出现回撤)")
        elif state == "tr":
            current_price = bars[-1].close
            range_mid = (market_state.range_high + market_state.range_low) / 2.0
            pos = "上半部" if current_price > range_mid else "下半部"
            print(f"[{interval}] 区间交易: 价格在区间{pos} "
                  f"(无合格反转信号)")

    def _log_reversal_analysis(self, interval: str, bars: List[BarData],
                                market_state: MarketStateResult):
        """输出反转架构分析过程(无信号时)"""
        checks = []

        # MTR条件检查
        if market_state.trend_bars >= config.MTR_TREND_MIN_BARS:
            checks.append(f"MTR条件1✓(趋势{market_state.trend_bars}棒)")
        else:
            checks.append(f"MTR条件1✗(趋势仅{market_state.trend_bars}棒<{config.MTR_TREND_MIN_BARS})")

        # 高潮检查
        avg_entity = market_state.avg_entity
        if avg_entity > 0:
            recent_exhaustion = False
            for i in range(max(0, len(bars) - 5), len(bars)):
                if self.reversal_detector.detector.is_exhaustion_bar(bars[i], avg_entity):
                    recent_exhaustion = True
                    break
            checks.append(f"高潮: {'⚠️检测到耗尽棒' if recent_exhaustion else '无'}")

        # 最终旗形检查
        if market_state.trend_bars >= config.FINAL_FLAG_MIN_TREND_BARS:
            checks.append(f"最终旗形: 趋势≥{config.FINAL_FLAG_MIN_TREND_BARS}棒(监控中)")
        else:
            checks.append(f"最终旗形: 趋势不足")

        print(f"[{interval}] 反转检查: {' | '.join(checks)}")

    def process_signals(self, all_signals: List[TradingSignal]):
        """处理所有信号(过滤→风控→去重→推送)"""
        # 获取30m市场状态(用于趋势方向过滤)
        ms_30m = self.market_states.get("30m")

        # 同周期同K线的信号按优先级排序,只保留最高优先级
        filtered_signals = []

        for signal in all_signals:
            market_state = self.market_states.get(signal.interval)
            if market_state is None:
                continue

            dir_cn = "多" if signal.direction == "long" else "空"
            tag = f"[{signal.interval}] {signal.setup_type} {dir_cn} @ ${signal.entry_price:.2f}"

            # 1. 过滤(带详细原因)
            filtered, fail_reason = self.signal_filter.filter_signal_with_reason(
                signal, market_state, ms_30m, self.market_states
            )
            if filtered is None:
                print(f"  ✗ {tag} 被过滤: {fail_reason}")
                continue

            # 2. 去重(检查是否是低优先级重复)
            if self.deduplicator.is_lower_priority_duplicate(filtered, filtered_signals):
                print(f"  ✗ {tag} 被去重: 低优先级重复")
                continue

            # 3. 去重(检查已通知记录)
            if self.deduplicator.is_duplicate(filtered, market_state.atr):
                print(f"  ✗ {tag} 被去重: 已通知过")
                continue

            # 4. 风控检查
            if not self.risk_manager.can_open_position(filtered):
                print(f"  ✗ {tag} 被风控拦截")
                continue

            print(f"  ✓ {tag} 通过全部检查")
            filtered_signals.append(filtered)

        # 推送通过所有检查的信号
        for signal in filtered_signals:
            market_state = self.market_states.get(signal.interval)

            # 计算仓位
            position_size = self.risk_manager.calc_position_size(signal)

            # 获取过滤描述
            filter_desc = self.signal_filter.get_filter_description(signal, market_state)

            # 统一入库（信号时间 / 品种 / 周期 / 形态 / 方向 / 入场 / 止损 / TP1 / TP2）
            signal_store.record_signal(
                signal_time=signal.timestamp,
                symbol=config.SYMBOL,
                interval=signal.interval,
                pattern=signal.setup_type,
                direction=signal.direction,
                entry=signal.entry_price,
                sl=signal.stop_price,
                tp1=signal.tp1_price,
                tp2=signal.tp2_price,
            )

            # 推送(飞书)
            self.notifier.send_signal(signal, market_state, filter_desc, position_size)

            # 推送(QQ邮箱)
            if config.QQ_EMAIL_ENABLED:
                self.email_notifier.send_signal(signal, market_state, filter_desc, position_size)

            # 记录到去重器
            self.deduplicator.add_signal(signal)

            # 日志
            setup_cn = {
                "second_entry": "二次入场", "failed_failure": "失败的失败",
                "h2_trend": "H2买进", "l2_trend": "L2卖出",
                "h1_strong": "H1买进", "l1_strong": "L1卖出",
                "mtr_conservative": "MTR保守", "mtr_aggressive": "MTR激进",
                "wedge_flag": "楔形旗形", "tr_reversal": "区间反转",
                "climax_reversal": "高潮反转", "final_flag": "最终旗形",
            }.get(signal.setup_type, signal.setup_type)

            direction_cn = "做多" if signal.direction == "long" else "做空"
            print(f"[{signal.interval}] ✅ 推送信号: {setup_cn} {direction_cn} "
                  f"@ ${signal.entry_price:.2f} SL:${signal.stop_price:.2f} "
                  f"TP1:${signal.tp1_price:.2f} TP2:${signal.tp2_price:.2f}")

        # 清理过期的去重记录
        current_prices = {}
        atrs = {}
        for interval, ms in self.market_states.items():
            if ms.ema20 > 0:
                current_prices[interval] = ms.ema20
                atrs[interval] = ms.atr
        self.deduplicator.cleanup(current_prices, atrs)

    def run(self):
        """主循环"""
        last_scan_second = -1

        while True:
            now = datetime.now()
            current_second = now.second

            # 每10秒扫描一次
            if current_second % config.SCAN_INTERVAL_SECONDS == 0 and current_second != last_scan_second:
                last_scan_second = current_second

                scan_time = now.strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n{'─' * 60}")
                print(f"[{scan_time}] 开始扫描...")
                print(f"{'─' * 60}")

                # 扫描所有周期
                all_signals = []
                for interval in config.INTERVALS:
                    signals = self.scan_interval(interval)
                    all_signals.extend(signals)

                # 处理信号
                if all_signals:
                    print(f"\n共检测到 {len(all_signals)} 个原始信号, 开始过滤...")
                    self.process_signals(all_signals)
                else:
                    print("\n无信号")

                # 风控状态
                print(f"\n风控: {self.risk_manager.get_risk_summary()}")
                print(f"去重: {self.deduplicator.get_summary()}")

            time.sleep(config.LOOP_SLEEP)


# ====================== 主程序入口 ======================

if __name__ == "__main__":
    try:
        system = TradingSystem()
        system.run()
    except KeyboardInterrupt:
        print("\n系统手动停止")
    except Exception as e:
        print(f"\n系统异常: {str(e)}")
        import traceback
        traceback.print_exc()
