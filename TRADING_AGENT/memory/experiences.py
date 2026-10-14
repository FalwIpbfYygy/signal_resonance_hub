"""经验生命周期的统计验证（Observed -> Candidate -> Statistically Supported -> Validated -> Deprecated）。

升级门槛（PRD Round 5 / 6 锁定）：样本量 n >= 30 且 PF > 1.10 且 bootstrap 下界 > 0。
全部确定性，使用 numpy 做 bootstrap。LLM 不参与数值判定。
"""
from __future__ import annotations

import numpy as np

STAGES = ["Observed", "Candidate", "Statistically Supported", "Validated", "Deprecated"]

# 升级门槛（来自已锁定决策）
MIN_N = 30
MIN_PF = 1.10
MIN_BOOTSTRAP_LOWER = 0.0


def compute_pf(pnls: list[float]) -> float:
    """Profit Factor = 总盈利 / |总亏损|。无亏损时返回 inf。"""
    wins = sum(p for p in pnls if p > 0)
    losses = sum(-p for p in pnls if p < 0)
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def bootstrap_lower(pnls: list[float], n_boot: int = 2000, seed: int = 0) -> float:
    """逐笔盈亏均值 bootstrap 置信区间下界（2.5 分位）。"""
    arr = np.asarray(pnls, dtype=float)
    if arr.size == 0:
        return 0.0
    rng = np.random.default_rng(seed)
    boot = [rng.choice(arr, size=arr.size, replace=True).mean() for _ in range(n_boot)]
    return float(np.percentile(boot, 2.5))


def evaluate(pnls: list[float]) -> dict:
    """返回评估指标与建议阶段。仅做统计，不写库。"""
    n = len(pnls)
    pf = compute_pf(pnls)
    lower = bootstrap_lower(pnls)
    metrics = {
        "n": n,
        "pf": pf,
        "bootstrap_lower": lower,
        "pass_n": n >= MIN_N,
        "pass_pf": pf >= MIN_PF,
        "pass_ci": lower > MIN_BOOTSTRAP_LOWER,
    }
    metrics["promote"] = all([metrics["pass_n"], metrics["pass_pf"], metrics["pass_ci"]])
    return metrics


def next_stage(current_stage: str, metrics: dict) -> str:
    """基于统计指标推进经验阶段（确定性状态机）。"""
    if metrics["promote"]:
        order = ["Observed", "Candidate", "Statistically Supported", "Validated"]
        if current_stage in order:
            idx = order.index(current_stage)
            return order[min(idx + 1, len(order) - 1)]
        return "Validated"
    # 样本足够但 PF/CI 不达标 -> 降级为 Deprecated
    if metrics["pass_n"] and not (metrics["pass_pf"] and metrics["pass_ci"]):
        return "Deprecated"
    return current_stage
