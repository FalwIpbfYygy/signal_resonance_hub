import os
import sys
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

import requests
import numpy as np
# ====================== 【新增底层工具：通道拟合 / AB-CD MM / 信号K / MTR工具集】 ======================
from dataclasses import dataclass

# 统一信号入库模块（位于项目根目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_store
from typing import List, Tuple, Optional
# ====================== 【全局配置常量｜PRD V1.1全部参数集中在此】 ======================


SYMBOL = ""
INTERVALS = ["5m", "15m", "30m", "1h", "4h"]

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

# 网络请求
REQUEST_TIMEOUT = 120
MAX_RETRY = 3
RETRY_SLEEP_SEC = 1
SCAN_INTERVAL_SECOND = 10
KLINES_LIMIT = 1000

# 指标参数
EMA_PERIOD = 20
M2_EMA_DISTANCE_RATIO = 0.003
TTR_BOUNDARY_RATIO = 0.33

# 缺口配置 V1.1
GAP_PRICE_DECIMAL = 2
GAP_EPS = 1e-4
GAP_MIN_GAP_TICK = 0.5


# ======================【时间显示口径｜FIX-C：K线原始时间(UTC) + 北京时间】======================
# 币安K线时间戳定义：bar[0]=open_time(UTC)，bar[6]=close_time=open_time+interval-1ms。
# 本脚本 trigger_k_ts 取自 t_end_ms(=close_time)，若直接 strftime 会出现两个问题：
#   1) 显示成 "xx:59:59"，与看盘软件上的K线标签（整点开盘时间）对不上；
#   2) datetime.fromtimestamp() 依赖【本机时区】，本地/服务器/容器时区不一致时同一信号显示不同时间。
# 现统一为显式双时区输出：K线原始时间（UTC，=交易所口径）+ 北京时间（UTC+8），与机器时区解耦。
TZ_UTC = timezone.utc
TZ_BEIJING = timezone(timedelta(hours=8))

# 各周期单根K时长（ms），用于由 close_time 反推 open_time
INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def kline_open_ms(end_ts_ms: int, interval: str) -> int:
    """由币安 close_time 反推该K线 open_time：close_time = open_time + interval - 1ms"""
    ms = INTERVAL_MS.get(interval)
    if not ms:
        return int(end_ts_ms)
    return int(end_ts_ms) + 1 - ms


def fmt_utc(ts_ms: int) -> str:
    """时间戳 → UTC 字符串（K线原始时间口径，与看盘软件K线标签一致）"""
    return datetime.fromtimestamp(int(ts_ms) / 1000, TZ_UTC).strftime(TIME_FMT)


def fmt_beijing(ts_ms: int) -> str:
    """时间戳 → 北京时间字符串（UTC+8）"""
    return datetime.fromtimestamp(int(ts_ms) / 1000, TZ_BEIJING).strftime(TIME_FMT)


def kline_time_pair(end_ts_ms: int, interval: str) -> Tuple[str, str, str, str]:
    """
    统一时间口径输出（以K线【开盘时刻】为准，即看盘软件上的K线标签）：
    返回 (开盘UTC, 开盘北京, 收盘UTC, 收盘北京)
    说明：信号在K线【闭合后】才被确认，故收盘时刻 = 信号实际触发时刻。
    """
    open_ms = kline_open_ms(end_ts_ms, interval)
    return (fmt_utc(open_ms), fmt_beijing(open_ms),
            fmt_utc(end_ts_ms), fmt_beijing(end_ts_ms))


def fmt_ts_show(ts_ms: int) -> str:
    """
    【可读化】把裸露的毫秒时间戳渲染为"北京 + UTC"双时区文本。
    用于替换告警文案里的原始时间戳（如 1789372800000 → 北京 2026-09-14 16:00:00）。
    """
    return f"北京 {fmt_beijing(ts_ms)}（UTC {fmt_utc(ts_ms)}）"


def bars_between(ts_from_ms: int, ts_to_ms: int, interval: str) -> Optional[int]:
    """两个K线时间戳之间相差多少根该周期K（周期未知时返回None）"""
    ms = INTERVAL_MS.get(interval)
    if not ms:
        return None
    return int(round((int(ts_to_ms) - int(ts_from_ms)) / ms))


# 人工可读的MTR类型说明（原样输出 high_mtr/low_mtr 对新人不友好）
MTR_TYPE_CN = {
    "high_mtr": "顶部反转测试(high_mtr)",
    "low_mtr": "底部反转测试(low_mtr)",
}


# ======================【新增：通道 & MM-AB=CD & MTR 专项配置｜PRD-V1.1】 ======================
# 通道拟合
# 【FIX-I·2026-09-15 回测修复】原值 100：在该窗口内有效摆动点数量中位数只有 5/5，
# 而 CHANNEL_MIN_SWING_NUM=6 → 通道几乎恒为 INVALID（实测真实 4h 可用率 4.7%）。
# 放宽到 200 后实测可用率 99%（150→91.5%），通道/MTR/MM 才真正具备计算前提。
CHANNEL_FIT_LOOKBACK = 200          # 通道拟合回溯K线数量（原 100，回测修正为 200）
CHANNEL_MIN_SWING_NUM = 6           # 拟合通道至少需要多少个波段高低点
CHANNEL_SLOPE_EPS = 1e-6            # 斜率极小阈值，区分接近水平震荡通道

# AB=CD / MM Measured Move
MM_TARGET_TOLERANCE_RATIO = 0.004   # MM目标容忍区间：价格距离D目标±0.4%以内，视作抵达MM目标区
MM_MIN_LEG_BAR_COUNT = 8            # A-B、C回调腿，每一条腿最少K线根数，过滤微型毛刺腿
MM_MIN_LEG_AMPLITUDE_RATIO = 0.006  # 波段腿最小波动幅度比例，过滤微小噪音波段
MM_GAP_ALIGN_TOLERANCE_RATIO = 0.005# 精准MM缺口：缺口位置与MM目标允许偏差比例

# MTR 自动检测专项参数（覆盖原有全局MTR_VALID_K_BAR_WINDOW，保持兼容）
MTR_VALID_K_BAR_WINDOW = 15         # MTR生成后有效窗口：本周期K线根数
MTR_BREAK_CHANNEL_THRESHOLD = 0.003 # 次级反向运动突破原有通道的最小偏离比例
MTR_EXTREME_TEST_TOLERANCE_RATIO = 0.002 # MTR测试原有波段极值允许的价格偏差
MTR_MIN_SECONDARY_SWING_BAR = 12    # MTR次级反向波段最少K线数量，过滤毛刺小回调

# 反转/信号K质量判断参数
SIGNALK_BODY_MIN_RATIO = 0.35       # 高质量信号K：实体占总波幅最低比例
SIGNALK_REVERSE_BODY_RATIO = 0.45   # 反转K实体占比阈值
WIRE_BAR_BODY_THRESHOLD = 0.20      # 铁丝网K，实体/全波幅低于该值判定低质量K



# 背景分类（V1.2 重构：多源趋势确认计数，取代 V1.1 证据打分门槛）
# V1.1 的 BG_SCORE/BG_THRESHOLD/BG_MIN_EVIDENCE_COUNT 因门槛与证据发射量严重不匹配
# （STRONG/WEAK 在真实行情中永远不可达，连强上升趋势都返回 CONFUSE）已废弃。
# 详见 judge_background() V1.2 重构说明。
BG_STRONG_CONFIRM = 3      # 同向(多或空)趋势确认票 ≥3 → STRONG_TREND
BG_WEAK_CONFIRM = 2        # 同向趋势确认票 =2 → WEAK_TREND
BG_TTR_CONFIRM = 2         # 仅震荡确认票 ≥2 且无明显方向 → TTR
BG_EMA_SLOPE_MIN_RATIO = 0.001   # EMA20 斜率最小比例阈值，过滤走平震荡

# Setup加权打分 V1.1
# ------------------------------------------------------------------------------
# 【2026-09-15 变更】删除 fib50_aligned(+6) 权重与 calc_fib_50() 函数。
#   决策依据（实测取证，非拍脑袋）：
#   ① 死权重取证：在 2999根4h + 2999根1h 真实K线上滚动重放 1263 条候选Setup，
#      fib50_aligned 命中 0 条；加 6 分后越阈样本数 146→146、286→286，新增推送 0 条
#      ⇒ 今天"接线"与"删除"在输出上完全等价，接上去等于没接。
#   ② 根因：50%回撤位所依赖的"腿"来自 extract_valid_swing_points()，
#      而该函数因"两个列表互相等待对方先有值"而恒返回空（详见该函数处注释），
#      → 不存在任何可用的腿，fib50 在物理上不可能命中。
#   ③ 让 fib50 真正生效的前提 = 修好摆动点死锁 + 放宽 CHANNEL_FIT_LOOKBACK。
#      而该修复会同时复活 MTR观察/衍生、MM精准缺口、缺口+通道Stop 共3类从未上线过的信号，
#      属于重量级行为变更，必须独立回测后单独发布，不得夹带。
#   ④ 恢复路径（待摆动点修复后执行，实测命中率 long=12.9% / short=30.6%）：
#      把 "fib50_aligned": 6 加回本表 → 在 M2B/M2S/MTR衍生反转 的 cond_hit 里写入
#      judge_fib50_aligned(klines, last_close, direction) 的判定结果。
# ------------------------------------------------------------------------------
SETUP_SCORE_WEIGHT = {
    "bg_strong_trend": 15,
    "bg_weak_trend": 8,
    "bg_ttr": 5,
    "breakout_gap_aligned": 20,
    "mm_aligned": 12,
    "channel_trendline_aligned": 10,
    "high_quality_signal_k": 12,
    "mtr_active_derived": 15,
    "two_leg_complete": 10,
    "gap_fail_twice": 18,
    "signal_k_poor_quality": -15,
    "ttr_middle_zone": -30,
    "bg_confuse": -999,
}

SETUP_MIN_PUSH_SCORE = 45

# 【FIX-J·2026-09-15 回测发现｜结构性悬空项】权重表对所有 Setup 使用同一个 45 分门槛，
# 但有两类 Setup 的 cond_hit 键集合【天然打不到 45 分】——与通道是否有效无关，
# 是权重表设计时没有核算过"分数上限"导致的悬空项（setup_audit/weight_ceiling.py 可复算）：
#   · MTR_DERIVED_REVERSAL_MTR窗口衍生反转
#       mtr_active_derived(15) + bg_weak_trend(8)|bg_ttr(5) + high_quality_signal_k(12)
#       上限 = 35（weak）/ 32（ttr）  → 永远 < 45
#   · TTR_BOUNDARY_REVERSAL_震荡上沿/下沿反转
#       bg_ttr(5) + high_quality_signal_k(12) + channel_trendline_aligned(10)
#       上限 = 27                    → 永远 < 45
# 修法：不动全局权重表（避免影响其他 Setup 的分数语义），
# 只对这两类"分数结构天生偏低"的 Setup 使用其可达区间内的专属阈值。
# 阈值选取保证"硬条件缺失就不推"：
#   · MTR衍生 32 → 必须同时满足 mtr_active_derived + high_quality_signal_k
#   · TTR边界 25 → 必须同时满足 bg_ttr + high_quality_signal_k + 通道有效
SETUP_MIN_PUSH_SCORE_OVERRIDE = {
    "MTR_DERIVED_REVERSAL_MTR窗口衍生反转": 32,
    "TTR_BOUNDARY_REVERSAL_震荡上沿反转做空": 25,
    "TTR_BOUNDARY_REVERSAL_震荡下沿反转做多": 25,
}

# 【FIX-L·2026-09-15 回测发现｜连续刷屏】M2B/M2S 这类信号的去重 key 含 trigger_k_ts，
# 而 trigger_k_ts 每根新K都会变 → 只要结构连续满足，就【每根K都推一条】。
# 回测实测（2026-01-01~09-15）：
#   4h 最长连续同向 4 根、连续≥3 次共 8 组
#   1h 最长连续同向 9 根、连续≥3 次共 32 组、单日峰值 18 条
#   30m 最长连续同向 12 根、连续≥3 次共 161 组、单日峰值 37 条  ← 明显的刷屏
# 修法：同一 (setup_name, direction) 在 N 根K冷却窗内只允许推送一次；
#       冷却窗之外的"新一波"照常推送（不同于全局去重，不会漏掉真正的二次入场机会）。
SETUP_SAME_DIR_COOLDOWN_BARS = 3

# 信号去重 V1.1
NOTIFY_EXPIRE_BAR_NUM = 20

# ======================【FIX-B 修复参数｜2026-09-15】======================
# 【C1 触发器】Setup3 缺口时效上限：
#   原逻辑仅判断 keep_open_k_num >= 3，无上限 → 一条永不回补的旧缺口
#   每根K都重新满足条件，永久复读（实测 4h 有缺口已敞开 103 根K = 17 天）。
#   现在给缺口加"有效期窗口"，只有敞开根数落在 [MIN, MAX] 区间才生成 Setup3。
GAP_FAIL_MIN_AGE_BAR = 3          # 缺口至少敞开N根K，才算"二次回补失败"
GAP_FAIL_MAX_AGE_BAR = 30         # 缺口敞开超过N根K后视为"陈旧缺口"，不再生成Setup3信号
# 同一条缺口在其有效期内只提醒一次（否则每个新K都会因 trigger_k_ts 变化而重新推送）
SETUP3_DEDUP_BY_GAP_ID = True

# 【C2 放大器】去重过期时间塌缩修复：
#   原 get_future_k_end_ts 在"目标K尚未产生"时直接返回最后一根K的end_ts，
#   导致 expire_k_end_ts == trigger_k_end_ts → 下一轮立即判过期 → 去重0%生效。
#   现在按K线周期外推；此开关为外推兜底（周期无法推断时使用）的允许开关。
NOTIFY_EXPIRE_EXTRAPOLATE = True  # True=按K线周期外推；False=退回旧行为（不推荐）

# 【惩罚权重补活】原 SETUP_SCORE_WEIGHT 里 signal_k_poor_quality / ttr_middle_zone
#   从未在 cond_hit 中被赋值 → 死权重，45分门槛几乎拦不住劣质信号。
ENABLE_PENALTY_WEIGHT_ACTIVATION = True   # True=启用劣质信号K扣分 + TTR中间区扣分

# MTR 上下文只保留最近N条（防全局内存无限增长；仅清理永不激活的记录）
MTR_CONTEXT_MAX_KEEP = 50

# ======================【飞书卡片推送配置｜2026-09-15】======================
# 飞书自定义机器人 interactive 卡片；header.template 仅允许下列取值
FEISHU_HEADER_TEMPLATE = {
    "long":    "green",   # 做多
    "short":   "red",     # 做空
    "observe": "orange",  # 观察预警
}
FEISHU_MAX_MD_LEN = 3800          # 单个 markdown 内容上限（超限易报 99991664）
FEISHU_CARD_FALLBACK_TEXT = True  # 卡片推送失败时，降级发纯文本，保证连通性

# ======================【QQ 邮箱通知配置｜新增 2026-09-17｜单机自用·明文】======================
# 本地单机使用；敏感凭据已从源码外置为环境变量（见 .env.example），请勿提交真实凭据。
# 授权码非 QQ 登录密码：「QQ邮箱网页端 → 设置 → 账户 → 开启 POP3/SMTP 服务 → 生成授权码」。
QQ_EMAIL_ENABLED = True                        # 是否启用QQ邮箱推送
QQ_EMAIL_SENDER = os.environ.get("QQ_EMAIL_SENDER", "")          # 发件邮箱
QQ_EMAIL_AUTH_CODE = os.environ.get("QQ_EMAIL_AUTH_CODE", "")        # QQ邮箱SMTP授权码（非登录密码）
QQ_EMAIL_RECEIVER = os.environ.get("QQ_EMAIL_RECEIVER", "")        # 收件邮箱
QQ_SMTP_SERVER = "smtp.qq.com"                 # SMTP服务器
QQ_SMTP_PORT = 465                             # SSL端口
QQ_EMAIL_RETRIES = 3                           # 发送失败重试次数
QQ_EMAIL_RETRY_DELAY = 15                      # 重试间隔(秒)
QQ_SMTP_TIMEOUT = 30                           # SMTP连接/发送超时(秒)

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("xau_pb_signal")

# ====================== 【自定义业务异常｜PRD V1.1】 ======================
class KlineCalcError(Exception):
    """K线指标计算异常：样本不足、除零、产出NaN/Inf，单周期降级跳过，不崩溃主程序"""
    pass

# ====================== 【全局内存状态：每个周期独立隔离】 ======================
# 信号去重存储：V1.1 使用notify_item结构体，基于K线时间戳过期淘汰
NotifyItem = Dict[str, Any]
g_notified: Dict[str, List[NotifyItem]] = {iv: [] for iv in INTERVALS}

# MTR上下文存储：每个周期独立，存储mtr_context字典
MtrContext = Dict[str, Any]
g_mtr_context: Dict[str, List[MtrContext]] = {iv: [] for iv in INTERVALS}

# 缺口存储：每个周期独立，gap_item字典
GapItem = Dict[str, Any]
g_gap_storage: Dict[str, List[GapItem]] = {iv: [] for iv in INTERVALS}

# ====================== 【网络层 binance_kline_client｜PRD V1.1】 ======================
def get_klines_retry(interval: str, limit: int = KLINES_LIMIT, retries: int = MAX_RETRY) -> Optional[List[List[Any]]]:
    """
    请求币安U本位合约K线，带重试；修复原始demo f-string url错误
    返回原始接口list；失败返回None
    """
    url = os.environ.get("BINANCE_FUTURES_KLINE_URL")
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "limit": limit
    }
    for attempt in range(1, retries + 1):
        try:
            logger.info(f"[{interval}] 尝试 {attempt}/{retries} 请求K线数据")
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"[{interval}] 请求K线成功，原始K线数量:{len(data)}")
            return data
        except Exception as e:
            logger.warning(f"[{interval}] 请求异常：{str(e)}，1s后重试")
            time.sleep(RETRY_SLEEP_SEC)
    logger.error(f"[{interval}] 全部重试失败，本周期跳过")
    return None


def parse_and_filter_closed_klines(raw_klines: List[List[Any]]) -> List[Dict[str, Any]]:
    """
    ✅ PRD V1.1 核心：逐条过滤未闭合K线，禁止简单pop(-1)
    输入：币安原始klines数组
    输出：仅【已经闭合】的K线结构化dict列表，按时间升序
    字段：t_start_ms, t_end_ms, open, high, low, close, volume
    """
    now_ms = int(time.time() * 1000)
    closed = []
    for bar in raw_klines:
        t_end_ms = int(bar[6])
        if t_end_ms >= now_ms:
            # 未收盘K线全部丢弃
            continue
        item = {
            "t_start_ms": int(bar[0]),
            "t_end_ms": t_end_ms,
            "open": float(bar[1]),
            "high": float(bar[2]),
            "low": float(bar[3]),
            "close": float(bar[4]),
            "volume": float(bar[5])
        }
        closed.append(item)
    logger.info(f"原始K线:{len(raw_klines)}根，过滤未闭合后有效闭合K:{len(closed)}根")
    return closed

# ====================== 【基础指标层 pb_base_calc｜PRD V1.1，全部做NaN/除零防护】 ======================
def calc_ema(values: np.ndarray, period: int) -> np.ndarray:
    """计算EMA，输入一维数组；样本不足抛KlineCalcError；输出全部有限值"""
    n = len(values)
    if n < period:
        raise KlineCalcError(f"EMA计算样本不足，需要至少{period}，实际{n}")
    alpha = 2.0 / (period + 1)
    ema = np.full_like(values, np.nan)
    ema[period - 1] = np.mean(values[:period])
    for i in range(period, n):
        ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
    if not np.all(np.isfinite(ema[period-1:])):
        raise KlineCalcError("EMA计算产出NaN/Inf")
    return ema


def get_band_high_low(klines: List[Dict[str, Any]], lookback: int = 120) -> Tuple[List[int], List[int]]:
    """
    识别波段HH/HL/LH/LL索引；返回 (high_idx_list, low_idx_list)
    lookback：回溯窗口；样本不足抛异常
    """
    n = len(klines)
    if n < lookback + 20:
        raise KlineCalcError(f"波段高低点样本不足，n={n}, lookback={lookback}")
    highs = np.array([k["high"] for k in klines])
    lows = np.array([k["low"] for k in klines])
    high_idx = []
    low_idx = []
    window = 5
    for i in range(window, n - window):
        if highs[i] == np.max(highs[i-window:i+window+1]):
            high_idx.append(i)
        if lows[i] == np.min(lows[i-window:i+window+1]):
            low_idx.append(i)
    return high_idx, low_idx


# 【2026-09-15 删除】calc_fib_50(leg_start_price, leg_end_price) -> (a+b)/2
#   仅3行的50%回撤位计算，定义后从未被调用，配套权重 fib50_aligned 也从未生效。
#   恢复方式见 SETUP_SCORE_WEIGHT 上方注释（需先修 extract_valid_swing_points 死锁）。


def detect_gaps(klines: List[Dict[str, Any]]) -> List[GapItem]:
    """
    ✅ PRD V1.1缺口识别；区分实体缺口；生成gap_item结构体；过滤微小毛刺缺口
    规则：实体跳空；区分bull向上缺口、bear向下缺口；区分OPEN/PARTIAL_FILL/FULL_ENTITY_FILL
    返回本周期全部缺口列表
    """
    gaps: List[GapItem] = []
    n = len(klines)
    if n < 20:
        raise KlineCalcError("缺口识别K线样本不足")
    for i in range(1, n):
        prev = klines[i - 1]
        curr = klines[i]
        # 实体区间
        prev_entity_low = min(prev["open"], prev["close"])
        prev_entity_high = max(prev["open"], prev["close"])
        curr_entity_low = min(curr["open"], curr["close"])
        curr_entity_high = max(curr["open"], curr["close"])
        gap_id = f"gap_{curr['t_end_ms']}"
        # Bull向上实体缺口：当前实体下限 > 前一根实体上限
        if curr_entity_low > prev_entity_high + GAP_EPS:
            gap_low = round(prev_entity_high, GAP_PRICE_DECIMAL)
            gap_high = round(curr_entity_low, GAP_PRICE_DECIMAL)
            gap_amplitude = abs(gap_high - gap_low)
            if gap_amplitude < GAP_MIN_GAP_TICK:
                continue
            gap_item: GapItem = {
                "gap_id": gap_id,
                "gap_type": "BREAKOUT_GAP",
                "direction": "bull",
                "gap_low": gap_low,
                "gap_high": gap_high,
                "create_k_ts": curr["t_end_ms"],
                "close_k_ts_list": [curr["t_end_ms"]],
                "fill_status": "OPEN",
                "fill_k_ts": None,
                "keep_open_k_num": 1,
                "anchor_k_note": "bull向上实体突破缺口"
            }
            gaps.append(gap_item)
        # Bear向下实体缺口：当前实体上限 < 前一根实体下限
        elif curr_entity_high < prev_entity_low - GAP_EPS:
            gap_low = round(curr_entity_high, GAP_PRICE_DECIMAL)
            gap_high = round(prev_entity_low, GAP_PRICE_DECIMAL)
            gap_amplitude = abs(gap_high - gap_low)
            if gap_amplitude < GAP_MIN_GAP_TICK:
                continue
            gap_item: GapItem = {
                "gap_id": gap_id,
                "gap_type": "BREAKOUT_GAP",
                "direction": "bear",
                "gap_low": gap_low,
                "gap_high": gap_high,
                "create_k_ts": curr["t_end_ms"],
                "close_k_ts_list": [curr["t_end_ms"]],
                "fill_status": "OPEN",
                "fill_k_ts": None,
                "keep_open_k_num": 1,
                "anchor_k_note": "bear向下实体突破缺口"
            }
            gaps.append(gap_item)
    # 更新每一条缺口的回补状态，遍历后续所有K
    for gap in gaps:
        if gap["fill_status"] == "FULL_ENTITY_FILL":
            continue
        g_low = gap["gap_low"]
        g_high = gap["gap_high"]
        dir_gap = gap["direction"]
        create_idx = next(idx for idx, k in enumerate(klines) if k["t_end_ms"] == gap["create_k_ts"])
        for j in range(create_idx + 1, len(klines)):
            k = klines[j]
            k_ent_low = round(min(k["open"], k["close"]), GAP_PRICE_DECIMAL)
            k_ent_high = round(max(k["open"], k["close"]), GAP_PRICE_DECIMAL)
            gap["close_k_ts_list"].append(k["t_end_ms"])
            gap["keep_open_k_num"] = j - create_idx
            if gap["fill_status"] == "FULL_ENTITY_FILL":
                break
            if dir_gap == "bull":
                # Bull缺口实体回补条件：K实体低点 <= gap_low
                if k_ent_low <= g_low + GAP_EPS:
                    gap["fill_status"] = "FULL_ENTITY_FILL"
                    gap["fill_k_ts"] = k["t_end_ms"]
                    break
                elif k["low"] <= g_low + GAP_EPS:
                    # 仅下影线刺入，PARTIAL_FILL，不改变状态为FULL
                    gap["fill_status"] = "PARTIAL_FILL"
            elif dir_gap == "bear":
                # Bear缺口实体回补条件：K实体高点 >= gap_high
                if k_ent_high >= g_high - GAP_EPS:
                    gap["fill_status"] = "FULL_ENTITY_FILL"
                    gap["fill_k_ts"] = k["t_end_ms"]
                    break
                elif k["high"] >= g_high - GAP_EPS:
                    gap["fill_status"] = "PARTIAL_FILL"
    return gaps


def calc_mm_abcd(klines: List[Dict[str, Any]], a_idx: int, b_idx: int, c_idx: int, mode: str = "entity") -> float:
    """
    AB=CD MM测量；mode=entity实体口径 / mode=wick引线(high/low)口径
    返回D目标价格；异常抛KlineCalcError
    """
    n = len(klines)
    if not (0 <= a_idx < b_idx < c_idx < n):
        raise KlineCalcError("AB=CD索引越界")
    if mode == "entity":
        a_p = klines[a_idx]["close"]
        b_p = klines[b_idx]["close"]
        c_p = klines[c_idx]["close"]
    else:
        a_p = klines[a_idx]["high"]
        b_p = klines[b_idx]["low"]
        c_p = klines[c_idx]["high"]
    leg_ab = b_p - a_p
    d_target = c_p + leg_ab
    if not np.isfinite(d_target):
        raise KlineCalcError("AB=CD产出NaN")
    return d_target






@dataclass
class SwingPoint:
    """波段摆动点结构体，统一引线/实体两种模式"""
    idx: int
    t_end_ms: int
    price: float
    point_type: str # "high" / "low"
    src_mode: str    # "wick"引线(high/low) / "entity"实体(open/close)

def extract_valid_swing_points(
    klines: List[Dict[str, Any]],
    lookback:int,
    min_leg_bar:int,
    min_amp_ratio:float,
    mode:str="wick",
    swing_window:int=5
)->Tuple[List[SwingPoint], List[SwingPoint]]:
    """
    提取有效摆动高低点，过滤毛刺微型波段；用于通道拟合、AB=CD、MTR识别

    ✅【FIX-H 已修复·2026-09-15】原实现存在"互相等待对方先有值"的死锁：
          swing_high 只在 len(swing_low)>0 时 append；
          swing_low  只在 len(swing_high)>0 时 append。
          → 两个列表互相等待，谁也迈不出第一步，函数【恒返回 ([], [])】。
    实测（修复前）：200根干净锯齿波 → 0/0 个；真实 PAXGUST 4h 抽样 → 243/243 全空。
    连锁后果（修复前）：fit_swing_channel / detect_abcd_mm 恒 invalid、
          detect_mtr_raw 从不被执行 → Setup4/6/7 三类信号永不产生，
          mm_aligned(+12) / channel_trendline_aligned(+10) / mtr_active_derived(+15)
          共 37 分权重永久不可达。
    修复后仍需 CHANNEL_FIT_LOOKBACK ≥ 150 才凑得够 CHANNEL_MIN_SWING_NUM=6
    （实测通道可用率 4.7% → 91.5%），故 FIX-I 已把该参数放宽到 200。

    :param klines:闭合K线列表
    :param lookback:回溯窗口
    :param min_leg_bar:单条波段最少K线根数
    :param min_amp_ratio:波段最小幅度相对价格比例，过滤噪音
    :param mode: wick引线 / entity实体
    :param swing_window:拐点左右回看窗口，找局部极值
    :return: (swing_high_points, swing_low_points)
    """
    n = len(klines)
    if n < lookback + 2*min_leg_bar:
        raise KlineCalcError("提取摆动点样本数量不足")
    end_idx = n
    start_idx = max(0, n - lookback)
    highs = np.array([k["high"] for k in klines])
    lows = np.array([k["low"] for k in klines])
    closes = np.array([k["close"] for k in klines])

    swing_high:List[SwingPoint]=[]
    swing_low:List[SwingPoint]=[]

    for i in range(start_idx+swing_window, end_idx-swing_window):
        if mode == "wick":
            p_high = highs[i]
            p_low = lows[i]
        elif mode == "entity":
            p_high = max(klines[i]["open"], klines[i]["close"])
            p_low = min(klines[i]["open"], klines[i]["close"])
        else:
            raise KlineCalcError("摆动点mode只能是 wick / entity")

        # 局部高点
        is_local_high = True
        for d in range(-swing_window, swing_window+1):
            if d == 0: continue
            if mode == "wick" and highs[i+d] > p_high + 1e-6:
                is_local_high=False
                break
            if mode == "entity" and max(klines[i+d]["open"],klines[i+d]["close"]) > p_high+1e-6:
                is_local_high=False
                break
        if is_local_high:
            # 校验波段幅度：和前一个低点做振幅过滤
            # 【FIX-H｜2026-09-15 修复死锁】原实现为 `if len(swing_low)>0:`，
            # 而低点分支又要求 `len(swing_high)>0` → 两列表互为前提，永远同时为空
            # （实测：合成锯齿波 0 个、真实 4h 抽样 243/243 全空）。
            # 修法：无对手方基准时【无条件】作为首个基准入列，之后才做振幅过滤。
            if len(swing_low) == 0:
                swing_high.append(SwingPoint(idx=i,t_end_ms=klines[i]["t_end_ms"],price=p_high,point_type="high",src_mode=mode))
            else:
                prev_low_p = swing_low[-1].price
                amp = abs(p_high - prev_low_p)/ (prev_low_p +1e-8)
                if amp >= min_amp_ratio:
                    swing_high.append(SwingPoint(idx=i,t_end_ms=klines[i]["t_end_ms"],price=p_high,point_type="high",src_mode=mode))

        # 局部低点
        is_local_low = True
        for d in range(-swing_window, swing_window+1):
            if d ==0: continue
            if mode == "wick" and lows[i+d] < p_low-1e-6:
                is_local_low=False
                break
            if mode == "entity" and min(klines[i+d]["open"],klines[i+d]["close"]) < p_low-1e-6:
                is_local_low=False
                break
        if is_local_low:
            # 【FIX-H】同上：首个低点无条件入列，破除与 swing_high 的互为前提
            if len(swing_high) == 0:
                swing_low.append(SwingPoint(idx=i,t_end_ms=klines[i]["t_end_ms"],price=p_low,point_type="low",src_mode=mode))
            else:
                prev_high_p = swing_high[-1].price
                amp = abs(p_low-prev_high_p)/(prev_high_p+1e-8)
                if amp >= min_amp_ratio:
                    swing_low.append(SwingPoint(idx=i,t_end_ms=klines[i]["t_end_ms"],price=p_low,point_type="low",src_mode=mode))

    # 进一步过滤：相邻两个摆动点间隔K线不能小于min_leg_bar
    def filter_min_gap(points:List[SwingPoint])->List[SwingPoint]:
        out=[]
        pre_idx = -9999
        for pt in points:
            if pt.idx - pre_idx >= min_leg_bar:
                out.append(pt)
                pre_idx = pt.idx
        return out
    swing_high = filter_min_gap(swing_high)
    swing_low = filter_min_gap(swing_low)
    return swing_high, swing_low

@dataclass
class ChannelFitResult:
    """通道拟合返回结构体"""
    valid: bool
    slope_upper: float
    slope_lower: float
    intercept_upper: float
    intercept_lower: float
    channel_type: str # "UP_TREND_CHANNEL"/"DOWN_TREND_CHANNEL"/"SIDEWAYS_RANGE"
    upper_band: np.ndarray
    lower_band: np.ndarray
    fit_x: np.ndarray
    residual_upper: float
    residual_lower: float

def fit_swing_channel(
    klines:List[Dict[str,Any]],
    lookback:int=CHANNEL_FIT_LOOKBACK,
    min_swing_count:int=CHANNEL_MIN_SWING_NUM,
    mode:str="wick"
)->ChannelFitResult:
    """
    最小二乘拟合波段高低点通道（上轨用高点序列，下轨用低点序列）
    返回通道上下轨每根K线对应的预测价格、斜率、通道类型
    通道类型：上升趋势通道 / 下降趋势通道 / 水平震荡通道
    """
    swing_high, swing_low = extract_valid_swing_points(
        klines,lookback=lookback,
        min_leg_bar=MM_MIN_LEG_BAR_COUNT,
        min_amp_ratio=MM_MIN_LEG_AMPLITUDE_RATIO,
        mode=mode
    )
    if len(swing_high) < min_swing_count or len(swing_low) < min_swing_count:
        return ChannelFitResult(valid=False,slope_upper=0,slope_lower=0,
                                intercept_upper=0,intercept_lower=0,channel_type="INVALID",
                                upper_band=np.array([]),lower_band=np.array([]),fit_x=np.array([]),
                                residual_upper=0,residual_lower=0)
    # 构造x：K线索引作为自变量
    x_high = np.array([p.idx for p in swing_high],dtype=np.float64)
    y_high = np.array([p.price for p in swing_high],dtype=np.float64)
    x_low = np.array([p.idx for p in swing_low],dtype=np.float64)
    y_low = np.array([p.price for p in swing_low],dtype=np.float64)

    # 最小二乘 y = k*x + b
    def lstsq_fit(x:np.ndarray,y:np.ndarray)->Tuple[float,float,float]:
        if len(x)<2:
            raise KlineCalcError("最小二乘拟合样本不足")
        x_mean = np.mean(x)
        y_mean = np.mean(y)
        var_x = np.sum((x-x_mean)**2)
        if abs(var_x) <1e-10:
            raise KlineCalcError("拟合X方差接近0，无法拟合通道")
        slope = np.sum((x-x_mean)*(y-y_mean)) / var_x
        intercept = y_mean - slope * x_mean
        y_pred = slope*x + intercept
        residual = np.mean(np.abs(y-y_pred))
        return slope,intercept,residual

    slope_upper,intercept_upper,res_upper = lstsq_fit(x_high,y_high)
    slope_lower,intercept_lower,res_lower = lstsq_fit(x_low,y_low)

    n = len(klines)
    fit_x_all = np.arange(0,n,dtype=np.float64)
    upper_band_all = slope_upper * fit_x_all + intercept_upper
    lower_band_all = slope_lower * fit_x_all + intercept_lower

    # 判断通道类型
    avg_slope = (slope_upper + slope_lower)/2.0
    if avg_slope > CHANNEL_SLOPE_EPS:
        c_type = "UP_TREND_CHANNEL"
    elif avg_slope < -CHANNEL_SLOPE_EPS:
        c_type = "DOWN_TREND_CHANNEL"
    else:
        c_type = "SIDEWAYS_RANGE"

    if not np.all(np.isfinite(upper_band_all)) or not np.all(np.isfinite(lower_band_all)):
        raise KlineCalcError("通道拟合产出NaN/Inf")

    return ChannelFitResult(
        valid=True,
        slope_upper=slope_upper,
        slope_lower=slope_lower,
        intercept_upper=intercept_upper,
        intercept_lower=intercept_lower,
        channel_type=c_type,
        upper_band=upper_band_all,
        lower_band=lower_band_all,
        fit_x=fit_x_all,
        residual_upper=res_upper,
        residual_lower=res_lower
    )

@dataclass
class ABCD_MM_Result:
    """AB=CD MM测量结果结构体"""
    valid: bool
    a_idx:int
    b_idx:int
    c_idx:int
    d_target_price:float
    leg_ab_amp:float
    leg_cd_projected_amp:float
    mode:str # wick / entity
    note:str

def detect_abcd_mm(
    klines:List[Dict[str,Any]],
    lookback:int=160,
    mode:str="wick"
)->ABCD_MM_Result:
    """
    自动识别AB=CD结构，输出D目标价格；对齐视频MM(Measured Move)
    A：波段起点；A-B：第一推动腿；B：第一推动终点；C：回调腿终点；C-D投影=A-B幅度，D为MM目标
    约束：每条腿满足最小K根数、最小振幅；过滤微型毛刺波段
    """
    swing_high, swing_low = extract_valid_swing_points(
        klines,lookback=lookback,
        min_leg_bar=MM_MIN_LEG_BAR_COUNT,
        min_amp_ratio=MM_MIN_LEG_AMPLITUDE_RATIO,
        mode=mode
    )
    all_swings:List[SwingPoint] = []
    all_swings.extend(swing_high)
    all_swings.extend(swing_low)
    all_swings.sort(key=lambda p:p.idx)
    n_swing = len(all_swings)
    if n_swing <4:
        return ABCD_MM_Result(valid=False,a_idx=0,b_idx=0,c_idx=0,d_target_price=0,
                              leg_ab_amp=0,leg_cd_projected_amp=0,mode=mode,note="摆动点数量不足")
    # 滑动遍历寻找A-B-C三段连续摆动
    for i in range(n_swing-3):
        A = all_swings[i]
        B = all_swings[i+1]
        C = all_swings[i+2]
        # 腿K线数量校验
        ab_bar_num = B.idx - A.idx
        bc_bar_num = C.idx - B.idx
        if ab_bar_num < MM_MIN_LEG_BAR_COUNT or bc_bar_num < MM_MIN_LEG_BAR_COUNT:
            continue
        # A-B振幅
        leg_ab_amp = abs(B.price-A.price)
        base_p = A.price if abs(A.price)>1e-6 else 1.0
        if leg_ab_amp / base_p < MM_MIN_LEG_AMPLITUDE_RATIO:
            continue
        # 区分多头AB-CD /空头AB-CD
        if A.point_type == "low" and B.point_type == "high":
            # 多头ABCD：A(low)→B(high)推动；B→C向下回调；C向上投影D
            projected_d = C.price + leg_ab_amp
        elif A.point_type == "high" and B.point_type == "low":
            # 空头ABCD：A(high)→B(low)推动；B→C向上回调；C向下投影D
            projected_d = C.price - leg_ab_amp
        else:
            # 摆动点高低交替不匹配，跳过本组
            continue
        if not np.isfinite(projected_d):
            raise KlineCalcError("AB=CD计算D目标出现NaN")
        return ABCD_MM_Result(
            valid=True,
            a_idx=A.idx,
            b_idx=B.idx,
            c_idx=C.idx,
            d_target_price=projected_d,
            leg_ab_amp=leg_ab_amp,
            leg_cd_projected_amp=leg_ab_amp,
            mode=mode,
            note=f"识别ABCD，Aidx={A.idx},Bidx={B.idx},Cidx={C.idx},D目标={projected_d:.2f}"
        )
    return ABCD_MM_Result(valid=False,a_idx=0,b_idx=0,c_idx=0,d_target_price=0,
                          leg_ab_amp=0,leg_cd_projected_amp=0,mode=mode,note="未找到满足条件AB-CD摆动结构")

def judge_signal_k_quality(k:Dict[str,Any])->Dict[str,bool]:
    """
    信号K质量判定：单根闭合K；返回字典各个布尔标记；PRD V1.1
    return {
        "is_high_quality_trend_k": bool,
        "is_good_reversal_k": bool,
        "is_wire_bar(铁丝网劣质K)": bool,
        "body_ratio":float
    }
    """
    o = k["open"]
    c = k["close"]
    h = k["high"]
    l = k["low"]
    total_range = abs(h-l) + 1e-8
    body = abs(c-o)
    body_ratio = body / total_range
    is_wire_bar = body_ratio < WIRE_BAR_BODY_THRESHOLD
    is_high_quality_trend_k = (not is_wire_bar) and (body_ratio >= SIGNALK_BODY_MIN_RATIO)
    is_good_reversal_k = (not is_wire_bar) and (body_ratio >= SIGNALK_REVERSE_BODY_RATIO)
    return {
        "is_high_quality_trend_k": is_high_quality_trend_k,
        "is_good_reversal_k": is_good_reversal_k,
        "is_wire_bar": is_wire_bar,
        "body_ratio": round(body_ratio,3)
    }

def is_price_in_mm_target_zone(price:float, mm_d_target:float, tolerance_ratio:float=MM_TARGET_TOLERANCE_RATIO)->bool:
    """判断价格是否落在MM-D目标容忍区间内"""
    if abs(mm_d_target) <1e-6:
        return False
    dev = abs(price-mm_d_target) / abs(mm_d_target)
    return dev <= tolerance_ratio

def match_gap_to_mm_target(gap_item:GapItem, mm_d_target:float)->bool:
    """精准MM缺口匹配：缺口区间是否落在MM-D目标容忍范围"""
    gap_mid = (gap_item["gap_low"] + gap_item["gap_high"])/2.0
    return is_price_in_mm_target_zone(gap_mid,mm_d_target,MM_GAP_ALIGN_TOLERANCE_RATIO)

@dataclass
class MTR_Detect_Output:
    """
    MTR自动检测输出结构体

    ⚠️【FIX-G·2026-09-15 回测发现｜P0 地雷已拆】原定义 10 个字段【全部无默认值】，
    而 detect_mtr_raw() 里有 5 处提前返回只传了 detect_success + note：
        · "摆动高低点不足，无法识别MTR次级反向波段"
        · "通道类型不匹配MTR"
        · "主极值之后没有足够次级反向摆动"
        · "没有找到击穿通道的有效次级反向波段"
        · "次级反向之后，未回头测试原有波段极值，不构成完整MTR"
    → 这 5 条路径一旦被走到就抛 TypeError（缺 8 个位置参数）。
    在修复摆动点死锁(FIX-H)之前，通道恒 INVALID，本函数永远在第一行就返回，
    所以这个 TypeError 从未暴露；一旦修好摆动点，它会立刻爆发并【吞掉整轮扫描】。
    修法：给非必填字段加默认值（detect_success 仍是唯一必填项），语义与调用点完全一致。
    """
    detect_success: bool
    mtr_type: Optional[str] = None
    secondary_swing_start_idx: Optional[int] = None
    secondary_swing_end_idx: Optional[int] = None
    test_extreme_price: Optional[float] = None
    test_extreme_idx: Optional[int] = None
    break_channel_flag: bool = False
    has_related_reverse_gap: bool = False
    related_gap_id: Optional[str] = None
    note: str = ""

def detect_mtr_raw(
    klines:List[Dict[str,Any]],
    channel_res:ChannelFitResult,
    gap_list:List[GapItem],
    lookback:int=140
)->MTR_Detect_Output:
    """
    ✅完整MTR原始自动检测逻辑（PRD V1.1）
    MTR = Measured Trend Reversal 趋势反转测试
    判定条件全部量化：
    1. 原有存在有效趋势通道（上升/下降通道，不是SIDEWAYS）
    2. 出现【次级反向波段】：足够K线数量、足够幅度，**实体突破原有通道轨道**
    3. 次级反向运动最好伴随一条反向突破缺口
    4. 次级反向完成之后，行情回头，去测试原有大波段的历史极值（前高 / 前低）
    5. 测试极值不要求100%精准，允许MTR_EXTREME_TEST_TOLERANCE_RATIO价格容差
    返回MTR原始检测结果；检测成功之后，上层业务生成mtr_context写入全局内存g_mtr_context
    """
    if not channel_res.valid or channel_res.channel_type == "SIDEWAYS_RANGE":
        return MTR_Detect_Output(detect_success=False,mtr_type=None,secondary_swing_start_idx=None,
                                 secondary_swing_end_idx=None,test_extreme_price=None,test_extreme_idx=None,
                                 break_channel_flag=False,has_related_reverse_gap=False,related_gap_id=None,
                                 note="通道无效或者是水平震荡通道，不满足MTR前置条件")
    n = len(klines)
    swing_high, swing_low = extract_valid_swing_points(
        klines,lookback=lookback,
        min_leg_bar=MTR_MIN_SECONDARY_SWING_BAR,
        min_amp_ratio=MM_MIN_LEG_AMPLITUDE_RATIO,
        mode="wick"
    )
    if len(swing_high) <3 or len(swing_low)<3:
        return MTR_Detect_Output(detect_success=False,note="摆动高低点不足，无法识别MTR次级反向波段")
    # 根据通道类型，区分MTR类型
    if channel_res.channel_type == "UP_TREND_CHANNEL":
        # 原有大趋势向上；MTR：次级向下反向运动，之后回头测试历史波段高点 → high_mtr(高点降低MTR)
        main_extreme_pt = max(swing_high,key=lambda x:x.price)
        mtr_type = "high_mtr"
        reverse_direction = "bear" #次级反向向下
    elif channel_res.channel_type == "DOWN_TREND_CHANNEL":
        # 原有大趋势向下；MTR：次级向上反向运动，回头测试历史波段低点 → low_mtr(低点抬高MTR)
        main_extreme_pt = min(swing_low,key=lambda x:x.price)
        mtr_type = "low_mtr"
        reverse_direction = "bull" #次级反向向上
    else:
        return MTR_Detect_Output(detect_success=False,note="通道类型不匹配MTR")

    test_extreme_price = main_extreme_pt.price
    test_extreme_idx = main_extreme_pt.idx

    # 寻找次级反向波段：发生在主极值之后
    post_extreme_swings = []
    all_sw = []
    all_sw.extend(swing_high)
    all_sw.extend(swing_low)
    all_sw.sort(key=lambda x:x.idx)
    post_extreme_swings = [s for s in all_sw if s.idx > test_extreme_idx]
    if len(post_extreme_swings) <2:
        return MTR_Detect_Output(detect_success=False,note="主极值之后没有足够次级反向摆动")

    # 遍历主极值之后，识别次级反向波段起点终点
    sec_start_idx = None
    sec_end_idx = None
    break_channel = False
    related_gap_id = None
    has_rev_gap = False

    for i in range(len(post_extreme_swings)-1):
        s1 = post_extreme_swings[i]
        s2 = post_extreme_swings[i+1]
        bar_cnt = s2.idx - s1.idx
        if bar_cnt < MTR_MIN_SECONDARY_SWING_BAR:
            continue
        # 判断这个波段是否是【反向】，并且价格实体击穿原有通道轨道
        # 取s2位置，通道理论轨道价格
        if mtr_type == "high_mtr":
            # 向上趋势，次级向下；取该索引通道下轨
            band_ref = channel_res.lower_band[s2.idx]
            price_ref = s2.price
            if (band_ref - price_ref)/band_ref >= MTR_BREAK_CHANNEL_THRESHOLD:
                break_channel = True
        elif mtr_type == "low_mtr":
            band_ref = channel_res.upper_band[s2.idx]
            price_ref = s2.price
            if (price_ref-band_ref)/band_ref >= MTR_BREAK_CHANNEL_THRESHOLD:
                break_channel = True
        if break_channel:
            sec_start_idx = s1.idx
            sec_end_idx = s2.idx
            # 查找这个次级波段内，是否存在对应方向的反向突破缺口
            for g in gap_list:
                if g["create_k_ts"] >= klines[sec_start_idx]["t_end_ms"] and g["create_k_ts"] <= klines[sec_end_idx]["t_end_ms"]:
                    if g["direction"] == reverse_direction and g["fill_status"] in ("OPEN","PARTIAL_FILL"):
                        has_rev_gap=True
                        related_gap_id = g["gap_id"]
                        break
            break
    if sec_start_idx is None or sec_end_idx is None or not break_channel:
        return MTR_Detect_Output(detect_success=False,note="没有找到击穿通道的有效次级反向波段")

    # 次级反向波段完成之后：后续K线回头去测试原有主极值，允许容差
    after_sec_start = sec_end_idx + 1
    test_hit = False
    for j in range(after_sec_start, min(n-1, after_sec_start+60)):
        k = klines[j]
        if mtr_type == "high_mtr":
            # high_mtr：回头测试历史高点；K的high接近主极值价格
            dev_ratio = abs(k["high"]-test_extreme_price)/test_extreme_price
            if dev_ratio <= MTR_EXTREME_TEST_TOLERANCE_RATIO:
                test_hit = True
                break
        elif mtr_type == "low_mtr":
            dev_ratio = abs(k["low"]-test_extreme_price)/test_extreme_price
            if dev_ratio <= MTR_EXTREME_TEST_TOLERANCE_RATIO:
                test_hit = True
                break
    if not test_hit:
        return MTR_Detect_Output(detect_success=False,note="次级反向之后，未回头测试原有波段极值，不构成完整MTR")

    # ✅全部条件满足：MTR原始检测成功
    return MTR_Detect_Output(
        detect_success=True,
        mtr_type=mtr_type,
        secondary_swing_start_idx=sec_start_idx,
        secondary_swing_end_idx=sec_end_idx,
        test_extreme_price=test_extreme_price,
        test_extreme_idx=test_extreme_idx,
        break_channel_flag=break_channel,
        has_related_reverse_gap=has_rev_gap,
        related_gap_id=related_gap_id,
        note=f"MTR检测成功，类型={mtr_type}；次级波段[{sec_start_idx}~{sec_end_idx}]；测试极值={test_extreme_price:.2f};反向缺口={related_gap_id}"
    )

def build_mtr_context_from_detect(
    detect_out:MTR_Detect_Output,
    create_k_idx:int,
    klines:List[Dict[str,Any]],
    bg_at_mtr:str
)->MtrContext:
    """
    MTR检测成功之后，生成Mtr_context字典，写入全局g_mtr_context内存；
    计算expire_k_ts：往后偏移MTR_VALID_K_BAR_WINDOW根K线的结束时间戳
    """
    create_k_ts = klines[create_k_idx]["t_end_ms"]
    # 计算过期K索引
    expire_idx = min(create_k_idx + MTR_VALID_K_BAR_WINDOW, len(klines)-1)
    expire_k_ts = klines[expire_idx]["t_end_ms"]
    mtr_id = f"mtr_{create_k_ts}_{detect_out.mtr_type}"
    ctx:MtrContext = {
        "mtr_id": mtr_id,
        "create_k_ts": create_k_ts,
        "expire_k_ts": expire_k_ts,
        "bg_at_mtr": bg_at_mtr,
        "mtr_type": detect_out.mtr_type,
        "mtr_test_extreme_price": detect_out.test_extreme_price,
        "related_gap_id": detect_out.related_gap_id,
        "status": "ACTIVE", # ACTIVE / EXPIRED / USED
        "sec_start_idx": detect_out.secondary_swing_start_idx,
        "sec_end_idx": detect_out.secondary_swing_end_idx,
        "note": detect_out.note
    }
    return ctx


# ====================== 【背景分类模块 bg_judge｜V1.2 多源确认计数】 ======================
def judge_background(klines: List[Dict[str, Any]], gap_list: List[GapItem]) -> Dict[str, Any]:
    """
    背景分类器 V1.2（修复 V1.1 致命缺陷）
    ------------------------------------------------------------------
    V1.1 缺陷：候选门槛 STRONG≥30 / WEAK≥20 远超证据可发射量（最多少/弱 20/8 分），
    且证据条数门槛 BG_MIN_EVIDENCE_COUNT=3 超过各背景实际可发射条数（强最多2/弱最多1/
    震荡最多3）。实测连"强上升趋势+21个开放缺口"都返回 CONFUSE → 除极窄 TTR 窗外，
    全周期被 CONFUSE 拦截，导致实盘/回测只有首周零星信号。

    V1.2 方案：改用【多源趋势确认计数】模型，每个确认源（EMA斜率+价格位置 /
    波段HH-HL或LH-LL / 通道拟合斜率 / 未回补突破缺口方向）独立投多/空/震荡票，
    按同向票数分级：
      - 同向(多或空) ≥ BG_STRONG_CONFIRM(3) 票 → STRONG_TREND
      - 同向 = BG_WEAK_CONFIRM(2) 票          → WEAK_TREND
      - 仅震荡 ≥ BG_TTR_CONFIRM(2) 票且无明显方向 → TTR
      - 其它（方向冲突/证据不足）              → CONFUSE
    每个确认源在样本不足时跳过（不投票），避免误判。

    输出契约不变：{bg_result, score_detail, evidence_list}
    """
    evidence: List[str] = []
    n = len(klines)
    closes = np.array([k["close"] for k in klines], dtype=np.float64)

    bull_conf = 0   # 多头趋势确认票
    bear_conf = 0   # 空头趋势确认票
    range_conf = 0  # 震荡确认票

    # ---- 确认源1：EMA20 斜率 + 价格位置 ----
    try:
        ema20 = calc_ema(closes, EMA_PERIOD)
        ema_look = min(EMA_PERIOD * 3, n - 1)
        if ema_look > 0:
            ema_delta = ema20[-1] - ema20[-ema_look]
            ema_slope_ratio = ema_delta / (abs(ema20[-1]) + 1e-8)
            price_above = closes[-1] > ema20[-1]
            price_below = closes[-1] < ema20[-1]
            if ema_slope_ratio > BG_EMA_SLOPE_MIN_RATIO and price_above:
                bull_conf += 1
                evidence.append("上升趋势正向：EMA20上行且价格位于EMA上方")
            elif ema_slope_ratio < -BG_EMA_SLOPE_MIN_RATIO and price_below:
                bear_conf += 1
                evidence.append("下降趋势正向：EMA20下行且价格位于EMA下方")
            else:
                range_conf += 1
                if abs(ema_slope_ratio) <= BG_EMA_SLOPE_MIN_RATIO:
                    evidence.append("震荡正向：EMA20走平，无明显趋势斜率")
                else:
                    evidence.append("震荡正向：价格与EMA方向背离，趋势未确认")
    except KlineCalcError:
        pass  # EMA 样本不足，跳过该确认源

    # ---- 确认源2：波段高低点 HH/HL 或 LH/LL ----
    try:
        high_idx, low_idx = get_band_high_low(klines, lookback=80)
        if len(high_idx) >= 3 and len(low_idx) >= 3:
            hh_seq = [klines[i]["high"] for i in high_idx[-3:]]
            ll_seq = [klines[i]["low"] for i in low_idx[-3:]]
            hh_hl = hh_seq[-1] > hh_seq[-2] > hh_seq[-3] and ll_seq[-1] > ll_seq[-2] > ll_seq[-3]
            ll_lh = hh_seq[-1] < hh_seq[-2] < hh_seq[-3] and ll_seq[-1] < ll_seq[-2] < ll_seq[-3]
            if hh_hl:
                bull_conf += 1
                evidence.append("上升趋势正向：近3个波段呈HH-HL结构")
            elif ll_lh:
                bear_conf += 1
                evidence.append("下降趋势正向：近3个波段呈LH-LL结构")
            else:
                range_conf += 1
                evidence.append("震荡正向：波段高低点无持续方向（来回交替）")
        else:
            range_conf += 1
            evidence.append("震荡正向：波段高低点样本不足，无法确认方向")
    except KlineCalcError:
        pass

    # ---- 确认源3：最小二乘通道拟合斜率 ----
    try:
        chan = fit_swing_channel(klines, mode="wick")
    except KlineCalcError:
        chan = None
    if chan is not None and chan.valid:
        avg_slope = (chan.slope_upper + chan.slope_lower) / 2.0
        if avg_slope > CHANNEL_SLOPE_EPS:
            bull_conf += 1
            evidence.append("上升趋势正向：最小二乘通道拟合上行")
        elif avg_slope < -CHANNEL_SLOPE_EPS:
            bear_conf += 1
            evidence.append("下降趋势正向：最小二乘通道拟合下行")
        else:
            range_conf += 1
            evidence.append("震荡正向：最小二乘通道拟合水平")
    # 通道无效（样本/摆动点不足）不投票，避免误判为震荡

    # ---- 确认源4：缺口行为 ----
    open_bull_gaps = [g for g in gap_list if g["direction"] == "bull"
                      and g["fill_status"] in ("OPEN", "PARTIAL_FILL") and g["keep_open_k_num"] >= 2]
    open_bear_gaps = [g for g in gap_list if g["direction"] == "bear"
                      and g["fill_status"] in ("OPEN", "PARTIAL_FILL") and g["keep_open_k_num"] >= 2]
    quick_fill_all = len([g for g in gap_list if g["fill_status"] == "FULL_ENTITY_FILL" and g["keep_open_k_num"] <= 3])
    if len(open_bull_gaps) >= 1:
        bull_conf += 1
        evidence.append("上升趋势正向：存在≥2根K未实体回补的向上突破缺口")
    if len(open_bear_gaps) >= 1:
        bear_conf += 1
        evidence.append("下降趋势正向：存在≥2根K未实体回补的向下突破缺口")
    if quick_fill_all >= 2:
        range_conf += 1
        evidence.append("震荡正向：多条缺口3根K内快速实体回补")

    # ---- 确认源5：铁丝网K占比（震荡特征） ----
    lookback_n = min(80, n)
    if lookback_n > 0:
        cross_count = 0
        sub_k = klines[-lookback_n:]
        for k in sub_k:
            body = abs(k["close"] - k["open"])
            total_range = abs(k["high"] - k["low"])
            if total_range < 1e-6 or body / (total_range + 1e-8) < 0.2:
                cross_count += 1
        wire_ratio = cross_count / lookback_n
        if wire_ratio > 0.35:
            range_conf += 1
            evidence.append(f"震荡正向：铁丝网K占比{wire_ratio:.2f}，K线大量重叠震荡")

    # ---- 分类判定（方向冲突降级为 CONFUSE） ----
    # 【FIX-K·2026-09-15 回测发现｜方向信息丢失】原实现把"上涨强趋势"与"下跌强趋势"
    # 合并为同一个 STRONG_TREND 标签，且返回值里【没有任何方向字段】，
    # 而 M2B(做多)/M2S(做空) 的 cond_hit 都写的是 bg_type=="STRONG_TREND"。
    # 后果：下跌趋势里 M2B(做多) 照样拿满趋势加分，上涨趋势里 M2S(做空) 也照样满分
    #       → 两个方向的候选数完全相等（回测 4h 174:174 / 1h 689:689 / 30m 2087:2087），
    #         "顺势 Setup" 实际不知道自己顺的是哪个势。
    # 修法：新增 bg_direction 输出（bull / bear / None），Setup1/Setup2 据此校验方向。
    total_bull = bull_conf
    total_bear = bear_conf
    total_range = range_conf

    if total_bull >= BG_STRONG_CONFIRM and total_bear == 0:
        bg_result, bg_direction = "STRONG_TREND", "bull"
    elif total_bear >= BG_STRONG_CONFIRM and total_bull == 0:
        bg_result, bg_direction = "STRONG_TREND", "bear"
    elif total_bull == BG_WEAK_CONFIRM and total_bear == 0:
        bg_result, bg_direction = "WEAK_TREND", "bull"
    elif total_bear == BG_WEAK_CONFIRM and total_bull == 0:
        bg_result, bg_direction = "WEAK_TREND", "bear"
    elif total_range >= BG_TTR_CONFIRM and total_range > total_bull + total_bear:
        # 【FIX-M·2026-09-15 回测连带修复】原判定为 `total_bull == 0 and total_bear == 0`
        # （要求"五个投票源里一个方向票都不能有"）。在通道拟合恒 INVALID 的年代，
        # 确认源3（通道斜率）从不投票，实际只有 3 个源，尚能偶尔凑出"零方向票"；
        # 修复摆动点(FIX-H)后通道恢复投票，方向票几乎每轮都有 → TTR 被系统性饿死：
        #   实测 TTR 轮数 1h 955→12、4h 27→0、30m 1289→376，Setup5 名存实亡。
        # 改为"震荡票严格多于方向票之和"，即真正的"震荡占优"，而不是"完全无方向"。
        # 注意：本分支位于 WEAK_TREND 之后，故 (bull=2,bear=0) 这类仍会先被判为弱趋势，不受影响。
        bg_result, bg_direction = "TTR", None
    else:
        bg_result, bg_direction = "CONFUSE", None

    score_detail = {
        "STRONG_TREND": total_bull if (total_bull >= BG_STRONG_CONFIRM and total_bear == 0)
                        else (total_bear if (total_bear >= BG_STRONG_CONFIRM and total_bull == 0) else 0),
        "WEAK_TREND": 1 if ((total_bull == BG_WEAK_CONFIRM or total_bear == BG_WEAK_CONFIRM) and total_bull * total_bear == 0) else 0,
        "TTR": total_range
    }
    return {
        "bg_result": bg_result,
        "bg_direction": bg_direction,   # 【FIX-K】bull / bear / None
        "score_detail": score_detail,
        "evidence_list": evidence
    }

# ====================== 【Setup识别模块 setup_detector｜PRD V1.1全部Setup+MTR生命周期】 ======================
def detect_setups(
    klines: List[Dict[str, Any]],
    gap_list: List[GapItem],
    bg_info: Dict[str, Any],
    mtr_ctx_list: List[MtrContext],
    ema20_arr: np.ndarray,
    interval: str = ""
) -> List[Dict[str, Any]]:
    """
    ✅【完整补齐版本｜PRD-V1.1】
    全部7大类Setup完整量化实现：
    Setup1: M2B 均线二次做多 High2
    Setup2: M2S 均线二次做空 Low2
    Setup3: 二次回补缺口失败顺势Setup
    Setup4: MTR原始自动检测 → MTR观察预警；窗口内满足条件生成 MTR_DERIVED_REVERSAL（可开仓）
    Setup5: TTR震荡边界反转Setup
    Setup6: 精准MM缺口到达反转 Setup（新增补齐）
    Setup7: 突破缺口 + 通道线叠加 Stop突破入场Setup（新增补齐）

    返回：候选Setup列表（未去重；内部完成硬性过滤 + Setup加权打分）
    """
    candidates: List[Dict[str, Any]] = []
    bg_type = bg_info["bg_result"]
    bg_dir = bg_info.get("bg_direction")   # 【FIX-K】"bull" / "bear" / None（TTR/CONFUSE 为 None）
    n = len(klines)
    if n < 80:
        return candidates
    last_k = klines[-1]
    last_k_idx = n-1
    last_k_ts = last_k["t_end_ms"]
    last_close = last_k["close"]
    last_ema20 = float(ema20_arr[-1])

    # -------- 【FIX-B 1/3】前置：信号K质量 + TTR区间位置（用于补活惩罚权重）--------
    _sig_k = judge_signal_k_quality(last_k)
    _in_ttr_middle = False
    if bg_type == "TTR":
        _h_all = [k["high"] for k in klines[-120:]]
        _l_all = [k["low"] for k in klines[-120:]]
        _ttr_h, _ttr_l = max(_h_all), min(_l_all)
        _zh = _ttr_h - _ttr_l
        # 价格落在区间中轴带（既非上沿也非下沿）→ 震荡中间区，视为无优势区
        _in_ttr_middle = (_ttr_l + _zh * TTR_BOUNDARY_RATIO) < last_close < (_ttr_h - _zh * TTR_BOUNDARY_RATIO)

    def _penalty_flags() -> Dict[str, bool]:
        """【FIX-B】补活原先从未被赋值的惩罚权重；开关关闭时全部返回False（等同旧行为）"""
        if not ENABLE_PENALTY_WEIGHT_ACTIVATION:
            return {"signal_k_poor_quality": False, "ttr_middle_zone": False}
        return {
            # 铁丝网劣质信号K（实体占比 < WIRE_BAR_BODY_THRESHOLD）→ -15分
            "signal_k_poor_quality": bool(_sig_k["is_wire_bar"]),
            # TTR 震荡中轴区，无边界优势 → -30分，可直接压到推送阈值之下
            "ttr_middle_zone": bool(_in_ttr_middle),
        }

    # ========== 【每轮前置计算：通道拟合 + AB-CD MM两套口径 + MTR原始检测】 ==========
    # 1 引线(wick)口径通道拟合
    chan_wick = fit_swing_channel(klines,mode="wick")
    # 2 实体(entity)口径通道拟合
    chan_entity = fit_swing_channel(klines,mode="entity")
    # 3 AB-CD MM 引线口径
    mm_wick = detect_abcd_mm(klines,mode="wick")
    # 4 AB-CD MM 实体口径
    mm_entity = detect_abcd_mm(klines,mode="entity")
    mm_list:List[ABCD_MM_Result]=[]
    if mm_wick.valid: mm_list.append(mm_wick)
    if mm_entity.valid: mm_list.append(mm_entity)

    # 5 ✅MTR原始自动检测；检测成功生成MTR上下文，追加进mtr_ctx_list
    if chan_wick.valid and chan_wick.channel_type not in ("SIDEWAYS_RANGE","INVALID"):
        mtr_raw_out = detect_mtr_raw(klines,channel_res=chan_wick,gap_list=gap_list)
        if mtr_raw_out.detect_success:
            new_mtr_ctx = build_mtr_context_from_detect(
                detect_out=mtr_raw_out,
                create_k_idx=last_k_idx,
                klines=klines,
                bg_at_mtr=bg_type
            )
            mtr_ctx_list.append(new_mtr_ctx)
            logger.info(f"[detect_setups] 新检测到MTR生成上下文，mtr_id={new_mtr_ctx['mtr_id']}，type={new_mtr_ctx['mtr_type']}")

    # -------- 辅助内部小函数：生成setup打分 --------
    def calc_setup_score(cond_hit: Dict[str, bool]) -> Tuple[int, Dict[str, int]]:
        total = 0
        detail = {}
        for k, v in cond_hit.items():
            if k not in SETUP_SCORE_WEIGHT:
                continue
            w = SETUP_SCORE_WEIGHT[k]
            if v:
                total += w
                detail[k] = w
        return total, detail

    def build_setup_base(setup_name:str,direction:str,trigger_k_ts:int,trigger_price:float,
                         bg_t:str,cond_text:List[str],target_price:Optional[float],risk_note:str,
                         score_dict:Optional[Dict[str,bool]]=None,observe_only:bool=False,
                         dedup_key:Optional[str]=None,dedup_expire_bars:Optional[int]=None,
                         cond_hit:Optional[Dict[str,bool]]=None)->Dict[str,Any]:
        # -------- 【FIX-E｜参数名不一致导致整条扫描链路崩溃】--------
        # 本函数第9参数名为 score_dict，但 MTR观察 / MTR衍生反转 / MM强趋势观察 / MM缺口反转
        # 共 4 处调用用的是关键字 cond_hit=… → 直接 TypeError:
        #   "build_setup_base() got an unexpected keyword argument 'cond_hit'"
        # 由于 scan() 有兜底 except，异常被吞成"未知兜底异常，本轮扫描跳过"，
        # 结果：只要该周期存在 ACTIVE 的 MTR，该周期【当轮所有信号全部丢失】。
        # 兼容修法：两个名字都收，语义等价（都是打分布尔字典）。
        if cond_hit is not None:
            score_dict = cond_hit
        if score_dict is None:
            score_dict = {}
        # -------- 【FIX-B 2/3】统一注入惩罚权重：单一注入点，避免逐个Setup漏加 --------
        # observe_only（MTR观察/MM强趋势观察）不参与分数门槛，无需注入。
        if not observe_only:
            _pen = _penalty_flags()
            if any(_pen.values()):
                score_dict = {**score_dict, **_pen}
        sc,sc_detail = calc_setup_score(score_dict)
        out = {
            "setup_name": setup_name,
            "direction": direction,
            "trigger_k_ts": trigger_k_ts,
            "trigger_price": trigger_price,
            "bg_type": bg_t,
            "cond_list": cond_text,
            "target_price": target_price,
            "score": sc,
            "score_detail": sc_detail,
            "risk_note": risk_note,
            "observe_only": observe_only
        }
        # 【FIX-D】可选：按"事件维度"去重（如同一条MTR在整个有效窗口内只提醒一次），
        # 避免 trigger_k_ts 随每根K变化导致同一事件被反复推送。
        if dedup_key:
            out["dedup_key"] = dedup_key
            out["dedup_expire_bars"] = dedup_expire_bars
        return out

    # ========== Setup1 M2B 均线二次做多 High2 ==========
    # 【FIX-K】必须"趋势方向=多头"才做多；原实现只要背景是趋势（不论涨跌）就允许做多
    if bg_type in ("STRONG_TREND", "WEAK_TREND") and bg_dir == "bull":
        price_dev = abs(last_close - last_ema20) / (last_ema20 + 1e-8)
        if price_dev <= M2_EMA_DISTANCE_RATIO:
            bull_open_gaps = [g for g in gap_list if g["direction"] == "bull" and g["fill_status"] != "FULL_ENTITY_FILL"]
            two_leg_ok = len(bull_open_gaps) >= 1
            cond_hit = {
                "bg_strong_trend": bg_type == "STRONG_TREND",
                "bg_weak_trend": bg_type == "WEAK_TREND",
                "two_leg_complete": two_leg_ok,
                "breakout_gap_aligned": len(bull_open_gaps)>=1,
                "high_quality_signal_k": judge_signal_k_quality(last_k)["is_high_quality_trend_k"]
            }
            cond_text = [
                f"背景:{bg_type}",
                f"价格距离EMA20偏差:{price_dev:.4f}在阈值内",
                f"两腿回调High2结构判定:{two_leg_ok}",
                f"叠加未实体回补多头突破缺口:{len(bull_open_gaps)>0}"
            ]
            s = build_setup_base("M2B_均线二次做多","long",last_k_ts,last_close,bg_type,
                                 cond_text,None,"M2B二次入场，需要观察后续K线跟随，跟随变差则Setup失效",cond_hit)
            candidates.append(s)

    # ========== Setup2 M2S 均线二次做空 Low2 ==========
    # 【FIX-K】必须"趋势方向=空头"才做空；原实现只要背景是趋势（不论涨跌）就允许做空
    if bg_type in ("STRONG_TREND", "WEAK_TREND") and bg_dir == "bear":
        price_dev = abs(last_close - last_ema20) / (last_ema20 + 1e-8)
        if price_dev <= M2_EMA_DISTANCE_RATIO:
            bear_open_gaps = [g for g in gap_list if g["direction"] == "bear" and g["fill_status"] != "FULL_ENTITY_FILL"]
            two_leg_ok = len(bear_open_gaps) >=1
            cond_hit = {
                "bg_strong_trend": bg_type == "STRONG_TREND",
                "bg_weak_trend": bg_type == "WEAK_TREND",
                "two_leg_complete": two_leg_ok,
                "breakout_gap_aligned": len(bear_open_gaps)>=1,
                "high_quality_signal_k": judge_signal_k_quality(last_k)["is_high_quality_trend_k"]
            }
            cond_text = [
                f"背景:{bg_type}",
                f"价格距离EMA20偏差:{price_dev:.4f}在阈值内",
                f"两腿回调Low2结构判定:{two_leg_ok}",
                f"叠加未实体回补空头突破缺口:{len(bear_open_gaps)>0}"
            ]
            s = build_setup_base("M2S_均线二次做空","short",last_k_ts,last_close,bg_type,
                                 cond_text,None,"M2S二次入场，观察后续K线跟随，跟随变差Setup失效",cond_hit)
            candidates.append(s)

    # ========== Setup3：二次回补缺口失败顺势Setup ==========
    # 【FIX-B 3/3】缺口时效窗口（治本）：原逻辑只有 >=3 无上限，
    # 一条永不回补的旧缺口会在每根K上反复满足条件 → 无限复读。
    # 现在要求 keep_open_k_num 落在 [GAP_FAIL_MIN_AGE_BAR, GAP_FAIL_MAX_AGE_BAR]。
    _stale_gap_cnt = 0
    for g in gap_list:
        if g["fill_status"] not in ("OPEN", "PARTIAL_FILL"):
            continue
        if g["keep_open_k_num"] < GAP_FAIL_MIN_AGE_BAR:
            continue
        if g["keep_open_k_num"] > GAP_FAIL_MAX_AGE_BAR:
            _stale_gap_cnt += 1
            continue
        cond_hit = {
            "gap_fail_twice": True,
            "breakout_gap_aligned": True,
            # 【FIX-K】"顺势"要求缺口方向与背景趋势方向一致（g["direction"] 与 bg_dir 同为 bull/bear）
            "bg_strong_trend": bg_type == "STRONG_TREND" and bg_dir == g["direction"],
            "bg_weak_trend": bg_type == "WEAK_TREND" and bg_dir == g["direction"],
            "high_quality_signal_k": _sig_k["is_high_quality_trend_k"]
        }
        dir_text = "多头" if g["direction"] == "bull" else "空头"
        cond_text = [
            f"背景:{bg_type}",
            f"{dir_text}突破缺口 gap_id={g['gap_id']}",
            f"缺口创建K时间:{fmt_ts_show(g['create_k_ts'])}",
            f"缺口状态:{g['fill_status']}，敞开K线数:{g['keep_open_k_num']}，多次引线试探未实体全回补",
            f"缺口时效:{g['keep_open_k_num']}/{GAP_FAIL_MAX_AGE_BAR}根K内（超龄缺口已过滤）"
        ]
        risk = "二次缺口回补失败顺势Setup，若后续出现实体回补缺口则信号直接失效"
        sig_dir = "long" if g["direction"] == "bull" else "short"
        s = build_setup_base("GAP_FAIL_TWICE_二次回补缺口失败顺势",sig_dir,last_k_ts,last_close,
                             bg_type,cond_text,None,risk,cond_hit)
        s["gap_id"] = g["gap_id"]          # 【FIX-B】结构化带出 gap_id，供去重按缺口维度收敛
        candidates.append(s)
    if _stale_gap_cnt > 0:
        logger.info(f"[Setup3] 过滤超龄缺口(GAP_FAIL_MAX_AGE_BAR={GAP_FAIL_MAX_AGE_BAR}) {_stale_gap_cnt} 条，不再复读")

    # ========== Setup4：MTR模块｜观察预警 + 窗口内衍生反转可开仓Setup ==========
    # 先更新全部MTR上下文状态：根据当前最新K时间戳，ACTIVE过期置EXPIRED
    for mtr in mtr_ctx_list:
        if mtr["status"] == "ACTIVE" and last_k_ts > mtr["expire_k_ts"]:
            mtr["status"] = "EXPIRED"
            logger.info(f"MTR {mtr['mtr_id']} 窗口过期（过期时间 {fmt_ts_show(mtr['expire_k_ts'])}），状态置EXPIRED")

    active_mtrs = [m for m in mtr_ctx_list if m["status"] == "ACTIVE"]
    for mtr in active_mtrs:
        # 【FIX-C】时间口径可读化：原实现把 create_k_ts / expire_k_ts / 当前Kts 原样输出，
        # 告警里全是 1789372800000 这类毫秒值，还得自己心算换算。
        # 现统一渲染为"北京 + UTC"双时区，并给出窗口剩余根数。
        _bw = bars_between(last_k_ts, mtr["expire_k_ts"], interval)
        _remain_text = (f"，剩余有效窗口 {_bw} 根{interval}K" if _bw is not None else "")
        # ① MTR【观察预警】observe_only，不可以开仓
        cond_text_mtr = [
            f"MTR id={mtr['mtr_id']}",
            f"MTR类型:{MTR_TYPE_CN.get(mtr['mtr_type'], mtr['mtr_type'])}",
            f"创建K时间:{fmt_ts_show(mtr['create_k_ts'])}",
            f"测试波段极值价格:{mtr['mtr_test_extreme_price']:.2f}",
            f"窗口过期时间:{fmt_ts_show(mtr['expire_k_ts'])}{_remain_text}",
            f"当前K时间:{fmt_ts_show(last_k_ts)}",
        ]
        obs_sig = build_setup_base(
            setup_name="MTR_OBSERVE_MTR趋势反转测试观察预警",
            direction="observe",
            trigger_k_ts=last_k_ts,
            trigger_price=last_close,
            bg_t=mtr["bg_at_mtr"],
            cond_text=cond_text_mtr,
            target_price=None,
            risk_note="⚠️MTR仅反转测试观察，**不可直接开仓！需要等待窗口内二次入场确认Setup**",
            cond_hit={},
            observe_only=True,
            # 【FIX-D】按 MTR 事件维度去重：同一条MTR在整个有效窗口内只提醒一次。
            # 否则 trigger_k_ts 每根新K都变 → 观察预警在窗口内被重推 MTR_VALID_K_BAR_WINDOW 次。
            dedup_key=f"mtr_id:{mtr['mtr_id']}",
            dedup_expire_bars=MTR_VALID_K_BAR_WINDOW,
        )
        candidates.append(obs_sig)

        # ② MTR窗口内寻找反转信号K，满足条件生成 MTR_DERIVED_REVERSAL 可开仓Setup
        sig_k_quality = judge_signal_k_quality(last_k)
        # MTR衍生反转前置条件：当前K是高质量反转K；背景不能是强趋势（强趋势优先顺势，禁止直接猜反转）
        if sig_k_quality["is_good_reversal_k"] and bg_type in ("WEAK_TREND","TTR"):
            cond_hit_derived = {
                "mtr_active_derived": True,
                "bg_weak_trend": bg_type == "WEAK_TREND",
                "bg_ttr": bg_type == "TTR",
                "high_quality_signal_k": sig_k_quality["is_good_reversal_k"]
            }
            # 方向：high_mtr →做空；low_mtr →做多
            der_dir = "short" if mtr["mtr_type"] == "high_mtr" else "long"
            cond_derived_text = cond_text_mtr + [
                "✅MTR有效窗口内检测到高质量反转信号K",
                "⚠️衍生反转Setup，必须人工复核K线跟随；MTR一旦过期，本逻辑不再生效"
            ]
            s_derived = build_setup_base(
                setup_name="MTR_DERIVED_REVERSAL_MTR窗口衍生反转",
                direction=der_dir,
                trigger_k_ts=last_k_ts,
                trigger_price=last_close,
                bg_t=bg_type,
                cond_text=cond_derived_text,
                target_price=None,
                risk_note="来自ACTIVE窗口MTR衍生反转；MTR一旦EXPIRED，该逻辑不再生效；人工复核K线跟随质量",
                cond_hit=cond_hit_derived,
                # 【FIX-D】同一条MTR的衍生反转同样只提醒一次（同事件维度收敛）
                dedup_key=f"mtr_derived:{mtr['mtr_id']}",
                dedup_expire_bars=MTR_VALID_K_BAR_WINDOW,
            )
            candidates.append(s_derived)
            # 标记该MTR为USED，防止后续K重复生成同一条衍生反转信号
            mtr["status"] = "USED"
            logger.info(f"MTR {mtr['mtr_id']} 匹配到窗口内反转K，生成MTR衍生反转Setup，标记USED")

    # ========== Setup5 TTR震荡区间边界反转Setup ==========
    if bg_type == "TTR":
        highs_all = [k["high"] for k in klines[-120:]]
        lows_all = [k["low"] for k in klines[-120:]]
        ttr_high = max(highs_all)
        ttr_low = min(lows_all)
        zone_height = ttr_high - ttr_low
        upper_zone = ttr_high - zone_height * TTR_BOUNDARY_RATIO
        lower_zone = ttr_low + zone_height * TTR_BOUNDARY_RATIO
        sig_k_q = judge_signal_k_quality(last_k)
        # 上沿做空反转
        if last_close >= upper_zone:
            cond_hit = {
                "bg_ttr": True,
                "high_quality_signal_k": sig_k_q["is_good_reversal_k"],
                "channel_trendline_aligned": chan_wick.valid
            }
            cond_text = [
                f"背景TTR震荡区间，区间上沿区域；区间高位={ttr_high:.2f}，上沿阈值={upper_zone:.2f}",
                "震荡边界反转，优先预判突破失败，不追突破"
            ]
            s = build_setup_base("TTR_BOUNDARY_REVERSAL_震荡上沿反转做空","short",last_k_ts,last_close,
                                 bg_type,cond_text,lower_zone,"震荡反转，目标优先看区间下沿，不要期待大级别波段",cond_hit)
            candidates.append(s)
        # 下沿做多反转
        elif last_close <= lower_zone:
            cond_hit = {
                "bg_ttr": True,
                "high_quality_signal_k": sig_k_q["is_good_reversal_k"],
                "channel_trendline_aligned": chan_wick.valid
            }
            cond_text = [
                f"背景TTR震荡区间，区间下沿区域；区间低位={ttr_low:.2f}，下沿阈值={lower_zone:.2f}",
                "震荡边界反转，优先预判突破失败，不追突破"
            ]
            s = build_setup_base("TTR_BOUNDARY_REVERSAL_震荡下沿反转做多","long",last_k_ts,last_close,
                                 bg_type,cond_text,upper_zone,"震荡反转，目标优先看区间上沿，不要期待大级别波段",cond_hit)
            candidates.append(s)

    # ========== ✅ Setup6 精准MM缺口到达反转 Setup（补齐PRD V1.1） ==========
    # 条件：1）抵达MM-D目标容忍区；2）目标区域同时出现突破缺口（精准MM缺口）；3）出现高质量反转K；
    # 规则：强趋势背景只输出【观察】，**不生成反转开仓信号；弱趋势/TTR才生成可开仓候选**
    for mm_res in mm_list:
        if not mm_res.valid:
            continue
        d_target = mm_res.d_target_price
        # 判断当前价格是否进入MM-D目标区间
        price_reach_mm = is_price_in_mm_target_zone(last_close, d_target)
        if not price_reach_mm:
            continue
        # 在目标区域寻找匹配的缺口
        matched_gaps = [g for g in gap_list if match_gap_to_mm_target(g,d_target)]
        if len(matched_gaps)==0:
            continue
        mm_gap = matched_gaps[0]
        sig_k_q = judge_signal_k_quality(last_k)
        if not sig_k_q["is_good_reversal_k"]:
            continue
        # 核心分支：强趋势仅观察；弱趋势/TTR生成可开仓Setup
        cond_base_text = [
            f"MM-AB=CD目标D={d_target:.2f}，当前价格进入MM目标容忍区间",
            f"匹配精准MM缺口 gap_id={mm_gap['gap_id']}，缺口方向={mm_gap['direction']}",
            f"该缺口创建K时间:{fmt_ts_show(mm_gap['create_k_ts'])}",
            f"信号K为高质量反转K，实体占比={sig_k_q['body_ratio']}"
        ]
        cond_hit_base = {
            "mm_aligned": True,
            "breakout_gap_aligned": True,
            "high_quality_signal_k": sig_k_q["is_good_reversal_k"]
        }
        if bg_type == "STRONG_TREND":
            # 强趋势：仅观察预警，禁止开仓反转
            obs_text = cond_base_text + ["⚠️强趋势背景，MM缺口到达仅做观察，不允许猜顶猜底反转开仓；等待趋势二次确认"]
            s_obs = build_setup_base(
                setup_name="MM_PRECISE_GAP_OBSERVE_强趋势精准MM缺口观察",
                direction="observe",
                trigger_k_ts=last_k_ts,
                trigger_price=last_close,
                bg_t=bg_type,
                cond_text=obs_text,
                target_price=d_target,
                risk_note="强趋势环境，MM到达仅观察，不可直接反转开仓，警惕短暂回调之后趋势继续延续",
                cond_hit=cond_hit_base,
                observe_only=True
            )
            candidates.append(s_obs)
        elif bg_type in ("WEAK_TREND","TTR"):
            # 弱趋势/震荡：生成可开仓精准MM缺口反转Setup；缺口方向决定反转方向
            rev_dir = "short" if mm_gap["direction"] == "bull" else "long"
            cond_hit = {**cond_hit_base, "bg_weak_trend":bg_type=="WEAK_TREND","bg_ttr":bg_type=="TTR"}
            cond_text = cond_base_text + ["✅弱趋势/震荡背景，满足精准MM缺口反转全部条件"]
            risk = "精准MM缺口反转；仍需要人工复核后续K线跟随；到达MM不等于行情必然反转，存在继续拓展新波段的可能性"
            s = build_setup_base(
                setup_name="MM_PRECISE_GAP_REVERSAL_精准MM缺口到达反转",
                direction=rev_dir,
                trigger_k_ts=last_k_ts,
                trigger_price=last_close,
                bg_t=bg_type,
                cond_text=cond_text,
                target_price=d_target,
                risk_note=risk,
                cond_hit=cond_hit
            )
            candidates.append(s)

    # ========== ✅ Setup7：突破缺口 + 通道线叠加 Stop突破入场Setup（补齐PRD V1.1） ==========
    if chan_wick.valid:
        upper_arr = chan_wick.upper_band
        lower_arr = chan_wick.lower_band
        curr_upper = upper_arr[last_k_idx]
        curr_lower = lower_arr[last_k_idx]
        # 遍历全部有效突破缺口 OPEN / PARTIAL_FILL
        for g in gap_list:
            if g["fill_status"] not in ("OPEN","PARTIAL_FILL"):
                continue
            gap_dir = g["direction"]
            cond_text_base = [
                f"通道拟合有效，通道类型={chan_wick.channel_type}",
                f"叠加有效突破缺口 gap_id={g['gap_id']},方向={gap_dir},缺口状态={g['fill_status']}",
                f"该缺口创建K时间:{fmt_ts_show(g['create_k_ts'])}"
            ]
            cond_hit_base = {
                "breakout_gap_aligned": True,
                "channel_trendline_aligned": True,
                "high_quality_signal_k": judge_signal_k_quality(last_k)["is_high_quality_trend_k"]
            }
            # 多头Stop突破：向上突破通道上沿 + bull突破缺口 + 高质量顺势趋势K
            if gap_dir == "bull" and last_close >= curr_upper*(1-1e-4):
                # 【FIX-K】Stop 突破同样要求"背景趋势方向 = 缺口方向"
                cond_hit = {**cond_hit_base,"bg_strong_trend":bg_type=="STRONG_TREND" and bg_dir=="bull","bg_weak_trend":bg_type=="WEAK_TREND" and bg_dir=="bull"}
                cond_text = cond_text_base + ["价格向上突破通道上沿，Stop顺势突破入场条件达成"]
                risk = "通道+缺口Stop突破入场；后续K线跟随一旦变差，警惕假突破陷阱，及时评估离场"
                s = build_setup_base("GAP_CHANNEL_STOP_LONG_缺口通道叠加多头Stop突破","long",last_k_ts,last_close,
                                     bg_type,cond_text,None,risk,cond_hit)
                candidates.append(s)
            # 空头Stop突破：向下突破通道下沿 + bear突破缺口 +高质量顺势趋势K
            elif gap_dir == "bear" and last_close <= curr_lower*(1+1e-4):
                cond_hit = {**cond_hit_base,"bg_strong_trend":bg_type=="STRONG_TREND" and bg_dir=="bear","bg_weak_trend":bg_type=="WEAK_TREND" and bg_dir=="bear"}
                cond_text = cond_text_base + ["价格向下突破通道下沿，Stop顺势突破入场条件达成"]
                risk = "通道+缺口Stop突破入场；后续K线跟随一旦变差，警惕假突破陷阱，及时评估离场"
                s = build_setup_base("GAP_CHANNEL_STOP_SHORT_缺口通道叠加空头Stop突破","short",last_k_ts,last_close,
                                     bg_type,cond_text,None,risk,cond_hit)
                candidates.append(s)

    return candidates





# ====================== 【信号去重模块 signal_deduplicate｜V1.1基于K时间戳过期淘汰】 ======================
def deduplicate_signals(candidate_signals: List[Dict[str, Any]], interval: str, latest_k_end_ts: int, closed_klines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    V1.1 重构去重：notify_item结构体，基于K线时间戳过期，不依赖系统时钟
    输入：候选信号列表，当前周期，最新K结束时间戳，全部闭合K线
    输出：过滤掉重复之后，允许推送的信号列表
    副作用：修改全局g_notified内存
    """
    notify_list = g_notified[interval]
    # Step1：本轮扫描优先淘汰过期记录
    new_notify = []
    for item in notify_list:
        if latest_k_end_ts >= item["expire_k_end_ts"]:
            continue
        new_notify.append(item)
    g_notified[interval] = new_notify

    def _infer_bar_ms(kline_list: List[Dict[str, Any]]) -> int:
        """从K线列表推断单根K时长(ms)：取末尾若干根相邻间距的中位数，抗单根异常"""
        if len(kline_list) < 2:
            return 0
        diffs = []
        for i in range(len(kline_list) - 1, max(len(kline_list) - 11, 0), -1):
            d = kline_list[i]["t_end_ms"] - kline_list[i - 1]["t_end_ms"]
            if d > 0:
                diffs.append(d)
        if not diffs:
            return 0
        diffs.sort()
        return diffs[len(diffs) // 2]

    def get_future_k_end_ts(base_ts: int, offset_bar: int, kline_list: List[Dict[str, Any]]) -> int:
        """
        工具函数：给定基准K的end_ts，向后偏移N根K，获取目标end_ts。

        【FIX-B 修复】原实现在"目标K尚未产生"（信号永远挂在最新一根已收盘K上）
        时返回 kline_list[-1]["t_end_ms"]，导致 expire_k_end_ts == trigger_k_end_ts，
        下一轮 Step1 立即判定过期 → 去重 100% 失效 → 每 10 秒重推一次。
        现改为：按K线周期外推；外推不可用时才退回旧行为（并告警）。
        """
        idx = None
        for i, k in enumerate(kline_list):
            if k["t_end_ms"] == base_ts:
                idx = i
                break
        if idx is None:
            # 基准K不在列表中（已被截断）→ 同样按周期外推
            bar_ms = _infer_bar_ms(kline_list)
            if NOTIFY_EXPIRE_EXTRAPOLATE and bar_ms > 0:
                return base_ts + offset_bar * bar_ms
            logger.warning(f"[dedup] 基准K时间戳 {base_ts}（{fmt_ts_show(base_ts)}）不在K线列表中且无法外推，退回旧行为")
            return kline_list[-1]["t_end_ms"]
        target_idx = idx + offset_bar
        if target_idx < len(kline_list):
            return kline_list[target_idx]["t_end_ms"]
        # ✅ 目标K尚未产生：按周期外推，保证 expire 严格大于 trigger
        bar_ms = _infer_bar_ms(kline_list)
        if NOTIFY_EXPIRE_EXTRAPOLATE and bar_ms > 0:
            return base_ts + offset_bar * bar_ms
        logger.warning(f"[dedup] 无法推断K线周期，过期时间外推失败，退回旧行为（去重可能失效）")
        return kline_list[-1]["t_end_ms"]

    final_out = []
    _bar_ms_cache = _infer_bar_ms(closed_klines)
    for sig in candidate_signals:
        trigger_ts = sig["trigger_k_ts"]
        price_bin = round(sig["trigger_price"], 1)

        # 【FIX-L】同一 (setup_name, direction) 的冷却窗过滤：
        # 结构连续满足时，原实现会每根K推一条（30m 曾出现单日 37 条、连续 12 根）。
        # 冷却窗内静默丢弃；离开冷却窗后的新一波仍会正常推送，不影响二次入场信号。
        if SETUP_SAME_DIR_COOLDOWN_BARS > 0 and _bar_ms_cache > 0:
            _cool_ms = SETUP_SAME_DIR_COOLDOWN_BARS * _bar_ms_cache
            _hit_cool = None
            for _x in g_notified[interval]:
                if _x.get("setup_name") != sig["setup_name"] or _x.get("direction") != sig["direction"]:
                    continue
                _delta = trigger_ts - _x["trigger_k_end_ts"]
                if 0 <= _delta < _cool_ms:
                    _hit_cool = _delta
                    break
            if _hit_cool is not None:
                logger.info(f"[{interval}] 同向冷却窗内（{_hit_cool // _bar_ms_cache}根K前已推过），跳过 setup={sig['setup_name']} dir={sig['direction']}")
                continue

        # 【FIX-B】Setup3 按 gap_id 收敛：同一条缺口在有效期内只提醒一次。
        # 否则每个新K的 trigger_k_ts 都不同 → 同一缺口被当成新信号重推。
        _gap_id = sig.get("gap_id")
        use_gap_key = bool(SETUP3_DEDUP_BY_GAP_ID and _gap_id)
        # 【FIX-D】事件维度去重优先：信号自带 dedup_key 时（MTR观察/衍生反转），
        # 按事件ID收敛，而不是按 trigger_k_ts —— 否则每根新K都会被当成新信号重推。
        _dedup_key = sig.get("dedup_key")
        if _dedup_key:
            sig_key = f"{interval}|{sig['setup_name']}|{sig['direction']}|{_dedup_key}"
        elif use_gap_key:
            sig_key = f"{interval}|{sig['setup_name']}|{sig['direction']}|gap_id:{_gap_id}"
        else:
            sig_key = f"{interval}|{sig['setup_name']}|{sig['direction']}|price_bin:{price_bin}|trigger_k_ts:{trigger_ts}"
        # 判断是否重复
        is_dup = any(x["signal_key"] == sig_key for x in g_notified[interval])
        if is_dup:
            logger.info(f"[{interval}] 信号重复，跳过推送 key={sig_key}")
            continue
        # 计算过期时间戳（Setup3按缺口维度 / MTR按事件维度去重时，存活期需覆盖整个有效窗口）
        if _dedup_key and sig.get("dedup_expire_bars"):
            _expire_bars = int(sig["dedup_expire_bars"])
        elif use_gap_key:
            _expire_bars = GAP_FAIL_MAX_AGE_BAR
        else:
            _expire_bars = NOTIFY_EXPIRE_BAR_NUM
        expire_ts = get_future_k_end_ts(trigger_ts, _expire_bars, closed_klines)
        # 安全兜底：过期时间必须严格晚于触发K，否则记录会被立即淘汰（去重失效）
        if expire_ts <= trigger_ts:
            _bar_ms = _infer_bar_ms(closed_klines)
            if _bar_ms > 0:
                expire_ts = trigger_ts + _expire_bars * _bar_ms
            logger.warning(f"[{interval}] 去重过期时间异常已兜底 expire={expire_ts}（{fmt_ts_show(expire_ts)}） trigger={trigger_ts}（{fmt_ts_show(trigger_ts)}）")
        # 写入内存
        new_item: NotifyItem = {
            "signal_key": sig_key,
            "setup_name": sig["setup_name"],   # 【FIX-L】供同向冷却窗比对
            "direction": sig["direction"],     # 【FIX-L】供同向冷却窗比对
            "trigger_k_end_ts": trigger_ts,
            "expire_k_end_ts": expire_ts
        }
        g_notified[interval].append(new_item)
        final_out.append(sig)
    return final_out

# ====================== 【飞书告警 feishu_notify｜interactive 卡片 V2｜2026-09-15】 ======================
# 🔧 飞书卡片开发通用注意事项（本实现逐条落实）：
#   1) 响应解析：必须校验 resp.json()["code"] == 0；HTTP 200 ≠ 送达；
#   2) 标签支持：elements 支持 markdown / lark_md / hr / image / action / note，没有 divider，分割线用 hr；
#   3) header.template 仅允许 blue / green / red / yellow / purple / orange / grey；
#   4) 内容长度：单个 markdown 内容建议 < 4096 字符（超长易报 99991664），本实现统一截断；
#   5) JSON 结构：card 必须含 config / header / elements 三个顶层字段，elements 必须是数组；
#   6) 调试：`python 14_setup_signal.py --test-card` 先发极简卡片验证 webhook 连通性。

def _truncate_md(text: str, limit: int = FEISHU_MAX_MD_LEN) -> str:
    """限制单段 markdown 长度，避免 99991664（内容过长）"""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 30] + "\n**…（内容过长已截断）**"


def _feishu_post(payload: Dict[str, Any], tag: str) -> bool:
    """统一发送入口 + 业务码校验。返回 True 表示飞书真正接收（code=0）。"""
    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        logger.error(f"{tag} 飞书请求异常：{type(e).__name__}: {e}")
        return False
    # ⚠️ 不轻信 HTTP 状态码：飞书业务错误一律走 body.code
    biz_code = None
    body_text = ""
    try:
        body = resp.json()
        biz_code = body.get("code")
        body_text = json.dumps(body, ensure_ascii=False)
    except Exception:
        body_text = resp.text or ""
    if resp.status_code == 200 and biz_code == 0:
        logger.info(f"{tag} 飞书推送成功 http=200 code=0")
        return True
    # 失败时打印完整响应体，便于快速定位关键字（11246 标签不支持 / 99991664 内容过长 等）
    logger.error(f"{tag} 飞书推送失败 http={resp.status_code} code={biz_code} body={body_text[:800]}")
    return False


def _feishu_plain_text(interval: str, sig: Dict[str, Any]) -> str:
    """纯文本摘要（降级兜底用）"""
    k_open_utc, k_open_bj, k_close_utc, k_close_bj = kline_time_pair(sig["trigger_k_ts"], interval)
    cond_text = "\n".join([f"- {x}" for x in sig.get("cond_list", [])])
    is_observe = sig.get("observe_only", False)
    if is_observe:
        return f"""【 价格行为观察预警｜{interval}周期】
K线时间(UTC)：{k_open_utc}
北京时间：{k_open_bj}
触发时刻(北京)：{k_close_bj}
触发价格：{sig['trigger_price']:.2f}
信号类型：{sig['setup_name']}
当前盘面背景：{sig['bg_type']}
👉盘面证据：
{cond_text}
⚠️MTR只是【反转测试】，不等于反转开仓信号；请等待二次入场确认Setup。
本消息仅盘面观测，不作为任何交易下单建议。""".strip()
    target = sig.get("target_price")
    target_text = f"{target:.2f}" if target is not None else "待后续MM测量"
    return f"""【 价格行为——V2 信号｜{interval}周期】
K线时间(UTC)：{k_open_utc}
北京时间：{k_open_bj}
触发时刻(北京)：{k_close_bj}
触发价格：{sig['trigger_price']:.2f}
信号类型：{sig['setup_name']}
方向：{sig['direction']}
当前盘面背景：{sig['bg_type']}
信号综合得分：{sig['score']}分（最低推送阈值：{SETUP_MIN_PUSH_SCORE}分）
👉 信号构成：
{cond_text}
🎯潜在测量目标(MM)：{target_text}
⚠️风险提示：本信号为观测告警非下单指令；需人工复核K线跟随；务必设置合理止损，禁止扛单。
补充说明：{sig.get('risk_note', '')}""".strip()


def build_feishu_card(interval: str, sig: Dict[str, Any]) -> Dict[str, Any]:
    """
    构建飞书 interactive 卡片：严格包含 config / header / elements 三个顶层字段。
    header.template 只取 FEISHU_HEADER_TEMPLATE 白名单内的值。

    【FIX-C 时间口径】卡片同时给出：
      - K线时间(UTC)：该K线开盘时刻的 UTC 时间（=看盘软件上的K线标签，与交易所一致）
      - 北京时间：同一时刻的 UTC+8 表示
    两者均显式指定时区，不受服务器/容器本机时区影响。
    """
    k_open_utc, k_open_bj, k_close_utc, k_close_bj = kline_time_pair(sig["trigger_k_ts"], interval)
    is_observe = sig.get("observe_only", False)
    direction = sig.get("direction", "observe")
    cond_lines = "\n".join([f"- {x}" for x in sig.get("cond_list", [])])

    if is_observe:
        emoji, title_prefix = "🟠", "观察预警"
        note_text = ("MTR 只是【反转测试】，不等于反转开仓信号；请等待二次入场确认 Setup。"
                     "本消息仅盘面观测，不作为任何交易下单建议。")
    else:
        emoji = "🟢" if direction == "long" else ("🔴" if direction == "short" else "🟠")
        title_prefix = "做多信号" if direction == "long" else ("做空信号" if direction == "short" else "信号")
        note_text = ("本信号为价格行为观测告警，仅做参考，非实盘下单指令；需人工复核K线跟随质量；"
                     "务必自行设置合理初始止损，禁止扛单。")

    # 【FIX-C】时间口径脚注：字段区展示K线开盘时刻，此处补全整根K线区间，消除 open/close 歧义
    time_note = (f"触发K线：{interval} ｜ UTC {k_open_utc} → {k_close_utc} "
                 f"｜ 北京 {k_open_bj} → {k_close_bj}（信号于K线收盘后确认）")

    # 白名单校验：非法 template 一律回落到 grey，避免 11246/参数错误
    tmpl = FEISHU_HEADER_TEMPLATE.get(direction, "grey")
    if tmpl not in ("blue", "green", "red", "yellow", "purple", "orange", "grey"):
        tmpl = "grey"

    elements: List[Dict[str, Any]] = []

    # ① 关键字段区（div.fields，text.tag 必须是 lark_md）
    if not is_observe:
        target = sig.get("target_price")
        target_text = f"{target:.2f}" if target is not None else "待后续MM测量"
        fields = [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**K线时间(UTC)**\n{k_open_utc}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**北京时间**\n{k_open_bj}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**K线周期**\n{interval}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**触发价格**\n{sig['trigger_price']:.2f}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**方向**\n{direction.upper()}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**盘面背景**\n{sig['bg_type']}"}},
            {"is_short": True, "text": {"tag": "lark_md",
                                        "content": f"**综合得分**\n{sig['score']} 分 / 阈值 {SETUP_MIN_PUSH_SCORE}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**MM测量目标**\n{target_text}"}},
        ]
        elements.append({"tag": "div", "fields": fields})
    else:
        elements.append({"tag": "div", "fields": [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**K线时间(UTC)**\n{k_open_utc}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**北京时间**\n{k_open_bj}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**K线周期**\n{interval}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**触发价格**\n{sig['trigger_price']:.2f}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**盘面背景**\n{sig['bg_type']}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**类型**\nMTR 观察预警"}},
        ]})

    # ② 信号构成（分割线用 hr，卡片不支持 divider）
    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md",
                                            "content": _truncate_md(f"**👉 信号构成**\n{cond_lines}")}})

    # ③ 风险说明
    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md",
                                            "content": _truncate_md(f"**⚠️ 风险与说明**\n{sig.get('risk_note', '')}")}})

    # ④ 底部 note（note 标签合法；内部用 plain_text 最稳妥）
    #    第一行固定为时间口径说明，确保任何一条推送都能自证"这根K线到底是哪个时间"
    elements.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": time_note},
        {"tag": "plain_text", "content": note_text},
    ]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": tmpl,
            "title": {"tag": "plain_text", "content": f"{emoji}  {title_prefix}｜{interval}"},
        },
        "elements": elements,
    }


def send_feishu_signal(interval: str, sig: Dict[str, Any]) -> bool:
    """发送飞书 interactive 卡片；失败降级纯文本；异常不阻塞主循环。返回是否送达。"""
    tag = f"[{interval}]"
    try:
        payload = {"msg_type": "interactive", "card": build_feishu_card(interval, sig)}
        if _feishu_post(payload, f"{tag} 卡片"):
            return True
        if FEISHU_CARD_FALLBACK_TEXT:
            logger.warning(f"{tag} 卡片推送失败，降级为纯文本重试")
            ok = _feishu_post({"msg_type": "text", "content": {"text": _feishu_plain_text(interval, sig)}},
                              f"{tag} 文本降级")
            return ok
        return False
    except Exception as e:
        logger.error(f"{tag} 飞书推送异常：{type(e).__name__}: {e}")
        return False


def send_test_card() -> bool:
    """极简卡片连通性自检（仅 header + 一段 markdown），用于排查 webhook 是否可用"""
    _now_ms = int(time.time() * 1000)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "✅ 14_setup_signal 飞书连通性自检"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"**Webhook 可达性测试**\n发送时间(UTC)：{fmt_utc(_now_ms)}\n北京时间：{fmt_beijing(_now_ms)}\n若你看到本条，说明卡片通道正常。"}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "仅连通性测试，非交易信号。"}]},
        ],
    }
    ok = _feishu_post({"msg_type": "interactive", "card": card}, "[test-card]")
    print("极简卡片发送结果:", "成功 (code=0)" if ok else "失败，请查看上方日志中的 body")
    return ok


# ====================== 【QQ 邮箱通知｜新增 2026-09-17】 ======================
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header


def _qq_email_enabled() -> bool:
    """QQ 邮箱凭据是否齐全（发件人 + 授权码）。"""
    return QQ_EMAIL_ENABLED and bool(QQ_EMAIL_SENDER and QQ_EMAIL_AUTH_CODE)


def build_email_html(interval: str, sig: Dict[str, Any]) -> str:
    """把信号渲染成 HTML 邮件正文（字段与飞书卡片对齐）。"""
    k_open_utc, k_open_bj, k_close_utc, k_close_bj = kline_time_pair(sig["trigger_k_ts"], interval)
    is_observe = sig.get("observe_only", False)
    direction = sig.get("direction", "observe")
    cond_lines = "".join(f"<li>{x}</li>" for x in sig.get("cond_list", []))

    if is_observe:
        title = " 价格行为观察预警"
        dir_text = "MTR 观察预警"
    else:
        _label = "做多" if direction == "long" else ("做空" if direction == "short" else "信号")
        title = f" 价格行为信号（{_label}）"
        target = sig.get("target_price")
        target_text = f"{target:.2f}" if target is not None else "待后续MM测量"
        dir_text = f"{direction.upper()}（MM目标 {target_text}）"

    rows = [
        ("K线时间(UTC)", k_open_utc),
        ("北京时间", k_open_bj),
        ("K线周期", interval),
        ("触发价格", f"{sig['trigger_price']:.2f}"),
        ("信号类型", sig.get("setup_name", "")),
        ("方向", dir_text),
        ("盘面背景", sig.get("bg_type", "")),
    ]
    if not is_observe:
        rows.append(("综合得分", f"{sig['score']} 分 / 阈值 {SETUP_MIN_PUSH_SCORE}"))

    row_html = "".join(
        f"<tr><td style='padding:4px 8px;border:1px solid #ddd;background:#f4f6fb;font-weight:bold'>{k}</td>"
        f"<td style='padding:4px 8px;border:1px solid #ddd'>{v}</td></tr>"
        for k, v in rows
    )
    return f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px">
  <h2 style="color:#0b57d0;margin:0 0 12px">{title}｜{interval}</h2>
  <table style="border-collapse:collapse;width:100%;font-size:14px">{row_html}</table>
  <h3 style="margin-top:16px;font-size:15px">👉 信号构成</h3>
  <ul style="font-size:14px;line-height:1.6">{cond_lines}</ul>
  <p style="margin-top:12px;color:#b3261e;font-size:13px">⚠️ {sig.get('risk_note', '')}</p>
  <p style="color:#888;font-size:12px">触发K线：{interval} ｜ UTC {k_open_utc} → {k_close_utc} ｜ 北京 {k_open_bj} → {k_close_bj}（信号于K线收盘后确认）</p>
</div>"""


def send_qq_email(interval: str, sig: Dict[str, Any]) -> bool:
    """通过 QQ 邮箱 SMTP 发送信号通知；异常不阻塞主循环。返回是否送达。"""
    if not _qq_email_enabled():
        logger.warning(f"[{interval}] QQ 邮箱未配置（QQ_EMAIL_SENDER / QQ_EMAIL_AUTH_CODE 缺失），跳过邮件通知")
        return False
    tag = f"[{interval}]"
    receiver = QQ_EMAIL_RECEIVER or QQ_EMAIL_SENDER
    subject = f"【】{sig.get('setup_name', '')} ｜ {interval} ｜ {'观察预警' if sig.get('observe_only') else sig.get('direction', '')}"
    html = build_email_html(interval, sig)
    msg = MIMEMultipart("alternative")
    msg["From"] = QQ_EMAIL_SENDER
    msg["To"] = receiver
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(html, "html", "utf-8"))
    last_err = None
    for attempt in range(1, QQ_EMAIL_RETRIES + 1):
        try:
            with smtplib.SMTP_SSL(QQ_SMTP_SERVER, QQ_SMTP_PORT, timeout=QQ_SMTP_TIMEOUT) as server:
                server.login(QQ_EMAIL_SENDER, QQ_EMAIL_AUTH_CODE)
                server.sendmail(QQ_EMAIL_SENDER, [receiver], msg.as_string())
            logger.info(f"{tag} QQ 邮箱推送成功 -> {receiver}（第{attempt}次尝试）")
            return True
        except Exception as e:
            last_err = e
            logger.warning(f"{tag} QQ 邮箱推送失败（第{attempt}/{QQ_EMAIL_RETRIES}次）：{type(e).__name__}: {e}")
            if attempt < QQ_EMAIL_RETRIES:
                time.sleep(QQ_EMAIL_RETRY_DELAY)
    logger.error(f"{tag} QQ 邮箱推送经 {QQ_EMAIL_RETRIES} 次重试仍失败：{type(last_err).__name__}: {last_err}")
    return False


def send_test_email() -> bool:
    """QQ 邮箱连通性自检：发一封极简 HTML 邮件验证凭据与 SMTP 通道。"""
    if not _qq_email_enabled():
        print("QQ 邮箱未配置：请设置 QQ_EMAIL_SENDER / QQ_EMAIL_AUTH_CODE 环境变量，或在配置块直接填写。")
        return False
    receiver = QQ_EMAIL_RECEIVER or QQ_EMAIL_SENDER
    _now = int(time.time() * 1000)
    msg = MIMEMultipart("alternative")
    msg["From"] = QQ_EMAIL_SENDER
    msg["To"] = receiver
    msg["Subject"] = Header("✅ 14_setup_signal QQ 邮箱连通性自检", "utf-8")
    html = (f"<div style='font-family:sans-serif'>"
            f"<h2>QQ 邮箱连通性测试</h2>"
            f"<p>发送时间(UTC)：{fmt_utc(_now)}</p>"
            f"<p>北京时间：{fmt_beijing(_now)}</p>"
            f"<p>若你看到此邮件，说明 QQ 邮箱通道正常。</p>"
            f"<p style='color:#888'>仅连通性测试，非交易信号。</p></div>")
    msg.attach(MIMEText(html, "html", "utf-8"))
    last_err = None
    for attempt in range(1, QQ_EMAIL_RETRIES + 1):
        try:
            with smtplib.SMTP_SSL(QQ_SMTP_SERVER, QQ_SMTP_PORT, timeout=QQ_SMTP_TIMEOUT) as server:
                server.login(QQ_EMAIL_SENDER, QQ_EMAIL_AUTH_CODE)
                server.sendmail(QQ_EMAIL_SENDER, [receiver], msg.as_string())
            print(f"QQ 邮箱自检邮件已发送至 {receiver}（第{attempt}次尝试）")
            return True
        except Exception as e:
            last_err = e
            print(f"QQ 邮箱自检失败（第{attempt}/{QQ_EMAIL_RETRIES}次）：{type(e).__name__}: {e}")
            if attempt < QQ_EMAIL_RETRIES:
                time.sleep(QQ_EMAIL_RETRY_DELAY)
    print(f"QQ 邮箱自检经 {QQ_EMAIL_RETRIES} 次重试仍失败：{type(last_err).__name__}: {last_err}")
    return False

# ====================== 【单周期scan入口｜PRD V1.1 完整扫描流程】 ======================
def _record_signal(interval: str, sig: Dict[str, Any]):
    """把 14_setup_signal 的 Setup 信号写入统一信号库（供信号共振统计）。

    时间口径：trigger_k_ts 就是触发K的 close_time（毫秒），与 10_harami_realtime /
    11_paxg_engulfing 的口径一致，直接交给 signal_store 按本地时区渲染。

    该脚本只产出 setup_name / trigger_price / target_price，没有独立止损位 → SL 留空；
    target_price 作为 TP1。observe_only（MTR观察预警）同样入库——它也会推送到飞书，
    用户看得到就该参与共振统计。
    """
    try:
        signal_store.record_signal(
            signal_time=sig.get("trigger_k_ts"), symbol=SYMBOL, interval=interval,
            pattern=sig.get("setup_name", ""), direction=sig.get("direction", ""),
            entry=sig.get("trigger_price"), sl=None,
            tp1=sig.get("target_price"), tp2=None,
        )
    except Exception as e:
        logger.warning(f"[{interval}] 信号入库异常: {e}")


def scan(interval: str):
    """单周期完整扫描入口；全部KlineCalcError本函数捕获，单周期跳过，不崩溃全局主循环"""
    try:
        logger.info(f"===== 开始扫描周期 {interval} =====")
        raw = get_klines_retry(interval)
        if raw is None or len(raw) < 50:
            logger.warning(f"[{interval}] K线数据不足，本轮直接跳过")
            return
        closed_klines = parse_and_filter_closed_klines(raw)
        if len(closed_klines) < 50:
            logger.warning(f"[{interval}] 过滤未闭合K后样本不足，本轮跳过")
            return
        # 提取序列
        closes = np.array([k["close"] for k in closed_klines], dtype=np.float64)
        highs = np.array([k["high"] for k in closed_klines], dtype=np.float64)
        lows = np.array([k["low"] for k in closed_klines], dtype=np.float64)
        # 1.基础指标计算
        ema20_arr = calc_ema(closes, EMA_PERIOD)
        gap_list = detect_gaps(closed_klines)
        g_gap_storage[interval] = gap_list
        logger.info(f"[{interval}] 识别到突破缺口数量:{len(gap_list)}")
        # 2.二元背景判定（打分）
        bg_info = judge_background(closed_klines, gap_list)
        bg_res = bg_info["bg_result"]
        logger.info(f"[{interval}] 背景判定结果={bg_res}；打分详情={bg_info['score_detail']}")
        logger.info(f"[{interval}] 背景证据列表：{json.dumps(bg_info['evidence_list'], ensure_ascii=False)}")
        if bg_res == "CONFUSE":
            logger.info(f"[{interval}] 背景模糊，本周期不生成任何交易类Setup，直接结束扫描")
            return
        # 3.读取本周期MTR上下文
        mtr_ctx = g_mtr_context[interval]
        # 更新MTR生命周期：过期的置EXPIRED
        latest_k_ts = closed_klines[-1]["t_end_ms"]
        for m in mtr_ctx:
            if m["status"] == "ACTIVE" and latest_k_ts > m["expire_k_ts"]:
                m["status"] = "EXPIRED"
        # 【FIX-B】MTR上下文裁剪：原实现只 append 从不清理 → 全局内存无限增长。
        # 仅裁剪永不激活的历史记录（EXPIRED/USED），保留全部 ACTIVE。
        if len(mtr_ctx) > MTR_CONTEXT_MAX_KEEP:
            _act = [m for m in mtr_ctx if m["status"] == "ACTIVE"]
            _hist = [m for m in mtr_ctx if m["status"] != "ACTIVE"][-MTR_CONTEXT_MAX_KEEP:]
            g_mtr_context[interval] = _act + _hist
            mtr_ctx = g_mtr_context[interval]
            logger.info(f"[{interval}] MTR上下文已裁剪至 {len(mtr_ctx)} 条（防止内存无限增长）")
        # 4.识别全部候选Setup
        candidate_setups = detect_setups(closed_klines, gap_list, bg_info, mtr_ctx, ema20_arr, interval)
        logger.info(f"[{interval}] 原始候选Setup数量：{len(candidate_setups)}")
        # 5.打分过滤：分数低于阈值（非observe_only）仅打印日志，不进入去重流程
        # 【FIX-J】阈值支持 Setup 级覆盖：分数结构天生偏低的 Setup 用其专属阈值，
        # 避免"设计上要发却永远发不出来"的悬空项（详见 SETUP_MIN_PUSH_SCORE_OVERRIDE）。
        valid_candidates = []
        for sig in candidate_setups:
            if sig.get("observe_only"):
                # MTR观察预警不参与分数门槛；但是依然要走信号去重，防止刷屏
                valid_candidates.append(sig)
            else:
                _th = SETUP_MIN_PUSH_SCORE_OVERRIDE.get(sig["setup_name"], SETUP_MIN_PUSH_SCORE)
                if sig["score"] >= _th:
                    valid_candidates.append(sig)
                else:
                    logger.info(f"[{interval}] 候选Setup {sig['setup_name']} 得分{sig['score']}（该Setup阈值{_th}），低于阈值，静默丢弃，不推送")
        # 6.信号去重
        after_dedup = deduplicate_signals(valid_candidates, interval, latest_k_ts, closed_klines)
        logger.info(f"[{interval}] 去重之后待推送信号数量:{len(after_dedup)}")
        # 7.逐条飞书 + QQ 邮箱双通道发送（两者独立，任一失败不影响另一条）
        _ok_cnt = 0
        for sig in after_dedup:
            if send_feishu_signal(interval, sig):
                _ok_cnt += 1
                _record_signal(interval, sig)
            send_qq_email(interval, sig)   # 邮件独立推送，不计入飞书成功计数
        if after_dedup:
            logger.info(f"[{interval}] 飞书推送结果：{_ok_cnt}/{len(after_dedup)} 条成功")
        logger.info(f"===== 周期 {interval} 扫描完成 =====\n")

    except KlineCalcError as e:
        logger.error(f"[{interval}] K线计算业务异常，本轮跳过，异常信息:{str(e)}")
        return
    except Exception as e:
        logger.exception(f"[{interval}] 发生未知兜底异常，本轮扫描跳过")
        time.sleep(2)
        return

# ====================== 【主循环入口】 ======================
def main():
    argv = sys.argv[1:]
    # ---- CLI 调试开关（2026-09-15 新增）----
    #   --test-card : 发送极简卡片，只验证 webhook 连通性，不跑扫描
    #   --test-email: 发送极简 QQ 邮件，验证邮箱凭据与 SMTP 通道，不跑扫描
    #   --once      : 只跑一轮全周期扫描后退出（部署前自检用）
    if "--test-card" in argv:
        send_test_card()
        return
    if "--test-email" in argv:
        send_test_email()
        return
    print("=" * 70)
    print(" 价格行为信号告警系统 V1.1 | PRD完整对齐，仅告警不自动交易")
    _boot_ms = int(time.time() * 1000)
    print(f"启动时间(UTC):{fmt_utc(_boot_ms)} | 北京时间:{fmt_beijing(_boot_ms)}")
    print("=" * 70)
    if "--once" in argv:
        print("[--once] 单轮扫描模式")
        for iv in INTERVALS:
            scan(iv)
        return
    last_scan_second = -1
    while True:
        now = datetime.now()
        curr_sec = now.second
        # 每10秒触发一轮，防止同一秒重复执行多次
        if curr_sec % SCAN_INTERVAL_SECOND == 1 and curr_sec != last_scan_second:
            for iv in INTERVALS:
                scan(iv)
            last_scan_second = curr_sec
        time.sleep(0.2)

if __name__ == "__main__":
    main()




