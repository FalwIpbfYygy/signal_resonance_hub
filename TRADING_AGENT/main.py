"""Agent 服务入口：FastAPI 接收 TRAE Webhook，提供 /webhook/signal 与 /health。

TRAE 侧薄转发层在 record_signal 后，携带共享 secret 头 POST 信号 JSON 到本端点。
本服务不 import TRAE 任何内部代码。每日交割单由 cron 调用 `python main.py report` 触发（不在进程内调度）。
"""
from __future__ import annotations

import hmac
import os
import sqlite3
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, Header

from config.loader import load_settings, Settings
from memory.schema import init_db
from memory.store import MemoryStore
from agent.orchestrator import Agent
from llm.client import LLMClient

# 阶段：shadow=只落库不推送；paper/live=推送飞书。paper=推建议但不自动下单。
PHASE = "paper"
DEFAULT_DB = os.path.join(os.path.dirname(__file__), "data", "agent.db")

# 占位 secret（用户尚未修改）。仅启动入口校验，避免误用明文占位值启动服务。
PLACEHOLDER_SECRET = "trae-agent-shared-secret-change-me"

# create_app 的 llm_client 默认哨兵：未显式传入时按生产行为建真实 LLMClient；
# 显式传入 None 则把 None 透传给 Agent（走规则分支，不发起真实 LLM 请求）。
_SENTINEL = object()


def _check_secret(x_agent_secret: str | None, settings: Settings) -> None:
    """校验 X-Agent-Secret 头；secret 为空或匹配失败一律 403。"""
    expected = settings.agent_webhook.get("secret") or ""
    if not expected or not hmac.compare_digest(x_agent_secret or "", expected):
        raise HTTPException(status_code=403, detail="invalid agent secret")


def validate_config(settings: Settings) -> None:
    """启动前配置校验（仅 __main__ 入口调用，不进 create_app，以免破坏使用真实 settings 的测试）。

    占位 secret 未修改则拒绝启动，防止明文占位值被当作真实密钥。
    """
    if settings.agent_webhook.get("secret") == PLACEHOLDER_SECRET:
        raise RuntimeError("占位 secret 未修改，拒绝启动：请先在 config/settings.yaml 设置 agent_webhook.secret")


def create_app(settings: Settings | None = None, store: MemoryStore | None = None, phase: str = PHASE,
               llm_client=_SENTINEL):
    settings = settings or load_settings()
    if store is None:
        os.makedirs(os.path.dirname(DEFAULT_DB), exist_ok=True)
        # 单用户低频场景：允许跨线程复用同一连接（Starlette 把同步端点放进线程池执行）
        conn = sqlite3.connect(DEFAULT_DB, check_same_thread=False)
        # 并发：WAL 允许读写并发 + busy_timeout 避免短暂锁等待直接报 database is locked
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        init_db(conn)
        store = MemoryStore(conn)
    # 仅当调用方未显式传入 llm_client 时才建真实客户端（生产默认）；
    # 测试可注入 llm_client=None（Agent 走规则分支，不发起真实 LLM 请求）。
    if llm_client is _SENTINEL:
        llm_client = LLMClient(settings)
    agent = Agent(settings, store, llm_client=llm_client, phase=phase)

    app = FastAPI(title=" Trading Decision Agent")
    app.state.agent = agent
    app.state.settings = settings

    @app.get("/health")
    def health():
        return {"status": "ok", "phase": phase}

    @app.post("/webhook/signal")
    async def webhook_signal(req: Request, x_agent_secret: str | None = Header(default=None)):
        _check_secret(x_agent_secret, settings)
        try:
            data = await req.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        decision = agent.process_signal(data)
        return {"ok": True, "decision": decision["decision"], "signal_id": decision.get("signal_id")}

    @app.post("/webhook/outcome")
    async def webhook_outcome(req: Request, x_agent_secret: str | None = Header(default=None)):
        _check_secret(x_agent_secret, settings)
        try:
            data = await req.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="payload must be a JSON object")
        agent.record_outcome(
            data["signal_id"],
            data.get("user_action"),
            data.get("outcome"),
            float(data.get("pnl", 0.0)),
        )
        return {"ok": True}

    return app


app = create_app()


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description=" Trading Decision Agent")
    parser.add_argument(
        "command", nargs="?", default="serve", choices=["serve", "report"],
        help="serve=启动常驻 HTTP 服务（由 nohup/restart_trade.sh/cron 管理启停）；"
             "report=生成并推送当日交割单后退出（由 cron 定时调用，不依赖进程内调度器）",
    )
    args = parser.parse_args()

    settings = load_settings()
    validate_config(settings)  # 占位 secret 未修改则拒绝启动

    if args.command == "report":
        # 每日交割单由 cron 定时触发（如 22:00 北京）。WAL 模式下可与常驻 serve 进程
        # 并发读写同一 SQLite 文件；shadow 阶段只打印不推送。
        text = app.state.agent.produce_daily_report()
        print(text)
    else:
        # 作为常驻进程启动；启停完全交给 restart_trade.sh / crontab，进程内不再内置调度器。
        uvicorn.run(app, host="127.0.0.1", port=8000)
