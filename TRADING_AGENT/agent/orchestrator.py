"""Agent 编排器：把信号经过 记忆 -> regime -> LLM分析 -> Guardian校验 -> 风险引擎 -> 落库/推送。

职责边界（PRD 铁律）：
  - 只产出决策建议，绝不下单；
  - 仅工作日日内、不持仓过夜（21:00 由 scheduler 发指令，本类提供 should_close_now）；
  - phase: shadow=只落库不推送；paper/live=推送飞书。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config.loader import Settings
from memory.store import MemoryStore
from memory.experiences import evaluate, next_stage
from risk.engine import BEIJING, KillSwitchState, check_kill_switch
from decision.engine import build_decision
from decision.prompts import build_analysis_prompt
from decision.guardian import guard
from market.regime import detect_regime
from journal.feishu import send_feishu, format_daily_report

DEFAULT_EQUITY = 10000.0


class Agent:
    def __init__(
        self,
        settings: Settings,
        store: MemoryStore,
        llm_client=None,
        phase: str = "shadow",
        regime_override: Optional[tuple] = None,
    ):
        self.settings = settings
        self.store = store
        self.llm = llm_client
        self.phase = phase
        self._regime_override = regime_override

    # ---------------- 工具 ----------------
    def today_key(self) -> str:
        return datetime.now(BEIJING).strftime("%Y-%m-%d")

    def detect_regime(self, metrics: Optional[Dict[str, float]] = None):
        if self._regime_override:
            return self._regime_override
        if metrics:
            return detect_regime(
                adx=metrics.get("adx", 10.0),
                atr=metrics.get("atr", 10.0),
                atr_ma=metrics.get("atr_ma", 10.0),
                trend_slope=metrics.get("trend_slope", 0.0),
                liquidity_sweep=metrics.get("liquidity_sweep", False),
                news_risk=metrics.get("news_risk", False),
            )
        return "RANGING", 0.6

    def _equity(self) -> float:
        v = self.store.get_working("equity")
        return float(v) if v else DEFAULT_EQUITY

    def should_close_now(self) -> bool:
        now = datetime.now(BEIJING)
        from risk.engine import should_warn_no_overnight

        return should_warn_no_overnight(
            now,
            warn_minutes=self.settings.trading.get("no_overnight_warn_minutes", 60),
            end=self.settings.trading.get("session_end", "22:00"),
        )

    # ---------------- 主流程 ----------------
    def process_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        regime_label, regime_conf = self.detect_regime(signal.get("market_metrics"))
        self.store.record_regime(
            datetime.now(BEIJING).isoformat(timespec="seconds"), regime_label, regime_conf
        )

        # LLM 分析提案（无 LLM 时默认 OBSERVE）
        if self.llm is not None:
            prompt = build_analysis_prompt(signal, regime_label, self._recent_memories())
            try:
                raw = self.llm.analyze_intraday(prompt)
                from llm.client import LLMClient

                proposal = LLMClient.parse_decision(raw)
            except Exception:
                proposal = {"decision": "OBSERVE", "confidence": 0.0, "reasons": ["LLM 调用失败"], "memory_refs": []}
        else:
            proposal = {"decision": signal.get("raw_score", 0) >= 0.65 and "EXECUTE" or "WAIT",
                        "confidence": float(signal.get("raw_score", 0.0)),
                        "reasons": [], "memory_refs": []}

        # Kill Switch
        day = self.today_key()
        ks: KillSwitchState = self.store.load_kill_switch(day)
        risk_cfg = self.settings.as_risk_config()
        triggered, _ = check_kill_switch(ks, risk_cfg, day)

        try:
            # Guardian 白名单二次校验（防御性）
            allowed = self.settings.guardrails.get("decision", {}).get(
                "allowed_actions", list(__import__("risk.engine").ALLOWED_ACTIONS)
            )
            guarded_decision, gflags = guard(proposal.get("decision", "OBSERVE"),
                                             float(proposal.get("confidence", 0.0)), risk_cfg, allowed,
                                             kill_switch_triggered=triggered)
            proposal["decision"] = guarded_decision
            proposal.setdefault("risk_flags", [])
            proposal["risk_flags"].extend(gflags)

            decision = build_decision(
                signal, regime_label, regime_conf, proposal, risk_cfg, self._equity(),
                kill_switch_triggered=triggered,
            )

            # 落库
            self.store.save_signal(signal)
            self.store.save_decision(decision)
            self.store.add_journal({
                "signal_id": decision.get("signal_id"),
                "ts": datetime.now(BEIJING).isoformat(timespec="seconds"),
                "regime": regime_label,
                "agent_decision": decision["decision"],
                "suggested_sl": decision.get("stop_loss"),
                "suggested_tp": decision.get("take_profit"),
                "suggested_size_pct": decision.get("position_size_pct"),
                "user_action": None,
                "outcome": None,
                "pnl": None,
                "note": "phase=" + self.phase,
            })
        except Exception as e:
            # 决策流程异常（Guardian/风控/落库任一环失败）-> 最小降级，确保信号不丢失
            decision = {
                "signal_id": signal.get("signal_id"),
                "symbol": signal.get("symbol"),
                "decision": "OBSERVE",
                "direction": signal.get("direction", "long"),
                "confidence": 0.0,
                "market_regime": regime_label,
                "setup_type": signal.get("setup_type"),
                "stop_loss": None,
                "take_profit": None,
                "position_size_pct": None,
                "reasons": [f"决策流程异常，降级为 OBSERVE: {type(e).__name__}: {e}"],
                "risk_flags": ["DEGRADED"],
                "memory_refs": [],
            }
            self.store.save_signal(signal)
            self.store.add_journal({
                "signal_id": decision.get("signal_id"),
                "ts": datetime.now(BEIJING).isoformat(timespec="seconds"),
                "regime": regime_label,
                "agent_decision": "OBSERVE",
                "suggested_sl": None,
                "suggested_tp": None,
                "suggested_size_pct": None,
                "user_action": None,
                "outcome": None,
                "pnl": None,
                "note": "phase=" + self.phase,
            })

        # 推送（仅 paper/live）
        if self.phase in ("paper", "live"):
            text = self._format_decision_message(decision)
            ok, detail = send_feishu(self.settings.feishu["webhook"], text, self.settings.get_proxy())
            decision["_feishu_ok"] = ok
            decision["_feishu_detail"] = detail

        return decision

    def record_outcome(self, signal_id: str, user_action: str, outcome: str, pnl: float) -> None:
        """用户手动下单后回填结果（用于经验统计、regime 统计与 KillSwitch 状态）。

        闭合经验反馈环：
          - 维持 KillSwitch 更新 + journal 回填；
          - 按信号 setup_type 聚合该 setup 的历史 pnl，做统计验证，达标则升级经验阶段；
          - 同时更新 memory_regime 统计，让 regime 表被填充。
        """
        day = self.today_key()
        ks = self.store.load_kill_switch(day)
        ks.record_trade(pnl, day)
        self.store.save_kill_switch(day, ks.daily_pnl_pct, ks.consecutive_losses)
        # 写入 journal 的结果字段（定位该 signal_id 最新一行）
        self.store.update_journal_outcome(signal_id, user_action, outcome, pnl)

        # ---- 经验反馈环（确定性统计，LLM 不参与判定）----
        setup_type = self.store.get_signal_setup_type(signal_id)
        if setup_type:
            pnls = self.store.get_pnls_by_setup_type(setup_type)
            if pnls:
                key = f"setup:{setup_type}"
                description = f"setup_type={setup_type} 经验闭环（自动统计）"
                current_stage = self.store.get_or_create_experience(key, description)
                metrics = evaluate(pnls)
                if metrics["promote"]:
                    new_stage = next_stage(current_stage, metrics)
                    self.store.update_experience(
                        key, metrics["n"], metrics["pf"], metrics["bootstrap_lower"], new_stage
                    )
            # regime 统计（regime 取自该 signal 最新 journal 行）
            regime = self.store.get_journal_regime(signal_id)
            if regime:
                self.store.update_regime_stats(regime, pnl > 0, pnl)

    def produce_daily_report(self) -> str:
        day = self.today_key()
        dec_list = self.store.get_daily_decisions_report(day)
        # 真实盈亏：当日 journal.pnl 中非空值求和（交割单实际录入）
        real_pnl = self.store.get_daily_realized_pnl(day)
        ks = self.store.load_kill_switch(day)
        ks_info = f"当日盈亏 {ks.daily_pnl_pct:+.2%}, 连亏 {ks.consecutive_losses} 笔"
        # 经验库变更：仅展示「今日发生过阶段更新」且当前为 Validated/Deprecated 的条目
        exp_changes = []
        for e in self.store.get_experiences():
            if e["stage"] in ("Validated", "Deprecated") and str(e.get("updated_at", "")).startswith(day):
                exp_changes.append(f"{e['key']}->{e['stage']}")
        text = format_daily_report(day, dec_list, ks_info, exp_changes, real_pnl)
        if self.phase in ("paper", "live"):
            send_feishu(self.settings.feishu["webhook"], text, self.settings.get_proxy())
        return text

    # ---------------- 内部 ----------------
    def _recent_memories(self) -> List[str]:
        eps = self.store.list_episodes(limit=5)
        return [f"[{e['regime']}] {e['agent_decision']} -> {e['actual_outcome']} ({e['pnl']})" for e in eps]

    def _format_decision_message(self, d: Dict[str, Any]) -> str:
        return (
            f"【信号 {d.get('signal_id')}】{d.get('direction')} {d.get('decision')}\n"
            f"regime: {d.get('market_regime')}\n"
            f"SL={d.get('stop_loss')} TP={d.get('take_profit')} size={d.get('position_size_pct')}\n"
            f"理由: {'; '.join(d.get('reasons', []))}\n"
            f"风控标记: {'; '.join(d.get('risk_flags', [])) or '无'}"
        )
