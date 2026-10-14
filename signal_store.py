# -*- coding: utf-8 -*-
"""
signal_store.py
统一信号入库模块：所有交易系统共用。

职责：
1. 接收各系统传入的标准化交易信号字段
2. 归一化方向、价格、时间
3. 按自然日写入本地 JSON 文件（追加，线程安全 + 跨进程文件锁）

存储位置：
    <项目根>/signal_data/signals_YYYYMMDD.json

字段（共 10 项，与日报表表格列一一对应）：
    time      信号时间      "YYYY-MM-DD HH:MM:SS"
    symbol    品种          "" / "PAXGUSDT"
    interval  K线周期       "5m" / "15m" / "1m/15m" 等
    pattern   形态/架构名   "OOPS做多" / "H2回调买进" 等
    direction 方向          "做多" / "做空"
    entry     入场价        float 或 None
    sl        止损价        float 或 None
    tp1       止盈TP1       float 或 None
    tp2       止盈TP2       float 或 None
"""
import os
import json
import time
import threading
from datetime import datetime

# 项目根目录（本文件所在目录）
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据目录
DATA_DIR = os.path.join(ROOT_DIR, "signal_data")
# 跨进程锁文件
LOCK_PATH = os.path.join(DATA_DIR, ".write.lock")

# 全局线程锁：同一进程内多个线程并发写入时互斥
_thread_lock = threading.Lock()

# 方向归一化映射
_DIR_BUY = {"buy", "long", "bull", "做多", "多", "1", "看涨", "低九"}
_DIR_SELL = {"sell", "short", "bear", "做空", "空", "-1", "看跌", "高九"}


class _FileLock:
    """跨进程文件锁：Linux/macOS 用 fcntl.flock，Windows 用 msvcrt.locking。

    与 threading.Lock 配合：线程锁处理同进程并发，文件锁处理多进程并发。
    """

    def __init__(self, path):
        self.path = path
        self.fp = None
        self._is_fcntl = False

    def __enter__(self):
        self.fp = open(self.path, "a+")
        # Linux / macOS：fcntl.flock（阻塞等待）
        try:
            import fcntl
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX)
            self._is_fcntl = True
            return self
        except (ImportError, OSError):
            pass
        # Windows：msvcrt.locking（阻塞等待）
        try:
            import msvcrt
            self.fp.seek(0)
            msvcrt.locking(self.fp.fileno(), msvcrt.LK_LOCK, 1)
            return self
        except (ImportError, OSError):
            # 极端情况：两套锁都不可用，退化为无锁（仍受线程锁保护）
            return self

    def __exit__(self, *args):
        try:
            if self._is_fcntl:
                import fcntl
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt
                self.fp.seek(0)
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            self.fp.close()
        except Exception:
            pass


def _normalize_direction(direction):
    """把各系统不同的方向写法统一为「做多/做空」。"""
    if direction is None:
        return None
    if isinstance(direction, str):
        d = direction.strip().lower()
        if d in _DIR_BUY:
            return "做多"
        if d in _DIR_SELL:
            return "做空"
        if "做多" in direction or direction == "多":
            return "做多"
        if "做空" in direction or direction == "空":
            return "做空"
    else:
        if direction == 1 or direction == "1":
            return "做多"
        if direction == -1 or direction == "-1":
            return "做空"
    return direction


def _normalize_price(price):
    """价格归一化：None/空串→None；否则转 float（保留 2 位）。"""
    if price is None or price == "":
        return None
    try:
        return round(float(price), 2)
    except (TypeError, ValueError):
        return None


# 支持的时间格式（统一补全为秒级，避免无秒时间被报表漏掉）
_TIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d",
]


def _normalize_time(signal_time):
    """时间归一化：统一转成 "YYYY-MM-DD HH:MM:SS"。

    支持三种入参：
    1. datetime 对象
    2. 字符串（见 _TIME_FORMATS）
    3. int/float 时间戳（毫秒或秒，自动区分）—— 例如接口 K 线的 close_time（毫秒）

    毫秒/秒时间戳按本地时区转为可读时间。
    """
    if signal_time is None:
        return None
    if isinstance(signal_time, datetime):
        return signal_time.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(signal_time, str):
        s = signal_time.strip()
        for fmt in _TIME_FORMATS:
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        # 无法识别的时间原样返回，交由报表侧兜底过滤
        return s
    # int / float 时间戳（毫秒或秒）
    if isinstance(signal_time, (int, float)):
        try:
            ts = float(signal_time)
            # 大于 1e12 视为毫秒时间戳（约 2001 年之后的毫秒值）
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError, OverflowError):
            pass
    return str(signal_time)


def _data_file(date_str=None):
    """返回指定日期的数据文件绝对路径，默认今天。"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(DATA_DIR, f"signals_{date_str}.json")


def _load(date_str):
    """读取指定日期已入库信号列表，文件不存在或损坏返回空列表。"""
    path = _data_file(date_str)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save(date_str, signals):
    """写入指定日期信号列表（覆盖写）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_data_file(date_str), "w", encoding="utf-8") as f:
        json.dump(signals, f, ensure_ascii=False, indent=2)


def record_signal(signal_time, symbol, interval, pattern, direction,
                  entry=None, sl=None, tp1=None, tp2=None):
    """入库一条交易信号。

    Args:
        signal_time: 信号时间（datetime 或 "YYYY-MM-DD HH:MM:SS"）
        symbol:      品种，如 "" / "PAXGUSDT"
        interval:    K线周期，如 "5m" / "15m" / "1m/15m"
        pattern:     形态/架构名称
        direction:   方向（各系统写法不同，内部归一化为「做多/做空」）
        entry/sl/tp1/tp2: 价格，可为 None（缺失，报表显示 "-"）
    """
    time_str = _normalize_time(signal_time)
    if time_str is None:
        return

    # 取信号时间里的日期作为落库日期（跨零点信号归入信号日期而非当前日期）
    date_key = time_str[:10].replace("-", "") if len(time_str) >= 10 else datetime.now().strftime("%Y%m%d")

    record = {
        "time": time_str,
        "symbol": symbol if symbol else "",
        "interval": interval if interval else "",
        "pattern": pattern if pattern else "",
        "direction": _normalize_direction(direction) or "",
        "entry": _normalize_price(entry),
        "sl": _normalize_price(sl),
        "tp1": _normalize_price(tp1),
        "tp2": _normalize_price(tp2),
    }

    with _thread_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        with _FileLock(LOCK_PATH):
            signals = _load(date_key)
            # 去重：完全相同的记录（9字段全等）跳过，保证「飞书推一条、统计记一条」
            if record in signals:
                print(f"[去重] 跳过重复信号: {record.get('time')} {record.get('pattern')} "
                      f"{record.get('direction')} @ {record.get('entry')}")
                return
            signals.append(record)
            _save(date_key, signals)


def load_signals(date_str):
    """供报表/外部读取指定日期的信号列表（加锁读，保证一致）。"""
    with _thread_lock:
        return _load(date_str)


if __name__ == "__main__":
    record_signal(datetime.now(), "", "15m", "测试信号", "BUY",
                  entry=3000.5, sl=2990.0, tp1=3010.0)
    print("测试写入完成，当前数据：")
    print(json.dumps(_load(datetime.now().strftime("%Y%m%d")), ensure_ascii=False, indent=2))