"""P0 + P1 修复回归测试（新增，沿用 agent/tests/test_orchestrator.py 与 tests/test_main.py 风格）。

覆盖：经验反馈环闭合、SQLite 并发、LLM 非法动作归一化 + 失败回退、
save_signal 结构化区间落库、交割单真实盈亏、secret 校验、调度器产出交割单。
"""
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
from llm.client import LLMClient
from main import create_app, validate_config
from fastapi import HTTPException
from fastapi.testclient import TestClient


# ---------------- fixtures ----------------
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


@pytest.fixture
def agent_shadow():
    """phase=shadow：process_signal / produce_daily_report 均不推送飞书，避免网络依赖。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, check_same_thread=False)
    init_db(conn)
    store_ = MemoryStore(conn)
    settings = load_settings()
    a = Agent(settings, store_, llm_client=None, phase="shadow", regime_override=("TRENDING_UP", 0.7))
    yield a
    conn.close()
    os.remove(path)


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, check_same_thread=False)
    init_db(conn)
    store_ = MemoryStore(conn)
    settings = load_settings()
    app = create_app(settings, store_, phase="paper", llm_client=None)
    c = TestClient(app)
    yield c
    conn.close()
    os.remove(path)


SIG = {
    "signal_id": "S1", "symbol": "", "direction": "long",
    "setup_type": "FVG", "entry_zone": [2000, 2004], "sl_zone": 1985.0,
    "tp_zone": 2025.0, "ts": "2026-09-29T12:00:00", "source_subsystem": "ICT",
    "raw_score": 0.8, "atr": 10.0,
}


# ---------------- 1. 经验反馈环闭合 ----------------
def test_feedback_loop_promotes_to_validated(agent_shadow):
    import numpy as np

    rng = np.random.default_rng(1)
    pnls = rng.normal(0.01, 0.01, 40).tolist()
    for i, pnl in enumerate(pnls):
        sig = dict(SIG, signal_id=f"F{i}", setup_type="FVG")
        agent_shadow.process_signal(sig)
        agent_shadow.record_outcome(f"F{i}", "executed", "win" if pnl > 0 else "loss", pnl)

    validated = agent_shadow.store.get_experiences(stage="Validated")
    assert validated, "应有升级到 Validated 的经验"
    assert validated[0]["key"] == "setup:FVG"

    # memory_regime 应被回填（regime 取自 regime_override）
    reg = agent_shadow.store.get_regime_stats("TRENDING_UP")
    assert reg is not None and reg["trade_count"] == 40

    # 交割单反映经验升级
    text = agent_shadow.produce_daily_report()
    assert "setup:FVG->Validated" in text


# ---------------- 2. SQLite 并发 ----------------
def test_concurrent_process_signal_and_report(agent_shadow):
    import threading

    errors = []

    def worker(tag: str):
        try:
            for i in range(5):
                sig = dict(SIG, signal_id=f"C{tag}-{i}", setup_type="FVG")
                agent_shadow.process_signal(sig)
            agent_shadow.produce_daily_report()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发出现异常: {errors}"
    assert not any("database is locked" in str(e) for e in errors)


# ---------------- 3. LLM 非法动作归一化 ----------------
def test_parse_decision_normalizes_illegal_action():
    d = LLMClient.parse_decision('{"decision":"BUY","confidence":0.9,"reasons":["x"],"memory_refs":[]}')
    assert d["decision"] == "OBSERVE"
    assert any("非法动作" in r for r in d.get("reasons", []))


def test_process_signal_normalizes_illegal_llm_action(agent_shadow):
    class FakeLLM:
        def analyze_intraday(self, prompt):
            return '{"decision":"BUY","confidence":0.9,"reasons":["raw"],"memory_refs":[]}'

    agent_shadow.llm = FakeLLM()
    d = agent_shadow.process_signal(dict(SIG))
    assert d["decision"] == "OBSERVE"


# ---------------- 4. LLM 失败回退 ----------------
def test_process_signal_falls_back_on_llm_error(agent_shadow):
    class BoomLLM:
        def analyze_intraday(self, prompt):
            raise RuntimeError("LLM down")

    agent_shadow.llm = BoomLLM()
    d = agent_shadow.process_signal(dict(SIG))
    assert d["decision"] == "OBSERVE"
    row = agent_shadow.store.conn.execute(
        "SELECT COUNT(*) FROM signals WHERE signal_id='S1'"
    ).fetchone()
    assert row[0] == 1
    jrow = agent_shadow.store.conn.execute(
        "SELECT COUNT(*) FROM journal WHERE signal_id='S1'"
    ).fetchone()
    assert jrow[0] == 1


# ---------------- 5. save_signal 结构化区间落库 ----------------
def test_save_signal_structured_zones(store):
    sig = {
        "signal_id": "Z1", "symbol": "", "direction": "long", "setup_type": "FVG",
        "entry_zone": [2000, 2004], "sl_zone": [1, 2], "tp_zone": {"a": 1},
        "ts": "2026-09-29T12:00:00", "source_subsystem": "ICT", "raw_score": 0.8,
        "payload": {"k": "v"},
    }
    store.save_signal(sig)  # 不应抛 InterfaceError
    row = store.conn.execute(
        "SELECT sl_zone, tp_zone FROM signals WHERE signal_id='Z1'"
    ).fetchone()
    assert row[0] == "[1, 2]"
    assert row[1] == '{"a": 1}'


# ---------------- 6. 交割单真实盈亏（非 Σ 仓位占比）----------------
def test_daily_report_real_pnl_not_position_sum(agent_shadow):
    sig = dict(SIG, signal_id="P1", setup_type="FVG")
    agent_shadow.process_signal(sig)
    agent_shadow.record_outcome("P1", "executed", "win", 0.05)
    text = agent_shadow.produce_daily_report()
    assert "当日已录入盈亏(实际): +5.00%" in text
    assert "若执行预估收益" not in text  # 标签已改为真实盈亏


# ---------------- 7. secret 校验 ----------------
def test_webhook_outcome_wrong_secret_403(client):
    r = client.post(
        "/webhook/outcome",
        json={"signal_id": "S1", "pnl": 0.01},
        headers={"X-Agent-Secret": "wrong"},
    )
    assert r.status_code == 403


def test_webhook_outcome_valid_secret(client):
    settings = load_settings()
    secret = settings.agent_webhook["secret"]
    with mock.patch("agent.orchestrator.send_feishu", return_value=(True, "ok")):
        client.post("/webhook/signal", json=SIG, headers={"X-Agent-Secret": secret})
        r = client.post(
            "/webhook/outcome",
            json={"signal_id": "S1", "user_action": "executed", "outcome": "win", "pnl": 0.02},
            headers={"X-Agent-Secret": secret},
        )
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_webhook_outcome_invalid_json(client):
    settings = load_settings()
    secret = settings.agent_webhook["secret"]
    r = client.post("/webhook/outcome", json="not-a-dict", headers={"X-Agent-Secret": secret})
    assert r.status_code == 400


def test_check_secret_accepts_valid_and_rejects_empty():
    from main import _check_secret

    settings = load_settings()
    secret = settings.agent_webhook["secret"]
    _check_secret(secret, settings)  # 不应抛异常
    with pytest.raises(HTTPException):
        _check_secret(None, settings)
    with pytest.raises(HTTPException):
        _check_secret("", settings)


def test_validate_config_rejects_placeholder():
    # 真实 settings.yaml 含占位 secret -> 入口应拒绝启动
    with pytest.raises(RuntimeError):
        validate_config(load_settings())


# ---------------- 8. 调度器产出交割单 ----------------
def test_produce_daily_report_text(agent_shadow):
    agent_shadow.process_signal(dict(SIG, signal_id="SCH1"))
    text = agent_shadow.produce_daily_report()
    assert isinstance(text, str)
    assert "交割单" in text


def test_scheduler_registers_job():
    try:
        from main import _start_scheduler
    except ImportError:
        pytest.skip("apscheduler 未安装")
    sched = _start_scheduler(agent_shadow)
    try:
        assert sched.running
    finally:
        sched.shutdown(wait=False)
