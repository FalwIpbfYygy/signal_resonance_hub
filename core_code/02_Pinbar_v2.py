'''

启动方式：python 02_Pinbar反转_升级_v2.py
（原始 02_Pinbar反转_升级.py 已停维护，本文件为唯一入口）

=== v2 → v2.1 升级（基于Price Action 公开教学 Hammer / Shooting Star 精确教学）===

【v2.0 - 2026/09/10 上轮 7 坑点修复】
  坑点1：WAIT_NEXT_CANDLE_CONFIRM 之前定义了但从没用 -> 现在真正落地：
        检测在"最新已收盘K线"(倒数第2根)，命中后进入 pending，
        等下一根收盘确认(未触发止损且价格守住)才发信号，减少毛刺假信号。
  坑点2：趋势判定只数涨跌K线 -> 增加 EMA 位置过滤 + 净方向(atr比例)阈值，
        并加大回看长度，震荡市不再频繁 bull/bear 切换。
  坑点3：无阻力时 TP=None 直接入库 -> 缺阻力时回退到 ATR 倍数目标。
  坑点4：只识别最后一根K -> 现在扫描最近 N 根收盘K(默认3)，且每根收盘K只处理一次。
  坑点5：缺Price Action 教学禁区过滤 -> 新增结构过滤：释放spike后禁交易；抄底(做多)前要求有积累(base区间不过宽)。
  坑点6：止损固定±1.5点 -> 改为 ATR 自适应止损(SL_ATR_MULT*ATR)。
  坑点7：无信号冷却 -> 单周期冷却 SIGNAL_COOLDOWN_SEC + 同SR区重复触发过滤。

【v2.1 - 2026/09/10 本轮 Price Action 教学 Hammer/Shooting Star 严格定义补强】
  优化1：Hammer / Shooting Star 严格定义（Price Action 教学原话）：
        - Hammer = "small body at the TOP with long lower wicks"
        - Shooting Star = "small body at the BOTTOM with long upper wicks"
        实现在 check_reversal_candle：除原"长影线 ≥ 2×body"外，强制 Hammer 的反向影线（上影）
        ≤ HAMMER_OPPOSITE_WICK_RATIO (0.4) × 正向影线（下影）；Shooting Star 反之。
        区分 4 类：bull_hammer / bear_shooting_star（严格）/ bull_pinbar / bear_pinbar（宽松）。
  优化2：震荡区间中段过滤：
        Price Action 教学原话：in the middle of a range / in the middle of nowhere = noise。
        新增 is_in_middle_of_range()：最近20根区间内当前K收盘所处百分位 30%-70% 视为噪声信号 → 静默拦截。
  优化3：确认 K 线方向对齐（Price Action 教学原话："one candlestick alone is a guess, two candlesticks aligned = a signal"）：
        check_confirmation 增加：做多→下根 close>open 且收盘守入场上方；做空反向。
  优化4：信号强度评分 grade_signal() 0-100：
        SR 接近度 + 形态标准度 + 反向影线比例 + 净方向强度 4 维加权。
        分 < MIN_SIGNAL_SCORE 默认 50 视为噪声 → 在发车前静默拦截（比冷却更前置）。
  优化5：启动指向 v2（START_CMD 常量）+ 头部 README 标注。

!!! Price Action 教学三大失效场景严禁触发：① 反向影线过长（不是真 Hammer/Shooting Star）② 区间中部（noise）③ 趋势中段（mid-trend fail）!!!

'''


import requests
import time
import os
import sys
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header

# 统一信号入库模块（位于项目根目录）；不存在时降级为本地 no-op，避免崩溃
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import signal_store
    HAVE_SIGNAL_STORE = True
except Exception:
    signal_store = None
    HAVE_SIGNAL_STORE = False

# ====================== 【策略参数｜严格对照原文】 ======================
SYMBOL = ""
# 小周期扫描周期（找入场）
# SCAN_INTERVALS = ["5m", "15m", "30m"]
# SCAN_INTERVALS = ["30m"]
SCAN_INTERVALS = ["15m", "30m"]
# 大周期：用来定整体趋势
TREND_TIMEFRAME = "4h"

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")

# ====================== QQ邮箱推送（双周期共振） ======================
QQ_EMAIL_SENDER = os.environ.get("QQ_EMAIL_SENDER", "")      # 发件邮箱
# 授权码从环境变量读取（QQ_EMAIL_AUTH_CODE），避免明文硬编码进代码/仓库
QQ_EMAIL_AUTH_CODE = os.environ.get("QQ_EMAIL_AUTH_CODE", "")    # QQ邮箱SMTP授权码
QQ_EMAIL_RECEIVER = os.environ.get("QQ_EMAIL_RECEIVER", "")    # 收件邮箱
QQ_SMTP_SERVER = "smtp.qq.com"
QQ_SMTP_PORT = 465
QQ_EMAIL_TIMEOUT = 30
# 双周期共振判定窗口（秒）：15m与30m信号触发时间差小于该值视为“同一时间”
DOUBLE_SIGNAL_WINDOW = 60

# ====================== v2.1.2 双周期共振门控（仅共振才发车） ======================
# 与回测 03 文件参数一致：30m 为锚定交易信号，需窗口内存在同向 15m 信号才发车；
# 15m 仅作共振确认，不独立发车。ENABLE_RESONANCE_GATE=False 可退回原各周期独立发车。
ENABLE_RESONANCE_GATE = True
RESONANCE_WINDOW_MS = 30 * 60 * 1000   # 30m 确认时间戳往前 30min 窗口（双向：|ts30-ts15|<=窗口）
_INTERVAL_MS = {"15m": 15 * 60 * 1000, "30m": 30 * 60 * 1000}  # 各周期 K 线时长（推算确认时间戳）
RESO_CACHE_MAX_MS = 4 * 60 * 60 * 1000  # v2.1.4：共振记录 wall 时钟最大保留 4h，超时清理（防旧信号跨很久误配，Bug4）

# Pinbar标准：影线至少实体2倍
PINBAR_WICK_MULTI = 2.0
# 吞没形态阈值：吞没实体至少覆盖前一根实体比例
ENGULF_RATIO = 1.0
# 判断趋势回溯K线数量（坑点2：加大，配合 EMA/净方向过滤）
TREND_LOOKBACK = 12
# 寻找局部高低点（支撑阻力）回看K线数量
SR_LOOKBACK = 30
# 价格距离关键位容忍比例（形态需要靠近支撑阻力）
SR_TOLERANCE_PCT = 0.002

# ====================== 启动命令常量 ======================
START_CMD = "python 02_Pinbar反转_升级_v2.py"   # ★ v2.1 起为唯一入口
# ============================================================

# ====================== v2 新增/调整参数 ======================
# 坑点1：启用“等待下一根K收盘确认”
WAIT_NEXT_CANDLE_CONFIRM = True
# 坑点2：趋势判定增强
TREND_EMA_MIN = 0.6        # 上涨/下跌根数占比阈值（需 >= lookback*该值）
TREND_NET_ATR = 0.3        # 净方向(末-首)需 > 0.3*ATR 才算有方向
# 坑点4：扫描最近 N 根“已收盘”K线寻找候选（不只看最后一根）
SCAN_HISTORY_CLOSED = 3
# 坑点6：ATR 自适应止损 / 坑点3：TP 回退倍数
ATR_PERIOD = 14
SL_ATR_MULT = 1.5          # 止损 = 极值 ± 1.5*ATR
TP_ATR_MULT = 2.5          # 缺阻力时 TP1 = 入场 ± 2.5*ATR（与SL构成 R:R≈1.67）
MIN_RR_FOR_TP1 = 1.5       # v2.1.1：TP1 保底盈亏比。有阻力但阻力过近时，TP1 推到 entry ± (SL_ATR_MULT*本值)*ATR，保底 R:R≥1.5
# 坑点5：Price Action 教学禁区结构过滤
ENABLE_STRUCTURE_FILTER = True
SPIKE_LOOKBACK = 5          # 信号前 N 根内查“释放”大K
SPIKE_ATR_MULT = 3.0        # 单根 range > 3*ATR 视为释放 spike
# v2.1.4：spike 精确化——需实体占 range 比例 > 该值，避免误禁长下影 Hammer（其 range 常>3*ATR 但实体极小，非失控 spike）
SPIKE_BODY_RATIO = 0.3       # range>3*ATR 且 body/range>0.3 才算失控大K，否则长影线反转K放行
BASE_LOOKBACK = 5           # 抄底前 N 根查“积累”base
BASE_RANGE_MULT = 2.0       # base 区间 <= 2*ATR 才算有积累（否则无筹码积累直接抄底=禁）
# 坑点7：冷却 + 同区复用过滤
SIGNAL_COOLDOWN_SEC = 1800  # 单周期信号冷却 30 分钟
SR_ZONE_REUSE_TOL = 0.005   # 同SR区(差异<0.5%)重复触发过滤
CONFIRM_SLIPPAGE = 0.0005   # 下一根确认时，价格相对入场的最大反向滑点容忍
# 缺陷3开关：确认K触碰SL的宽松/严格判定
#   False（严格，默认）：确认K盘中最低/最高价触及SL即判定确认失效（最保守，过滤插针假信号）
#   True （宽松）：仅当确认K收盘价跌破SL才失效，允许盘中插针刺破后收回（减少漏信号）
CONFIRM_ALLOW_INTRABAR_SL_HIT = False

# v2.1.4：运行状态老化（防实盘 7x24 内存无限增长，Bug8）
STATE_KEEP_MS = 14 * 24 * 60 * 60 * 1000   # fired_keys 等状态保留 14 天，过期清理
STATE_CLEANUP_INTERVAL_SEC = 3600          # 每 1 小时执行一次老化清理

# ====================== v2.1 新增参数（Price Action 教学 Hammer/Shooting Star） ======================
# 优化1：Hammer / Shooting Star 严格定义
#   反向影线 / 正向影线 ≤ 该比值才算严格的 Hammer 或 Shooting Star（Price Action 教学："small body at TOP/BOTTOM"）
#   > 该比值但 长影线 ≥ 2×body 的，归类为 "宽松版 Pinbar"
HAMMER_OPPOSITE_WICK_RATIO = 0.4
# 优化2：震荡区间中段过滤
ENABLE_RANGE_MIDDLE_FILTER = True
RANGE_FILTER_WINDOW = 20        # 回看多少根 K 计算"区间"
RANGE_MIDDLE_LO = 0.30          # 当前 close 百分位 < 该值视为下沿
RANGE_MIDDLE_HI = 0.70          # > 该值视为上沿；[LO, HI] 区间内 = 噪声信号
# 优化3：确认 K 线方向对齐
ENABLE_CONFIRM_DIRECTION = True
# 优化4：信号强度评分 (0-100)
ENABLE_SIGNAL_SCORE = True
MIN_SIGNAL_SCORE = 50           # 低于该分视为噪声信号，静默拦截
SCORE_W_SR = 30                 # SR 接近度权重
SCORE_W_FORM = 35               # 形态标准度权重（影线比 + 反向影线比例）
SCORE_W_TREND = 35              # 净方向强度权重
# ======================================================================================

# 防重复告警（保留兼容）
notified = {interval: set() for interval in SCAN_INTERVALS}
# 缓存大周期K线，减少频繁请求
cache_4h_kline = None
cache_4h_expire = 0

# 最近一次信号 {周期: {"detect_time": float, "kline_ts": int, "msg": str}}
recent_signal = {}
# 已发送QQ邮箱的共振信号对，避免重复推送 {(15m kline_ts, 30m kline_ts)}
qq_sent_pairs = set()

# ---- v2 运行时状态 ----
pending_signals = {}        # {interval: cand_dict}  待下一根确认的候选
last_scanned_ts = {}        # {interval: ts}         已处理的最新收盘K（每根只处理一次）
cooldown_until = {}         # {interval: epoch}      冷却截止时间
last_sr_level = {}          # {interval: level}      上次触发所用SR区，防同区复用
fired_keys = set()          # {(interval, candle_ts)} 已真正发过的信号，防重复
last_cleanup_ts = 0.0       # v2.1.4 Bug8：状态老化清理计时


def _score_to_stars(score):
    """信号评分 → ★ 等级（ICT 卡片视觉参考）"""
    try:
        s = float(score)
    except Exception:
        return "★"
    if s >= 85: return "★★★★★"
    if s >= 75: return "★★★★"
    if s >= 65: return "★★★"
    if s >= 50: return "★★"
    return "★"


def _session_cn(hour_beijing):
    """按北京时间映射交易时段（亚洲盘 / 欧洲盘 / 美洲盘）"""
    h = hour_beijing
    if 7 <= h < 16: return "亚洲盘"
    if 16 <= h < 23: return "欧洲盘"
    return "美洲盘"


def _confirmed_check_list(cand, confirmed, resonance_hit):
    """根据 cand/上下文产出【确认条件】勾选清单（只勾真实通过的项）"""
    items = []
    ct = cand.get("candle_type", "")
    if ct in ("bull_hammer", "bear_shooting_star"):
        items.append("✅严格形态")
    if ct.endswith("pinbar"):
        items.append("✅ Pinbar形态")
    if "engulf" in ct:
        items.append("✅吞没形态")
    sr = cand.get("sr_level")
    if sr is not None:
        items.append("✅靠近支撑/阻力")
    bt = cand.get("big_trend")
    direction = cand.get("direction")
    if bt in ("bull", "bear") and direction in ("做多", "做空"):
        if (bt == "bear" and direction == "做多") or (bt == "bull" and direction == "做空"):
            items.append("✅大周期定方向")
    sc = cand.get("score")
    if sc is not None and sc >= MIN_SIGNAL_SCORE:
        items.append(f"✅评分达标({sc:.0f}/100)")
    if confirmed:
        items.append("✅下一根确认")
    items.append("✅结构无禁区")
    items.append("✅非区间中段")
    if resonance_hit:
        items.append("✅双周期共振")
    return items


def build_feishu_card(cand, interval, confirmed, resonance_hit=False):
    """构造飞书交互式卡片 payload（msg_type=interactive）。"""
    direction = cand.get("direction", "—")
    pattern = cand.get("pattern", "—")
    candle_type = cand.get("candle_type", "")
    score = cand.get("score")
    big_trend = cand.get("big_trend", "—")
    entry = cand.get("entry"); sl = cand.get("sl")
    tp1 = cand.get("tp1"); tp2 = cand.get("tp2")

    # 方向 + 颜色模板
    is_buy = direction == "做多"
    arrow = "📈" if is_buy else "📉"
    header_template = "green" if is_buy else "red"
    direction_emoji = "🟢" if is_buy else "🔴"
    title = f"{arrow}  Pinbar 信号 · {interval}"

    # 时段（按北京时间）
    from datetime import datetime as _dt
    hour_bj = _dt.now().hour
    session = _session_cn(hour_bj)

    # 时间：GMT + 北京
    candle_ts = cand.get("candle_ts")
    if candle_ts:
        gmt_str = _dt.utcfromtimestamp(int(candle_ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        bj_str = _dt.fromtimestamp(int(candle_ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    else:
        gmt_str = bj_str = "—"

    # 盈亏比 = TP1距离 / SL距离
    if entry and sl and tp1 is not None:
        sl_dist = abs(entry - sl)
        tp1_dist = abs(tp1 - entry)
        rr = (tp1_dist / sl_dist) if sl_dist > 1e-7 else 0
        rr_str = f"1:{rr:.2f}"
    else:
        rr_str = "—"

    def fmt(v):
        return f"{v:,.2f}" if isinstance(v, (int, float)) else "—"

    # 评分 → ★
    stars = _score_to_stars(score)

    # 确认条件清单
    checks = _confirmed_check_list(cand, confirmed, resonance_hit)
    checks_text = "  \n".join(checks) if checks else "—"

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": header_template,
                "title": {"tag": "plain_text", "content": title}
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**方向:** {direction_emoji} {direction} {pattern}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**周期:** {interval}  ｜  **时段:** {session}"}}
                    ]
                },
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**大周期趋势:** {big_trend}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**信号强度:** {stars}  ({score:.0f}/100)" if isinstance(score, (int, float)) else f"**信号强度:** {stars}"}}
                    ]
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**入场:** {fmt(entry)}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**止损:** {fmt(sl)}"}}
                    ]
                },
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**目标1:** {fmt(tp1)}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**目标2:** {fmt(tp2)}"}}
                    ]
                },
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**风险回报:** {rr_str}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**形态:** {candle_type or '—'}"}}
                    ]
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"**确认条件**\n{checks_text}"}
                },
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**GMT:** {gmt_str}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**北京时间:** {bj_str}"}}
                    ]
                },
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": "学习交流，不作为交易依据"}]}
            ]
        }
    }
    return card


def send_feishu(cand, interval, confirmed, resonance_hit=False):
    """v2.1.3：飞书推送改卡片格式（msg_type=interactive）。构造卡片 → POST webhook。"""
    card = build_feishu_card(cand, interval, confirmed, resonance_hit=resonance_hit)
    send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        resp = requests.post(FEISHU_WEBHOOK, json=card)
        # 飞书自定义机器人业务结果在 body 的 code 字段（HTTP 永远 200），必须解析
        biz = resp.json() if resp.content else {}
        biz_code = biz.get("code")
        biz_msg = biz.get("msg", "")
        if biz_code == 0:
            print(f"[{send_time}] 飞书推送 | OK | msg_type=interactive | biz_code:{biz_code}")
        else:
            print(f"[{send_time}] 飞书推送 | 业务失败 | HTTP:{resp.status_code} biz_code:{biz_code} msg:{biz_msg} | 卡片标题:{card.get('card',{}).get('header',{}).get('title',{}).get('content','')}")
    except Exception as e:
        print(f"[{send_time}] 推送异常: {str(e)}")


def send_qq_email(subject, body):
    """QQ邮箱发送（SSL直连，失败仅打印日志）"""
    send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = Header("量化告警系统", "utf-8").encode() + f" <{QQ_EMAIL_SENDER}>"
    msg["To"] = Header("交易者", "utf-8").encode() + f" <{QQ_EMAIL_RECEIVER}>"
    msg["Subject"] = Header(subject, "utf-8")
    try:
        server = smtplib.SMTP_SSL(QQ_SMTP_SERVER, QQ_SMTP_PORT, timeout=QQ_EMAIL_TIMEOUT)
        server.login(QQ_EMAIL_SENDER, QQ_EMAIL_AUTH_CODE)
        server.sendmail(QQ_EMAIL_SENDER, [QQ_EMAIL_RECEIVER], msg.as_string())
        try:
            server.quit()
        except Exception:
            pass
        print(f"[{send_time}] QQ邮箱推送 | {subject}")
    except Exception as e:
        print(f"[{send_time}] QQ邮箱发送异常: {str(e)}")


def try_send_double_signal(interval, msg, kline_ts):
    """双周期共振：15m与30m同一时间都触发信号时，合并推送QQ邮箱"""
    recent_signal[interval] = {"detect_time": time.time(), "kline_ts": kline_ts, "msg": msg}
    if "15m" not in recent_signal or "30m" not in recent_signal:
        return
    s15 = recent_signal["15m"]
    s30 = recent_signal["30m"]
    if abs(s15["detect_time"] - s30["detect_time"]) > DOUBLE_SIGNAL_WINDOW:
        return
    pair = (s15["kline_ts"], s30["kline_ts"])
    if pair in qq_sent_pairs:
        return
    qq_sent_pairs.add(pair)
    body = s15["msg"].strip() + "\n\n" + "=" * 30 + "\n\n" + s30["msg"].strip()
    subject = " 双Pinbar共振信号 | 15m+30m"
    send_qq_email(subject, body)


# ====================== v2.1.2 双周期共振门控（仅共振才发车） ======================
# 记录两周期最近信号，供共振配对（不依赖是否最终发车）
recent_reso = {}


def _confirm_ts(interval, candle_ts):
    """推算信号 K 线的【确认收盘】时间戳 = 开盘时间 + 周期时长"""
    return candle_ts + _INTERVAL_MS.get(interval, 0)


def build_signal_msg(cand, interval, confirmed):
    """构造飞书/QQ 通用信号文案（从 fire_signal 抽出，供共振合并复用）"""
    candle_ts = cand["candle_ts"]
    entry = cand["entry"]; sl = cand["sl"]; tp1 = cand["tp1"]; tp2 = cand["tp2"]
    tp1_ref = f"{tp1:,.2f}" if tp1 is not None else "ATR回退"
    tp2_ref = f"{tp2:,.2f}" if tp2 is not None else "—"
    candle_type_ref = cand.get("candle_type", "")
    score_ref = cand.get("score", "—")
    return f"""
【{SYMBOL} {interval} 信号触发{'（下一根已确认）' if confirmed else ''}】
形态：{cand['pattern']}（{candle_type_ref}）
形态强度：{cand['strength']}
入场参考：{entry:.2f}
大周期({TREND_TIMEFRAME})趋势：{cand['big_trend']}
触发逻辑：{cand['logic']}
信号评分(score)：{score_ref}/100
风控参考
👉止损：{sl:,.2f}（ATR自适应 {cand['atr']:.2f}×{SL_ATR_MULT}）
👉目标1：{tp1_ref}
👉目标2：{tp2_ref}
策略原则：等待K线确认再择机入场，分批减仓，单笔风险≤2%资金
【学习交流，不作为交易依据】
        """.strip()


def send_resonance_qq(cand30, cand15, ts30):
    """共振命中：合并 15m+30m 两信号发一封 QQ 邮件"""
    body = (build_signal_msg(cand15, "15m", True).strip()
            + "\n\n" + "=" * 30 + "\n\n"
            + build_signal_msg(cand30, "30m", True).strip())
    send_qq_email(" 双Pinbar共振信号 | 15m+30m", body)


def _reso_expired(rec, now_ms):
    """共振记录是否超出 wall 时钟保留期（Bug4：防很久以前的 15m 与新 30m 误配）"""
    return (now_ms - rec.get("wall", 0)) > RESO_CACHE_MAX_MS

def _clean_reso(now_ms):
    """清理超时的共振记录"""
    for _k in ("15m", "30m"):
        _rec = recent_reso.get(_k)
        if _rec and _reso_expired(_rec, now_ms):
            recent_reso.pop(_k, None)

def maybe_fire_resonance(interval, cand):
    """
    v2.1.2 共振门控唯一发车入口（取代直接 fire_signal）。
    规则（与回测 03 文件一致）：30m 为锚定交易信号，需窗口内存在同向 15m 信号才发车；
    15m 仅作共振确认，不独立发车。ENABLE_RESONANCE_GATE=False 时退回原各周期独立发车。
    v2.1.4：双向窗口（15m 先到等30m / 30m 先到等15m 均可匹配）；共振记录加 wall 时钟过期清理（Bug4）。
    """
    if not ENABLE_RESONANCE_GATE:
        fire_signal(cand, interval, confirmed=True)
        return

    now = time.time()
    now_ms = int(now * 1000)
    _clean_reso(now_ms)

    ts_confirm = _confirm_ts(interval, cand["candle_ts"])
    recent_reso[interval] = {
        "confirm_ts": ts_confirm,
        "direction": cand["direction"],
        "cand": cand,
        "wall": now_ms,
    }

    other = "15m" if interval == "30m" else "30m"
    rec_other = recent_reso.get(other)
    resonated = False
    cand30 = cand15 = None
    if rec_other and rec_other["direction"] == cand["direction"] and not _reso_expired(rec_other, now_ms):
        delta = abs(ts_confirm - rec_other["confirm_ts"])
        if delta <= RESONANCE_WINDOW_MS:
            resonated = True
            if interval == "30m":
                cand30, cand15 = cand, rec_other["cand"]
            else:
                cand30, cand15 = rec_other["cand"], cand

    if resonated:
        fire_signal(cand30, "30m", confirmed=True, resonance_hit=True)
        send_resonance_qq(cand30, cand15, cand30["candle_ts"])
        cooldown_until["30m"] = now + SIGNAL_COOLDOWN_SEC
        cooldown_until["15m"] = now + SIGNAL_COOLDOWN_SEC
        fired_keys.add(("30m", cand30["candle_ts"]))
        fired_keys.add(("15m", cand15["candle_ts"]))
        print(f"✅ [共振发车] 30m+15m 同向共振（{cand['direction']} | 30m@{cand30['candle_ts']} 15m@{cand15['candle_ts']} | Δ={delta/60000:.1f}min）")
        return

    # 未共振：按角色处理（30m 暂存待 15m 触发；15m 仅记录不发车）
    if interval == "30m":
        print(f"⏸  [共振待配] 30m 信号暂存，等待窗口内同向15m（{cand['direction']} @{ts_confirm}）")
    else:
        print(f"ℹ️  [共振模式] 15m 信号已记录（{cand['direction']}），等待30m共振才发车")
    fired_keys.add((interval, cand["candle_ts"]))
    cooldown_until[interval] = now + SIGNAL_COOLDOWN_SEC


def record_signal_safe(**kw):
    """入库（signal_store 不可用时降级为日志，不崩溃）"""
    if HAVE_SIGNAL_STORE:
        try:
            signal_store.record_signal(**kw)
        except Exception as e:
            print(f"[WARN] signal_store 入库失败: {e}")
    else:
        print(f"[INFO] signal_store 不可用，跳过入库: {kw.get('pattern')}")


def get_klines_retry(interval, limit=100, retries=3):
    """获取币安合约K线 重试机制"""
    url = f"{os.environ.get('BINANCE_FUTURES_KLINE_URL')}?symbol={SYMBOL}&interval={interval}&limit={limit}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url)
            if resp.status_code == 200:
                print(f"[{now_str}] 请求成功 | 周期:{interval} 尝试次数:{attempt} status:{resp.status_code}")
                return resp.json()
            else:
                print(f"[{now_str}] 请求异常状态码 | 周期:{interval} 尝试次数:{attempt} status:{resp.status_code}")
        except Exception as e:
            print(f"[{now_str}] 请求抛出异常 | 周期:{interval} 尝试次数:{attempt} error:{str(e)}")
        time.sleep(1)
    print(f"[{now_str}] 请求最终失败 | 周期:{interval} 已用尽重试次数")
    return None


def parse_kline_list(raw_klines):
    """原始币安K线转结构化列表 [timestamp, o,h,l,c,vol] float"""
    data = []
    for k in raw_klines:
        ts = k[0]
        o = float(k[1])
        h = float(k[2])
        l = float(k[3])
        c = float(k[4])
        vol = float(k[5])
        data.append([ts, o, h, l, c, vol])
    return data


def find_support_resistance(k_data, lookback):
    """
    寻找近期局部高低点（支撑阻力）
    return: [support_list, resistance_list]
    """
    supports = []
    resistances = []
    length = len(k_data)
    window = 3  # 左右各3根对比判定局部极值
    for i in range(window, length - window):
        low_i = k_data[i][3]
        high_i = k_data[i][2]
        # 局部低点
        left_low = min([k_data[i - w][3] for w in range(1, window + 1)])
        right_low = min([k_data[i + w][3] for w in range(1, window + 1)])
        if low_i <= left_low and low_i <= right_low:
            supports.append(low_i)
        # 局部高点
        left_high = max([k_data[i - w][2] for w in range(1, window + 1)])
        right_high = max([k_data[i + w][2] for w in range(1, window + 1)])
        if high_i >= left_high and high_i >= right_high:
            resistances.append(high_i)
    return supports, resistances


def price_near_level(price, levels, tolerance_pct):
    """判断价格是否靠近任意支撑/阻力位"""
    for lv in levels:
        diff = abs(price - lv) / lv
        if diff <= tolerance_pct:
            return True
    return False


def nearest(levels, price):
    """返回 levels 中离 price 最近的价位（用于SR区追踪）"""
    if not levels:
        return None
    return min(levels, key=lambda x: abs(x - price))


def calc_atr(kdata, end_idx, period=ATR_PERIOD):
    """计算 ATR（真实波幅均值）"""
    if end_idx < period:
        return None
    trs = []
    for i in range(end_idx - period + 1, end_idx + 1):
        if i == 0:
            tr = kdata[i][2] - kdata[i][3]
        else:
            pc = kdata[i - 1][4]
            hh = kdata[i][2]
            ll = kdata[i][3]
            tr = max(hh - ll, abs(hh - pc), abs(ll - pc))
        trs.append(tr)
    return sum(trs) / len(trs)


def judge_trend(k_data, end_idx, lookback):
    """
    趋势判定（坑点2 增强版）
    三重过滤：①涨跌根数占比 ②净方向需 > TREND_NET_ATR*ATR ③收盘价位于均线同侧
    return bull / bear / side
    """
    if end_idx < lookback:
        return "side"
    closes = [k_data[i][4] for i in range(end_idx - lookback, end_idx + 1)]
    up = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    down = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])
    net = closes[-1] - closes[0]
    atr = calc_atr(k_data, end_idx, lookback) or 1.0
    ratio = lookback * TREND_EMA_MIN
    ema = sum(closes) / len(closes)  # 简单均值作为基准线
    if up >= ratio and net > TREND_NET_ATR * atr and closes[-1] > ema:
        return "bull"
    if down >= ratio and net < -TREND_NET_ATR * atr and closes[-1] < ema:
        return "bear"
    return "side"


def check_reversal_candle(o, h, l, c, wick_min_mult):
    """
    v2.1 优化1：Hammer / Shooting Star / Pinbar 严格判定（Price Action 教学原话定义）
      Hammer         = small body at TOP + long lower wicks           → bull_hammer
      Shooting Star  = small body at BOTTOM + long upper wicks        → bear_shooting_star
      宽松版 Pinbar   = 长正向影线 ≥ 2×body，但反向影线不够短          → bull_pinbar / bear_pinbar

    严格判定约束：反向影线 / 正向影线 ≤ HAMMER_OPPOSITE_WICK_RATIO (0.4)
    返回 None / ("bull_hammer"|"bull_pinbar", level)
         / ("bear_shooting_star"|"bear_pinbar", level)
    """
    body_top = max(o, c)
    body_bottom = min(o, c)
    body = body_top - body_bottom
    if body <= 1e-7:
        return None

    upper_wick = h - body_top
    lower_wick = body_bottom - l

    # 防御性：若两影线都极短，根本不是反转 K 线
    if upper_wick < 1e-7 and lower_wick < 1e-7:
        return None

    # ---- 看涨（Hammer / 看涨 Pinbar）----
    if lower_wick >= upper_wick * 1.2 and lower_wick >= body * wick_min_mult:
        r = lower_wick / body
        if r >= 3.0:
            lv = "极强"
        elif r >= 2.5:
            lv = "标准"
        else:
            lv = "偏弱"
        # 严格 Hammer：上影极短，实体"在顶部"
        if upper_wick <= HAMMER_OPPOSITE_WICK_RATIO * lower_wick and upper_wick <= body * 0.5:
            return ("bull_hammer", f"Hammer·{lv}")
        # 宽松 Pinbar：仍然长下影但反向影线不算极短
        return ("bull_pinbar", f"Pinbar·{lv}")

    # ---- 看跌（Shooting Star / 看跌 Pinbar）----
    if upper_wick >= lower_wick * 1.2 and upper_wick >= body * wick_min_mult:
        r = upper_wick / body
        if r >= 3.0:
            lv = "极强"
        elif r >= 2.5:
            lv = "标准"
        else:
            lv = "偏弱"
        # 严格 Shooting Star：下影极短，实体"在底部"
        if lower_wick <= HAMMER_OPPOSITE_WICK_RATIO * upper_wick and lower_wick <= body * 0.5:
            return ("bear_shooting_star", f"ShootingStar·{lv}")
        # 宽松 Pinbar
        return ("bear_pinbar", f"Pinbar·{lv}")

    return None


def check_engulfing(prev_o, prev_c, curr_o, curr_c, min_ratio):
    """
    吞没形态识别
    return None / bull_engulf / bear_engulf
    """
    prev_body_top = max(prev_o, prev_c)
    prev_body_bottom = min(prev_o, prev_c)
    curr_body_top = max(curr_o, curr_c)
    curr_body_bottom = min(curr_o, curr_c)

    prev_body_len = prev_body_top - prev_body_bottom
    curr_body_len = curr_body_top - curr_body_bottom
    if prev_body_len < 1e-7 or curr_body_len < 1e-7:
        return None

    # 看涨吞没：阳线实体完全包住前面阴线实体
    if curr_o < curr_c and curr_body_bottom <= prev_body_bottom and curr_body_top >= prev_body_top:
        if curr_body_len / prev_body_len >= min_ratio:
            return "bull_engulf"
    # 看跌吞没：阴线包住前面阳线
    if curr_o > curr_c and curr_body_bottom <= prev_body_bottom and curr_body_top >= prev_body_top:
        if curr_body_len / prev_body_len >= min_ratio:
            return "bear_engulf"
    return None


def is_in_middle_of_range(kdata, idx):
    """
    v2.1 优化2：震荡区间中段过滤（Price Action 教学原话："in the middle of a range = noise"）
    判断当前 K 收盘价在【其之前】RANGE_FILTER_WINDOW 根上下文的高低区间内所处百分位是否落在 [LO, HI]。
    返回 True 表示处于中段噪声位置，应静默拦截。

    注意：百分位窗口刻意排除候选 K 线本身（idx 不参与），否则一根深插针反转的下影会把区间
    低点拉极低，使其自身收盘价被误算进中段而遭误杀——这正是"中段过滤误杀真实反转"的根因。
    """
    lo = max(0, idx - RANGE_FILTER_WINDOW)
    ctx = [kdata[i] for i in range(lo, idx)]   # 仅取候选之前的上文，不含候选自身
    if len(ctx) < 5:
        return False  # 数据不足，不拦
    hh = max(k[2] for k in ctx)
    ll = min(k[3] for k in ctx)
    span = hh - ll
    if span <= 1e-7:
        return False
    pct = (kdata[idx][4] - ll) / span
    return RANGE_MIDDLE_LO <= pct <= RANGE_MIDDLE_HI


def grade_signal(cand, kdata, big_trend, atr):
    """
    v2.1 优化4：信号强度评分 (0-100)
      - SR 接近度 (权重 SCORE_W_SR=30)
      - 形态标准度 (SCORE_W_FORM=35)：影线比 + 反向影线比例（Hammer 严格形态加分）
      - 净方向强度 (SCORE_W_TREND=35)
    """
    sr_score = 0.0
    sr_level = cand.get("sr_level")
    if sr_level is not None and atr > 0:
        # SR 越接近满分越高（≤0.3% 满分）
        diff_pct = abs(cand["entry"] - sr_level) / sr_level
        sr_score = max(0.0, 1.0 - diff_pct / 0.01) * 100  # 1% 归零

    # 形态标准度
    form_score = 0.0
    # v2.1.4 Bug5 修复：用真实开盘价 open（detect_candidate 已保存）；c=收盘价即入场 entry
    o = cand.get("open", cand["entry"])
    h = cand["high"]
    l = cand["low"]
    c = cand["entry"]
    body = abs(c - o) or 1e-7
    if cand["direction"] == "做多":
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        if lower_wick > 0:
            r = lower_wick / body
            op_ratio = upper_wick / lower_wick if lower_wick > 0 else 1.0
            r_score = min(r / 3.0, 1.0) * 60
            op_score = max(0.0, 1.0 - op_ratio / HAMMER_OPPOSITE_WICK_RATIO) * 40
            form_score = r_score + op_score
    elif cand["direction"] == "做空":
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        if upper_wick > 0:
            r = upper_wick / body
            op_ratio = lower_wick / upper_wick if upper_wick > 0 else 1.0
            r_score = min(r / 3.0, 1.0) * 60
            op_score = max(0.0, 1.0 - op_ratio / HAMMER_OPPOSITE_WICK_RATIO) * 40
            form_score = r_score + op_score

    # 净方向强度
    trend_score = 0.0
    if big_trend == "bear" and cand["direction"] == "做多":
        trend_score = 70
    elif big_trend == "bull" and cand["direction"] == "做空":
        trend_score = 70
    elif big_trend == "side":
        trend_score = 25  # 趋势不明确给低分
    if atr > 0:
        # 额外加成：净方向与趋势同向且越强越高分（0 ~ +30），仅作正向加成
        # 关键修复：bonus 必须下界钳制为 0，否则净方向为负时会把 trend_score 拖成负数，
        # 导致「大周期跌+支撑反转」这种标准场景被异常低分误杀。
        net_atr = cand.get("net_atr") or 0.0
        bonus = max(0.0, min(net_atr / (3 * TREND_NET_ATR), 1.0)) * 30
        trend_score = min(trend_score + bonus, 100)

    total = (sr_score * SCORE_W_SR + form_score * SCORE_W_FORM + trend_score * SCORE_W_TREND) / 100.0
    return round(total, 1)


def passes_structure_filter(kdata, idx, atr, direction):
    """
    坑点5：Price Action 教学禁区结构过滤
    ① 释放 spike：信号前 SPIKE_LOOKBACK 根内有单根 range > SPIKE_ATR_MULT*atr 的大K -> 禁
    ② 无积累抄底：仅对做多(抄底)要求 base 区间 <= BASE_RANGE_MULT*atr，
       否则“下跌后无筹码积累直接抄底”= 禁
    """
    # 禁区1：释放 spike（v2.1.4 Bug6 修复：循环含反转K自身 idx；且用实体占比精确化，避免误禁长影线 Hammer）
    lo = max(0, idx - SPIKE_LOOKBACK)
    for i in range(lo, idx + 1):
        rng = kdata[i][2] - kdata[i][3]
        if rng > SPIKE_ATR_MULT * atr:
            body_i = abs(kdata[i][4] - kdata[i][1])  # 实体大小
            if body_i / rng > SPIKE_BODY_RATIO:       # 实体占比大=失控大K；长影线反转K实体极小，放行
                return False
    # 禁区2：抄底/摸顶需有积累（v2.1.4 缺陷2 修复：做空对称补顶部横盘 base 校验）
    if direction in ("做多", "做空"):
        bl = max(0, idx - BASE_LOOKBACK)
        base_high = max(kdata[i][2] for i in range(bl, idx))
        base_low = min(kdata[i][3] for i in range(bl, idx))
        base_range = base_high - base_low
        if base_range > BASE_RANGE_MULT * atr:
            return False
    return True


def detect_candidate(kdata, idx, big_trend, atr):
    """
    在指定已收盘K线(idx)上检测候选信号。
    返回 None 或 dict（含 pattern/direction/strength/entry/high/low/s_list/r_list/sr_level/logic）。
    """
    o, h, l, c = kdata[idx][1], kdata[idx][2], kdata[idx][3], kdata[idx][4]
    # SR 只基于该K之前的K线（不含自身），避免自参考/未来函数
    s_list, r_list = find_support_resistance(kdata[:idx], SR_LOOKBACK)
    price_near_s = price_near_level(c, s_list, SR_TOLERANCE_PCT)
    price_near_r = price_near_level(c, r_list, SR_TOLERANCE_PCT)

    pin_signal = check_reversal_candle(o, h, l, c, PINBAR_WICK_MULTI)
    prev = kdata[idx - 1]
    engulf_signal = check_engulfing(prev[1], prev[4], o, c, ENGULF_RATIO)

    # 场景1：大周期下跌(bear) + 价格靠近支撑 → 看涨形态（逆小势、在支撑做反转）
    if big_trend == "bear" and price_near_s:
        bull_rc = pin_signal and pin_signal[0] in ("bull_hammer", "bull_pinbar")
        if bull_rc or engulf_signal == "bull_engulf":
            sig_type = pin_signal[1] if bull_rc else "看涨吞没(做多)"
            strength = pin_signal[1] if bull_rc else "标准吞没形态"
            candle_type = pin_signal[0] if bull_rc else "engulf"
            return dict(pattern=sig_type, direction="做多", strength=strength,
                        candle_type=candle_type,
                        entry=c, open=o, high=h, low=l, s_list=s_list, r_list=r_list,
                        sr_level=nearest(s_list, c),
                        logic="大周期下跌趋势，回调至支撑区域出现反转形态")
    # 场景2：大周期上涨(bull) + 价格靠近阻力 → 看跌形态
    if big_trend == "bull" and price_near_r:
        bear_rc = pin_signal and pin_signal[0] in ("bear_shooting_star", "bear_pinbar")
        if bear_rc or engulf_signal == "bear_engulf":
            sig_type = pin_signal[1] if bear_rc else "看跌吞没(做空)"
            strength = pin_signal[1] if bear_rc else "标准吞没形态"
            candle_type = pin_signal[0] if bear_rc else "engulf"
            return dict(pattern=sig_type, direction="做空", strength=strength,
                        candle_type=candle_type,
                        entry=c, open=o, high=h, low=l, s_list=s_list, r_list=r_list,
                        sr_level=nearest(r_list, c),
                        logic="大周期上涨趋势，反弹至阻力区域出现反转形态")
    return None


def check_confirmation(pend, next_c):
    """
    v2.0 + v2.1 优化3：下一根已收盘，确认候选是否有效。
    做多：下根未打止损 + 收盘守在入场上方 + 下根 close>open（方向对齐）
    做空：下根未打止损 + 收盘守在入场下方 + 下根 close<open（方向对齐）
    缺陷3开关 CONFIRM_ALLOW_INTRABAR_SL_HIT：
      False（严格，默认）：以盘中最低/最高价是否触及SL判定（插针即失效）
      True （宽松）：仅以收盘价是否跌破SL判定（允许盘中插针收回）
    """
    entry = pend["entry"]
    sl = pend["sl"]
    direction = pend["direction"]
    n_open, n_high, n_low, n_close = next_c[1], next_c[2], next_c[3], next_c[4]
    if direction == "做多":
        if not CONFIRM_ALLOW_INTRABAR_SL_HIT:
            if n_low <= sl:
                return False
        else:
            if n_close <= sl:
                return False
        if n_close < entry * (1 - CONFIRM_SLIPPAGE):
            return False
        if ENABLE_CONFIRM_DIRECTION and n_close <= n_open:
            return False
        return True
    else:
        if not CONFIRM_ALLOW_INTRABAR_SL_HIT:
            if n_high >= sl:
                return False
        else:
            if n_close >= sl:
                return False
        if n_close > entry * (1 + CONFIRM_SLIPPAGE):
            return False
        if ENABLE_CONFIRM_DIRECTION and n_close >= n_open:
            return False
        return True


def fire_signal(cand, interval, confirmed, resonance_hit=False):
    """真正发信号：入库 + 飞书卡片 + signal_store；记入 fired_keys 防重复。
    注：QQ 共振合并邮件改由 maybe_fire_resonance 在共振命中时统一发送。"""
    candle_ts = cand["candle_ts"]
    dt_str = datetime.fromtimestamp(int(candle_ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    entry = cand["entry"]
    record_signal_safe(
        signal_time=dt_str, symbol=SYMBOL, interval=interval,
        pattern=cand["pattern"], direction=cand["direction"],
        entry=entry, sl=cand["sl"], tp1=cand["tp1"], tp2=cand["tp2"],
    )
    send_feishu(cand, interval, confirmed, resonance_hit=resonance_hit)
    fired_keys.add((interval, candle_ts))
    notified[interval].add(candle_ts)
    candle_type_ref = cand.get("candle_type", "")
    score_ref = cand.get("score", "—")
    print(f"✅ [{dt_str}] {interval} | {cand['pattern']}({candle_type_ref}) | 价格:{entry:.2f} | score={score_ref} | 确认={confirmed}")


def compute_sl_tp(cand, atr):
    """
    坑点3&6：ATR 自适应止损 + 缺阻力时 TP 回退。
    v2.1.1 修复：TP1 保底盈亏比 MIN_RR_FOR_TP1。
      保底 tp1 距离基于【真实 SL 距离】(含影线) × MIN_RR，而非裸 ATR 倍数，
      避免深插针形态因影线过长导致真实 SL 距离被放大、保底 R:R 名不副实。
      有阻力且阻力距离 >= 保底时使用阻力位（R:R 更优），否则用保底目标。
    返回 (sl, tp1, tp2)
    """
    entry = cand["entry"]
    if cand["direction"] == "做多":
        sl = cand["low"] - SL_ATR_MULT * atr
        sl_dist = entry - sl                          # 真实止损距离（含影线，基准一致）
        tp1_min_dist = sl_dist * MIN_RR_FOR_TP1       # 保底 R:R = MIN_RR_FOR_TP1
        above = [rv for rv in cand["r_list"] if rv > entry]
        tp1_resist = min(above) if above else None
        tp1 = tp1_resist if (tp1_resist is not None and (tp1_resist - entry) >= tp1_min_dist) else entry + tp1_min_dist
        tp2 = tp1 + TP_ATR_MULT * atr
    else:
        sl = cand["high"] + SL_ATR_MULT * atr
        sl_dist = sl - entry
        tp1_min_dist = sl_dist * MIN_RR_FOR_TP1
        below = [sv for sv in cand["s_list"] if sv < entry]
        tp1_resist = max(below) if below else None
        tp1 = tp1_resist if (tp1_resist is not None and (entry - tp1_resist) >= tp1_min_dist) else entry - tp1_min_dist
        tp2 = tp1 - TP_ATR_MULT * atr
    return sl, tp1, tp2


def get_big_trend():
    """获取4H大周期趋势，带缓存减少请求"""
    global cache_4h_kline, cache_4h_expire
    now_ts = time.time()
    # 缓存120秒，避免疯狂请求4H
    if cache_4h_kline is not None and now_ts < cache_4h_expire:
        kdata = cache_4h_kline
    else:
        raw = get_klines_retry(TREND_TIMEFRAME, limit=80)
        if not raw:
            return "side", None
        kdata = parse_kline_list(raw)
        cache_4h_kline = kdata
        cache_4h_expire = now_ts + 120
    trend = judge_trend(kdata, len(kdata) - 1, TREND_LOOKBACK)
    return trend, kdata


def scan_interval(interval):
    """单周期扫描主逻辑（v2：已收盘K检测 + 下一根确认 + 冷却 + 结构过滤）"""
    _age_out_state()  # v2.1.4 Bug8：定期老化清理全局状态，防实盘内存无限增长
    big_trend, _ = get_big_trend()
    raw_k = get_klines_retry(interval, limit=SR_LOOKBACK + 25)
    if not raw_k or len(raw_k) < SR_LOOKBACK + 2:
        return
    kdata = parse_kline_list(raw_k)
    n = len(kdata)
    # 最新“已收盘”K = 倒数第2根（最后一根是未收盘当根）
    closed_idx = n - 2
    if closed_idx < SR_LOOKBACK:
        return
    last_closed_ts = kdata[closed_idx][0]

    # ---- 分支A：有待确认信号，检查下一根是否已收盘并确认 ----
    if WAIT_NEXT_CANDLE_CONFIRM and interval in pending_signals:
        pend = pending_signals[interval]
        if last_closed_ts > pend["candle_ts"]:
            # 下一根已收盘，做确认判定
            if check_confirmation(pend, kdata[closed_idx]):
                maybe_fire_resonance(interval, pend)
            else:
                # 确认失效：标记已评估，避免 pinbar 在后续扫描反复重新进入 pending
                fired_keys.add((interval, pend["candle_ts"]))
            pending_signals.pop(interval, None)
            # v2.1.4 Bug1 修复：确认完成（已 pop）后继续扫描本轮新候选，不再 return 漏信号
        else:
            return  # 下一根尚未收盘，才阻断本轮新候选扫描，避免 pending 堆叠

    # ---- 每根收盘K只处理一次（坑点4：防重复/防堆叠）----
    if last_scanned_ts.get(interval) == last_closed_ts:
        return
    last_scanned_ts[interval] = last_closed_ts

    # ---- 冷却中（坑点7）----
    if time.time() < cooldown_until.get(interval, 0):
        return

    atr = calc_atr(kdata, closed_idx, ATR_PERIOD)
    if atr is None:
        atr = max(kdata[closed_idx][2] - kdata[closed_idx][3], 0.5)

    # ---- 在最近 N 根收盘K上找候选（坑点4：不只看最后一根）----
    cand = None
    scan_from = max(SR_LOOKBACK, closed_idx - SCAN_HISTORY_CLOSED + 1)
    for idx in range(closed_idx, scan_from - 1, -1):
        if (interval, kdata[idx][0]) in fired_keys:
            continue
        c = detect_candidate(kdata, idx, big_trend, atr)
        if c:
            c["candle_idx"] = idx
            c["candle_ts"] = kdata[idx][0]
            c["atr"] = atr
            cand = c
            break
    if not cand:
        return

    # ---- 结构过滤（Price Action 教学禁区，坑点5）----
    if ENABLE_STRUCTURE_FILTER and not passes_structure_filter(kdata, cand["candle_idx"], atr, cand["direction"]):
        return

    # ---- v2.1 优化2：震荡区间中段过滤（Price Action 教学："middle of a range = noise"）----
    if ENABLE_RANGE_MIDDLE_FILTER and is_in_middle_of_range(kdata, cand["candle_idx"]):
        print(f"⏸  [中段噪声] {interval} 跳过：当前K处于最近{RANGE_FILTER_WINDOW}根区间 [{RANGE_MIDDLE_LO:.0%},{RANGE_MIDDLE_HI:.0%}] 内，形态无上下文")
        return

    # ---- v2.1 优化4：信号强度评分（前置拦截噪声，比冷却更早期）----
    # 收集净方向强度供评分使用（用最近 lookback 根的净位移 / atr）
    lookback_start = max(0, cand["candle_idx"] - TREND_LOOKBACK)
    closes_slice = [kdata[i][4] for i in range(lookback_start, cand["candle_idx"] + 1)]
    net = abs(closes_slice[-1] - closes_slice[0])
    cand["net_atr"] = net / atr if atr > 0 else 0.0
    score = grade_signal(cand, kdata, big_trend, atr)
    cand["score"] = score
    if ENABLE_SIGNAL_SCORE and score < MIN_SIGNAL_SCORE:
        print(f"⏸  [评分拦截] {interval} 跳过：score={score} < MIN={MIN_SIGNAL_SCORE}（{cand['pattern']}）")
        return

    # ---- 同SR区复用过滤（坑点7）----
    sr = cand.get("sr_level")
    if sr is not None:
        last_sr = last_sr_level.get(interval)
        if last_sr is not None and abs(sr - last_sr) / sr < SR_ZONE_REUSE_TOL:
            return
        last_sr_level[interval] = sr

    # ---- ATR 自适应 SL / TP（坑点3&6）----
    sl, tp1, tp2 = compute_sl_tp(cand, atr)
    cand.update(sl=sl, tp1=tp1, tp2=tp2, big_trend=big_trend)

    if WAIT_NEXT_CANDLE_CONFIRM:
        # 进入 pending，等下一根收盘确认后再发
        pending_signals[interval] = cand
        return

    maybe_fire_resonance(interval, cand)


def _age_out_state():
    """v2.1.4 Bug8：定期清理过期状态，防实盘 7x24 内存无限增长"""
    global last_cleanup_ts
    now = time.time()
    if now - last_cleanup_ts < STATE_CLEANUP_INTERVAL_SEC:
        return
    last_cleanup_ts = now
    now_ms = int(now * 1000)
    before = len(fired_keys)
    fired_keys_copy = {(iv, ts) for (iv, ts) in fired_keys if now_ms - ts < STATE_KEEP_MS}
    fired_keys.clear()
    fired_keys.update(fired_keys_copy)
    for iv in list(last_scanned_ts.keys()):
        if now_ms - last_scanned_ts[iv] > STATE_KEEP_MS:
            last_scanned_ts.pop(iv, None)
    for iv in list(cooldown_until.keys()):
        if cooldown_until[iv] < now:
            cooldown_until.pop(iv, None)
    print(f"🧹 [状态老化] fired_keys {before}→{len(fired_keys)}")


def reset_state():
    """仅供测试用：清空所有运行时状态"""
    pending_signals.clear()
    last_scanned_ts.clear()
    cooldown_until.clear()
    last_sr_level.clear()
    fired_keys.clear()
    for _k in notified:
        notified[_k].clear()
    recent_signal.clear()
    qq_sent_pairs.clear()
    recent_reso.clear()
    global cache_4h_kline, cache_4h_expire
    cache_4h_kline = None
    cache_4h_expire = 0


if __name__ == "__main__":
    print("=" * 75)
    print("Pinbar+吞没 价格行为监控 v2.1（Hammer/Shooting Star严格定义+区间中段过滤+信号评分）")
    print(f"★ 启动命令：{START_CMD}")
    print(f"监控周期: {SCAN_INTERVALS} | 大周期:{TREND_TIMEFRAME}")
    print(f"Pinbar影线倍数:{PINBAR_WICK_MULTI} | 反向影线比阈值:{HAMMER_OPPOSITE_WICK_RATIO}（Hammer/Shooting Star严格）")
    print(f"区间中段过滤:{ENABLE_RANGE_MIDDLE_FILTER}({RANGE_MIDDLE_LO:.0%}~{RANGE_MIDDLE_HI:.0%}) | 信号评分:{ENABLE_SIGNAL_SCORE}(min={MIN_SIGNAL_SCORE})")
    print(f"等待确认:{WAIT_NEXT_CANDLE_CONFIRM} | 趋势回看:{TREND_LOOKBACK} | ATR止损:{SL_ATR_MULT}× | 冷却:{SIGNAL_COOLDOWN_SEC}s")
    print(f"结构过滤:{ENABLE_STRUCTURE_FILTER} | 扫描历史:{SCAN_HISTORY_CLOSED}根")
    print("=" * 75)

    last_check_sec = -1
    while True:
        now = datetime.now()
        sec = now.second
        # 每10秒一轮扫描
        if sec % 10 == 0 and sec != last_check_sec:
            for tf in SCAN_INTERVALS:
                scan_interval(tf)
            last_check_sec = sec
        time.sleep(0.1)

