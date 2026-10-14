#  自主决策 Agent — 工程保障综合审查报告

- **审查日期**：2026-09-29
- **审查对象**：`D:/shit/TRAE/TRADING_AGENT`（已完成编码的 13 个源文件 + 8 个测试套件，61 用例全绿，启动冒烟通过）
- **审查方**：EngineeringAssuranceTeam（4 名子代理并行：code-reviewer / architect / testing-expert / sre-engineer）
- **审查目标**：上线前质量把关（工作流 1：代码审查），识别安全、架构、测试、运维四类风险，去重合并为统一待办。

---

## TL;DR（一句话结论）

全部模块已落地、61 用例全绿、启动冒烟通过，**两条最硬铁律严格达成**（不依赖 TRAE 内部代码 / 确定性风控不受 LLM 污染）；但存在 **1 项 P0（学习闭环死代码）+ 6 项 P1（安全/并发/鲁棒性/数据正确性）**，当前 shadow 阶段仅落库不推送，问题暂未引爆，一旦切到 paper/live 或依赖经验统计即暴露功能性缺陷与泄露风险。**建议本报告交付后即进入修复阶段（P0 + P1 必修，P2 按需）。**

---

## 核心结论卡片

| 维度 | 评级 | 结论 |
|------|------|------|
| 铁律达成（不依赖 TRAE / 确定性风控） | 🟢 达标 | 薄转发层独立，风控引擎零 LLM 依赖，Guardian 两阶段校验 |
| 测试覆盖（已写） | 🟢 达标 | 61 用例全绿，含风控/记忆/regime/LLM 解析/决策/飞书/主流程 |
| 安全（密钥/传输/鉴权） | 🔴 风险 | 明文 api_key 落盘 + 占位 secret + 0.0.0.0 无 TLS |
| 并发（SQLite） | 🟠 风险 | 单连接 + `check_same_thread=False` 无 WAL/busy_timeout，高并发会 `database is locked` |
| 鲁棒性（LLM 异常） | 🟠 风险 | LLM 返回非法动作 → 全链路 500 丢信号；`save_signal` 对 list/dict 字段崩溃 |
| 数据正确性（交割单） | 🟠 风险 | 「预估收益」实为 Σ 名义仓位占比，误导；忽略真实 pnl |
| 学习闭环（经验升级） | 🔴 失效 | `record_outcome` 未接通 `evaluate→update_experience`，PRD 铁律形同虚设 |
| 可观测性 | 🟡 缺失 | 全仓无 logging、无真实健康探针、调度器失败静默 |
| 明文配置意图（用户裁定） | 🟢 保留 | 单用户本地项目，明文配置本身可接受，但需加 .gitignore 防止入库 |

---

## 一、审查发现（按严重度排序，已去重）

### 🔴 P0 级（1 项）

#### P0-1 经验自动升级死代码 → 学习闭环未闭合
- **位置**：`agent/orchestrator.py:142-154`（`record_outcome`）、`agent/orchestrator.py:168`（`produce_daily_report` 的 `exp_changes`）、`memory/store.py:176-191`（`update_regime_stats` 从不调用）
- **根因**：`record_outcome` 只做两件事——更新 `KillSwitchState`（`ks.record_trade` + `save_kill_switch`）和回填 `journal` 的 `user_action/outcome/pnl`。**全程未调用** `experiences.evaluate` / `next_stage` / `update_experience`。
- **影响**：
  1. PRD 铁律「统计验证（PF>1.10 且 n≥30 且 bootstrap 下界>0）才升 Validated」在生产期**完全不生效**——经验库只会停留在 `Observed`，永远不会自动升 `Candidate/Statistically Supported/Validated`，所谓"6 层记忆 + 经验生命周期"形同摆设。
  2. `produce_daily_report` 的 `exp_changes` 恒为空（line 168 永远筛不到 `Validated/Deprecated`），交割单「经验库变更」永远显示"无"。
  3. `memory_regime` 表永被填充（`update_regime_stats` 无人调用），regime 维度的胜率/PF 统计缺失。
- **修复方向**：
  - `record_outcome` 中：按 `signal.setup_type` 或 `regime` 维度聚合该信号历史 pnl → `evaluate(pnls)` 得 `metrics + promote` → 若需升级则 `update_experience(...)`；同时调 `update_regime_stats`。
  - 新增 `POST /webhook/outcome` 回填入口（`main.py`），让 TRAE 侧在用户手动下单并收盘后 POST `pnl`。
  - 让 `exp_changes` 真正反映 `get_experiences(stage in (Validated, Deprecated))` 的当日变更。

---

### 🟠 P1 级（6 项）

#### P1-1 明文 api_key 落盘入库风险
- **位置**：`config/settings.yaml:11`（真实 `sk-...`）、`config/settings.yaml:9`（webhook）
- **根因**：用户裁定"全部明文、单用户"，意图合理；但当前 `settings.yaml` 与代码同目录、**无 `.gitignore`**（已确认 Glob 无结果），一旦 `git add` 或打包外发即泄露。
- **影响**：api_key 泄露 = 他人可用你的百炼额度刷量/产生费用；webhook 泄露 = 飞书机器人可被冒用推送。
- **修复方向**（不违背"明文"意图）：
  - 新增 `.gitignore`，排除 `config/settings.yaml` 与 `data/agent.db`（及 `data/` 目录）；
  - 在 PRD / README 标注"settings.yaml 含密钥，禁止入库/外发"；
  - 建议用户在百炼后台 **rotate 一次 api_key**（旧 key 已落盘，视为已暴露）。

#### P1-2 占位 secret 未改 + 0.0.0.0 + 无 TLS
- **位置**：`config/settings.yaml` `agent_webhook.secret = "trae-agent-shared-secret-change-me"`（占位未改）、`main.py:79` `host="0.0.0.0"`、`main.py:46` 用 `!=` 明文比较 secret、`main.py:43-47` 无 TLS。
- **根因**：占位 secret 是默认值；`0.0.0.0` 暴露到所有网卡；secret 比较未用 `hmac.compare_digest`（存在时序侧信道）。
- **影响**：占位 secret 等于无鉴权，任何能访问端口者都能伪造信号注入决策；`0.0.0.0` 在公网环境直接暴露。
- **修复方向**：
  - 启动时若 secret 仍为占位值，**拒绝启动并告警**（fail-fast）；
  - secret 比较改 `hmac.compare_digest`；
  - 单用户本地场景，`host` 改 `127.0.0.1`（如需远程再走 TLS 反代，不要裸 HTTP 暴露）。

#### P1-3 SQLite 单连接跨线程 → `database is locked`
- **位置**：`main.py:30` `sqlite3.connect(DEFAULT_DB, check_same_thread=False)`；`memory/store.py` 全程复用 `self.conn`。
- **根因**：FastAPI 把同步端点放进线程池执行（webhook 高频），APScheduler 在独立线程 22:00 触发 `produce_daily_report`。两个线程共享同一连接，**未设 WAL、未设 `busy_timeout`、无锁串行化**。
- **影响**：并发读写时极易抛 `sqlite3.OperationalError: database is locked`，导致信号落库失败或交割单生成失败。
- **修复方向**：
  - 连接时执行 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`（WAL 允许多读一写，busy_timeout 让写冲突时等待而非立即失败）；
  - `MemoryStore` 加 `threading.Lock` 串行化写操作（或改为每请求新建连接 + 连接池思路）；
  - webhook 端点包 `try/except`，锁冲突时降级重试而非 500。

#### P1-4 LLM 非法动作 → 全链路 500 丢信号
- **位置**：`agent/orchestrator.py:104` `guard(...)` 调用；`decision/guardian.py:24` `validate_decision(...)`；`risk/engine.py:147-148` `raise ValueError(...)`；`main.py:54` `agent.process_signal(data)` 未包 try。
- **根因**：`orchestrator.process_signal` 的 `try`(line 82-88) **只包住** `analyze_intraday` + `parse_decision`。一旦 `parse_decision` 返回非法动作（如 LLM 吐 `BUY`/`HOLD`/`SELL`，不在 `ALLOWED_ACTIONS`），后续 `guard()` → `validate_decision` 在 `engine.py:148` **抛 `ValueError`**，该异常在 try 之外，一路漏到 `main.py:54`，webhook 未捕获 → **HTTP 500，信号丢失**。
- **影响**：LLM 偶发输出偏差即导致整条信号链路崩溃，且 `send_feishu` 失败也无通知（见 P2-3），形成"静默丢信号"。
- **修复方向**（源头治理 + 兜底双保险）：
  - 在 `llm/client.py` 的 `parse_decision` 中对 `decision` 字段做白名单归一化：不在 `ALLOWED_ACTIONS` 则映射为 `OBSERVE` 并附 `risk_flags` 警告；
  - 在 `orchestrator.process_signal` 把 `guard + build_decision + 落库` 整体包 `try`，异常时降级为 `OBSERVE` 决策并仍落库（不丢信号）。

#### P1-5 `save_signal` 在 `sl_zone`/`tp_zone` 为 list/dict 时崩溃
- **位置**：`memory/store.py:40-42`
- **根因**：`entry_zone` 做了 `json.dumps if isinstance(list/dict)`，但 `sl_zone`(line 41)、`tp_zone`(line 42) 直接 `sig.get(...)` 未做同款编码。
- **影响**：TRAE 侧若传 `sl_zone=[a,b]` / `tp_zone={...}` 这类结构化区间，sqlite3 会抛 `InterfaceError`（无法序列化），信号落库失败。
- **修复方向**：`sl_zone`/`tp_zone` 与 `entry_zone` 统一处理（list/dict → `json.dumps`，否则原样），或抽一个 `_jsonify` 辅助函数。

#### P1-6 每日交割单「预估收益」实为 Σ 名义仓位占比（误导）
- **位置**：`agent/orchestrator.py:165` `est = sum(position_size_pct)`；`journal/feishu.py:52` `若执行预估收益: {est_pnl_pct:+.2%}`
- **根因**：`position_size_pct` 是"单笔名义仓位占净值比"（如 2%），求和得到的是"今日所有信号建议仓位之和"，与"盈亏"无任何关系；且 `record_outcome` 已录入真实 `journal.pnl`，此处却完全忽略。
- **影响**：用户看交割单会误以为"今日预估赚 X%"，实际是仓位占用百分比，严重误导决策复盘。
- **修复方向**：
  - 改用真实 `journal.pnl` 求和（已回填的才计入），字段改名为"当日已录入盈亏"/"实际盈亏"；
  - 若需"名义敞口"统计则单独成行、明确标注"敞口占比"，不与盈亏混淆。

---

### 🟡 P2 级（架构 / 测试 / 运维，3 项）

#### P2-1 架构：多条"已实现未接线"死代码
- 反馈环：`record_outcome` 未接经验评估（见 P0-1）。
- 周度复盘：`analyze_weekly`（`llm/client.py`）与 `produce_daily_report` 的周聚合从未被调度器调用。
- 时段闸：`risk/engine.py` 的 `is_tradeable_now` 在 `process_signal` 中**从未调用**——非工作日/非交易时段进来的信号也会照常分析（仅靠 `should_close_now` 控制 21:00 指令，时段准入缺失）。
- 真实 regime：`detect_regime` 默认 `RANGING, 0.6`（`orchestrator.py:56`），`market_metrics` 若 TRAE 不传 ADX/ATR 则永远走默认，`market/regime.py` 的真实判定形同摆设。
- **建议 ADR**：
  - ADR-1 Repository 抽象：`MemoryStore` 直接耦合 sqlite3，建议抽 `MemoryRepository` 接口便于测试与替换；
  - ADR-2 闭合反馈环：outcome 必须回流到 experience/regime 统计（P0-1 修复即落地）；
  - ADR-3 强化约束强制 + 密钥治理：占位 secret 拒绝启动、`.gitignore`（P1-1/2）。

#### P2-2 测试：关键路径零覆盖
- 调度器 22:00 生成交割单：零测试（`_start_scheduler` 未测，时区/cron 行为未验证）；
- LLM 真实路径 / 失败回退：`process_signal` 仅用 mock LLM，真实 `LLMClient.analyze_intraday` 的解析/降级无测试；
- `REDUCE_RISK` / `REJECT` 路径：未端到端覆盖（仅 `validate_decision` 单测涉及降级 WAIT）；
- `record_outcome` 真实回填 + 经验升级：零测试（因为当前是死代码）。

#### P2-3 运维：全仓无可观测性
- 无 `logging`：落库失败、LLM 异常、飞书推送失败均只在返回值里，进程无日志；
- 无真实健康探针：`/health` 仅返回 `{"status":"ok"}`，不检查 DB/LLM/飞书可达；
- 无进程守护：崩溃无自动重启；
- 调度器 `add_job(..., "cron", hour=22)`（`main.py:68`）未设 `misfire_grace_time`/`coalesce`/`max_instances`，进程在 22:00 busy/重启则静默错过，且 `produce_daily_report` 内 `send_feishu` 失败无人通知。

---

### 🟢 P3 级（代码卫生，4 项，低优先）
- `agent/orchestrator.py` / `store.py` 多处 `__import__("risk.engine")` 动态导入，应改为顶部静态 import。
- `market/regime.py` 的 `KNOWN_REGIMES` 含 `UNCERTAIN` 但 `detect_regime` 从不返回，存在语义漂移。
- `decision/engine.py` / schema 中 `risk_score` 为占位字段，无计算逻辑。
- 类型注解用了小写 `any`（`journal/feishu.py:38` `List[Dict[str, any]]`），应为 `Any`（来自 `typing`）。

---

## 二、修复优先级与行动清单（P0 + P1，含 file:line）

| 编号 | 文件:行 | 改动 | 严重度 |
|------|---------|------|--------|
| P0-1 | `agent/orchestrator.py:142-154` | `record_outcome` 接通 `evaluate→next_stage→update_experience`；新增 `update_regime_stats` 调用；让 `produce_daily_report:168` 的 `exp_changes` 取自真实升级 | 🔴 |
| P0-1b | `main.py` | 新增 `POST /webhook/outcome` 回填入口（secret 校验 + 调 `record_outcome`） | 🔴 |
| P1-1 | 新增 `.gitignore` | 排除 `config/settings.yaml`、`data/`、`*.db` | 🟠 |
| P1-1b | PRD/README | 标注"settings.yaml 含密钥，禁止入库"；建议 rotate api_key | 🟠 |
| P1-2 | `main.py:46` | secret 改 `hmac.compare_digest`；占位值 fail-fast 拒绝启动 | 🟠 |
| P1-2b | `main.py:79` | `host` 改 `127.0.0.1`（远程场景再上 TLS 反代） | 🟠 |
| P1-3 | `main.py:30` | 连接加 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000` | 🟠 |
| P1-3b | `memory/store.py` | `MemoryStore` 写操作加 `threading.Lock` 串行化 | 🟠 |
| P1-4 | `llm/client.py` `parse_decision` | 非法动作白名单归一化为 `OBSERVE` + 警告 flag | 🟠 |
| P1-4b | `agent/orchestrator.py:104-118` | `guard+build_decision+落库` 整体包 try，异常降级 `OBSERVE` 仍落库 | 🟠 |
| P1-5 | `memory/store.py:40-42` | `sl_zone`/`tp_zone` 与 `entry_zone` 统一 `json.dumps` 处理 | 🟠 |
| P1-6 | `agent/orchestrator.py:165` + `journal/feishu.py:52` | 改用真实 `journal.pnl` 求和，改名"实际盈亏"，与"敞口占比"分离 | 🟠 |

**P2/P3 按需**：接 logging + 真实 `/health`（`P2-3`）、补调度器/LLM 路径/REDUCE_RISK/REJECT/record_outcome 测试（`P2-2`）、接 `is_tradeable_now` 时段准入与真实 regime 入参（`P2-1`）、清理动态 import/占位字段（`P3`）。

---

## 三、修复后验证闸门（新增测试必须覆盖）

1. **反馈环**：`record_outcome` 录入 30+ 笔正期望样本 → `get_experiences` 出现 `Validated` 阶段；`produce_daily_report` 的 `exp_changes` 非空。
2. **并发**：开 2 线程同时 `process_signal` + 调度器 `produce_daily_report` → 无 `database is locked`。
3. **LLM 非法动作**：`parse_decision` 返回 `BUY` → 归一为 `OBSERVE`，不再 500。
4. **LLM 失败回退**：`analyze_intraday` 抛异常 → `process_signal` 返回 `OBSERVE` 决策且落库成功。
5. **save_signal 结构化区间**：`sl_zone=[a,b]`/`tp_zone={...}` → 落库成功无 InterfaceError。
6. **交割单正确性**：`record_outcome` 回填 pnl 后 `produce_daily_report` 显示真实盈亏，而非 Σ 仓位占比。
7. **secret 安全**：占位 secret 启动拒绝；`hmac.compare_digest` 通过；错误 secret 返 403；`127.0.0.1` 绑定。
8. **调度器**：mock 当前时间 22:00 → `produce_daily_report` 被调用且飞书推送（或失败有日志）。

> 全部通过后，原 61 用例应仍全绿（回归闸门），新增用例一并纳入 `pytest`。

---

## 四、关键正项（修复时务必保留，勿破坏）

- ✅ **两铁律严格达成**：`main.py` 不 import TRAE 任何代码；`risk/engine.py` 确定性风控零 LLM 依赖；`decision/guardian.py` 两阶段 Propose→Validate 闸门。
- ✅ **飞书 code==0 校验**（`journal/feishu.py:30`）：HTTP 200 不等于成功，业务码必须校验——这是 S2 修复的核心纪律，保留。
- ✅ **代理纪律**：`llm/client.py` 与 `journal/feishu.py` 均 `trust_env=False` + `proxies` 取 config，上线置空即零代理。
- ✅ **61 用例全绿 + 启动冒烟通过**：`/health` 200、正确 secret 的 webhook 返回 WAIT 决策（印证 LLM→确定性校验全链路）。
- ✅ **明文配置意图**（用户 2026-09-29 裁定）：保留 `config/settings.yaml` 明文，仅补 `.gitignore` 防入库，不引入环境变量隔离。

---

*报告由 EngineeringAssuranceTeam 主理人汇编 4 份并行子代理审查（code-reviewer / architect / testing-expert / sre-engineer），所有论断均经源码 file:line 实证。*
