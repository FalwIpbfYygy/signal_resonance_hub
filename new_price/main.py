# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 主循环引擎

流水线(10s 轮询 → 仅新收盘K线触发):
  数据 fetch → 增量缓存 → 市场状态 → 信号棒 → 数推计数 → 过滤 → 开仓
  → 风控(仓位建议) → 去重 → 飞书异步推送

+ 30min 生存日志(最新收盘K线/价格/市场状态)

用法:
  python main.py           # 常驻运行
  python main.py --once    # 单次扫描(每周期各拉一次, 便于验证)
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store

import config
import data
import indicators as ind
import market_state as ms
import signal_bars as sb
import pullback_count as pc
import filter as fl
import entry as en
import risk as rk
import dedup as dd
import notifier as nt

BJT = timezone(timedelta(hours=8))

_TREND_STATES = ("trend_up", "trend_down", "broad_up", "broad_down")


def _fmt_ts(ms_val: int) -> str:
    return datetime.fromtimestamp(ms_val / 1000.0, tz=BJT).strftime("%m-%d %H:%M")


class TradingEngine:
    """把全部模块串成一条流水线。"""

    def __init__(self):
        self.cache = data.KlineCache()
        self.judge = ms.MarketStateJudge()
        self.detector = sb.SignalBarDetector()
        self.filter_ = fl.SignalFilter()
        self.entry = en.EntryEngine()
        self.risk = rk.RiskManager()
        self.dedup = dd.SignalDedup()
        self.notifier = nt.Notifier()
        self._last_close = {}   # interval -> 已处理的最新收盘K线 close_time

    # ---------- 单周期扫描 ----------

    def scan(self, interval: str):
        raw = data.fetch_klines(interval)
        if not raw:
            return
        self.cache.update(interval, raw)
        arr = self.cache.get_arrays(interval)
        if not arr or len(arr["closes"]) < config.EMA_PERIOD + 5:
            return

        last_close = arr["last_close_time"]
        # 仅新收盘K线触发重算(PRD §9.4 未收盘拦截)
        if self._last_close.get(interval) == last_close:
            return
        self._last_close[interval] = last_close

        bars = sb.build_bars(arr)
        avg_entity = ind.calc_avg_entity(arr["opens"], arr["closes"],
                                         config.AVG_ENTITY_PERIOD)
        atr = ind.calc_atr(arr["highs"], arr["lows"], arr["closes"], config.ATR_PERIOD)
        if atr <= 0 or avg_entity <= 0:
            return

        mstate = self.judge.judge(arr["opens"], arr["highs"], arr["lows"],
                                  arr["closes"])
        high_levels, low_levels = pc.count_pullback_levels(bars)

        idx = len(bars) - 1          # 最新已收盘棒
        for s in self.detector.detect_all(bars, idx, avg_entity, atr):
            plan = self._route(s, mstate, bars, high_levels, low_levels, interval)
            if plan:
                self._emit(plan, s, bars, interval)

    # ---------- 路由(趋势顺势 / 末段旗形 / 区间fade) ----------

    def _route(self, s: sb.SignalBar, mstate: ms.MarketStateResult,
               bars, high_levels, low_levels, interval: str):
        if s.direction not in ("long", "short"):
            return None

        # 信号棒质量门槛(全路径统一): 只保留 strong/strongest, 禁止 general(PRD 决策④)
        if (not s.is_tradeable) or (s.grade not in config.SIGNAL_GRADES_ALLOWED):
            return None

        state = mstate.state

        # 紧凑区间/数据不足 → 不产信号
        if state in ("tight_range", "unknown"):
            return None

        # 趋势态
        if state in _TREND_STATES:
            want_long = mstate.direction > 0
            is_long = s.direction == "long"
            if want_long == is_long:
                # 顺势: 过滤 → 开仓
                res = self.filter_.evaluate(s, mstate, bars, high_levels, low_levels)
                if not res.passed:
                    return None
                level = high_levels[s.index] if is_long else low_levels[s.index]
                return self.entry.build_trend_plan(s, mstate, level, res.grade, interval)
            # 逆势反转棒 → 末段旗形(内部判定触磁)
            return self.entry.build_final_flag_plan(s, mstate, s.grade, interval)

        # 区间态 → 看衰突破 fade(默认关闭: 黄金趋势品种, 区间均值回归无edge)
        if state == "range" and config.FADE_ENABLED:
            return self.entry.build_fade_plan(s, mstate, s.grade, interval)

        return None

    # ---------- 去重 → 风控 → 推送 ----------

    def _emit(self, plan: en.TradePlan, s: sb.SignalBar, bars, interval: str):
        bar_id = bars[s.index].close_time
        ok, reason = self.dedup.should_send(interval, plan.direction, s.pattern,
                                            s.index, bar_id, plan.grade)
        if not ok:
            return                       # 冷却/重复, 静默丢弃(不刷屏)
        advice = self.risk.suggest_position(plan)
        self.notifier.send_signal(plan, bar_id, advice)
        self.dedup.mark_sent(interval, plan.direction, s.pattern,
                             s.index, bar_id, plan.grade)
        # 统一入库（供信号共振统计）：信号时间 = 信号棒 close_time，与实盘推送同一口径
        try:
            signal_store.record_signal(
                signal_time=bar_id, symbol=config.SYMBOL, interval=plan.interval,
                pattern=plan.setup_type, direction=plan.direction,
                entry=plan.entry_price, sl=plan.stop_price,
                tp1=plan.tp1_price, tp2=plan.tp2_price,
            )
        except Exception as e:
            print(f"[入库异常] {plan.interval} {plan.setup_type}: {e}")
        self._log_emit(plan, bar_id, advice)

    @staticmethod
    def _log_emit(plan, bar_id, advice):
        d = "多" if plan.direction == "long" else "空"
        print(f"[信号] {plan.interval} {plan.setup_type}|{d}|{plan.grade} "
              f"入场={plan.entry_price} 止损={plan.stop_price} "
              f"TP1={plan.tp1_price} TP2={plan.tp2_price} 风险{advice.risk_pct}%")

    # ---------- 生存日志 ----------

    def alive_log(self):
        parts = []
        for interval in config.INTERVALS:
            arr = self.cache.get_arrays(interval)
            if not arr or len(arr["closes"]) < config.EMA_PERIOD + 5:
                parts.append(f"{interval}:未就绪")
                continue
            last_close = arr["last_close_time"]
            price = arr["closes"][-1]
            mstate = self.judge.judge(arr["opens"], arr["highs"],
                                      arr["lows"], arr["closes"])
            parts.append(f"{interval}:{_fmt_ts(last_close)} C={price:.2f} "
                         f"[{self.judge.describe(mstate)}]")
        now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[ALIVE {now}] " + " | ".join(parts))

    # ---------- 主循环 ----------

    def run(self, once: bool = False):
        print("=" * 60)
        print(f" 价格行为交易系统 v2  {config.SYMBOL}")
        print(f"主周期={config.PRIMARY_INTERVAL}  扫描={config.SCAN_INTERVAL_SECONDS}s  "
              f"生存日志={config.ALIVE_LOG_SEC}s")
        print("=" * 60)

        if once:
            for interval in config.INTERVALS:
                self.scan(interval)
            self.alive_log()
            return

        # 首轮预热(加载缓存, 不推送首棒历史信号)
        for interval in config.INTERVALS:
            raw = data.fetch_klines(interval)
            if raw:
                self.cache.update(interval, raw)
                arr = self.cache.get_arrays(interval)
                if arr:
                    self._last_close[interval] = arr["last_close_time"]
        self.alive_log()

        last_scan = -1
        last_alive = time.time()
        while True:
            now = datetime.now()
            if now.second % config.SCAN_INTERVAL_SECONDS == 0 \
                    and now.second != last_scan:
                last_scan = now.second
                for interval in config.INTERVALS:
                    self.scan(interval)

            if time.time() - last_alive >= config.ALIVE_LOG_SEC:
                last_alive = time.time()
                self.alive_log()

            time.sleep(config.LOOP_SLEEP)


def main():
    once = "--once" in sys.argv
    TradingEngine().run(once=once)


if __name__ == "__main__":
    main()