# ====================== ICT  策略配置 ======================
import os

# ---- 品种 ----
SYMBOL = ""
SYMBOL_SMT = "DXY"  # SMT对标品种（合成美元指数，与黄金负相关）

# ---- SMT 对标（TwelveData 合成 DXY）----
# 币安期货无 DXY 合约，改从 TwelveData 拉取 6 个外汇对按 ICE 公式合成。
# 限流约束（免费版）：每分钟 ≤8 次、每天 ≤800 次，故必须串行请求 + 长缓存。
SMT_ENABLED = True
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "2ae6d3a251304c89a5d99ea936d082f3")
SMT_CACHE_SECONDS = 900     # DXY 序列缓存 15 分钟（6 次请求/轮，约 96 轮/天，低于 800 上限）
SMT_LOOKBACK = 500          # 合成 DXY 回看根数（1min）

# ---- 时间框架 ----
TF_ENTRY = "1m"      # 入场确认
TF_STRUCTURE = "5m"   # 短期结构
TF_EXECUTION = "15m"  # 核心执行框架（标记亚洲盘/伦敦盘）
TF_1H = "1h"          # 1小时级别
TF_4H = "4h"          # 4小时级别
TF_DAILY = "1d"       # 日线

ALL_TIMEFRAMES = [TF_ENTRY, TF_STRUCTURE, TF_EXECUTION, TF_1H, TF_4H, TF_DAILY]
# 各级别拉取的K线数量
KLINE_LIMIT = {
    TF_ENTRY: 300,      # 1m: 5小时
    TF_STRUCTURE: 400,  # 5m: 约33小时
    TF_EXECUTION: 300,  # 15m: 约3天
    TF_1H: 200,         # 1h: 约8天
    TF_4H: 200,         # 4h: 约33天
    TF_DAILY: 100,      # 日线: 约3个月
}

# ---- Binance API ----
BINANCE_FUTURES_URL = os.environ.get("BINANCE_FUTURES_KLINE_URL")

# ---- 飞书 ----
# FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS"

# ICT
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

# ---- 飞书推送强度门槛 ----
# 只推送信号强度 >= 此值的信号（4 = ⭐⭐⭐⭐及以上）。
# 低于该星级的信号彻底丢弃：不入库、不推送、不占级别冷却、不占全局间隔。
MIN_PUSH_STRENGTH = 4


# ---- 关键级别缓冲（价格触及的容差，约0.05%）----
LEVEL_TOUCH_BUFFER_PCT = 0.05  # 0.05% of price

# ---- ATR ----
ATR_PERIOD = 14

# ---- 信号去重 ----
SIGNAL_DEDUP_MINUTES = 5  # 同一信号5分钟内不重复推送

# ---- 时段确认阈值 ----
# 亚洲盘需要更多确认（至少2个确认信号）
ASIA_MIN_CONFIRMATIONS = 2
# 伦敦/纽约盘只需1个确认
LONDON_NY_MIN_CONFIRMATIONS = 1

# ---- 扫描间隔 ----
SCAN_INTERVAL_SECONDS = 10
