"""K线数据结构"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Candle:
    """标准化K线"""
    open_time: datetime      # 开盘时间
    close_time: datetime     # 收盘时间
    open: float
    high: float
    low: float
    close: float
    volume: float            # 成交量（基础资产）
    quote_volume: float      # 成交额
    trades: int              # 成交笔数
    taker_buy_volume: float  # 主动买入量

    # 计算属性
    body: float = 0.0        # |close - open|
    direction: int = 0       # 1=阳线, -1=阴线, 0=十字星
    upper_wick: float = 0.0  # 上影线
    lower_wick: float = 0.0  # 下影线
    is_bullish: bool = False
    is_bearish: bool = False

    def __post_init__(self):
        self.body = abs(self.close - self.open)
        if self.close > self.open:
            self.direction = 1
            self.is_bullish = True
            self.upper_wick = self.high - self.close
            self.lower_wick = self.open - self.low
        elif self.close < self.open:
            self.direction = -1
            self.is_bearish = True
            self.upper_wick = self.high - self.open
            self.lower_wick = self.close - self.low
        else:
            self.direction = 0
            self.upper_wick = self.high - self.close
            self.lower_wick = self.open - self.low


@dataclass
class SwingPoint:
    """摆动点"""
    index: int           # 在K线列表中的索引
    price: float         # 价格
    time: datetime       # 时间
    is_high: bool        # True=高点, False=低点
    candle: Candle       # 对应的K线


@dataclass
class FVG:
    """公允价值缺口"""
    index: int           # FVG生成K线的索引
    time: datetime       # 生成时间
    direction: int       # 1=多头FVG, -1=空头FVG
    upper: float         # 上边界
    lower: float         # 下边界
    size: float          # 缺口大小
    is_bsg: bool = False # 是否突破结构缺口
    is_inverted: bool = False  # 是否已被反转
    inflection_point: Optional[float] = None  # 拐点价格


@dataclass
class CSDSignal:
    """交付状态改变信号"""
    index: int
    time: datetime
    direction: int  # 1=多头CSD, -1=空头CSD


@dataclass
class OrderBlock:
    """订单块"""
    index: int
    time: datetime
    direction: int  # 1=多头OB, -1=空头OB
    upper: float    # OB上边界
    lower: float    # OB下边界


@dataclass
class KeyLevel:
    """关键级别"""
    name: str              # 级别名称
    price: float           # 价格
    level_type: str        # "high" 或 "low"
    timeframe: str         # 来源时间框架
    time: datetime         # 级别产生时间
    is_support: bool       # True=支撑(低点), False=阻力(高点)
    status: str = "UNTOUCHED"  # UNTOUCHED / TOUCHED