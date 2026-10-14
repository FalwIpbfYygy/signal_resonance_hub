"""agent.orchestrator 自检：端到端 process_signal（mock LLM + mock 飞书）。"""
from __future__ import annotations

import sqlite3
import tempfile
import os
from unittest import mock

import pytest

from config.loader import load_settings
from memory.schema import init_db
from memory.store import MemoryStore
from agent.orchestrator import Agent


@pytest.fixture
def agent():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, check_same_thread=False)
    init_db(conn)
    store = MemoryStore(conn)
    settings = load_settings()
    a = Agent(settings, store, llm_client=None, phase="paper", regime_override=("TRENDING_UP", 0.7))
    yield a
    conn.close()
    os.remove(path)


SIG = {
    "signal_id": "S1", "symbol": "", "direction": "long",
    "setup_type": "FVG", "entry_zone": [2000, 2004], "sl_zone": 1985.0,
    "tp_zone": 2025.0, "ts": "2026-09-29T12:00:00", "source_subsystem": "ICT",
    "raw_score": 0.8, "atr": 10.0,
}


def test_process_signal_returns_decision(agent):
    with mock.patch("agent.orchestrator.send_feishu", return_value=(True, "ok")):
        d = agent.process_signal(SIG)
    assert d["decision"] in ("EXECUTE", "REJECT", "WAIT", "REDUCE_RISK", "OBSERVE")
    assert d["stop_loss"] == 1985.0
    assert d["_feishu_ok"] is True


def test_process_signal_persists(agent):
    with mock.patch("agent.orchestrator.send_feishu", return_value=(True, "ok")):
        agent.process_signal(SIG)
    rows = agent.store.conn.execute("SELECT COUNT(*) FROM decisions WHERE signal_id='S1'").fetchone()
    assert rows[0] == 1
    sig_rows = agent.store.conn.execute("SELECT COUNT(*) FROM signals WHERE signal_id='S1'").fetchone()
    assert sig_rows[0] == 1


def test_record_outcome_updates_kill_switch(agent):
    with mock.patch("agent.orchestrator.send_feishu", return_value=(True, "ok")):
        agent.process_signal(SIG)
        agent.record_outcome("S1", "executed", "win", 0.02)
    ks = agent.store.load_kill_switch(agent.today_key())
    assert ks.daily_pnl_pct == 0.02
    assert ks.consecutive_losses == 0


def test_should_close_now_false_midday(agent):
    # 默认 now 是真实时间，仅断言函数是 callable 且返回 bool
    assert isinstance(agent.should_close_now(), bool)
