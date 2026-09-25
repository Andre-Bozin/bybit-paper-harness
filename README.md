# Bybit Paper Harness

**Multi-agent paper trading infrastructure for Bybit.**

Test strategies against live order books — without risking capital.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## Why

Most retail algo strategies fail in production for one reason: **fees and execution kill the edge**.

Backtests look great with idealized fills. Real fills are worse, delays matter, and Taker fees eat everything. By the time you notice — the deposit is gone.

**Bybit Paper Harness** runs your strategies against **live Bybit WebSocket data** with realistic simulation:

- Order book cache (delta-stream, top-50 levels)
- Touch + level-eaten fill modeling
- Maker/Taker fee simulation (from your actual Bybit account)
- Multi-agent parallel testing (N strategies at once)
- No risk — nothing is sent to the exchange

---

## Features

| Feature | Status |
|---|---|
| Live order book (delta-stream) | ✅ |
| Realistic fill model (touch + level-eaten) | ✅ |
| Maker/Taker fees from Bybit API | ✅ |
| Multi-symbol support | ✅ |
| Multi-agent parallel execution | ✅ |
| Symbol-agnostic (works with any linear perp) | ✅ |
| Volatility-based signals (sigma%) | ✅ |
| Time decay, stop-loss, hard kill | ✅ |
| Unit tests (90 tests) | ✅ |
| Web UI | roadmap |
| Multi-exchange (CCXT) | roadmap |
| Deterministic replay | roadmap |
| Walk-forward testing | roadmap |

---

## Quick Start

### Requirements

- Python 3.10+
- Bybit API keys (Read + Trade, withdraw disabled)

### Installation

    git clone https://github.com/YOUR_USERNAME/bybit-paper-harness.git
    cd bybit-paper-harness
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

### Configuration

Copy the example and adjust:

    cp configs/example_doge.json configs/my_strategies.json

Minimal config:

    {
      "meta": {
        "symbol": "DOGEUSDT",
        "category": "linear",
        "start_balance": 1000.0
      },
      "agents": [
        {
          "name": "fade_obi",
          "signal": "fade_obi",
          "symbol": "DOGEUSDT",
          "qty": 100,
          "obi_threshold": 0.65,
          "min_std_dev": 0.05,
          "tp_sl_ratio": 2.0
        }
      ]
    }

### Run

    python3 -m src.harness --config configs/my_strategies.json

Logs go to `logs/`:
- `events.log` — human-readable event stream
- `trade_<agent>.log` — per-agent JSON-lines
- `agent_<agent>.log` — fill-level events

### Analyze

    python3 -m src.analyze

Outputs a comparison table of all agents: entries, closes, stop-losses, PnL.

---

## Architecture

    src/
      config.py        # paths, config loading
      metadata.py      # Bybit instrument specs + cache
      session.py       # PaperSession — simulated exchange
      market.py        # MarketState — order book, sigma%, OBI
      strategies.py    # Signal generation
      agent.py         # Agent — position lifecycle
      harness.py       # Orchestrator — WebSocket + N agents

**Design principles:**

- **Symbol-agnostic.** All signals work with dimensionless values (OBI, sigma%). Change `symbol` in JSON — done.
- **No absolute constants.** All thresholds scale as % of price. BTC, DOGE, XRP all work identically.
- **Metadata-driven.** tickSize, qtyStep, minNotional, fees — fetched from Bybit API, cached locally.
- **Testable.** 90 unit tests cover sessions, markets, strategies, agents.

---

## Built-in Strategies

| Strategy | Signal Logic |
|---|---|
| `fade_obi` | Sell when OBI > +threshold; Buy when OBI < -threshold |
| `direct_obi` | Buy when OBI > +threshold; Sell when OBI < -threshold |
| `momentum_10s` / `momentum_60s` | Buy on price rise over window |
| `meanrev_10s` / `meanrev_60s` | Fade the move over window |

All strategies respect `min_std_dev` (volatility gate) — no trades in dead markets.

---

## Configuration Reference

### meta

| Field | Default | Description |
|---|---|---|
| symbol | DOGEUSDT | Default symbol for agents |
| category | linear | Bybit category (linear, inverse, spot) |
| start_balance | 1000.0 | Virtual starting USDT |
| maker_fee | 0.0002 | Override. If unset — fetched from Bybit |
| taker_fee | 0.00055 | Same |

### agents[]

| Field | Default | Description |
|---|---|---|
| name | required | Unique agent name |
| signal | required | Strategy key |
| symbol | from meta | Trading symbol |
| qty | required | Order size (base units) |
| obi_threshold | 0.65 | For *_obi strategies |
| min_std_dev | 0.05 | Minimum sigma% (volatility gate) |
| tp_sl_ratio | 2.0 | Stop-loss distance as multiple of TP margin |
| decay_start | 180 | Seconds before time decay begins |
| decay_duration | 600 | Duration of decay until exit |
| hard_kill | 900 | Force-close position after N seconds |
| sl_cooldown | 300 | Pause after stop-loss hit |

---

## Roadmap

- [ ] Web UI (FastAPI + React) — configure and monitor agents in browser
- [ ] Deterministic replay — record live data, replay offline
- [ ] Walk-forward testing — validate on out-of-sample periods
- [ ] Multi-exchange (CCXT) — Binance, OKX, Bybit
- [ ] Partial fills modeling
- [ ] Slippage modeling for market orders
- [ ] Telegram notifications
- [ ] Docker image

---

## Safety

This tool **does not place real orders**. It connects only to:
- Public WebSocket (stream.bybit.com) — market data
- Public REST (api.bybit.com/v5/market/) — instrument metadata

API keys are **optional** — used only to fetch your account's actual fee tier. If omitted, defaults are used.

**Never commit `.env` or `config.py` with secrets.** `.gitignore` blocks them.

---

## Testing

    python3 tests/test_session.py
    python3 tests/test_market.py
    python3 tests/test_strategies.py
    python3 tests/test_agent.py

All tests use synthetic data — no network access, no real exchange.

---

## License

MIT — see LICENSE.

---

## Contributing

Issues and PRs welcome. For major changes, please open an issue first.
