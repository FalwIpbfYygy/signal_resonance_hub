"""配置加载器。

读取 config/settings.yaml，暴露为 Settings 对象。敏感字段（LLM 密钥、飞书 webhook、
Agent 共享 secret）已外置为环境变量，加载时优先用环境变量覆盖 yaml 中的占位值，
避免把密钥写进仓库。上线新加坡服务器时把 proxy 置空即零代理。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict

try:
    import yaml
except ImportError as e:  # pragma: no cover - 依赖缺失提示
    raise ImportError(
        "缺少 PyYAML 依赖。请在该项目 venv 中执行: pip install pyyaml"
    ) from e


_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "settings.yaml")


@dataclass
class Settings:
    raw: Dict[str, Any]

    # ---- 便捷访问器 ----
    @property
    def llm(self) -> Dict[str, Any]:
        return self.raw["llm"]

    @property
    def feishu(self) -> Dict[str, Any]:
        return self.raw["feishu"]

    @property
    def agent_webhook(self) -> Dict[str, Any]:
        return self.raw["agent_webhook"]

    @property
    def trading(self) -> Dict[str, Any]:
        return self.raw["trading"]

    @property
    def risk(self) -> Dict[str, Any]:
        return self.raw["risk"]

    @property
    def guardrails(self) -> Dict[str, Any]:
        return self.raw.get("guardrails", {})

    def get_proxy(self) -> str | None:
        """返回代理地址；空字符串视为零代理（返回 None）。"""
        p = self.raw.get("proxy", "")
        return p if p else None

    def as_risk_config(self):
        """转换为 risk.engine.RiskConfig（避免循环 import，返回 dict 由调用方构造）。"""
        from risk.engine import RiskConfig

        r = self.risk
        return RiskConfig(
            max_risk_per_trade_pct=r.get("max_risk_per_trade_pct", 0.03),
            daily_loss_stop_pct=r.get("daily_loss_stop_pct", 0.05),
            consecutive_loss_stop=r.get("consecutive_loss_stop", 5),
            atr_sl_multiplier=r.get("atr_sl_multiplier", 1.5),
            tp_r_multiplier=r.get("tp_r_multiplier", 1.5),
            max_concurrent_signals=r.get("max_concurrent_signals", 3),
            confidence_threshold=r.get("confidence_threshold", 0.65),
            contract_multiplier=r.get("contract_multiplier", 100.0),
        )


def load_settings(path: str | None = None) -> Settings:
    path = path or _DEFAULT_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"配置文件根必须是映射: {path}")
    _apply_env_overrides(data)
    return Settings(raw=data)


# 环境变量 -> yaml 字段 映射（仅覆盖「存在且非空」的环境变量；yaml 占位为空时由 env 填充）
_ENV_OVERRIDES = {
    "llm": {"api_key": "LLM_API_KEY", "workspace_id": "LLM_WORKSPACE_ID", "base_url": "LLM_BASE_URL"},
    "feishu": {"webhook": "FEISHU_WEBHOOK"},
    "agent_webhook": {"secret": "AGENT_SECRET"},
}


def _apply_env_overrides(data: dict) -> None:
    """用环境变量覆盖 yaml 中对应的敏感字段；环境变量不存在或为空则保持 yaml 原值。"""
    for section, mapping in _ENV_OVERRIDES.items():
        for key, env_var in mapping.items():
            env_val = os.environ.get(env_var)
            if env_val:
                data.setdefault(section, {})[key] = env_val
