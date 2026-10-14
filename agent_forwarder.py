# -*- coding: utf-8 -*-
"""
agent_forwarder.py
TRAE ->  Trading Decision Agent 薄转发层（常驻进程）。

职责：
  轮询 TRAE 的 signal_data/ 归一化信号，按字段映射后 POST 到 Agent 的
  /webhook/signal 端点（带共享 secret 头）。Agent 收到后产出决策建议并推送飞书。

设计铁律（与本项目其他系统一致）：
  - 不修改 TRAE 任何既有代码（record_signal 等保持原样，本脚本独立轮询）。
  - 零代理：requests.Session 设 trust_env=False + proxies={}，服务器直连 Agent。
  - 幂等：用稳定 signal_id（symbol|pattern|time）去重；Agent 侧对 signal_id 做
    INSERT OR REPLACE，重复 POST 也不会产生重复决策。
  - 单实例：文件锁防止误跑多个转发进程重复推送。

字段映射（TRAE 归一化记录 -> Agent webhook 负载）：
  TRAE.direction(做多/做空) -> Agent.direction(long/short)
  TRAE.pattern(形态名)      -> Agent.setup_type（经验反馈按此聚合）
  TRAE.entry/sl/tp1         -> Agent.entry_zone/sl_zone/tp_zone
  TRAE 无 signal_id/raw_score -> 由本脚本派生稳定 id / 给默认强度分

部署：
  1. 拷贝到 ~/TRAE/agent_forwarder.py（与 signal_store.py 同目录）
  2. 在 restart_trade.sh 的 kill 模式里加入 agent_forwarder.py，并在末尾 nohup 启动
  3. 确认 AGENT_SECRET 与 TRADING_AGENT/config/settings.yaml 的 agent_webhook.secret 一致
"""
import os
import sys
import time
import json
import fcntl
from datetime import datetime, timedelta

import requests
import signal_store


# ====================== 配置 ======================
# Agent 服务地址（与 TRADING_AGENT/main.py 的 uvicorn 绑定一致，默认 127.0.0.1:8000）
AGENT_BASE_URL = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8000")
# 必须与 TRADING_AGENT/config/settings.yaml 的 agent_webhook.secret 完全一致
AGENT_SECRET = os.environ.get("AGENT_SECRET", "REPLACE_WITH_YOURS")

SCAN_INTERVAL = 15          # 轮询间隔（秒）
LOOKBACK_DAYS = 2           # 轮询窗口：今天 + 昨天（跨零点场景）
DEFAULT_RAW_SCORE = 0.7     # TRAE 记录无原始分，给一个中性强度分供 Agent 提示词使用

# 已转发 signal_id 持久化文件（防重启后重复推送；与 resonance 的 sent_ids 分离）
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agent_forwarded.json")
# 单实例锁
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agent_forwarder.lock")


# ====================== 方向映射 ======================
_DIR_MAP = {"做多": "long", "做空": "short"}


def _to_agent_direction(direction):
    """TRAE 归一化方向 -> Agent 方向。未知一律视为 long（保守）。"""
    return _DIR_MAP.get(direction, "long")


def _build_signal_id(rec):
    """派生稳定 signal_id：同一笔信号每次轮询得到相同 id，保证幂等 upsert。"""
    return "{}|{}|{}".format(
        rec.get("symbol", ""),
        rec.get("pattern", ""),
        rec.get("time", ""),
    )


def _to_payload(rec):
    """把一条 TRAE 归一化记录映射为 Agent webhook 负载。"""
    return {
        "signal_id": _build_signal_id(rec),
        "symbol": rec.get("symbol"),
        "direction": _to_agent_direction(rec.get("direction")),
        "setup_type": rec.get("pattern") or "unknown",
        "raw_score": DEFAULT_RAW_SCORE,
        "entry_zone": rec.get("entry"),
        "sl_zone": rec.get("sl"),
        "tp_zone": rec.get("tp1"),
        "interval": rec.get("interval"),
        "source": "TRAE",
    }


# ====================== 状态持久化 ======================
def _load_forwarded():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(data)
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def _save_forwarded(ids):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f, ensure_ascii=False)
    except OSError:
        pass


# ====================== 转发 ======================
def _forward(session, payload):
    """POST 一条信号到 Agent。返回是否成功（仅成功才记为已转发）。"""
    url = AGENT_BASE_URL + "/webhook/signal"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        resp = session.post(
            url,
            headers={"Content-Type": "application/json", "X-Agent-Secret": AGENT_SECRET},
            json=payload,
            timeout=10,
        )
    except Exception as e:
        print(f"[{now}] ❌ 转发异常（Agent 可能未启动）: {e}")
        return False
    if resp.status_code != 200:
        print(f"[{now}] ❌ Agent HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    try:
        body = resp.json()
    except Exception:
        print(f"[{now}] ❌ Agent 响应非 JSON: {resp.text[:200]}")
        return False
    if not isinstance(body, dict) or not body.get("ok"):
        print(f"[{now}] ❌ Agent 返回失败: {body}")
        return False
    print(f"[{now}] ✅ 转发成功 {payload['signal_id']} -> {body.get('decision')}")
    return True


# ====================== 单实例锁 ======================
def _acquire_lock():
    fp = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fp
    except OSError:
        fp.close()
        return None


# ====================== 主循环 ======================
def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print("TRAE -> Agent 信号转发层启动" + ("（DRY-RUN 仅打印不推送）" if dry_run else ""))
    print(f"Agent 地址: {AGENT_BASE_URL}/webhook/signal")
    print(f"轮询间隔: {SCAN_INTERVAL}s | 窗口: 今天+昨天({LOOKBACK_DAYS}天)")
    print(f"零代理: trust_env=False + proxies={{}}")
    print("=" * 60)

    if not dry_run:
        lock_fp = _acquire_lock()
        if lock_fp is None:
            print("⚠️ 检测到已有转发进程在运行，本进程退出。")
            return

    forwarded = _load_forwarded()
    print(f"[状态] 已记录已转发信号数: {len(forwarded)}")

    # 零代理会话（服务器直连 Agent，绝不走代理）
    session = requests.Session()
    session.trust_env = False
    session.proxies = {}

    try:
        while True:
            now = datetime.now()
            # 轮询窗口：今天 + 往前 LOOKBACK_DAYS-1 天（跨零点）
            days = [(now - timedelta(days=i)).strftime("%Y%m%d") for i in range(LOOKBACK_DAYS)]
            # 用 dict 折叠 pending：signal_data 里存在重复记录（相同 symbol|pattern|time
            # 但 tp2/sl 等字段略有差异），同一 signal_id 只保留一条，避免重复 POST/重复推飞书。
            pending = {}
            for d in days:
                for rec in signal_store.load_signals(d):
                    sid = _build_signal_id(rec)
                    if sid in forwarded:
                        continue
                    pending[sid] = _to_payload(rec)

            if not pending:
                if not dry_run:
                    time.sleep(SCAN_INTERVAL)
                else:
                    print(f"[{now:%H:%M:%S}] 无新信号待转发")
                    break
                continue

            print(f"[{now:%H:%M:%S}] 发现 {len(pending)} 条新信号，开始转发")
            new_ok = 0
            for sid, payload in pending.items():
                # 二次兜底：本轮内若同一 sid 被重复加入（理论上 dict 已折叠），跳过
                if sid in forwarded:
                    continue
                if dry_run:
                    print(f"  [dry-run] 将转发: {sid} -> {json.dumps(payload, ensure_ascii=False)}")
                    forwarded.add(sid)
                    new_ok += 1
                    continue
                if _forward(session, payload):
                    forwarded.add(sid)
                    new_ok += 1

            _save_forwarded(forwarded)
            print(f"[{now:%H:%M:%S}] 本轮转发成功 {new_ok}/{len(pending)}，累计已转发 {len(forwarded)}")

            if dry_run:
                break
            time.sleep(SCAN_INTERVAL)
    finally:
        if not dry_run and 'lock_fp' in dir() and lock_fp is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                lock_fp.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
