"""memory 包自检：schema 建表、store CRUD、经验生命周期统计。"""
from __future__ import annotations

import sqlite3
import tempfile
import os

import pytest

from memory.schema import init_db
from memory.store import MemoryStore
from memory.experiences import evaluate, next_stage, compute_pf, bootstrap_lower


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, check_same_thread=False)
    init_db(conn)
    s = MemoryStore(conn)
    yield s
    conn.close()
    os.remove(path)


def test_init_db_creates_12_tables(store):
    tables = {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    expected = {
        "signals", "decisions", "journal", "memory_working", "memory_episodic",
        "memory_semantic", "memory_strategy", "memory_mistake", "regimes",
        "memory_regime", "experiences", "kill_switch_state",
    }
    assert expected.issubset(tables)


def test_save_and_get_signal(store):
    sig = {"signal_id": "S1", "symbol": "", "direction": "long",
           "setup_type": "FVG", "entry_zone": [2000, 2005], "sl_zone": 1985,
           "tp_zone": 2025, "ts": "2026-09-29T12:00:00", "source_subsystem": "ICT",
           "raw_score": 0.8, "payload": {"k": "v"}}
    store.save_signal(sig)
    row = store.conn.execute("SELECT signal_id, direction, raw_score FROM signals WHERE signal_id='S1'").fetchone()
    assert row == ("S1", "long", 0.8)


def test_working_memory_roundtrip(store):
    store.set_working("equity", "10000")
    assert store.get_working("equity") == "10000"
    store.set_working("equity", "12000")
    assert store.get_working("equity") == "12000"
    store.clear_working()
    assert store.get_working("equity") is None


def test_regime_stats_accumulate(store):
    store.update_regime_stats("TRENDING_UP", win=True, pnl=0.02)
    store.update_regime_stats("TRENDING_UP", win=False, pnl=-0.01)
    stats = store.get_regime_stats("TRENDING_UP")
    assert stats["trade_count"] == 2
    assert stats["win_count"] == 1


def test_experience_lifecycle(store):
    store.add_experience("exp1", "伦敦段 FVG 失效偏高")
    assert store.get_experiences("Observed")[0]["key"] == "exp1"
    store.update_experience("exp1", n=40, pf=1.3, bootstrap_lower=0.005, stage="Validated")
    exp = store.get_experiences("Validated")[0]
    assert exp["n"] == 40 and exp["pf"] == 1.3


# ---------------- 经验统计验证 ----------------
def test_compute_pf():
    assert compute_pf([0.02, 0.03, -0.01]) == pytest.approx(5.0)  # 0.05 / 0.01
    assert compute_pf([0.01, 0.02]) == float("inf")
    assert compute_pf([-0.01, -0.02]) == 0.0


def test_evaluate_promotes_when_strong():
    # 40 笔，明显正期望、PF>1.1、bootstrap 下界>0
    rng = __import__("numpy").random.default_rng(1)
    pnls = rng.normal(0.01, 0.01, 40).tolist()
    m = evaluate(pnls)
    assert m["n"] == 40
    assert m["promote"] is True
    stage = next_stage("Candidate", m)
    assert stage in ("Statistically Supported", "Validated")


def test_evaluate_deprecate_when_negative():
    pnls = [-0.01] * 40  # 全亏
    m = evaluate(pnls)
    assert m["promote"] is False
    assert next_stage("Candidate", m) == "Deprecated"
