#  Autonomous Trading Decision Agent — 开发文档（v1.0）

> 状态：**决策已锁定，进入编码阶段**。经 6 轮对齐（Round 1–6，共 19 项决策），本文档为正式 PRD。
> 创建日期：2026-09-29 ｜ 升级 v1.0：2026-09-29
> 作者：工程保障团队（lead 综合）

---

## ⚠️ 三条铁律（来自用户澄清，最高优先级，任何设计不得违反）

1. **Agent 是「决策/建议层」，不是「执行层」**。Agent 判断这一单要不要下、给止盈止损与仓位建议，但**不下单**。用户本人手动下单。
2. **仅工作日日内交易，不持仓过夜**。Agent 所有决策、风控、Kill Switch 都必须尊重这条；收盘前必须给出「平仓/不持仓过夜」的明确指令。
3. **每日飞书推送 Agent 当天的「交割单」**。Agent 记录自己给出的每一条建议与实际结果，收盘后汇总发送。

## 🔧 配置约定（单用户本地项目）

- **用户明确裁定（2026-09-29）**：本项目仅供用户一人使用，不需要环境变量/密钥隔离那套工程化做法。**所有配置一律明文写在 `config/settings.yaml`**，代码直接读取，不做 env var 间接层。
- 含密钥的配置项（API Key、Workspace ID、飞书 webhook、webhook secret、代理地址、DB 路径）均明文落盘到 `config/settings.yaml`。
- 非强制提醒：`settings.yaml` 含密钥，**请勿提交到公开仓库或分享给他人**；单机自用下这不构成实际风险。
- 代理：本地开发将 `proxy: "http://127.0.0.1:10808"` 明文写在 `settings.yaml`；上线（新加坡服务器）把 `proxy` 置空字符串即零代理（代码不读任何环境变量，仅读配置文件）。

---

## PART 1 · 可行性判断

**核心结论：作为「决策支持/风控过滤层」可行性高（HIGH）；作为「自主下单执行层」可行性低（LOW，且本设计明确不采用）。**

市面实证（2024–2026 调研）：
- **Alpha Arena（Nof1，6 模型真实加密永续 $1万/17天）**：仅 Qwen3 Max +22.32% 盈利，DeepSeek +4.89%，GPT-5 -62.66%、Gemini -56.71%、Grok4 -45.3%、Claude -30.81%。共性致命伤：**过度交易、手续费吞噬利润、胜率仅 25–30%**。
- **HKU Agentic Trader（2026-04，10 模型实时外汇）**：Qwen 累计最强 ~10%，DeepSeek -15.10%；**交易次数多 ≠ 收益好**。
- **AI-Trader 基准（HKU）**：6 LLM 全自主交易美股/加密/A股，多数低收益、弱风控；**风控能力决定跨市场稳健性**。
- **TradingAgents（UCLA/MIT，LangGraph 多智能体）**：多智能体辩论准确率 68.5% vs 单一 45.3%，但仍非直接盈利保证。
- **FinMem（ICLR workshop）**：分层记忆 + profiling/decision/reflection 循环，提升决策一致性。
- **Guardian Pattern（Boxmining）**：两阶段 **Propose(LLM) → Validate(确定性规则)**，硬限制（单笔 2–5%、置信度阈值 65–70%、Kill Switch、回撤限制、人工复核）+ YAML 护栏即代码。

**为什么本设计避开了致命伤**：原策略 TRAE 是**已存在的规则化信号系统**（4 子系统共振），本 Agent 不替代它、不在黑盒里交易，而是做三件事——(a) 对已有信号做**第二意见过滤**（REJECT/REDUCE_RISK 掉坏 setup）；(b) 用**确定性风险引擎**算仓位/SL/TP（LLM 不碰精确计算）；(c) 用记忆做**regime 感知**与**错误复盘**。最终下单权在用户手里（human-in-the-loop），飞书日志形成**反馈闭环**供学习。这把「LLM 自主交易」的高失败率模式，转成了「规则信号 + LLM 分析 + 确定性风控 + 人审」的低风险模式。

> 对齐点 1.1：是否同意将 Agent 定位为「**原策略信号的决策过滤器 + 风控顾问**」，而非新的信号源？

---

## PART 2 · 为何有效

- **确定性风险引擎**：ATR/仓位/SL/TP/Kill Switch 全由 Python 算，消除 LLM 算术幻觉。
- **分层记忆**：避免重复犯同一类错误；regime 记忆让 Agent 在震荡/趋势切换时调整。
- **结构化 JSON 决策**：每个决策可审计、可回放、可统计验证。
- **Human-in-the-loop**：用户手动下单，最终控制保留在人，规避过度交易与黑盒爆仓。
- **反馈闭环**：每日飞书交割单 → 经验库统计验证 → 升级为 Validated Experience。

---

## PART 3 · 为何无效（失败模式前置）

- LLM 被允许算仓位/SL → 数值错误。
- Agent 越权变成自主执行 → 过度交易、手续费吞噬。
- 记忆未做统计验证就升级 → 迷信（把噪声当规律）。
- 原策略信号质量本身差 → Agent 无法「点石成金」（垃圾进垃圾出）。
- 「不持仓过夜」未被强制 → 跳空缺口风险。
- 记忆只记赢不记亏 → 幸存者偏差。

---

## PART 4 · 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│  TRAE 原策略（独立进程，不 import，不修改）                    │
│  4 子系统 → signal_store.record_signal()                      │
└───────────────────────────┬─────────────────────────────────┘
                            │  (低耦合：API / Webhook / MQ / 共享事件)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Signal Adapter  （仅读取信号，翻译为 Agent 内部 Signal 模型） │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Agent Orchestrator                                            │
│   ├─ Market Regime Detector  (Python，基于 ATR/ADX/波动)      │
│   ├─ Memory Manager          (6 层记忆读写 + 经验生命周期)     │
│   ├─ LLM Analyst             (只做分析与建议文本，不计算)      │
│   ├─ Risk Engine             (确定性：仓位/SL/TP/KillSwitch)   │
│   ├─ Decision Validator      (Guardian：LLM提议→规则校验)      │
│   └─ Journal & Reporter      (记录建议+结果，飞书每日交割单)   │
└───────────────────────────┬─────────────────────────────────┘
                            │  结构化决策 JSON
                            ▼
                 用户手动下单（执行层不在 Agent）
                            │
                            ▼
                 收盘后 Journal 汇总 → 飞书交割单
```

> 对齐点 4.1：Signal Adapter 用哪种传输？（见 PART 12 / 对齐问题）
> 对齐点 4.2：是否允许 Agent 在「WAIT/OBSERVE」时主动拉取行情自行分析，还是只被动接收 TRAE 信号？

---

## PART 5 · 记忆架构

6 层：
1. **Working**：当前交易日上下文（持仓状态、今日信号、今日盈亏、Kill Switch 状态）。
2. **Episodic**：单笔事件回忆（某信号→Agent 决策→实际结果→复盘）。
3. **Semantic**：从事件提炼的「知识点」（如「伦敦段 FVG 失败率偏高」）。
4. **Strategy**：当前生效的偏好/规则（如「趋势市只做顺向」）。
5. **Mistake**：明确记录的错误模式与规避动作。
6. **Market Regime**：regime 标签 + 该 regime 下的历史表现统计。

**经验生命周期（强制统计验证才升级）**：
`Observed` → `Candidate` → `Statistically Supported` → `Validated` → `Deprecated`
- 升级到 `Validated` 的必要条件（提议）：样本量 n ≥ 30 且 bootstrap 置信区间下界 > 0（或 PF > 1.10，对标你的  回测口径）。
- 任何 `Validated` 经验若在新 regime 下连续失效 → 降级 `Deprecated`。

> 对齐点 5.1：统计验证的「正收益」口径用哪种？（PF>1.10 / 胜率>50% / 期望收益>0，需定一个主指标）

---

## PART 6 · 决策引擎

5 个动作：`EXECUTE` / `REJECT` / `WAIT` / `REDUCE_RISK` / `OBSERVE`

结构化 JSON 输出（字段归属表 —— 明确 LLM / Python / 禁止）：

| 字段 | 来源 | 说明 |
|------|------|------|
| decision | LLM | 5 选 1 |
| direction | Signal | 多/空来自原信号 |
| confidence | LLM | 0–1，需过阈值(提议65–70%) |
| market_regime | Python | Regime Detector 算 |
| setup_type | Signal+LLM | 信号类型 |
| entry | Signal/Manual | Agent 只给参考，用户定 |
| stop_loss | **Python(禁止LLM)** | ATR 派生 |
| take_profit | **Python(禁止LLM)** | R 倍数派生 |
| position_size_pct | **Python(禁止LLM)** | 风险引擎算 |
| risk_score | Python | 综合评分 |
| signal_quality | Python | 信号质量分 |
| reasons | LLM | 自然语言解释 |
| risk_flags | Python | 触发的风险标记 |
| memory_refs | Python | 引用的记忆 ID |

> 对齐点 6.1：confidence 阈值定多少？（65% / 70% / 可调）
> 对齐点 6.2：REJECT 是否直接「不推送飞书提醒用户」，还是「推送但标注拒绝」？

---

## PART 7 · 风险引擎（确定性 Python）

- **SL**：基于 ATR（如 entry ± 1.5×ATR 或结构位取较优），**纯 Python**。
- **TP**：基于 R 倍数（如 1.5R / 2R），**纯 Python**。
- **仓位**：风险百分比法 —— `size% = (账户风险% × 账户净值) / (|entry-SL| × 合约乘数)`，单笔上限 2–5%（可调）。
- **日内约束**：仅工作日；UTC+8 交易时段内；**收盘前 N 分钟强制发出「不持仓过夜」指令**。
- **Kill Switch（硬限制）**：
  - 单日亏损 ≥ X% → 当日停手；
  - 连续亏损 ≥ K 笔 → 降频/停手；
  - 单笔风险超限 → 拒绝 EXECUTE。
- 全部为**确定性代码 + 配置化阈值（YAML/JSON）**，LLM 无权修改。

> 对齐点 7.1：单笔风险% 默认多少？（2% / 3% / 5%）
> 对齐点 7.2：Kill Switch 日亏损阈值%（如 3% / 5%）与连续亏损笔数（如 3 / 5）？

---

## PART 8 · 学习架构

- 每日收盘：Journal 沉淀为 Episodic；批量事件聚合为 Semantic/Strategy 候选。
- 周期性（提议每周/每 30 笔）：对 Candidate 做统计检验 → 升级或丢弃。
- 人工复核：Validated 经验上线前需用户确认（30 天观察，对标 Guardian 人工复核）。

---

## PART 9 · 交易日志（Journal）

每条记录：`signal_id, timestamp, regime, agent_decision, suggested_sl/tp/size, user_action(实际下单?), outcome(盈/亏/未执行), pnl, note`
每日飞书交割单：汇总当日所有建议 + 实际执行 + 盈亏 + Kill Switch 触发情况 + 经验库变更。

---

## PART 10 · LLM 架构

- **选型（提议）**：Fast = `deepseek-v4-pro-0813`（你的私有空间）/ Reasoning = 备用深度模型 / Fallback = 另一家。
- **Prompt**：结构化 system prompt + 禁止 markdown（`clean_text` 约束沿用）。
- **代理**：本地 `proxy: "http://127.0.0.1:10808"` 明文写在 `settings.yaml`（开发期）；生产置空即零代理。
- **密钥**：`DASHSCOPE_API_KEY` / `WORKSPACE_ID` 明文写在 `config/settings.yaml` 的 `llm` 段，代码直接读取（单用户项目，不做 env var 隔离）。
- **职责边界**：仅输出 analysis / reasons / confidence / decision 选择；禁止输出数值化的 SL/TP/仓位。

> 对齐点 10.1：是否引入 Reasoning 模型做「周度复盘」而 Fast 模型做「盘中决策」？

---

## PART 11 · 数据库设计

**MVP：SQLite**（单文件，零运维）。核心表：
- `signals`（接收的原信号）
- `decisions`（Agent 决策 JSON）
- `journal`（交易日志）
- `memory_*`（6 层记忆）
- `experiences`（经验生命周期）
- `regimes`（regime 历史与统计）
- `kill_switch_state`（当日状态）

**Production（提议）**：PostgreSQL + `pgvector`（记忆语义检索）+ Redis（Working 层热数据）。
> 对齐点 11.1：MVP 是否直接用 SQLite 即可，还是你希望一步到位 Postgres？

---

## PART 12 · 项目目录

```
D:/shit/TRAE/TRADING_AGENT/
├── agent/          # Orchestrator + 各子模块编排
├── memory/         # 6 层记忆 + 经验生命周期
├── market/         # Regime Detector
├── decision/       # 决策引擎 + Decision Validator(Guardian)
├── risk/           # 确定性风险引擎 + Kill Switch
├── execution/      # 仅记录用户执行结果（不自动下单）
├── evaluation/     # 统计验证 / 回测 OOS / 实验
├── journal/        # 日志 + 飞书每日交割单
├── prompts/        # LLM prompt 模板
├── config/         # YAML 护栏即代码 + 明文 settings.yaml 加载
├── docs/           # 本文档
└── main.py         # 入口
```

> 对齐点 12.1：Signal Adapter 传输方式 ——
> (a) TRAE 暴露 HTTP API，Agent 轮询；
> (b) TRAE 主动 Webhook push 到 Agent；
> (c) 共享消息队列（MQ）；
> (d) 共享文件/DB 事件表。
> 约束：**不得 import TRAE 内部代码**。

---

## PART 13–16 · 生命周期 & Shadow / Paper / Live

- **PHASE 0 Observer**：只接收信号、建记忆，不输出决策。
- **PHASE 1 Shadow**：输出决策建议但**不推送飞书、不计入经验库权重**（仅观察 LLM+规则表现）。
- **PHASE 2 Paper**：输出建议 + 飞书推送，记录「若执行」盈亏，经验库开始统计但不自动升级。
- **PHASE 3 Small Capital**：用户按建议小资金手动下单，真实闭环。
- **PHASE 4 Controlled Live / PHASE 5 Continuous Eval**：持续评估 + 人工复核升级。

> 对齐点 13.1：第一个可运行里程碑做到哪个 Phase？（提议：先做到 PHASE 1 Shadow 验证管道）

---

## PART 17–20 · 实验设计 / 回测 OOS / 防过拟合 / 失败模式

- 对照实验：A(原策略) / B(原策略+Agent) / C(Agent 独立)，统一成本/滑点/风险口径。
- 防过拟合清单：Look-ahead / Survivorship / Data Snooping / Overfitting / Regime Shift / Concept Drift / Selection / Execution Bias。
- OOS：训练/验证/测试分段，bootstrap CI 下界 > 0 才算有效。

---

## PART 21–24 · MVP / Production / 路线图 / 首阶段代码

- **MVP 范围**：Signal Adapter(一种传输) + 记忆(Working/Episodic 起步) + 风险引擎(确定性) + 决策 JSON + 飞书每日交割单 + PHASE 1 Shadow。
- **Production**：完整 6 层记忆 + 统计验证升级 + Postgres+pgvector + 多传输 + Kill Switch 全开。
- **路线图**：对齐文档 → MVP Shadow → Paper → 小资金 → 持续评估。
- **首阶段代码**：先在 `config/` 落地明文 `settings.yaml` + 代理开关；`risk/` 落地确定性引擎；`memory/` 落地 SQLite schema。

---

## 待对齐问题汇总（将分多轮逐条确认）

- [x] 1.1 Agent 定位（过滤器 vs 信号源）→ ✅ 信号过滤器+风控顾问
- [x] 4.1 / 12.1 Signal Adapter 传输方式 → ✅ TRAE 主动 Webhook 推送
- [x] 4.2 是否主动拉行情 → ✅ 仅被动接收
- [x] 5.1 经验验证主指标 → ✅ PF>1.10 (n≥30, bootstrap下界>0)
- [x] 6.1 confidence 阈值 → ✅ 0.65
- [x] 6.2 REJECT 推送行为 → ✅ 推送并标注拒绝
- [x] 7.1 单笔风险% → ✅ 3%
- [x] 7.2 Kill Switch 参数 → ✅ 日亏5%/连亏5笔
- [x] 10.1 是否双模型 → ✅ flash(盘中)+pro(周度)
- [x] 11.1 MVP 数据库选型 → ✅ SQLite
- [x] 13.1 首个里程碑 Phase → ✅ PHASE 1 Shadow

> 本文档为 v1.0，决策已全部锁定，进入编码阶段。

---

## ✅ 已确认决策（Round 1）

| # | 决策点 | 结论 |
|---|--------|------|
| 1.1 | Agent 定位 | **信号过滤器 + 风控顾问**（不产出新信号，不下单） |
| 12.1 | Signal Adapter 传输 | **TRAE 主动 Webhook 推送**到 Agent HTTP 端点（不 import TRAE 内部代码） |
| 13.1 | 首个里程碑 | **PHASE 1 Shadow**（输出决策 JSON，不推送飞书、不计入经验权重） |
| 11.1 | MVP 数据库 | **SQLite**（单文件，后续可迁 Postgres+pgvector） |

## ✅ 已确认决策（Round 2 — 风险护栏）

| # | 决策点 | 结论 |
|---|--------|------|
| 7.1 | 单笔最大风险% | **3%**（账户净值 ×3%，确定性计算，LLM 不可改） |
| 7.2 | Kill Switch | **日亏损 ≥5% 停手 + 连续亏损 ≥5 笔降频/停手** |
| 6.1 | confidence 阈值 | **≥0.65** 才允许 EXECUTE，否则降级 WAIT/OBSERVE |
| 6.2 | REJECT 行为 | **仍飞书推送并标注「已拒绝+原因」**（信息透明、保留审计） |

## ✅ 已确认决策（Round 3 — 数据源 / 验证 / 模型）

| # | 决策点 | 结论 |
|---|--------|------|
| 4.2 | 是否主动拉行情 | **仅被动接收 TRAE 信号**，Agent 不接独立行情源（最简、无未来函数风险） |
| 5.1 | 经验验证主指标 | **PF > 1.10 且 n ≥ 30、bootstrap 下界 > 0** 才升 Validated（对标  回测口径） |
| 10.1 | LLM 选型 | **双模型**：盘中 Fast = `deepseek-v4.1-flash`，周度 Reasoning = `deepseek-v4-pro-0813` |

## ✅ 已确认决策（Round 4 — 集成 / 推送 / 时段）

| # | 决策点 | 结论 |
|---|--------|------|
| 12.2 | Webhook 端点 | **FastAPI** 起 `/webhook/signal`，用**共享 secret token** 校验来源（不 import TRAE 代码） |
| 9.2 | 飞书机器人 | 使用用户提供的专用机器人，hook 明文写在 `config/settings.yaml` 的 `feishu.webhook`（单用户项目，不做 env var 隔离） |
| 7.3 | 不持仓过夜 | 强制平仓指令**提前 60 分钟**发出（即北京时间 **21:00**） |
| 13.2 | 工作日/时段 | **周一至周五**，交易时段 **北京时间 09:00–22:00**（非全天） |

## ✅ 已确认决策（Round 5 — 集成契约 / 范围）

| # | 决策点 | 结论 |
|---|--------|------|
| 12.3 | TRAE 侧改动 | **加薄转发层**：`signal_store.record_signal()` 后异步 POST 同份 JSON 给 Agent 端点；**不碰策略/信号生成逻辑** |
| 12.4 | Signal 契约字段 | 标准字段集：`symbol, direction, setup_type, entry_zone, sl_zone, tp_zone, timestamp, source_subsystem, raw_score` |
| 9.3 | 交割单字段 | 标准交付：日期/信号总数/决策分布/建议仓位+SL-TP汇总/KillSwitch触发/经验库变更/若执行盈亏估算 |
| 21.1 | MVP 首阶段范围 | **全模块一次到位**（6 层记忆 + 经验生命周期 + 统计验证 + Kill Switch 首里程碑即交付） |

## ✅ 已确认决策（Round 6 — 验证 / 记忆表 / 收尾）

| # | 决策点 | 结论 |
|---|--------|------|
| 17.1 | 增益主指标 | **A(原策略全量信号若执行) vs B(Agent 放行/调整后若执行)** 的 PF / 期望收益 / 最大回撤；Agent 有效 = B 控回撤且不损 PF |
| 18.1 | OOS 口径 | **历史 signal_store 回放 + 实时 Shadow 双轨**交叉验证 |
| 11.2 | 记忆表结构 | **12 表**：signals / decisions / journal / memory_working / memory_episodic / memory_semantic / memory_strategy / memory_mistake / memory_regime / experiences / regimes / kill_switch_state |
| — | 文档状态 | 收敛为 **v1.0**，进入正式编码 |
