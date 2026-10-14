"""journal.feishu 自检：code==0 校验 + 交割单格式。"""
from __future__ import annotations

from unittest import mock

from journal.feishu import send_feishu, format_daily_report


def _fake_resp(status_code=200, body=None):
    r = mock.Mock()
    r.status_code = status_code
    if body is None:
        r.json.side_effect = ValueError("not json")
        r.text = "plain"
    else:
        r.json.return_value = body
        r.text = str(body)
    return r


def test_send_ok_when_code_zero():
    with mock.patch("journal.feishu.requests.Session.post", return_value=_fake_resp(200, {"code": 0, "msg": "ok"})):
        ok, detail = send_feishu("https://hook", "hi")
    assert ok is True and detail == "ok"


def test_send_fails_on_http_500():
    with mock.patch("journal.feishu.requests.Session.post", return_value=_fake_resp(500, {"code": 0})):
        ok, detail = send_feishu("https://hook", "hi")
    assert ok is False and "HTTP 500" in detail


def test_send_fails_on_business_code_nonzero():
    # HTTP 200 但业务码非 0 —— 这是 S2 的核心坑，必须识别为失败
    with mock.patch("journal.feishu.requests.Session.post", return_value=_fake_resp(200, {"code": 19021, "msg": "freq limit"})):
        ok, detail = send_feishu("https://hook", "hi")
    assert ok is False and "业务码" in detail


def test_send_fails_on_non_json():
    with mock.patch("journal.feishu.requests.Session.post", return_value=_fake_resp(200, None)):
        ok, detail = send_feishu("https://hook", "hi")
    assert ok is False


def test_format_daily_report():
    decisions = [
        {"signal_id": "S1", "direction": "long", "decision": "EXECUTE", "stop_loss": 1985, "take_profit": 2025, "position_size_pct": 0.04},
        {"signal_id": "S2", "direction": "short", "decision": "REJECT", "stop_loss": None, "take_profit": None, "position_size_pct": None},
    ]
    txt = format_daily_report("2026-09-29", decisions, "未触发", ["exp1 -> Validated"], 0.012)
    assert "交割单 2026-09-29" in txt
    assert "EXECUTE=1" in txt and "REJECT=1" in txt
    assert "exp1 -> Validated" in txt
    assert "+1.20%" in txt
