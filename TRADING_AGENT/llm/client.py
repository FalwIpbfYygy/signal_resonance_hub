"""百炼 LLM 客户端（OpenAI 兼容 /chat/completions，requests 实现）。

纪律（沿用你的安全基线）：
  - 代理仅本地：proxy 来自 settings.get_proxy()；生产置空则 proxies={} 且 trust_env=False，
    确保环境里残留的任何代理变量一律失效（不读 HTTP_PROXY/HTTPS_PROXY 环境变量）。
  - LLM 只返回分析文本（decision/reasons/confidence 由后续解析），不参与任何精确数值计算。
"""
from __future__ import annotations

import json
from typing import Optional

import requests

from config.loader import Settings
from risk.engine import ALLOWED_ACTIONS


SYSTEM_PROMPT = (
    "你是  日内交易的决策分析助手。只做分析与建议，禁止计算精确数字"
    "(止损/止盈/仓位由风控引擎计算)。输出 JSON："
    '{"decision":"EXECUTE|REJECT|WAIT|REDUCE_RISK|OBSERVE",'
    '"confidence":0.0~1.0,"reasons":["..."],"memory_refs":["..."]}'
)


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        proxy = settings.get_proxy()
        self._session = requests.Session()
        self._session.trust_env = False  # 拒绝环境代理
        self._session.proxies = {"http": proxy, "https": proxy} if proxy else {}

    def _chat(self, model: str, user_prompt: str, temperature: float) -> str:
        base = self.settings.llm["base_url"].rstrip("/")
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.llm['api_key']}",
            "Content-Type": "application/json",
        }
        clean = self.settings.llm.get("clean_text", "")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt + "\n" + clean},
            ],
            "temperature": temperature,
        }
        resp = self._session.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def analyze_intraday(self, prompt: str) -> str:
        return self._chat(self.settings.llm["intraday_model"], prompt, 0.7)

    def analyze_weekly(self, prompt: str) -> str:
        return self._chat(self.settings.llm["weekly_model"], prompt, 0.3)

    @staticmethod
    def parse_decision(text: str) -> dict:
        """从 LLM 文本中尽量解析出决策 JSON（容错：截取第一个 {...}）。

        归一化：若 LLM 返回的动作不在 ALLOWED_ACTIONS 白名单内，强制降级为 OBSERVE
        并在 reasons 追加警告，避免非法动作流入风控/落库。
        """
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1:
                return {"decision": "OBSERVE", "confidence": 0.0, "reasons": [text[:200]], "memory_refs": []}
            parsed = json.loads(text[start : end + 1])
        except Exception:
            return {"decision": "OBSERVE", "confidence": 0.0, "reasons": [text[:200]], "memory_refs": []}

        if not isinstance(parsed, dict):
            return {"decision": "OBSERVE", "confidence": 0.0, "reasons": [text[:200]], "memory_refs": []}

        decision = parsed.get("decision", "OBSERVE")
        if decision not in ALLOWED_ACTIONS:
            parsed["decision"] = "OBSERVE"
            reasons = parsed.get("reasons")
            if not isinstance(reasons, list):
                reasons = []
            reasons.append(f"LLM 返回非法动作 {decision} 已归一化为 OBSERVE")
            parsed["reasons"] = reasons
        return parsed
