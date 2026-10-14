#  Trading Decision Agent —— 开发与运维经验总结

> 目的：把本项目（单用户本地  **决策** Agent，不下单、只出建议、落库 + 飞书推送每日交割单）从 0 到 1 落地过程中踩过的坑、定的铁律、验证过的机制固化成文档，供后续任何二次开发、部署、排障直接复用，**避免重复踩坑与无效返工**。
>
> 适用边界（先读这段，避免误用）：
> - 这是**决策建议系统**，不是下单系统，不碰账户/仓位/经纪商 API。
> - 单用户、本地运行（用户自己的 Linux 服务器），**明文配置、无多用户/权限/密钥安全工程**——这是用户明确裁定的取舍，不是疏漏。
> - 仅工作日、北京时间 09:00–22:00、日内不过夜。
> - 项目路径：`D:/shit/TRAE/TRADING_AGENT/`（服务器 `/home/ubuntu/TRADING_AGENT/`）。

---

## 一、三条铁律（任何改动都不得违反）

| # | 铁律 | 落地位置 | 违反后果 |
|---|------|----------|----------|
| 1 | **Agent 只决策、绝不下单** | `decision/engine.py` 只产出建议字段；无任何经纪商/下单调用 | 失去"决策与执行分离"的合规边界，责任不清 |
| 2 | **仅工作日日内、不持仓过夜** | `config/settings.yaml:29-34` 时段 + `agent/orchestrator.py:63 should_close_now` + `no_overnight_warn_minutes` | 隔夜跳空风险由"建议"背书，违背设计初衷 |
| 3 | **每日飞书推送交割单** | `journal/feishu.py` + `main.py:125` cron 调 `report` | 无复盘数据 → 经验反馈环断链（见 P0-1） |

> 铁律是**设计约束**，不是功能开关。任何"看起来更方便"的改动（如加定时自动下单、加隔夜持仓建议、把推送改成可选）都属于违规，应直接拒绝。

---

## 二、开发经验

### 2.1 确定性风控引擎 + Guardian 两阶段校验（LLM 禁算数字）
- **结论**：所有数字（SL/TP/仓位/风险百分比）由 `risk/engine.py` 的**确定性公式**算出，LLM 只负责"语义分析 + 提议决策动作 + 给理由"。LLM 输出的任何数字在 `decision/engine.py:build_decision` 里被**忽略并重算**。
- 配置化的护栏在 `config/settings.yaml:37-52`：`risk.*` 是确定性参数，`guardrails.allowed_actions` 是动作白名单，`guardrails.kill_switch.enabled` 是硬开关。
- **Guardian 二次校验**（`decision/guardian.py:guard`）：在 LLM 提案之后、落库之前再挡一层——动作不在白名单 → 归一为 `OBSERVE`；`confidence < 0.65` → 不允许 `EXECUTE`；KillSwitch 触发 → 强制 `REJECT`。
- **为什么重要**：LLM 会幻觉数字、会越权。把"算账"和"判断"分离，系统行为才可复现、可审计、可在无 LLM 时降级（注入 `llm_client=None` 走规则分支，见 2.3）。

### 2.2 反馈环必须接线，否则就是死代码（P0-1 教训）
- **教训**：初版 `record_outcome`（回填用户实际盈亏）与经验统计/regime 统计/KillSwitch 状态更新**没有真正接线**——代码存在但从未被调用路径闭合，导致经验库永远是空的、regime 表永远是空的、KillSwitch 日亏损统计失效。这是一个**P0 级逻辑缺陷**：系统"看起来在跑"，实则核心学习闭环是断的。
- **修复后**（已验证）：`agent/orchestrator.py:175-207` 的 `record_outcome` 现在真正做四件事——
  1. `ks.record_trade(pnl, day)` + `save_kill_switch` 维持日亏损/连亏状态；
  2. `update_journal_outcome` 回填 journal 真实盈亏；
  3. 按 `setup_type` 聚合历史 pnl → `evaluate(pnls)` 做 numpy bootstrap 统计验证 → 达标则 `next_stage` 升级经验；
  4. `update_regime_stats` 填充 regime 表。
- **红线**：任何"统计/学习/反馈"类代码，写完必须有一条**真实调用路径**把它跑起来，并用测试证明它真的改变了状态。没有调用路径的统计代码 = 死代码。

### 2.3 测试必须 hermetic（不依赖真实外部）
- **结论**：75 个用例能在无网络、无真实 LLM、无真实飞书的情况下全绿。
- 做法：
  - LLM 依赖通过 `create_app(llm_client=None)` 注入 `None`（`main.py:49-65`），Agent 走规则分支，**不发起任何 HTTP 请求**。
  - 飞书推送在测试里用 `monkeypatch`/`respx` 或在 `shadow` 阶段不推送；即便推送也只校验"调用了、传参对"，不真发。
  - SQLite 用临时文件或内存库，跑完即删（run-then-delete，不留垃圾）。
- **为什么**：外部依赖会让你"以为过了"，其实只是网络没抖。hermetic 测试才能在任何机器上复现。

### 2.4 独立复测机制（防实现代理虚报）
- **教训（真实发生）**：实现代理报"75 passed"，独立复测实为 **74 passed + 1 failed（flaky）**，失败用例 `test_concurrent_process_signal_and_report` 抛 `InterfaceError`。根因：单 sqlite3 连接跨线程共享，锁只包写、读方法 + orchestrator 直连 `self.store.conn` 绕过锁。
- **修复**：读方法全改 `_read`/`_read_one` 加锁（`memory/store.py:47-57`）；`threading.Lock` → `RLock`（`:38`）；orchestrator 移除直连 conn，改走 `get_daily_decisions_report`（`:294-302`）。重跑 **75 passed 且并发用例连跑稳定**。
- **纪律**：凡标"完成"的改动，**必须换一个独立进程/视角复测**，重点打边界与并发，不轻信实现方的自测数字。

### 2.5 配置驱动护栏，别把规则写死在代码里
- `allowed_actions`、`kill_switch.enabled`、`risk.*` 全部 YAML 化。改阈值/开关动配置不动代码，降低二次开发的改动面与回归风险。

---

## 三、运维经验

### 3.1 进程管理模式：cron + nohup + restart_trade.sh，进程内**不内嵌调度器**
- **决策**：删除原 APScheduler 进程内调度器（`main.py` 已无 `_start_scheduler`），改为 `argparse` 子命令：`serve`（常驻 HTTP）、`report`（生成并推送当日交割单后退出）。
- **服务器实际跑法**：
  - `crontab` 06:30 调 `restart_trade.sh` 启动、23:20 调脚本关闭；
  - 每日 22:00（北京）cron 调 `python main.py report` 推交割单；
  - `restart_trade.sh` 负责 kill 旧进程 + 清日志 + nohup 启动所有业务脚本（含本 Agent）。
- **依赖清单**（`requirements.txt`，已验证）：`fastapi>=0.110 / uvicorn>=0.27 / pyyaml>=6.0 / numpy>=1.26 / requests>=2.28`。**不需要**装 `apscheduler`（已移除）、`pandas`（未用）、`sqlite3`（标准库）。
- **红线**：新增定时任务，**优先 cron 而非进程内 scheduler**。进程内调度器在 cron 重启 + nohup 模型下会成为"幽灵定时"，难以观测与排查。

### 3.2 绝对路径启动陷阱（pkill/pgrep 模式匹配）
- **坑**：用户手动 `nohup python3 main.py serve` 起的进程，命令行只含 `main.py`；脚本的 pkill/pgrep 模式 `TRADING_AGENT/main.py` **匹配不到** → 重启时杀不掉旧进程、旧进程占着 8000 端口，新进程起不来却无报错。
- **修复**（`deliverables/deploy/restart_trade.sh:53`）：用绝对路径启动——
  ```bash
  nohup "${VENV_PY}" /home/ubuntu/TRADING_AGENT/main.py serve > "${LOG_ROOT}/agent_serve.log" 2>&1 &
  ```
  这样进程命令行含 `TRADING_AGENT/main.py`，上面的 kill 模式（`:11`）才能可靠匹配。
- **纪律**：凡脚本用 `pkill -f <pattern>` / `pgrep -f <pattern>` 管理进程，**启动命令行必须含该 pattern 字符串**（用绝对路径最稳）。部署后第一件事：`ps aux | grep TRADING_AGENT` 确认命令行里出现了路径片段。

### 3.3 proxy 置空（零代理纪律）
- 本地开发机走代理；**上线新加坡服务器必须置空**。
- 落地：`config/settings.yaml:26` `proxy: ""`；`journal/feishu.py:16` `session.trust_env = False` + `proxies` 仅取 config（空即零代理）。
- **纪律**：服务器部署 checklist 第一条——确认 `proxy: ""`。残留代理会让飞书推送/任何外呼走错路由、静默失败。

### 3.4 PHASE 切换纪律
- `main.py:22` `PHASE = "paper"`（推建议、不自动下单）。可选 `shadow`（只落库不推送）/ `live`（推送）。
- 切换 phase 只改这一行 + 确认 `config/settings.yaml` 对应开关，**不要**顺手改其他逻辑。

### 3.5 SQLite 并发：WAL + busy_timeout + 统一加锁
- `main.py:55-58`：`check_same_thread=False` + `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000`。
- 所有读写统一走 `MemoryStore._write` / `_read` / `_read_one`（`memory/store.py:41-57`），持 `RLock`，**禁止**任何模块直连 `self.store.conn` 自己执行 SQL（见 2.4 教训）。
- WAL 模式下，`serve` 常驻进程与 cron 调 `report` 可**并发读写同一 db 文件**而不互锁。

### 3.6 secret 占位 fail-fast
- `main.py:26` `PLACEHOLDER_SECRET`；`main.py:40-46` `validate_config`：若 `agent_webhook.secret` 仍是占位值，**直接抛 RuntimeError 拒绝启动**。
- 部署前必须由用户生成强随机 secret 写入 `config/settings.yaml:23`（请生成强随机值，切勿使用占位或示例值）。**绝不**用占位值上线。

### 3.7 .gitignore 防密钥泄露（但仍是明文，权衡见第四节）
- `.gitignore:1` 已把 `config/settings.yaml` 排除，避免 secret/飞书 webhook/LLM api_key 被误提交。
- 注意：这只是"防 git 推送"，文件仍是**明文落盘在服务器磁盘**。单用户本地项目的可接受取舍（见 4.1），但若未来多人/多机，必须改为环境变量或密钥管理。

---

## 四、其他经验（配置 / 安全 / 解耦）

### 4.1 明文配置权衡（用户裁定，非疏漏）
- 用户明确：单用户本地项目，**全明文 `config/settings.yaml`，不做环境变量隔离**。接受的风险是"磁盘明文"，换来的好处是部署简单、排障直观。
- **不要**自作主张加 `.env` / 密钥管理服务 / 多用户鉴权——这属于"为不存在的需求做工程"，正是要避免的无效二次开发。

### 4.2 飞书 code==0 纪律（HTTP 200 ≠ 成功）
- `journal/feishu.py:13-33`：`send_feishu` 先校验 `status_code==200`，**再**解析 JSON 校验业务码 `code == 0`。飞书返回 `code!=0`（如 webhook 过期、频率限制）时 HTTP 仍是 200，若只判状态码会误以为"推送成功"。
- **红线**：凡是调用外部 HTTP 服务（飞书/LLM/行情），**必须校验业务码/响应体语义**，不能只 `resp.status_code == 200`。

### 4.3 TRAE 薄转发层解耦（Agent 不 import TRAE）
- Agent 服务**不 import TRAE 任何内部代码**。TRAE 侧在 `record_signal` 后，携带共享 secret 头（`X-Agent-Secret`）POST 信号 JSON 到 `/webhook/signal`（`main.py:75-85`）。
- 好处：两侧独立演进、独立部署、独立测试，互不改代码。secret 用 `hmac.compare_digest` 恒定时间比较（`main.py:36`），避免时序侧信道。

### 4.4 时区与时段准入
- 全局用 `risk/engine.py:BEIJING`（`Asia/Shanghai`）时区对象，不用本地 naive datetime。
- 工作日判定用 Python `weekday()`（周一=0…周五=4，`settings.yaml:31`），与 cron 的周字段对齐。
- `should_close_now` 在收盘前 `no_overnight_warn_minutes`（默认 60 分钟，即 21:00）发"不持仓过夜"指令。

---

## 五、避坑清单（无效二次开发红线 · 复制备用）

> 任何改动前过一遍这条清单。命中任一条而没理由，就是"无效二次开发"，应停下与用户确认。

1. **改动前先读历史**：动 `TRADING_AGENT` 任何文件前，先读 `config/settings.yaml` 与本文档，确认已落地的铁律与已踩的坑，不重复造轮子。
2. **不突破三条铁律**（§一）：不加自动下单、不加隔夜持仓建议、不删每日推送。
3. **不加不存在的工程**：单用户项目不加多用户/权限/密钥管理服务/环境变量隔离（除非用户明确要求）。
4. **新增定时任务用 cron**，不往进程里塞 scheduler（§3.1）。
5. **凡外部 HTTP 调用校验业务码**，不只看状态码（§4.2）。
6. **凡 SQLite 跨线程，统一 `_read/_write` 加锁**，禁止直连 `conn`（§2.4/3.5）。
7. **凡"统计/反馈/学习"代码，必须有真实调用路径 + 测试证明其改了状态**，否则是死代码（§2.2）。
8. **凡标"完成"先独立复测**边界与并发，不轻信实现方自测（§2.4）。
9. **部署/重启后必查进程命令行**含匹配 pattern（绝对路径启动，§3.2）；必查 `proxy: ""`（§3.3）；必查 secret 非占位（§3.6）。
10. **测试必须 hermetic**：注入 `llm_client=None`、mock 外部 HTTP、用临时 db，无网络也能全绿（§2.3）。
11. **配置化护栏**：阈值/开关改 YAML 不动代码（§2.5）。
12. **不为了"优雅"重写已验证模块**：`-1` 重写的代价 >> 局部修补，先问"改这个能解决什么真实问题"。

---

## 六、一键部署核对表（服务器落地用）

```
[ ] pip install -r requirements.txt          # fastapi/uvicorn/pyyaml/numpy/requests
[ ] 确认 config/settings.yaml:
      proxy: ""                               # 零代理（§3.3）
      agent_webhook.secret: <强随机非占位>     # §3.6
      PHASE = "paper"（main.py:22）           # §3.4
[ ] crontab:
      06:30  restart_trade.sh 启动
      22:00  python main.py report 推交割单
      23:20  restart_trade.sh 关闭
[ ] 启动后: ps aux | grep TRADING_AGENT       # 命令行须含 TRADING_AGENT/main.py（§3.2）
[ ] curl 127.0.0.1:8000/health               # {"status":"ok","phase":"paper"}
[ ] 观察 agent_serve.log 无 InterfaceError / database is locked
```

---
*本文档基于 `TRADING_AGENT` 项目 2026-09-29 状态撰写，引用文件均经逐字核对（main.py / config/settings.yaml / memory/store.py / agent/orchestrator.py / journal/feishu.py / deliverables/deploy/restart_trade.sh / requirements.txt）。后续若有架构变更，请同步更新本文件对应章节。*
