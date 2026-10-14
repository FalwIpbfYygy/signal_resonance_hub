"""main.py FastAPI 端点自检（TestClient，mock 飞书）。"""
from __future__ import annotations

import sqlite3
import tempfile
import os
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from config.loader import load_settings
from memory.schema import init_db
from memory.store import MemoryStore
from main import create_app


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path, check_same_thread=False)
    init_db(conn)
    store = MemoryStore(conn)
    settings = load_settings()
    app = create_app(settings, store, phase="paper", llm_client=None)
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


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_webhook_rejects_bad_secret(client):
    r = client.post("/webhook/signal", json=SIG, headers={"X-Agent-Secret": "wrong"})
    assert r.status_code == 403


def test_webhook_accepts_valid_secret(client):
    settings = load_settings()
    secret = settings.agent_webhook["secret"]
    with mock.patch("agent.orchestrator.send_feishu", return_value=(True, "ok")):
        r = client.post("/webhook/signal", json=SIG, headers={"X-Agent-Secret": secret})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["signal_id"] == "S1"


def test_webhook_rejects_invalid_json(client):
    settings = load_settings()
    secret = settings.agent_webhook["secret"]
    r = client.post("/webhook/signal", json="not-a-dict", headers={"X-Agent-Secret": secret})
    # 非 JSON 对象 -> 400（端点已做 dict 校验）
    assert r.status_code == 400
