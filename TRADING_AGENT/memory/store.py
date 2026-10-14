"""MemoryStore：对 12 张表的确定性 CRUD 封装（标准库 sqlite3）。

6 层记忆映射：
  working   -> memory_working
  episodic  -> memory_episodic
  semantic  -> memory_semantic
  strategy  -> memory_strategy
  mistake   -> memory_mistake
  regime    -> memory_regime + regimes
经验生命周期 -> experiences

并发：所有写操作统一走 `_write`、所有读操作统一走 `_read`/`_read_one`，
二者均持 `self._lock`（`threading.RLock()`，可重入），避免单 sqlite3 连接
跨线程并发使用导致 `database is locked` / `InterfaceError`。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Dict, List, Optional


def _now() -> str:
    return __import__("datetime").datetime.now().isoformat(timespec="seconds")


def _jsonify(v: Any) -> Any:
    """结构化区间（list/dict）落库为 JSON 文本；标量原样返回。"""
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v


class MemoryStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # RLock：复合方法（如 get_or_create_experience 内部调 add_experience）
        # 会嵌套加锁，可重入避免死锁。
        self._lock = threading.RLock()

    # ---------------- 写操作统一入口（持锁） ----------------
    def _write(self, sql: str, params=()) -> None:
        with self._lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    # ---------------- 读操作统一入口（持锁） ----------------
    def _read(self, sql: str, params=()) -> List[tuple]:
        """在锁内执行只读查询并立即 fetchall，避免游标跨线程使用。"""
        with self._lock:
            cur = self.conn.execute(sql, params)
            return cur.fetchall()

    def _read_one(self, sql: str, params=()):
        """在锁内执行只读查询并立即 fetchone。"""
        with self._lock:
            cur = self.conn.execute(sql, params)
            return cur.fetchone()

    # ---------------- signals / decisions / journal ----------------
    def save_signal(self, sig: Dict[str, Any]) -> None:
        self._write(
            """INSERT OR REPLACE INTO signals
               (signal_id, symbol, direction, setup_type, entry_zone, sl_zone, tp_zone,
                ts, source_subsystem, raw_score, payload)
               VALUES (:signal_id, :symbol, :direction, :setup_type, :entry_zone, :sl_zone,
                       :tp_zone, :ts, :source_subsystem, :raw_score, :payload)""",
            {
                "signal_id": sig.get("signal_id"),
                "symbol": sig.get("symbol"),
                "direction": sig.get("direction"),
                "setup_type": sig.get("setup_type"),
                "entry_zone": _jsonify(sig.get("entry_zone")),
                "sl_zone": _jsonify(sig.get("sl_zone")),
                "tp_zone": _jsonify(sig.get("tp_zone")),
                "ts": sig.get("ts"),
                "source_subsystem": sig.get("source_subsystem"),
                "raw_score": sig.get("raw_score"),
                "payload": json.dumps(sig.get("payload", {}), ensure_ascii=False),
            },
        )

    def save_decision(self, d: Dict[str, Any]) -> None:
        self._write(
            """INSERT INTO decisions
               (signal_id, decision, direction, confidence, market_regime, setup_type,
                stop_loss, take_profit, position_size_pct, risk_score, signal_quality,
                reasons, risk_flags, memory_refs)
               VALUES (:signal_id, :decision, :direction, :confidence, :market_regime, :setup_type,
                       :stop_loss, :take_profit, :position_size_pct, :risk_score, :signal_quality,
                       :reasons, :risk_flags, :memory_refs)""",
            {
                "signal_id": d.get("signal_id"),
                "decision": d.get("decision"),
                "direction": d.get("direction"),
                "confidence": d.get("confidence"),
                "market_regime": d.get("market_regime"),
                "setup_type": d.get("setup_type"),
                "stop_loss": d.get("stop_loss"),
                "take_profit": d.get("take_profit"),
                "position_size_pct": d.get("position_size_pct"),
                "risk_score": d.get("risk_score"),
                "signal_quality": d.get("signal_quality"),
                "reasons": json.dumps(d.get("reasons", []), ensure_ascii=False),
                "risk_flags": json.dumps(d.get("risk_flags", []), ensure_ascii=False),
                "memory_refs": json.dumps(d.get("memory_refs", []), ensure_ascii=False),
            },
        )

    def add_journal(self, j: Dict[str, Any]) -> None:
        self._write(
            """INSERT INTO journal
               (signal_id, ts, regime, agent_decision, suggested_sl, suggested_tp,
                suggested_size_pct, user_action, outcome, pnl, note)
               VALUES (:signal_id, :ts, :regime, :agent_decision, :suggested_sl, :suggested_tp,
                       :suggested_size_pct, :user_action, :outcome, :pnl, :note)""",
            {
                "signal_id": j.get("signal_id"),
                "ts": j.get("ts", _now()),
                "regime": j.get("regime"),
                "agent_decision": j.get("agent_decision"),
                "suggested_sl": j.get("suggested_sl"),
                "suggested_tp": j.get("suggested_tp"),
                "suggested_size_pct": j.get("suggested_size_pct"),
                "user_action": j.get("user_action"),
                "outcome": j.get("outcome"),
                "pnl": j.get("pnl"),
                "note": j.get("note"),
            },
        )

    def update_journal_outcome(self, signal_id: str, user_action: Any, outcome: Any, pnl: Any) -> None:
        """回填某 signal 最新 journal 行的 user_action/outcome/pnl（持锁写）。"""
        self._write(
            "UPDATE journal SET user_action=?, outcome=?, pnl=? "
            "WHERE id=(SELECT MAX(id) FROM journal WHERE signal_id=?)",
            (user_action, outcome, pnl, signal_id),
        )

    # ---------------- working memory ----------------
    def set_working(self, key: str, value: str) -> None:
        self._write(
            "INSERT OR REPLACE INTO memory_working (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, _now()),
        )

    def get_working(self, key: str) -> Optional[str]:
        row = self._read_one("SELECT value FROM memory_working WHERE key=?", (key,))
        return row[0] if row else None

    def get_all_working(self) -> Dict[str, str]:
        return {r[0]: r[1] for r in self._read("SELECT key, value FROM memory_working")}

    def clear_working(self) -> None:
        self._write("DELETE FROM memory_working")

    # ---------------- episodic ----------------
    def add_episode(self, ep: Dict[str, Any]) -> None:
        self._write(
            """INSERT INTO memory_episodic
               (signal_id, ts, regime, agent_decision, actual_outcome, pnl, reflection)
               VALUES (:signal_id, :ts, :regime, :agent_decision, :actual_outcome, :pnl, :reflection)""",
            {
                "signal_id": ep.get("signal_id"),
                "ts": ep.get("ts", _now()),
                "regime": ep.get("regime"),
                "agent_decision": ep.get("agent_decision"),
                "actual_outcome": ep.get("actual_outcome"),
                "pnl": ep.get("pnl"),
                "reflection": ep.get("reflection"),
            },
        )

    def list_episodes(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._read(
            "SELECT id, signal_id, ts, regime, agent_decision, actual_outcome, pnl, reflection "
            "FROM memory_episodic ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        cols = ["id", "signal_id", "ts", "regime", "agent_decision", "actual_outcome", "pnl", "reflection"]
        return [dict(zip(cols, r)) for r in rows]

    # ---------------- semantic / strategy / mistake ----------------
    def add_semantic(self, content: str, source_episodes: str = "") -> None:
        self._write(
            "INSERT INTO memory_semantic (content, source_episodes) VALUES (?, ?)",
            (content, source_episodes),
        )

    def add_strategy(self, rule: str, active: bool = True) -> None:
        self._write(
            "INSERT INTO memory_strategy (rule, active) VALUES (?, ?)", (rule, int(active))
        )

    def add_mistake(self, pattern: str, avoidance: str) -> None:
        self._write(
            "INSERT INTO memory_mistake (pattern, avoidance) VALUES (?, ?)", (pattern, avoidance)
        )

    # ---------------- regime ----------------
    def record_regime(self, ts: str, regime: str, confidence: float, metrics: str = "") -> None:
        self._write(
            "INSERT INTO regimes (ts, regime, confidence, metrics) VALUES (?, ?, ?, ?)",
            (ts, regime, confidence, metrics),
        )

    def update_regime_stats(self, regime: str, win: bool, pnl: float) -> None:
        cur = self._read_one(
            "SELECT trade_count, win_count, total_pnl FROM memory_regime WHERE regime=?", (regime,)
        )
        if cur is None:
            self._write(
                "INSERT INTO memory_regime (regime, trade_count, win_count, total_pnl) VALUES (?, 1, ?, ?)",
                (regime, int(win), pnl),
            )
        else:
            tc, wc, tp = cur
            self._write(
                "UPDATE memory_regime SET trade_count=?, win_count=?, total_pnl=? WHERE regime=?",
                (tc + 1, wc + int(win), tp + pnl, regime),
            )

    def get_regime_stats(self, regime: str) -> Optional[Dict[str, Any]]:
        row = self._read_one(
            "SELECT regime, trade_count, win_count, total_pnl, pf FROM memory_regime WHERE regime=?",
            (regime,),
        )
        if not row:
            return None
        return dict(zip(["regime", "trade_count", "win_count", "total_pnl", "pf"], row))

    # ---------------- experiences (lifecycle) ----------------
    def add_experience(self, key: str, description: str, stage: str = "Observed") -> None:
        self._write(
            "INSERT OR IGNORE INTO experiences (key, description, stage) VALUES (?, ?, ?)",
            (key, description, stage),
        )

    def get_or_create_experience(self, key: str, description: str) -> str:
        """经验 key 不存在则创建为 Observed 并返回 'Observed'，否则返回现有 stage。"""
        row = self._read_one("SELECT stage FROM experiences WHERE key=?", (key,))
        if row is None:
            self.add_experience(key, description, "Observed")
            return "Observed"
        return row[0]

    def update_experience(self, key: str, n: int, pf: float, bootstrap_lower: float, stage: str) -> None:
        self._write(
            """UPDATE experiences SET n=?, pf=?, bootstrap_lower=?, stage=?, updated_at=?
               WHERE key=?""",
            (n, pf, bootstrap_lower, stage, _now(), key),
        )

    def get_experiences(self, stage: Optional[str] = None) -> List[Dict[str, Any]]:
        if stage:
            rows = self._read(
                "SELECT key, description, stage, n, pf, bootstrap_lower, updated_at "
                "FROM experiences WHERE stage=?",
                (stage,),
            )
        else:
            rows = self._read(
                "SELECT key, description, stage, n, pf, bootstrap_lower, updated_at FROM experiences"
            )
        cols = ["key", "description", "stage", "n", "pf", "bootstrap_lower", "updated_at"]
        return [dict(zip(cols, r)) for r in rows]

    # ---------------- 反馈环辅助查询 ----------------
    def get_signal_setup_type(self, signal_id: str) -> Optional[str]:
        row = self._read_one(
            "SELECT setup_type FROM signals WHERE signal_id=?", (signal_id,)
        )
        return row[0] if row else None

    def get_pnls_by_setup_type(self, setup_type: str) -> List[float]:
        rows = self._read(
            "SELECT j.pnl FROM signals s JOIN journal j ON s.signal_id=j.signal_id "
            "WHERE s.setup_type=? AND j.pnl IS NOT NULL",
            (setup_type,),
        )
        return [r[0] for r in rows]

    def get_journal_regime(self, signal_id: str) -> Optional[str]:
        row = self._read_one(
            "SELECT regime FROM journal WHERE signal_id=? ORDER BY id DESC LIMIT 1", (signal_id,)
        )
        return row[0] if row else None

    def get_daily_realized_pnl(self, day: str) -> float:
        rows = self._read(
            "SELECT pnl FROM journal WHERE ts LIKE ? AND pnl IS NOT NULL", (day + "%",)
        )
        return float(sum(r[0] for r in rows))

    def get_daily_decisions_report(self, day: str) -> List[Dict[str, Any]]:
        """当日交割单所需的 decisions JOIN journal 行（持锁读）。"""
        rows = self._read(
            "SELECT d.signal_id, direction, decision, stop_loss, take_profit, position_size_pct "
            "FROM decisions d JOIN journal j ON d.signal_id=j.signal_id WHERE j.ts LIKE ?",
            (day + "%",),
        )
        cols = ["signal_id", "direction", "decision", "stop_loss", "take_profit", "position_size_pct"]
        return [dict(zip(cols, r)) for r in rows]

    # ---------------- kill switch persistence ----------------
    def save_kill_switch(self, day: str, daily_pnl_pct: float, consecutive_losses: int) -> None:
        self._write(
            """INSERT OR REPLACE INTO kill_switch_state (day, daily_pnl_pct, consecutive_losses, updated_at)
               VALUES (?, ?, ?, ?)""",
            (day, daily_pnl_pct, consecutive_losses, _now()),
        )

    def load_kill_switch(self, day: str):
        from risk.engine import KillSwitchState

        row = self._read_one(
            "SELECT daily_pnl_pct, consecutive_losses FROM kill_switch_state WHERE day=?", (day,)
        )
        st = KillSwitchState()
        if row:
            st.daily_pnl_pct = row[0]
            st.consecutive_losses = row[1]
            st._day = day
        return st
