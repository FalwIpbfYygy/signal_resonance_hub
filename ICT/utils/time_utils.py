"""时区转换 + 黄金交易时段识别"""
from datetime import datetime, timezone, timedelta
from typing import Tuple

# GMT/UTC 时区
UTC = timezone.utc
# 北京时间 UTC+8
CST = timezone(timedelta(hours=8))


def utc_to_gmt(dt: datetime) -> datetime:
    """将任意datetime统一转换为GMT时间（用于时段判断）"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def get_session_gmt(dt: datetime) -> Tuple[str, int]:
    """
    根据GMT时间判断当前黄金交易时段
    返回: (时段名称, 权重级别)
    权重级别: 1=最高, 2=高, 3=低
    """
    dt = utc_to_gmt(dt)
    hour = dt.hour
    minute = dt.minute
    time_val = hour + minute / 60.0

    # 伦敦开盘 (08:00-10:00 GMT) → 最高权重
    if 8.0 <= time_val < 10.0:
        return ("伦敦开盘", 1)
    # 伦敦定盘价上午 (10:00-11:00 GMT)
    if 10.0 <= time_val < 11.0:
        return ("伦敦定盘(AM)", 2)
    # 伦敦-纽约重叠 (13:00-17:00 GMT) → 最高权重
    if 13.0 <= time_val < 17.0:
        return ("伦敦-纽约重叠", 1)
    # 伦敦定盘价下午 (15:00 GMT附近)
    if 14.5 <= time_val < 15.5:
        return ("伦敦定盘(PM)", 2)
    # 纽约盘 (13:00-22:00 GMT，排除重叠部分已判断)
    if 17.0 <= time_val < 22.0:
        return ("纽约盘", 2)
    # 伦敦盘非开盘 (10:00-13:00 GMT)
    if 10.0 <= time_val < 13.0:
        return ("伦敦盘", 2)
    # 亚洲盘 (00:00-08:00 GMT) → 低权重
    return ("亚洲盘", 3)


def get_session_display(session: str) -> str:
    """获取时段的中文标签"""
    labels = {
        "亚洲盘": "🌙 亚洲盘（低权重）",
        "伦敦盘": "🇬🇧 伦敦盘",
        "伦敦开盘": "🚀 伦敦开盘（最高权重）",
        "伦敦定盘(AM)": "📊 伦敦定盘AM",
        "伦敦定盘(PM)": "📊 伦敦定盘PM",
        "伦敦-纽约重叠": "🏛️ 伦敦-纽约重叠（最高权重）",
        "纽约盘": "🇺🇸 纽约盘",
    }
    return labels.get(session, session)


def get_min_confirmations(session_weight: int) -> int:
    """根据时段权重返回最小确认信号数"""
    from config import ASIA_MIN_CONFIRMATIONS, LONDON_NY_MIN_CONFIRMATIONS
    if session_weight >= 3:  # 亚洲盘
        return ASIA_MIN_CONFIRMATIONS
    return LONDON_NY_MIN_CONFIRMATIONS


def ms_to_datetime(ms_timestamp: int) -> datetime:
    """毫秒时间戳 → datetime"""
    return datetime.fromtimestamp(ms_timestamp / 1000.0, tz=UTC)


def format_cst(dt: datetime) -> str:
    """格式化为北京时间字符串"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    cst_dt = dt.astimezone(CST)
    return cst_dt.strftime("%Y-%m-%d %H:%M:%S")


def format_gmt(dt: datetime) -> str:
    """格式化为GMT时间字符串"""
    gmt_dt = utc_to_gmt(dt)
    return gmt_dt.strftime("%Y-%m-%d %H:%M:%S GMT")