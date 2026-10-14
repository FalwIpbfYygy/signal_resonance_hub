# signal-resonance-hub

一套**多源、规则化的交易信号聚合与共振提醒系统**。

系统将多个互相独立的子系统（K 线形态、ICT 市场结构、均线、谐波、Pinbar、分型、OOPS 跳空、Heikin-Ashi）产生的信号，统一归一化到一个共享存储层作为唯一事实源，再经过**跨周期共振**层筛选，最后把结构化提醒推送到聊天机器人和邮件。

全部基于纯 `if-then-else` 规则——**严格无未来函数**（信号只依赖触发时刻已收盘的当根 K 线），完全可解释、可自检。所有配置与密钥均外置为环境变量。

> ⚠️ 仅用于学习 / 研究，不构成任何投资建议。本仓库**不包含带成本的可交易回测**，也**不声称任何实盘收益**。

---

## 功能特性

- **规则化、因果严谨**：每个信号只读取信号触发时刻已收盘的数据。
- **统一信号存储层**（`signal_store.py`）：归一化、去重后的唯一事实源，线程锁 + 跨进程文件锁保证并发安全。
- **跨周期共振筛选**（`resonance.py`）：合并多源信号，按周期 / 状态达成一致后才放行提醒。
- **零侵入扩展**：共振 / 转发 / 日报层不修改任何子系统的源码——新增策略直接接入即可。
- **多渠道推送**（聊天机器人 + 邮件）且**送达一致性校验**（HTTP 200 不等于成功，必须校验返回 `code == 0`）。
- **配置全外置**：源码中无任何明文密钥 / webhook / 代理 / 接口地址。
- **可自检**：`verify_*.py` 一致性脚本 + 可复用回测框架。

---

## 架构

```
 策略子系统（core_code/、high_winss/、ICT/、Al_Brookss/、
   new_price/、hammer_pattern/ ……）
        │  产生原始信号
        ▼
 signal_store.py          ── 归一化、去重后的唯一事实源
        │
        ▼
 resonance.py             ── 跨周期共振筛选
        │
        ├────────► agent_forwarder.py   ── 转发给决策 Agent（可选）
        └────────► report.py            ── 每日日报
        │
        ▼
  聊天机器人（飞书）  +  邮件（SMTP）   ── 推送提醒
```

`master.py` 启动器把 `core_code/` 下的实时策略以受监管线程方式在单个进程内运行。

---

## 目录结构

| 路径 | 作用 |
|------|------|
| `core_code/` | 实时策略脚本（Pinbar、谐波、TD 九转、分型、孕线、OOPS、EMA-HA、WM、setup）——由 `master.py` 启动 |
| `signal_store.py` | 归一化信号存储层（唯一事实源） |
| `resonance.py` | 跨周期共振筛选 |
| `agent_forwarder.py` | 转发到决策 Agent 的薄层 |
| `report.py` | 每日日报生成 |
| `high_winss/` | 19 个 K 线形态检测器 |
| `ICT/` | ICT 多周期市场结构信号 |
| `Al_Brookss/` | Brooks 价格行为信号 |
| `new_price/` | 全流水线 + 市场状态路由 |
| `hammer_pattern/` | 锤子 / 针形形态检测 + 回测 |
| `TRADING_AGENT/` | 可选的 LLM 决策 Agent |
| `PRD_信号共振提醒.md` | 原始中文设计文档（PRD） |

---

## 技术栈

- **语言**：Python 3.10+
- **数据处理**：NumPy、Pandas
- **网络**：`requests`
- **配置**：`os.environ` + PyYAML（密钥全部外置）
- **行情数据**：交易所 K 线 REST 接口（通过环境变量注入）
- **LLM（可选）**：OpenAI 兼容端点（阿里云百炼工作空间）
- **推送**：聊天机器人 webhook + SMTP 邮件
- **运维**：`cron` + `nohup` + Python `venv`
- **版本控制**：Git，密钥通过 `.gitignore` 隔离

---

## 快速开始

```bash
git clone https://github.com/FalwIpbfYygy/signal_resonance_hub.git
cd signal_resonance_hub

python -m venv venv
source venv/bin/activate          # Windows：venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env              # 然后填入你自己的 webhook / 密钥
```

一键启动全部实时策略：

```bash
python core_code/master.py
```

或单独运行某一层：

```bash
python resonance.py               # 共振筛选
python report.py                  # 每日日报
```

---

## 配置说明

所有密钥都在 `.env` 中（已被 git 忽略）。复制 `.env.example` 并填入你自己的值。
推送类变量留空则跳过该渠道；代理类变量留空表示**直连（零代理）**。

| 变量 | 用途 | 是否必填 |
|------|------|----------|
| `FEISHU_WEBHOOK` / `RESONANCE_WEBHOOK` / `REPORT_WEBHOOK` | 聊天机器人推送地址 | 启用推送时必填 |
| `AGENT_SECRET` / `AGENT_BASE_URL` | 转发层共享 secret + Agent 地址 | 可选 |
| `QQ_EMAIL_SENDER` / `QQ_EMAIL_RECEIVER` / `QQ_EMAIL_AUTH_CODE` | 邮件推送（SMTP） | 可选 |
| `BACKTEST_PROXY` / `HAMMER_PROXY` | 本地开发代理（留空 = 直连） | 可选 |
| `BINANCE_FUTURES_KLINE_URL` / `BINANCE_SPOT_KLINE_URL` | 交易所 K 线接口 | 拉数据时必填 |
| `LLM_API_KEY` / `LLM_WORKSPACE_ID` / `LLM_BASE_URL` | LLM 决策端点 | 启用 AI 模式时必填 |

`TRADING_AGENT` 额外读取 `config/settings.yaml`——复制 `config/settings.example.yaml`
为 `config/settings.yaml`，并用上述同名环境变量覆盖。

> 🔒 **切勿提交 `.env` 或 `settings.yaml`**，二者已在 `.gitignore` 中。

---

## 部署（服务器端）

项目设计为在运行 Ubuntu 的云服务器上常驻运行：

1. **隔离环境**：创建 `venv`，`pip install -r requirements.txt`。
2. **常驻进程**：用 `nohup` 启动策略（仓库内置的
   `TRADING_AGENT/deliverables/deploy/restart_trade.sh` 支持一键启停）。
3. **定时日报**：用 `cron`（例如每日 22:00）触发 `report.py`。
4. **服务器端零代理**：`BACKTEST_PROXY` / `HAMMER_PROXY` 留空，进程直连
   （`trust_env=False`）。

示例 cron 条目（路径按你的实际布局调整）：

```cron
0 22 * * * /home/ubuntu/venv/bin/python /home/ubuntu/signal_resonance_hub/report.py >> /home/ubuntu/cron.log 2>&1
```

---

## 工程原则

- **因果优先**：信号只能读取其所在 K 线已收盘的 OHLC。
- **配置外置**：源码中零明文密钥 / 接口。
- **零侵入**：新子系统接入时无需改动分发层代码。
- **可自检**：`verify_*.py` 脚本守护流水线不变量。
- **送达一致性**：仅当渠道返回 `code == 0` 才视为推送成功。

---

## 项目状态

作为个人信号中台 / 量化策略骨架持续使用。架构稳定，各策略逻辑按 PRD 演进。

## 许可证

基于 [MIT 许可证](LICENSE) 发布。
