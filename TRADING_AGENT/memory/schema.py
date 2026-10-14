"""SQLite 12 表 schema（MVP）。使用标准库 sqlite3，零额外 ORM 依赖。"""
from __future__ import annotations

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id TEXT UNIQUE,
  symbol TEXT,
  direction TEXT,
  setup_type TEXT,
  entry_zone TEXT,
  sl_zone TEXT,
  tp_zone TEXT,
  ts TEXT,
  source_subsystem TEXT,
  raw_score REAL,
  payload TEXT,
  received_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id TEXT,
  decision TEXT,
  direction TEXT,
  confidence REAL,
  market_regime TEXT,
  setup_type TEXT,
  stop_loss REAL,
  take_profit REAL,
  position_size_pct REAL,
  risk_score REAL,
  signal_quality REAL,
  reasons TEXT,
  risk_flags TEXT,
  memory_refs TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id TEXT,
  ts TEXT,
  regime TEXT,
  agent_decision TEXT,
  suggested_sl REAL,
  suggested_tp REAL,
  suggested_size_pct REAL,
  user_action TEXT,
  outcome TEXT,
  pnl REAL,
  note TEXT
);

CREATE TABLE IF NOT EXISTS memory_working (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_episodic (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id TEXT,
  ts TEXT,
  regime TEXT,
  agent_decision TEXT,
  actual_outcome TEXT,
  pnl REAL,
  reflection TEXT
);

CREATE TABLE IF NOT EXISTS memory_semantic (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT,
  source_episodes TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_strategy (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule TEXT,
  active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_mistake (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pattern TEXT,
  avoidance TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS regimes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  regime TEXT,
  confidence REAL,
  metrics TEXT
);

CREATE TABLE IF NOT EXISTS memory_regime (
  regime TEXT PRIMARY KEY,
  trade_count INTEGER DEFAULT 0,
  win_count INTEGER DEFAULT 0,
  total_pnl REAL DEFAULT 0.0,
  pf REAL DEFAULT 0.0,
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experiences (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE,
  description TEXT,
  stage TEXT DEFAULT 'Observed',
  n INTEGER DEFAULT 0,
  pf REAL DEFAULT 0.0,
  bootstrap_lower REAL DEFAULT 0.0,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kill_switch_state (
  day TEXT PRIMARY KEY,
  daily_pnl_pct REAL DEFAULT 0.0,
  consecutive_losses INTEGER DEFAULT 0,
  updated_at TEXT DEFAULT (datetime('now'))
);
"""


def init_db(conn) -> None:
    """在给定 sqlite3 连接上创建全部 12 张表（幂等）。"""
    conn.executescript(_SCHEMA)
    conn.commit()
