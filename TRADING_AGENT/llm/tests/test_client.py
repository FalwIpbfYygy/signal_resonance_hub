"""llm.client 自检：解析容错 + 代理纪律（零代理时 proxies 必须为空）。"""
from __future__ import annotations

from llm.client import LLMClient


def test_parse_clean_json():
    text = '{"decision":"EXECUTE","confidence":0.8,"reasons":["trend up"],"memory_refs":["m1"]}'
    d = LLMClient.parse_decision(text)
    assert d["decision"] == "EXECUTE"
    assert d["confidence"] == 0.8


def test_parse_with_surrounding_text():
    text = '好的，我的分析：\n{"decision":"WAIT","confidence":0.4,"reasons":["low conf"],"memory_refs":[]}\n以上。'
    d = LLMClient.parse_decision(text)
    assert d["decision"] == "WAIT"


def test_parse_garbage_falls_back():
    d = LLMClient.parse_decision("模型没返回 JSON")
    assert d["decision"] == "OBSERVE"


class _FakeSettings:
    class _LLM(dict):
        pass

    def __init__(self, proxy):
        self._proxy = proxy
        self.llm = {"api_key": "x", "base_url": "https://example.com", "intraday_model": "m", "weekly_model": "m",
                    "clean_text": ""}

    def get_proxy(self):
        return self._proxy


def test_session_zero_proxy_when_empty():
    c = LLMClient(_FakeSettings(""))
    assert c._session.proxies == {}
    assert c._session.trust_env is False


def test_session_uses_proxy_when_set():
    c = LLMClient(_FakeSettings("http://127.0.0.1:10808"))
    assert c._session.proxies == {"http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"}
