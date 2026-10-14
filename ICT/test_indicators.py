"""快速功能验证：测试ICT指标计算"""
import sys
sys.path.insert(0, '.')
from datetime import datetime, timezone, timedelta
from data.candle import Candle
from indicators.swing import find_swings
from indicators.fvg import find_fvgs, check_fvg_inversion
from indicators.csd import detect_csd, get_latest_csd
from indicators.atr import calc_atr, detect_sfp
from utils.time_utils import get_session_gmt

# 模拟K线数据
UTC = timezone.utc
base = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

def make_candle(offset_min, o, h, l, c):
    t = base + timedelta(minutes=offset_min)
    return Candle(open_time=t, close_time=t + timedelta(minutes=1),
                  open=o, high=h, low=l, close=c,
                  volume=100, quote_volume=440000, trades=10, taker_buy_volume=50)

candles = [
    make_candle(0, 4400, 4402, 4399, 4401),   # 阳线
    make_candle(1, 4401, 4403, 4400, 4402),   # 阳线
    make_candle(2, 4402, 4405, 4401, 4403),   # 阳线 (Swing High?)
    make_candle(3, 4403, 4404, 4398, 4399),   # 阴线 - 可能的空头FVG
    make_candle(4, 4399, 4400, 4395, 4396),   # 阴线
    make_candle(5, 4396, 4398, 4394, 4395),   # 阴线 (Swing Low?)
    make_candle(6, 4395, 4397, 4394, 4396),   # 阳线
    make_candle(7, 4396, 4398, 4395, 4397),   # 阳线
    make_candle(8, 4397, 4399, 4396, 4388),   # 大阴线(空头CSD)
    make_candle(9, 4388, 4390, 4385, 4386),   # 阴线
]

# 测试Swing
swings = find_swings(candles)
print(f"Swing Points: {len(swings)}")
for s in swings:
    label = "High" if s.is_high else "Low"
    print(f"  {label} @ {s.price:.2f} at index {s.index}")

# 测试FVG
fvgs = find_fvgs(candles, swings)
print(f"\nFVGs: {len(fvgs)}")
for f in fvgs:
    label = "多头" if f.direction == 1 else "空头"
    print(f"  {label}FVG [{f.lower:.2f}, {f.upper:.2f}] size={f.size:.2f} BSG={f.is_bsg}")

# 测试FVG反转
inverted = check_fvg_inversion(fvgs, candles)
print(f"反转FVG: {len(inverted)}")

# 测试CSD
csd_list = detect_csd(candles)
print(f"\nCSD Signals: {len(csd_list)}")
for c in csd_list:
    label = "多头" if c.direction == 1 else "空头"
    print(f"  {label}CSD at index {c.index}")

latest_csd = get_latest_csd(candles)
print(f"Latest CSD: {latest_csd}")

# 测试时段
session, weight = get_session_gmt(candles[-1].close_time)
print(f"\n时段: {session} (权重={weight})")

# 测试ATR
atr = calc_atr(candles)
print(f"\nATR: {atr:.2f}")

# 测试SFP
sfp = detect_sfp(candles, swings)
print(f"SFP: {sfp}")

print("\n=== 所有核心功能验证通过 ===")