# -*- coding: utf-8 -*-
"""PAXGUSDT 高胜算形态交易系统 —— 全局配置

所有可调参数集中在此，改配置不改代码。
数据源：币安现货 K 线接口。
方法论：蒂莫西·奈特《高胜算形态交易》全书 24 章。
"""

import os

# ====================== 标的与接口 ======================
SYMBOL = "PAXGUSDT"
BASE_URL = os.environ.get("BINANCE_SPOT_KLINE_URL")

# ====================== 周期体系 ======================
# 主信号周期（出信号）
SIGNAL_INTERVALS = ["30m", "1h", "4h"]
# 确认层（仅作次要确认，暂不单独出信号）
CONFIRM_INTERVAL = "15m"
# 禁用周期（历史噪音高、胜率低）
FORBIDDEN_INTERVALS = ["5m", "1m", "3m"]
# 各主信号周期对应的「趋势背景」周期（取更高一级）
TREND_INTERVALS = {"30m": "1h", "1h": "4h", "4h": "4h"}

# ====================== 请求参数 ======================
LIMIT = 1000
REQUEST_RETRIES = 10
REQUEST_TIMEOUT = 20
SCAN_INTERVAL_SECONDS = 10

# ====================== 飞书推送 ======================
# FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS"
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

# ====================== QQ邮箱推送 ======================
# ⚠️ 敏感信息建议通过环境变量注入，避免硬编码进代码/仓库：
#     set QQ_EMAIL_SENDER=xxx        set QQ_EMAIL_AUTH_CODE=xxx        set QQ_EMAIL_RECEIVER=xxx
QQ_EMAIL_ENABLED = True                        # 是否启用QQ邮箱推送
QQ_EMAIL_SENDER = os.environ.get("QQ_EMAIL_SENDER", "")  # 发件邮箱
QQ_EMAIL_AUTH_CODE = os.environ.get("QQ_EMAIL_AUTH_CODE", "")  # QQ邮箱SMTP授权码
QQ_EMAIL_RECEIVER = os.environ.get("QQ_EMAIL_RECEIVER", "")  # 收件邮箱
QQ_SMTP_SERVER = "smtp.qq.com"                 # SMTP服务器
QQ_SMTP_PORT = 465                             # SSL端口
QQ_EMAIL_RETRIES = 3                           # 发送失败重试次数
QQ_EMAIL_RETRY_DELAY = 15                      # 重试间隔(秒)
QQ_SMTP_TIMEOUT = 60                           # SMTP连接/发送超时(秒)

# ====================== 风控 ======================
RISK_PCT = 0.02              # 单笔风险默认 2%
RISK_PCT_MAX = 0.05          # 硬上限 5%
STOP_BUFFER_ATR = 0.5        # 止损缓冲（ATR 倍数）

# ====================== ATR ======================
ATR_PERIOD = 14
ATR_COMPRESS_THRESHOLD = 0.8  # ATR 比过低判定低波动压缩，跳过

# ====================== 摆动点 ======================
SWING_LEFT = 3
SWING_RIGHT = 3

# ====================== 量能确认 ======================
VOLUME_MA_N = 20
VOLUME_BREAK_MULT = 1.8      # 突破放量：当根量 >= N 根均量 × k

# ====================== 形态参数 ======================
NECKLINE_SLOPE_LIMIT_DEG = 10.0   # 颈线倾斜上限（度）
CUP_HANDLE_RETRACE_RATIO = 1.0 / 3.0  # 杯柄回调上限（1/3 杯幅）
FLAG_MAX_BARS = 90            # 旗形/三角旗时间上限（根，近似 3 个月）
FLAG_POLE_MIN_ATR = 3.0       # 旗杆最小幅度（ATR 倍数）
TRIANGLE_TOUCH_MIN = 2        # 三角形各线接触点下限
WEDGE_TOUCH_MIN = 3           # 楔形各线接触点下限
CHANNEL_TOUCH_MIN = 2         # 通道各线接触点下限
CHANNEL_LOOKBACK = 200        # 通道回归回看根数（避免跨价格阶段拟合失真）
CHANNEL_MIN_WIDTH_ATR = 1.0   # 通道最小宽度（ATR 倍数，过滤噪声级通道）
CHANNEL_TOUCH_TOL_ATR = 0.3   # 通道接触点容差（ATR 倍数）
MULTI_TOUCH_MIN = 2           # 多重顶/底触及次数下限
LEVEL_TOLERANCE_ATR = 0.3     # 判断「同一价位」的容差（ATR 倍数）

# ====================== 缺口 ======================
GAP_MIN_ATR = 0.2             # 缺口最小阈值（ATR 倍数，代 25 美分）

# ====================== 斐波纳契 ======================
# 标准比率集合（原书「68.1%」为 61.8% 笔误，并补漏列的 78.6%）
FIB_LEVELS = [23.6, 38.2, 50.0, 61.8, 78.6, 100.0]
FIB_ENABLED = False           # 默认观察模式：不产生实际信号，仅输出「友好度报告」
FIB_FRIENDLY_WINDOW = 500     # 友好友校验回看根数
FIB_FRIENDLY_MIN_HIT_RATE = 0.6  # 判定「友好」的最低命中率

# ====================== 信号去重 ======================
COOLDOWN_BARS = 12            # 同方向冷却根数
OPEN_DISALLOW_MINUTES = 30    # 单根 K 线前 30 分钟不建新信号

# ====================== 信号等级 ======================
GRADE_STRONGEST = "strongest"
GRADE_STRONG = "strong"
GRADE_GENERAL = "general"     # 不推送

# ====================== 信号过滤（基于历史回测胜率审计） ======================
# 直接禁用：历史回测中「高触发 + 低胜率 + 持续亏损」的形态。
# 依据（已了结信号胜率，见 all_signals_log.txt 审计）：
#   支撑失效 16.5% / 三角旗(看跌) 35.7% / 下降三角形 0% / 旗形(看跌) 45.2% / 圆顶 71.4%(n=7 且为逆势做空)
PATTERN_BLACKLIST = [
    "支撑失效",
    "三角旗(看跌)",
    "下降三角形",
    "旗形(看跌)",
    "圆顶",
]

# 做空降权：仅允许该等级及以上的做空信号（做空整体胜率 51.8%，接近打平，需最强级才放行）。
# 取值 "strongest"（仅三星做空）/ "strong"（二星及以上做空）/ "off"（不限制）。
SHORT_MIN_GRADE = "strongest"

# ====================== 回测预热与日志 ======================
WARMUP_BARS = 200               # 每周回测向前多拉取的预热根数（保证指标/趋势在周初即热，消除周边界冷启动偏差）
CLEAR_SIGNAL_LOG_ON_START = True  # 每次回测启动时清空 all_signals_log.txt，避免多次运行重复累计


