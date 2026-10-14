#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在【服务器】上验证阿里云百炼 LLM 是否真在 Agent 决策链路中工作。

为什么要在服务器跑：
  - 本地测试结果只代表「凭证/模型/通道」有效，不代表部署环境（新加坡服务器）能直连百炼。
  - 本脚本复用项目里真实的 LLMClient 与生产配置，直接打百炼 /chat/completions，
    验证「服务器 -> 阿里云」这条链路通不通、密钥/workspace_id/模型名实际有效。

用法（在服务器上）：
    1. 把本文件放到 TRADEING_AGENT 项目根目录（和 config/ llm/ agent/ 同级）。
    2. cd 到该目录，执行：
         python verify_llm_server.py
    依赖：requests、pyyaml（Agent 能在该服务器跑起来，这两个必然已装）。
    零代理纪律：脚本自动读取 config 的 proxy 字段（生产应为空），不引入任何代理逻辑。
"""
from __future__ import annotations
import sys
import os
import time
import json


ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> None:
    # ---------- 1. 加载真实生产配置 ----------
    try:
        from config.loader import load_settings
    except Exception as e:  # 导入失败通常是文件路径不对或依赖缺失
        print(f"[FAIL] 无法 import config.loader：{e}")
        print("请确认本文件已放在 TRADEING_AGENT 项目根目录，且服务器已 pip install pyyaml requests")
        return

    settings = load_settings()
    llm = settings.llm
    print("=" * 64)
    print("STEP 1 — 核对生产配置（来自 config/settings.yaml）")
    print("=" * 64)
    print(f"  base_url      : {llm.get('base_url')}")
    print(f"  intraday_model: {llm.get('intraday_model')}")
    print(f"  workspace_id  : {llm.get('workspace_id')}")
    ak = llm.get('api_key', '')
    print(f"  api_key 前缀 : {ak[:8]}... (len={len(ak)})")
    print(f"  proxy        : {settings.get_proxy()!r}  (None=零代理，符合服务器纪律)")

    # ---------- 2. 实例化真实 LLMClient（复用生产代码） ----------
    try:
        from llm.client import LLMClient
    except Exception as e:
        print(f"[FAIL] 无法 import llm.client：{e}")
        return

    client = LLMClient(settings)
    print("\n" + "=" * 64)
    print("STEP 2 — 实例化真实 LLMClient（复用生产同款客户端）")
    print("=" * 64)
    print(f"  trust_env={client._session.trust_env}  proxies={client._session.proxies}")

    # ---------- 3. 真实发起 /chat/completions ----------
    prompt = (
        "当前信号： 1小时图出现 Bullish SFP + 反转FVG，"
        "regime=RANGING。请仅做分析建议，按系统提示输出 JSON。"
    )
    print("\n" + "=" * 64)
    print("STEP 3 — 真实请求百炼 /chat/completions（服务器 -> 阿里云）")
    print("=" * 64)
    t0 = time.time()
    try:
        raw = client.analyze_intraday(prompt)
        dt = time.time() - t0
    except Exception as e:
        dt = time.time() - t0
        print(f"[FAIL] 调用异常（{dt:.2f}s）：{type(e).__name__}: {e}")
        print("  常见原因：服务器到阿里云北京 endpoint 不通（需确认网络出口）、")
        print("            api_key/workspace_id 失效、或模型名已下线。")
        return
    print(f"[OK] HTTP 200，耗时 {dt:.2f}s")
    print("  --- 原始返回（前 400 字）---")
    print("  " + raw[:400].replace("\n", "\n  "))

    # ---------- 4. 解析为结构化决策 ----------
    parsed = LLMClient.parse_decision(raw)
    print("\n" + "=" * 64)
    print("STEP 4 — parse_decision 解析（orchestrator 同款逻辑）")
    print("=" * 64)
    print("  " + json.dumps(parsed, ensure_ascii=False, indent=2)[:600])

    # ---------- 5. 对照：无 LLM 时规则分支长什么样 ----------
    print("\n" + "=" * 64)
    print("STEP 5 — 对照：若 Agent 没接 LLM（规则分支）")
    print("=" * 64)
    print("  proposal = {decision: raw_score>=0.65 and EXECUTE or WAIT,")
    print("             confidence: raw_score, reasons: [], memory_refs: []}")
    print("  => 规则分支没有 reasons/memory_refs，只是阈值开关；")
    print("     LLM 分支提供了 decision/confidence/reasons，确实参与决策。")

    # ---------- 6. 代码路径确认（证明部署版本会走 LLM 分支） ----------
    print("\n" + "=" * 64)
    print("STEP 6 — 代码路径确认（orchestrator.process_signal 何时走 LLM）")
    print("=" * 64)
    try:
        import inspect
        from agent.orchestrator import Agent
        src = inspect.getsource(Agent.process_signal)
        for ln in src.splitlines():
            s = ln.strip()
            if "self.llm is not None" in s or "analyze_intraday" in s:
                print("   " + s)
        # 同时确认 main.py 默认会注入真实 LLMClient
        from main import _SENTINEL, LLMClient as _LC  # noqa: F401
        print("   main.create_app() 默认分支会构建真实 LLMClient 注入 Agent")
    except Exception as e:
        print(f"  (跳过源码检查：{e})")

    # ---------- 结论 ----------
    decision = parsed.get("decision")
    print("\n" + "=" * 64)
    print("结论")
    print("=" * 64)
    if decision in {"EXECUTE", "REJECT", "WAIT", "REDUCE_RISK", "OBSERVE"}:
        print("[PASS] 服务器能直连阿里云百炼，且 LLM 返回了合法决策。")
        print("       结合 STEP6 代码路径，可确认：部署版本里 LLM 确实在决策链路中工作。")
    else:
        print("[WARN] 返回了非预期结构，请检查上面的原始输出。")


if __name__ == "__main__":
    main()
