# -*- coding: utf-8 -*-
"""
resonance.py
信号共振检测常驻进程。

职责：
1. 轮询 signal_data/ 下已入库的信号（复用 signal_store.load_signals）
2. 检测 5 分钟滑动窗口内「同方向信号数 ≥ 3」的共振
3. 触发时向独立飞书群推送简短提醒，并同步发一份到 QQ 邮箱

计数口径（2026-09-16 按用户决策调整，可一键回滚）：
- 见下方 DEDUP_BY_PATTERN 开关。
- 当前 False：窗口内每条信号各计 1 票（同一形态推送 N 次计 N 票）。
  用户 2026-09-16 决策：先按条数跑几天，效果不好再改回按形态去重。
- 原版设计：按 (symbol, pattern, direction) 去重，同一形态只计 1 票。

其他约定：
- 独立进程运行，现有各套系统零改动
- 品种合并： 与 PAXGUSDT 一起统计，不隔离（保持现状，与 PRD FR-4 相反）
- 冷却：同方向触发后 15 分钟内不重复提醒（记在内存，进程重启即清零）
- 跨零点：读取当天 + 昨天两个 JSON
"""
import os
import time
import requests
from datetime import datetime, timedelta

import signal_store

try:
    import qq_mail
except Exception:  # noqa: BLE001  邮件通道缺失不应影响共振主流程
    qq_mail = None


# ====================== 配置 ======================
RESONANCE_WEBHOOK = os.environ.get("RESONANCE_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE_WITH_YOURS")
WINDOW_MINUTES = 5          # 共振窗口（分钟）
MIN_COUNT = 3               # 触发阈值（口径见 DEDUP_BY_PATTERN）
COOLDOWN_MINUTES = 15       # 同方向冷却（分钟）
SCAN_INTERVAL = 10          # 扫描间隔（秒）

# 计数口径开关（回滚只需把这一行改回 True，其余代码无需改动）
#   False = 按条数计票（当前）：同一形态在窗口内推送 N 次 → 计 N 票
#   True  = 按形态去重（原版）：同 (symbol, pattern, direction) 只计 1 票
DEDUP_BY_PATTERN = True

# 方向标签映射（signal_store 归一化后的值）
_DIR_LABEL = {"做多": "看多", "做空": "看空"}

# 单实例锁文件
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".resonance.lock")

# 同方向最近一次推送时间戳 {direction: ts}
_last_push = {}


# ====================== 时间工具 ======================
def _parse_time(s):
    """解析信号时间字符串为 datetime，失败返回 None。"""
    if not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


# ====================== 数据读取 ======================
def _load_recent_signals():
    """读取当天 + 昨天已入库信号（跨零点场景）。"""
    now = datetime.now()
    days = [now.strftime("%Y%m%d"), (now - timedelta(days=1)).strftime("%Y%m%d")]
    signals = []
    for d in days:
        signals.extend(signal_store.load_signals(d))
    return signals


# ====================== 共振检测 ======================
def _find_resonance(signals, now):
    """在窗口内按方向聚合信号。

    返回 {direction: [{"signal": 原始记录, "time": datetime}, ...]}，
    仅含数量达标的信号。计数口径由 DEDUP_BY_PATTERN 决定。
    """
    window_start = now - timedelta(minutes=WINDOW_MINUTES)
    buckets = {"做多": [], "做空": []}
    seen = {"做多": set(), "做空": set()}

    for s in signals:
        t = _parse_time(s.get("time", ""))
        if t is None:
            continue
        if t < window_start or t > now:
            continue
        direction = s.get("direction", "")
        if direction not in buckets:
            continue
        if DEDUP_BY_PATTERN:
            # 同 (symbol, pattern, direction) 只计 1 次，避免同一指标扫描重复推送被重复计数
            key = (s.get("symbol"), s.get("pattern"), direction)
            if key in seen[direction]:
                continue
            seen[direction].add(key)
        buckets[direction].append({"signal": s, "time": t})

    result = {}
    for direction, items in buckets.items():
        items.sort(key=lambda x: x["time"])
        if len(items) >= MIN_COUNT:
            result[direction] = items
    return result


# ====================== 推送 ======================
def _build_text(direction, items):
    """构造简短共振提醒文本。

    计数用「原始条数」（标题里的 ×N），但列表按 (形态, 品种, 周期) 折叠并标注 ×N，
    避免同一形态被推送多次时整段重复、反而读不出信息。
    """
    label = _DIR_LABEL.get(direction, direction)
    start = items[0]["time"]
    end = items[-1]["time"]
    lines = [
        f"⚡ 信号共振提醒【{label} × {len(items)}】",
        "",
        f"窗口：{start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%H:%M')}",
        "",
    ]
    folded = {}
    for it in items:
        s = it["signal"]
        k = (s.get("pattern", ""), s.get("symbol", ""), s.get("interval", ""))
        folded.setdefault(k, []).append(it["time"])
    for (pat, sym, iv), times in folded.items():
        cnt = f" ×{len(times)}" if len(times) > 1 else ""
        lines.append(f"• {pat} | {sym} {iv} | {times[0].strftime('%H:%M')}{cnt}")
    return "\n".join(lines)


def _push(text):
    """推送到独立飞书群。返回是否真正送达。

    注意：飞书自定义机器人**业务失败时也会返回 HTTP 200**，必须校验响应体
    code == 0，否则"推送成功"是假的（本项目其他系统踩过同一个坑）。
    """
    payload = {"msg_type": "text", "content": {"text": text}}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        resp = requests.post(RESONANCE_WEBHOOK, json=payload, timeout=30)
    except Exception as e:
        print(f"[{now}] ❌ 共振提醒推送异常: {e}")
        return False

    if resp.status_code != 200:
        print(f"[{now}] ❌ 共振提醒推送失败 HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    try:
        body = resp.json()
    except Exception:
        print(f"[{now}] ❌ 共振提醒推送失败：响应非 JSON: {resp.text[:200]}")
        return False
    if not isinstance(body, dict) or body.get("code") != 0:
        print(f"[{now}] ❌ 共振提醒推送失败 业务码 {body.get('code') if isinstance(body, dict) else '?'}"
              f": {str(body)[:200]}")
        return False
    print(f"[{now}] ✅ 共振提醒推送成功")
    return True


def _push_mail(direction, items, text):
    """共振提醒同步发一份到 QQ 邮箱（未配置时静默跳过）。"""
    if qq_mail is None or not qq_mail.is_enabled():
        return
    label = _DIR_LABEL.get(direction, direction)
    subject = (f"⚡ 信号共振提醒【{label} × {len(items)}】"
               f" {items[-1]['time'].strftime('%m-%d %H:%M')}")
    body = f"{text}\n\n— 信号共振检测 resonance.py\n【学习交流，不作为交易依据】"
    qq_mail.send_async(subject, body)


# ====================== 单实例锁 ======================
class _InstanceLock:
    """跨平台单实例保护：Linux 用 fcntl，Windows 用 msvcrt。

    防止误跑多个共振进程重复推送。
    """

    def __init__(self, path):
        self.path = path
        self.fp = None
        self._is_fcntl = False

    def acquire(self) -> bool:
        # Linux / macOS：fcntl.flock（非阻塞）
        try:
            import fcntl
            self.fp = open(self.path, "a+")
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._is_fcntl = True
            return True
        except (ImportError, OSError):
            pass
        # Windows：msvcrt.locking
        try:
            import msvcrt
            self.fp = open(self.path, "a+")
            self.fp.seek(0)
            msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except (ImportError, OSError):
            return False

    def release(self):
        try:
            if not self.fp:
                return
            if self._is_fcntl:
                import fcntl
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            else:
                import msvcrt
                self.fp.seek(0)
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
            self.fp.close()
        except Exception:
            pass


# ====================== 主循环 ======================
def main():
    print("=" * 60)
    print("信号共振检测进程启动")
    print(f"共振窗口: {WINDOW_MINUTES} 分钟")
    print(f"计数口径: {'按形态去重（同形态只计 1 票）' if DEDUP_BY_PATTERN else '按条数（每条信号计 1 票）'}")
    print(f"触发阈值: ≥{MIN_COUNT} 票（同方向）")
    print(f"品种: 合并统计（ + PAXGUSDT）")
    print(f"同方向冷却: {COOLDOWN_MINUTES} 分钟")
    print(f"扫描间隔: {SCAN_INTERVAL} 秒")
    if qq_mail is not None and qq_mail.is_enabled():
        print(f"提醒通道: 飞书 + QQ邮箱（{qq_mail.QQ_EMAIL_SENDER} → {qq_mail.QQ_EMAIL_RECEIVER}）")
    else:
        print("提醒通道: 飞书（QQ邮箱未配置，已跳过）")
    print("=" * 60)

    lock = _InstanceLock(LOCK_FILE)
    if not lock.acquire():
        print("⚠️ 检测到已有共振进程在运行，本进程退出。")
        return

    try:
        while True:
            try:
                now = datetime.now()
                signals = _load_recent_signals()
                resonance = _find_resonance(signals, now)

                for direction, items in resonance.items():
                    # 冷却检查
                    last = _last_push.get(direction, 0)
                    if time.time() - last < COOLDOWN_MINUTES * 60:
                        print(f"[{now:%H:%M:%S}] {_DIR_LABEL.get(direction)}共振已触发但处于冷却期，跳过")
                        continue
                    text = _build_text(direction, items)
                    if _push(text):
                        _last_push[direction] = time.time()
                        _push_mail(direction, items, text)

            except Exception as e:
                import traceback
                traceback.print_exc()

            time.sleep(SCAN_INTERVAL)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
