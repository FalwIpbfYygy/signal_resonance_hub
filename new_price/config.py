# -*- coding: utf-8 -*-
"""
 价格行为交易系统 v2 - 配置模块
依据 PRD_价格行为交易系统_v1.md §11(全表已确认)

理论依据：Al Brooks《趋势篇》《区间篇》《反转篇》 + 《太妃PA学习》《学委方方土PA专题课》

【已锁定定义(趋势篇)】
- 交易区间: "市场要么处于趋势之中，要么处于交易区间之中"
- 紧凑交易区间/带刺铁丝(barbwire): 3条以上大部分重叠+十字星+长尾线 → 不交易
- 微型通道/紧凑通道: 回撤小，只持续1~3根 → 趋势最强端
- 台阶形态(宽通道): 连续3+趋势波段，双向交易 → 趋势最弱端
"""
import os

# ====================== 代理配置 ======================
# os.environ["HTTP_PROXY"] = "http://127.0.0.1:10808"
# os.environ["HTTPS_PROXY"] = "http://127.0.0.1:10808"

# ====================== 交易标的与周期(已确认①) ======================
SYMBOL = ""                       # 固定  永续，后续统一走该接口
INTERVALS = ["5m", "15m", "30m", "1h", "4h"]
PRIMARY_INTERVAL = "5m"                  # 主信号周期(已确认②)
CONFIRM_INTERVALS = ["15m", "1h"]        # 确认/背景周期
CONTEXT_INTERVAL = "4h"                  # 大级别脉络
KLINE_LIMIT = 1000                       # 每请求K线数量(已确认)
KLINE_URL = os.environ.get("BINANCE_FUTURES_KLINE_URL")

# ====================== 飞书推送 ======================
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

# ====================== 扫描配置 ======================
SCAN_INTERVAL_SECONDS = 10               # 轮询间隔
LOOP_SLEEP = 0.1
ALIVE_LOG_SEC = 1800                     # 生存日志 30min
REQUEST_TIMEOUT = 8                      # HTTP超时
REQUEST_RETRIES = 3

# ====================== 均线/ATR参数 ======================
EMA_PERIOD = 20                          # Brooks 核心均线
EMA_SLOPE_PERIOD = 5                     # 斜率回溯
ATR_PERIOD = 14                          # Wilder ATR

# ====================== 棒线分类与信号棒 ======================
DOJI_BODY_RATIO = 0.20                   # 十字星实体占比(>0.20=伪Doji排除,锁定)
STRONG_BAR_MULT = 1.0                    # 强势趋势棒实体倍数(相对20棒平均)
EXHAUSTION_MULT = 3.0                    # 耗尽棒实体倍数
AVG_ENTITY_PERIOD = 20                   # 平均实体计算周期
REVERSAL_TAIL_MIN = 0.33                 # 反转棒尾线最小占比
REVERSAL_TAIL_MAX = 0.25                 # 反转棒反向尾线最大占比
REVERSAL_ENTITY_MIN = 0.8                # 反转棒实体最小倍数
REVERSAL_CLOSE_LOOKBACK = 5              # 收盘反转检查回溯棒数
REVERSAL_CLOSE_MIN_REVERSE = 3           # 收盘需反转最少棒数
SIGNAL_GRADES_ALLOWED = ["strong", "strongest"]  # 仅高分级(已确认④)
TICK_SIZE = 0.01                         # 最小跳动

# ====================== 市场状态判定阈值 ======================
# --- 趋势/区间通用 ---
TREND_SLOPE_THRESHOLD = 0.0001           # EMA斜率近水平阈值(相对价格)
TREND_PRICE_ABOVE_EMA_RATIO = 0.6        # 60%棒线在EMA上方=多头
RANGE_EMA_CROSS_COUNT = 4                # 穿越EMA≥4次=区间
HURST_PERIOD = 50
HURST_THRESHOLD = 0.55                   # <0.55均值回归
OVERLAP_PERIOD = 10
OVERLAP_RATIO_THRESHOLD = 0.6            # 重叠率>0.6

# --- 紧凑区间(barbwire) ---
ATR_RATIO_PERIOD = 10
ATR_RATIO_THRESHOLD = 0.8                # ATR比率<0.8=波动压缩

# --- 宽/窄通道分界(两本新笔记核心增量) ---
# 代理变量: 回调深度占比 = 回调腿幅度 / 推动腿幅度
# 逆势方能否靠限价/突破单盈利 → 回调越深越宽，趋势越弱
PULLBACK_RATIO_TREND = 0.5               # <0.5=急速/窄通道(强趋势)
PULLBACK_RATIO_RANGE = 0.75              # >=0.75=回调过深，趋势失效→区间
SWING_LOOKBACK = 2                       # 波段点确认棒数
SWING_DIRECTION_COUNT = 3                # 方向判定所需最近波段点数
PULLBACK_PAIRS = 3                       # 回调深度取最近N个腿
RANGE_HORIZONTAL_BARS = 20               # 横向运行≥20根=区间

# ====================== 数推/回调计数(高123/低123) ======================
# 锁定来源: 学委方方土PA.txt L279-283(高) / L322-326(低)
MAX_COUNT_LEVEL = 4                      # 计数封顶(高4+/低4+，超过仍标4)
COUNT_SIGNAL_MIN_LEVEL = 1               # 有效计数起点(高1/低1)
COUNT_REF_LOOKBACK = 20                  # 参考峰滚动窗口(最近N棒高点/低点, 代理"最近波段极值")

# ====================== 过滤模块 ======================
# 依据 PRD §7 + 太妃PA L170(80%法则) / L344-347(回调失效) / 学委 L293(数推=好位置)
DIRECTION_ALIGN_REQUIRED = True          # 趋势态强制顺势(80%法则)
REQUIRE_PULLBACK_POSITION = True         # 信号棒须落在数推点位(高1-3/低1-3)
EXCLUDE_COUNT_LEVEL_0 = True             # 排除0计数(信号棒不在回调位置=噪音,去micro双底)
EXCLUDE_COUNT_LEVEL_4 = True             # 排除高4/低4(已陷区间,动能枯竭)
MAX_PULLBACK_RATIO = 0.75                # 回调深度≥0.75=趋势失效(趋势信号拒绝)
# --- 可选过滤(V1默认关闭,骨架预留) ---
MAGNET_FILTER_ENABLED = False            # 磁体拦阻
MAGNET_PROXIMITY_ATR = 0.5               # 磁体距离阈值(×ATR)
TIME_FILTER_ENABLED = False              # 美盘时段过滤
TIME_SESSION_START_H = 20                # 美盘开始 BJT 20:00
TIME_SESSION_END_H = 4                   # 美盘结束 BJT 04:00(跨日)
VOLUME_FILTER_ENABLED = False            # 量能背离过滤(主动买入占比)
TAKER_BUY_ALIGN_THRESHOLD = 0.5          # 主动买入占比阈值

# ====================== 开仓逻辑 ======================
# 依据 PRD §6 + §8, 锁定定义见 entry.py 头注释(太妃/学委 L引用)
SL_BUFFER_ATR = 1.0                      # 止损缓冲 ×ATR(实验: 0.3→1.0 加宽以降cost_R; 原确认值0.3, PRD⑤)
TP1_R = 1.0                              # 剥头皮第一目标 1R(60%胜率基准)
TP2_R_MIN = 1.5                          # 波段第二目标下限 1.5R
TP2_R_SWING = 2.0                        # 波段完整目标 2R(40%胜率)
TP1_CLOSE_RATIO = 0.5                    # TP1 平仓比例
TP2_CLOSE_RATIO = 0.5                    # TP2 平仓比例
ENTRY_STOP_TICK = 1                      # 停止单入场高出信号棒极值 1 tick(学委 L295)
FADE_ENABLED = False                     # fade(看衰突破/区间边线均值回归)开关: 黄金为趋势品种, V1默认关闭
FADE_EDGE_ATR = 0.5                      # fade: 距区间边线 0.5×ATR 内判定为「触边」
FINAL_FLAG_MAGNET_ATR = 0.4              # 末段旗形: 触及磁体 0.4×ATR 内判定「触磁」

# ====================== 风控模块 ======================
# 依据 PRD §8, 锁定定义: 太妃 L62/L674/L683(期望0.2+入场先定止损止盈)
#                       学委 L24/L630(止损大小仓位)/L1349(勿死扛勿亏损加仓)/L1365(三推减仓)
RISK_PCT = 1.0                          # 单笔风险占账户比例 %(PRD §8.3: 1-2%)
ACCOUNT_EQUITY = 10000.0                # 账户权益(USD, 用户按实际修改)
ADD_RISK_PCT = 0.5                      # 加仓单笔风险%(较底仓更小, 金字塔式)
ADD_LEVEL_MAX = 2                       # 仅趋势早期(高/低1-2)允许加仓
ADD_PROFIT_BUFFER_ATR = 0.3             # 加仓需已浮盈 ≥ 0.3×ATR(禁止摊平亏损)
INVALIDATE_VOLUME_MULT = 2.0            # 区间真突破判定: 量能 ≥ 2×近N棒均量

# ====================== 信号去重 ======================
# 依据 PRD §9: K线级(open_time主键) + 冷却 + 形态级(仅升级再推) + 未收盘拦截
DEDUP_COOLDOWN_BARS = 3                  # 同周期同方向冷却N根(PRD §9.2)

# ====================== 回测配置 ======================
# 依据 PRD_回测统计_价格行为交易系统_v1.md(已确认: 各周期独立/2026-01-01起/触及成交/TP1后保本)
BACKTEST_START = "2026-01-01"            # 回测起点
BACKTEST_END = None                      # None=至今日(可设 "YYYY-MM-DD")
MAX_FUTURE_BARS = 100                    # 信号后观测K线数(未触发→timeout)
COST_SLIPPAGE_PCT = 0.0002               # 单边滑点 0.02%
COST_COMMISSION_PCT = 0.0004             # 单边 taker 佣金 0.04%
MIN_SAMPLE = 25                          # 分组样本门槛(低于此不做结论)
IS_RATIO = 0.8                           # In-Sample / Out-of-Sample 切分比
BACKTEST_EXCLUDE_WEEKEND = False         # 是否剔除周末(False=与实盘一致, 永续7×24)
BACKTEST_WARMUP_BARS = 60                # 预热K线数(指标稳定后再出信号)

# ====================== 周期分钟数 ======================
INTERVAL_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}