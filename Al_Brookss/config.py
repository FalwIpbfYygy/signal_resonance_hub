# -*- coding: utf-8 -*-
"""
 Al Brooks 价格行为交易系统 - 配置模块
所有可调参数集中管理，便于回测优化和实盘调整
"""
import os

# ====================== 交易标的与周期 ======================
SYMBOL = ""
INTERVALS = ["5m", "15m", "30m", "1h", "4h"]
KLINE_LIMIT = 1000           # 每次请求K线数量
KLINE_URL = os.environ.get("BINANCE_FUTURES_KLINE_URL")

# ====================== 飞书推送 ======================
# FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS"
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

# ====================== QQ邮箱推送 ======================
QQ_EMAIL_ENABLED = True                        # 是否启用QQ邮箱推送
QQ_EMAIL_SENDER = os.environ.get("QQ_EMAIL_SENDER", "")          # 发件邮箱
# 授权码从环境变量读取（QQ_EMAIL_AUTH_CODE），避免明文硬编码进代码/仓库
QQ_EMAIL_AUTH_CODE = os.environ.get("QQ_EMAIL_AUTH_CODE", "")  # QQ邮箱SMTP授权码
QQ_EMAIL_RECEIVER = os.environ.get("QQ_EMAIL_RECEIVER", "")        # 收件邮箱
QQ_SMTP_SERVER = "smtp.qq.com"                 # SMTP服务器
QQ_SMTP_PORT = 465                             # SSL端口
QQ_EMAIL_RETRIES = 3                           # 发送失败重试次数
QQ_EMAIL_RETRY_DELAY = 15                       # 重试间隔(秒)
QQ_SMTP_TIMEOUT = 30                           # SMTP连接/发送超时(秒)

# ====================== 扫描配置 ======================
SCAN_INTERVAL_SECONDS = 10   # 扫描间隔(秒)
LOOP_SLEEP = 0.1             # 主循环休眠(秒)
REQUEST_TIMEOUT = 120          # HTTP请求超时(秒)
REQUEST_RETRIES = 3          # 重试次数

# ====================== Brooks理论参数 ======================
# --- 棒线分类 ---
DOJI_BODY_RATIO = 0.20       # 十字星实体占比阈值(伪Doji排除)
STRONG_BAR_MULT = 1.0        # 强势趋势棒实体倍数(相对20棒平均)
EXHAUSTION_MULT = 3.0        # 耗尽棒实体倍数
AVG_ENTITY_PERIOD = 20       # 平均实体计算周期

# --- 反转棒 ---
REVERSAL_TAIL_MIN = 0.33     # 反转棒尾线最小占比(棒高比例)
REVERSAL_TAIL_MAX = 0.25     # 反转棒反向尾线最大占比
REVERSAL_ENTITY_MIN = 0.8    # 反转棒实体最小倍数(相对20棒平均)
REVERSAL_CLOSE_LOOKBACK = 5  # 收盘反转检查回溯棒数
REVERSAL_CLOSE_MIN_REVERSE = 3  # 收盘需反转的最少棒数

# --- 信号强度 ---
SIGNAL_GRADES_ALLOWED = ["strong", "strongest"]

# --- EMA参数 ---
EMA_PERIOD = 20              # Brooks核心均线
EMA_SLOPE_PERIOD = 5         # 斜率计算周期

# --- ATR参数 ---
ATR_PERIOD = 14              # ATR计算周期

# --- 过滤参数 ---
EMA_DIST_ATR_MULT = 0.5      # EMA距离过滤: |close-EMA| > 0.5×ATR
ATR_RATIO_THRESHOLD = 0.8    # ATR比率<0.8=紧凑TR(不交易)
HURST_THRESHOLD = 0.55       # Hurst<0.55=均值回归(不交易)
HURST_PERIOD = 50            # Hurst计算周期
OVERLAP_RATIO_THRESHOLD = 0.6  # 棒线重叠率>60%=TTR

# --- 趋势判定 ---
TREND_CLOSE_SERIES_PERIOD = 20  # 收盘价序列方向计算周期
TREND_CLOSE_DOMINANCE = 12   # 上升/下降棒数≥12=趋势
TREND_SLOPE_THRESHOLD = 0.0001  # EMA斜率近水平阈值
TREND_PRICE_ABOVE_EMA_RATIO = 0.6  # 60%以上棒线在EMA上方=多头趋势
TREND_MIN_BARS = 20          # 趋势最少持续棒数

# --- 回撤计数 ---
HL_LOOKBACK = 20             # H/L计数回溯棒数
H1_CONSECUTIVE_LIMIT = 3     # 连续H1信号限制(超过=动量衰退)
SECOND_ENTRY_LOOKBACK = 5    # 二次入场回溯棒数

# --- 趋势结构 ---
SPIKE_MIN_BARS = 3           # 尖峰最少趋势棒数
SPIKE_MIN_STRONG_RATIO = 0.6  # 尖峰中强势棒占比
MICRO_CHANNEL_MIN_BARS = 2   # 微型通道最少棒数
MICRO_CHANNEL_MAX_BARS = 10  # 微型通道最多棒数
MICRO_CHANNEL_TOUCH_RATIO = 0.7  # 微型通道触及率
STAIR_MIN_SEGMENTS = 3       # 台阶形态最少段数
FINAL_FLAG_MIN_TREND_BARS = 50  # 最终旗形前趋势最少棒数
FINAL_FLAG_BARS = 10         # 最终旗形横盘棒数

# --- MTR ---
MTR_TREND_MIN_BARS = 20      # MTR条件1: 趋势最少棒数
MTR_BREAKOUT_MIN_BARS = 3    # MTR条件2: 逆势尖峰最少棒数
MTR_TEST_ATR_TOLERANCE = 0.5  # MTR条件3: 极点测试容差(×ATR)

# --- 止损止盈 ---
SL_ATR_MULT = 0.3            # 止损缓冲: 信号棒极值±0.3×ATR
SL_LARGE_BAR_RATIO = 0.6     # 信号棒异常大时止损比例
TP1_R_MULT = 1.0             # TP1: 1R了结50%
TP2_R_MULT = 1.5             # TP2: 1.5R了结50%
TP_STRONG_TP1_R = 2.0        # 强趋势TP1: 2R了结50%
TP_STRONG_TP2_R = 3.0        # 强趋势TP2: 3R了结25%
TP1_CLOSE_RATIO = 0.5        # TP1平仓比例
TP2_CLOSE_RATIO = 0.5        # TP2平仓比例

# --- 仓位管理 ---
RISK_PER_TRADE = 0.01        # 单笔风险1%
MAX_POSITIONS = 3            # 最大同时持仓
MAX_SAME_DIRECTION = 2       # 单方向最大持仓
DAILY_LOSS_LIMIT = 0.03      # 日亏损限额3%

# --- 去重 ---
DEDUP_PRICE_ATR_MULT = 0.3   # 价格相近阈值: 0.3×ATR
DEDUP_EXPIRY_BARS = 10       # 去重有效期(棒数)
DEDUP_PRICE_EXPIRE_MULT = 2.0  # 价格远离>2×ATR=过期

# --- 时段(北京时间) ---
SESSIONS = {
    "asian":   (8, 14),      # 亚盘
    "eu_open": (14, 15),     # 欧盘开盘
    "eu":      (15, 20),     # 欧盘
    "us_open": (20, 21),     # 美盘开盘
    "us":      (21, 26),     # 美盘(26=次日2点)
}
SESSION_WEIGHTS = {
    "asian": 0.6, "eu_open": 0.8, "eu": 1.0,
    "us_open": 1.2, "us": 1.0,
}

# --- 多周期共振(可选) ---
RESONANCE_ENABLED = True
RESONANCE_MAP = {
    "5m":  ["15m", "30m"],
    "15m": ["30m", "1h"],
    "30m": ["1h", "4h"],
    "1h":  ["4h"],
    "4h":  [],
}

# --- 架构优先级(数字越小优先级越高) ---
SETUP_PRIORITY = {
    "second_entry":      0,   # 二次入场 P0
    "failed_failure":    1,   # 失败的失败 P0
    "h2_trend":          2,   # H2趋势中 P1
    "l2_trend":          2,   # L2趋势中 P1
    "h1_strong":         3,   # H1强趋势 P1
    "l1_strong":         3,   # L1强趋势 P1
    "mtr_conservative":  4,   # MTR保守 P1
    "wedge_flag":        5,   # 楔形旗形 P2
    "tr_reversal":       5,   # TR顶底反转 P2
    "mtr_aggressive":    6,   # MTR激进 P3
    "climax_reversal":   6,   # 高潮反转 P3
    "final_flag":        6,   # 最终旗形 P3
}

# --- 交易者方程参数(胜率赋值+最小回报) ---
TRADER_EQUATION = {
    "second_entry":      {"win_rate": 0.65, "min_rr": 1.5},
    "failed_failure":    {"win_rate": 0.65, "min_rr": 1.5},
    "h2_trend":          {"win_rate": 0.60, "min_rr": 1.5},
    "l2_trend":          {"win_rate": 0.60, "min_rr": 1.5},
    "h1_strong":         {"win_rate": 0.55, "min_rr": 1.5},
    "l1_strong":         {"win_rate": 0.55, "min_rr": 1.5},
    "mtr_conservative":  {"win_rate": 0.60, "min_rr": 1.5},
    "wedge_flag":        {"win_rate": 0.55, "min_rr": 1.5},
    "tr_reversal":       {"win_rate": 0.55, "min_rr": 1.5},
    "mtr_aggressive":    {"win_rate": 0.45, "min_rr": 3.0},
    "climax_reversal":   {"win_rate": 0.45, "min_rr": 3.0},
    "final_flag":        {"win_rate": 0.45, "min_rr": 3.0},
}

# --- 入场方式 ---
ENTRY_METHOD_STOP = "stop"   # 止损单入场(默认)
ENTRY_METHOD_LIMIT = "limit" # 限价单入场

# 周期对应的分钟数(用于K线收盘判断)
INTERVAL_MINUTES = {
    "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240,
}
