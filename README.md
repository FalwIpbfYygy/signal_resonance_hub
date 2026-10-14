# signal-resonance-hub

A multi-source, rule-based trading-signal aggregation and resonance-alert system.

It normalizes signals from several independent subsystems (candlestick patterns, ICT
market structure, moving averages, harmonics, Pinbar, fractals, OOPS gaps, Heikin-Ashi)
into a shared store as the single source of truth, filters them through a cross-timeframe
**resonance** layer, then pushes structured alerts to chat-bot and email channels.

Everything is built on pure `if-then-else` rules — **strictly no look-ahead bias**
(signals depend only on the *closed* bar at the signal moment), fully explainable and
self-testable. All configuration and secrets are externalized to environment variables.

> ⚠️ Educational / research only. Not investment advice. This repository ships **no
> live-trading backtest with costs**, and makes **no claim about real-world profitability**.

---

## Features

- **Rule-based, causal**: every signal uses only data available at the signal bar's close.
- **Unified signal store** (`signal_store.py`): a single normalized fact source with
  thread-lock + cross-process file-lock concurrency safety.
- **Cross-timeframe resonance** (`resonance.py`): merges multi-source signals and filters
  by regime / timeframe agreement before any alert fires.
- **Zero-intrusion extension**: the resonance / forwarder / report layers never modify the
  source of any subsystem — drop a new strategy in and it just plugs in.
- **Multi-channel push** (chat-bot + email) with **delivery-consistency checks**
  (HTTP 200 is *not* treated as success; payload `code == 0` is required).
- **Externalized config**: no hardcoded secrets / webhooks / proxies / endpoints in source.
- **Self-testing**: `verify_*.py` sanity scripts and a reusable backtest framework.

---

## Architecture

```
 strategy subsystems (core_code/, high_winss/, ICT/, Al_Brookss/,
   new_price/, hammer_pattern/ ...)
        │  emit raw signals
        ▼
 signal_store.py          ── normalized, de-duplicated single source of truth
        │
        ▼
 resonance.py             ── cross-timeframe resonance filtering
        │
        ├────────► agent_forwarder.py   ── forward to decision agent (optional)
        └────────► report.py            ── daily digest
        │
        ▼
  chat-bot (Feishu)  +  email (SMTP)   ── push alerts
```

A `master.py` launcher runs the realtime `core_code/` strategies as supervised threads
in one process.

---

## Repository layout

| Path | Role |
|------|------|
| `core_code/` | Realtime strategy scripts (Pinbar, harmonics, TD-nine-turn, fractals, harami, OOPS, EMA-HA, WM, setup) — launched by `master.py` |
| `signal_store.py` | Normalized signal storage (single source of truth) |
| `resonance.py` | Cross-timeframe resonance filter |
| `agent_forwarder.py` | Thin forwarder to the decision agent |
| `report.py` | Daily digest generator |
| `high_winss/` | 19 candlestick-pattern detectors |
| `ICT/` | ICT multi-timeframe market-structure signals |
| `Al_Brookss/` | Brooks price-action signals |
| `new_price/` | Full pipeline + market-state router |
| `hammer_pattern/` | Hammer / pin-bar detection + backtest |
| `TRADING_AGENT/` | Optional LLM decision agent |
| `PRD_信号共振提醒.md` | Original Chinese design doc (PRD) |

---

## Tech stack

- **Language**: Python 3.10+
- **Data**: NumPy, Pandas
- **Network**: `requests`
- **Config**: `os.environ` + PyYAML (all secrets externalized)
- **Market data**: exchange K-line REST endpoints (injected via env)
- **LLM (optional)**: OpenAI-compatible endpoint (Aliyun BaiLian workspace)
- **Push**: chat-bot webhook + SMTP email
- **Ops**: `cron` + `nohup` + Python `venv`
- **VCS**: Git, secrets isolated via `.gitignore`

---

## Quick start

```bash
git clone https://github.com/FalwIpbfYygy/signal_resonance_hub.git
cd signal_resonance_hub

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env              # then fill in YOUR webhook / keys
```

Run all realtime strategies in one process:

```bash
python core_code/master.py
```

Or run a layer on its own:

```bash
python resonance.py               # resonance filter
python report.py                  # daily digest
```

---

## Configuration

All secrets live in `.env` (git-ignored). Copy `.env.example` and fill in your own values.
Leaving a channel blank skips that channel; leaving a proxy blank means **direct connect
(zero proxy)**.

| Variable | Purpose | Required |
|----------|---------|----------|
| `FEISHU_WEBHOOK` / `RESONANCE_WEBHOOK` / `REPORT_WEBHOOK` | Chat-bot push targets | for push |
| `AGENT_SECRET` / `AGENT_BASE_URL` | Forwarder shared secret + agent URL | optional |
| `QQ_EMAIL_SENDER` / `QQ_EMAIL_RECEIVER` / `QQ_EMAIL_AUTH_CODE` | Email push (SMTP) | optional |
| `BACKTEST_PROXY` / `HAMMER_PROXY` | Local dev proxy (blank = direct) | optional |
| `BINANCE_FUTURES_KLINE_URL` / `BINANCE_SPOT_KLINE_URL` | Exchange K-line endpoints | for data |
| `LLM_API_KEY` / `LLM_WORKSPACE_ID` / `LLM_BASE_URL` | LLM decision endpoint | for AI mode |

`TRADING_AGENT` additionally reads `config/settings.yaml` — copy
`config/settings.example.yaml` to `config/settings.yaml` and override via the same env vars.

> 🔒 **Never commit `.env` or `settings.yaml`.** They are already in `.gitignore`.

---

## Deployment (server)

The project is designed to run persistently on a cloud VPS running Ubuntu:

1. **Isolated env**: create a `venv`, `pip install -r requirements.txt`.
2. **Persistent process**: launch strategies with `nohup` (the bundled
   `TRADING_AGENT/deliverables/deploy/restart_trade.sh` does one-command start/stop).
3. **Scheduled digest**: a `cron` job (e.g. daily 22:00) triggers `report.py`.
4. **Zero proxy on server**: leave `BACKTEST_PROXY` / `HAMMER_PROXY` blank so the process
   connects directly (`trust_env=False`).

Example cron entry (adjust paths to your layout):

```cron
0 22 * * * /home/ubuntu/venv/bin/python /home/ubuntu/signal_resonance_hub/report.py >> /home/ubuntu/cron.log 2>&1
```

---

## Engineering principles

- **Causality first**: a signal may only read OHLC that is already closed at its bar.
- **Config externalized**: source contains zero plaintext secrets / endpoints.
- **Zero-intrusion**: new subsystems plug in without editing the distribution layers.
- **Self-verifying**: `verify_*.py` scripts guard pipeline invariants.
- **Delivery consistency**: a push is "delivered" only when the channel returns `code == 0`.

---

## Status

Actively used as a personal signal middleware / quant-strategy skeleton. The architecture
is stable; individual strategy logic evolves per the PRD.

## License

Released under the [MIT License](LICENSE).
