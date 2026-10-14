"""信号检测引擎 - 核心逻辑

流程：
1. 级别触及检测
2. 确认信号检查（CSD + 反转FVG + 流动性清扫）
3. 信号优先级排序
4. 信号去重
"""
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass

from data.candle import Candle, KeyLevel, FVG, CSDSignal
from data.dxy import fetch_dxy_series
from indicators.csd import get_latest_csd, has_bearish_csd, has_bullish_csd
from indicators.fvg import find_fvgs, check_fvg_inversion
from indicators.swing import find_swings
from indicators.atr import detect_sfp, detect_liquidity_sweep
from indicators.smt import detect_smt
from utils.time_utils import get_session_gmt, get_min_confirmations
import config


@dataclass
class TradeSignal:
    """交易信号"""
    direction: str           # "BUY" 或 "SELL"
    level: KeyLevel          # 触发级别
    signal_type: str         # 信号类型描述
    strength: int            # 信号强度 1-5
    confirmations: List[str] # 确认信号列表
    entry_price: float       # 入场价格
    stop_loss: float         # 止损价格
    take_profit: float       # 止盈目标
    session: str             # 交易时段
    timestamp: datetime      # 信号时间


class SignalEngine:
    """ICT信号检测引擎"""

    def __init__(self):
        # 上一轮信号键，用于去重
        self._last_signal_keys: set = set()

    def scan(self, data: Dict[str, List[Candle]],
             levels: List[KeyLevel]) -> List[TradeSignal]:
        """
        扫描所有关键级别，检测信号

        返回: TradeSignal列表
        """
        signals = []
        if "1m" not in data or "15m" not in data:
            return signals

        candles_1m = data["1m"]
        candles_15m = data["15m"]
        current_price = candles_1m[-1].close
        current_time = candles_1m[-1].close_time

        # 计算1分钟图指标
        swings_1m = find_swings(candles_1m)
        fvgs_1m = find_fvgs(candles_1m, swings_1m)
        latest_csd = get_latest_csd(candles_1m)
        sfp_result = detect_sfp(candles_1m, swings_1m)

        # 获取当前时段
        session_name, session_weight = get_session_gmt(current_time)
        min_confirm = get_min_confirmations(session_weight)

        # ---- SMT 背离（黄金 vs 合成 DXY，负相关）----
        smt_dir = None
        if config.SMT_ENABLED:
            try:
                dxy_series = fetch_dxy_series()
                smt_dir = detect_smt(candles_1m, dxy_series)
            except Exception:
                smt_dir = None  # DXY 拉取失败不阻断主流程

        # ---- 市场结构偏向（Market Bias）：解决 BUY/SELL 同时出现的问题 ----
        # 用1h图swing结构判定：HH+HL=做多偏向，LH+LL=做空偏向，中性=不交易
        bias = self._get_market_bias(data)

        # 遍历所有级别
        for level in levels:
            if level.status != "TOUCHED":
                continue

            # 检查级别是否在合理范围内
            dist_pct = abs(current_price - level.price) / level.price * 100
            if dist_pct > 0.15:  # 偏离超过0.15%不触发
                continue

            # ---- 空头信号检测（仅当偏向为中性或做空时） ----
            if not level.is_support and bias != "LONG":  # 阻力位 + 非做多偏向
                signal = self._check_sell_signal(
                    level, candles_1m, fvgs_1m, latest_csd,
                    sfp_result, current_price, min_confirm,
                    session_name, current_time, smt_dir
                )
                if signal:
                    signals.append(signal)

            # ---- 多头信号检测（仅当偏向为中性或做多时） ----
            if level.is_support and bias != "SHORT":  # 支撑位 + 非做空偏向
                signal = self._check_buy_signal(
                    level, candles_1m, fvgs_1m, latest_csd,
                    sfp_result, current_price, min_confirm,
                    session_name, current_time, smt_dir
                )
                if signal:
                    signals.append(signal)

        # 价格加权排序（强度高的在前）
        signals.sort(key=lambda s: s.strength, reverse=True)

        return signals

    def _get_market_bias(self, data: Dict[str, List[Candle]]) -> str:
        """
        用1h图swing结构判定市场偏向（解决BUY/SELL矛盾）。
        
        双模型逻辑：
        - 牛市结构：HH（更高高点）+ HL（更高低点）→ LONG
        - 熊市结构：LH（更低高点）+ LL（更低低点）→ SHORT  
        - 混合/不明确：NEUTRAL → 两个方向都允许（但会触发全局间隔过滤）
        """
        if "1h" not in data or len(data["1h"]) < 20:
            return "NEUTRAL"

        candles_1h = data["1h"]
        swings_1h = find_swings(candles_1h)
        if len(swings_1h) < 4:
            return "NEUTRAL"

        # 提取最近4个摆动点分析结构
        recent = swings_1h[-6:]  # 取最近6个，确保有足够的high/low
        highs = [s for s in recent if s.is_high]
        lows = [s for s in recent if not s.is_high]

        if len(highs) < 2 or len(lows) < 2:
            return "NEUTRAL"

        # 比较最近两个高点、两个低点
        hh = highs[-1].price > highs[-2].price  # higher high?
        hl = lows[-1].price > lows[-2].price     # higher low?

        lh = highs[-1].price < highs[-2].price   # lower high?
        ll = lows[-1].price < lows[-2].price     # lower low?

        if hh and hl:
            return "LONG"
        elif lh and ll:
            return "SHORT"
        else:
            return "NEUTRAL"

    def _check_sell_signal(self, level: KeyLevel, candles_1m: List[Candle],
                           fvgs_1m: List[FVG], latest_csd: CSDSignal,
                           sfp_result: dict, current_price: float,
                           min_confirm: int, session: str,
                           current_time: datetime,
                           smt_dir: Optional[str] = None) -> Optional[TradeSignal]:
        """检测空头信号"""
        confirmations = []
        strength = 0

        # 1. 流动性清扫检查（扫过高点）
        sweep = detect_liquidity_sweep(candles_1m, "bearish", lookback=15)
        if sweep:
            confirmations.append("流动性清扫（扫过短期高点）")
            strength += 1

        # 2. CSD检查
        if latest_csd and latest_csd.direction == -1:
            confirmations.append("空头CSD（阴线吞噬阳线）")
            strength += 1
        elif has_bearish_csd(candles_1m, lookback=3):
            confirmations.append("空头CSD（近期阴线吞噬）")
            strength += 1

        # 3. 反转FVG检查
        inverted = check_fvg_inversion(fvgs_1m, candles_1m)
        bearish_inverted = [f for f in inverted if f.direction == 1]  # 多头FVG被反转
        if bearish_inverted:
            confirmations.append("反转FVG（多头FVG被反转）")
            strength += 1

        # 4. SFP检查
        if sfp_result.get("bearish_sfp"):
            confirmations.append(f"空头SFP（扫过高点未收盘突破）")
            strength += 1

        # 5. SMT 背离检查（黄金 HH 但美元未创新低 → 虚假突破，看跌）
        if smt_dir == "bearish":
            confirmations.append("SMT背离（黄金HH/美元未走弱）")
            strength += 1

        # 6. BSG加成
        for fvg in fvgs_1m[-3:]:
            if fvg.is_bsg and fvg.direction == -1:
                confirmations.append("BSG（突破结构缺口）")
                strength += 2  # BSG高权重
                break

        if len(confirmations) < min_confirm:
            return None

        # 计算入场/止损/止盈
        entry_price = current_price
        # 止损：级别上方最近Swing High + 缓冲
        swings_1m = find_swings(candles_1m)
        stop_loss = level.price
        for s in reversed(swings_1m):
            if s.is_high and s.price > level.price:
                stop_loss = s.price * 1.001  # 0.1%缓冲
                break
        if stop_loss == level.price:
            stop_loss = level.price * 1.003  # 默认0.3%缓冲

        # 止盈：最近的下方支撑级别
        # 简化：1.5倍风险回报
        risk = stop_loss - entry_price
        take_profit = entry_price - risk * 1.5

        return TradeSignal(
            direction="SELL",
            level=level,
            signal_type=self._build_signal_type(confirmations),
            strength=min(strength, 5),
            confirmations=confirmations,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            session=session,
            timestamp=current_time,
        )

    def _check_buy_signal(self, level: KeyLevel, candles_1m: List[Candle],
                          fvgs_1m: List[FVG], latest_csd: CSDSignal,
                          sfp_result: dict, current_price: float,
                          min_confirm: int, session: str,
                          current_time: datetime,
                          smt_dir: Optional[str] = None) -> Optional[TradeSignal]:
        """检测多头信号"""
        confirmations = []
        strength = 0

        # 1. 流动性清扫检查（扫过低点）
        sweep = detect_liquidity_sweep(candles_1m, "bullish", lookback=15)
        if sweep:
            confirmations.append("流动性清扫（扫过短期低点）")
            strength += 1

        # 2. CSD检查
        if latest_csd and latest_csd.direction == 1:
            confirmations.append("多头CSD（阳线吞噬阴线）")
            strength += 1
        elif has_bullish_csd(candles_1m, lookback=3):
            confirmations.append("多头CSD（近期阳线吞噬）")
            strength += 1

        # 3. 反转FVG检查
        inverted = check_fvg_inversion(fvgs_1m, candles_1m)
        bullish_inverted = [f for f in inverted if f.direction == -1]  # 空头FVG被反转
        if bullish_inverted:
            confirmations.append("反转FVG（空头FVG被反转）")
            strength += 1

        # 4. SFP检查
        if sfp_result.get("bullish_sfp"):
            confirmations.append(f"多头SFP（扫过低点未收盘突破）")
            strength += 1

        # 5. SMT 背离检查（黄金 LL 但美元未创新高 → 虚假突破，看涨）
        if smt_dir == "bullish":
            confirmations.append("SMT背离（黄金LL/美元未走强）")
            strength += 1

        # 6. BSG加成
        for fvg in fvgs_1m[-3:]:
            if fvg.is_bsg and fvg.direction == 1:
                confirmations.append("BSG（突破结构缺口）")
                strength += 2
                break

        if len(confirmations) < min_confirm:
            return None

        entry_price = current_price
        # 止损：级别下方最近Swing Low - 缓冲
        swings_1m = find_swings(candles_1m)
        stop_loss = level.price
        for s in reversed(swings_1m):
            if not s.is_high and s.price < level.price:
                stop_loss = s.price * 0.999
                break
        if stop_loss == level.price:
            stop_loss = level.price * 0.997

        risk = entry_price - stop_loss
        take_profit = entry_price + risk * 1.5

        return TradeSignal(
            direction="BUY",
            level=level,
            signal_type=self._build_signal_type(confirmations),
            strength=min(strength, 5),
            confirmations=confirmations,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            session=session,
            timestamp=current_time,
        )

    def _build_signal_type(self, confirmations: List[str]) -> str:
        """构建信号类型描述"""
        types = []
        if any("BSG" in c for c in confirmations):
            types.append("BSG")
        if any("反转FVG" in c for c in confirmations):
            types.append("反转FVG")
        if any("CSD" in c for c in confirmations):
            types.append("CSD")
        if any("SFP" in c for c in confirmations):
            types.append("SFP")
        return " + ".join(types) if types else "未分类"

    def dedup_key(self, signal: TradeSignal) -> str:
        """生成信号去重键（5分钟桶）"""
        bucket = signal.timestamp.replace(second=0, microsecond=0)
        bucket = bucket.replace(minute=(bucket.minute // 5) * 5)
        return f"{signal.level.name}_{signal.direction}_{bucket.strftime('%Y%m%d_%H%M')}"

    def is_duplicate(self, signal: TradeSignal) -> bool:
        """检查是否为重复信号"""
        key = self.dedup_key(signal)
        if key in self._last_signal_keys:
            return True
        self._last_signal_keys.add(key)
        # 清理过期的键（保留最近50个）
        if len(self._last_signal_keys) > 50:
            self._last_signal_keys = set(list(self._last_signal_keys)[-50:])
        return False