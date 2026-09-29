# Bybit Paper Harness

**Multi-agent paper trading infrastructure for Bybit.**

Test strategies against live order books — without risking capital.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Tests](https://github.com/Andre-Bozin/bybit-paper-harness/actions/workflows/tests.yml/badge.svg)](https://github.com/Andre-Bozin/bybit-paper-harness/actions/workflows/tests.yml)
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
---

## Fees & Applicability

**Bybit retail fees are higher than most backtests assume.**

For a typical retail account (as of 2026):

| Tier | Maker | Taker |
|---|---|---|
| **Standard retail** | **0.036%** | **0.100%** |
| VIP0 | 0.020% | 0.055% |
| VIP1+ | 0.018% | 0.050% |

If you have `BYBIT_API_KEY` in `.env`, the harness fetches your actual fees per symbol via `/v5/account/fee-rate`. Otherwise it uses realistic defaults (0.00036 / 0.001).

### The applicability rule

A symbol is only tradeable when the natural movement over the strategy's hold window exceeds round-trip fees by a margin:

   3 x sigma_pct(60s)  >  fee_round_trip_pct  x  1.5

Otherwise fees dominate gross PnL — **no signal can overcome them**.

### Empirical results — two independent runs

We ran the same 12-agent configuration twice: first with assumed retail fees (0.02% / 0.055%), then with **real fees fetched from Bybit** (0.036% / 0.1%).

**Run 1 — assumed fees (0.020% / 0.055%), 19h:**

| Symbol | WR% | Decay exits | Net PnL | Fee/Gross |
|---|---|---|---|---|
| DOGEUSDT | 53-67% | 50% | -1.83 | 1.7x |
| BTCUSDT | 48-58% | 85% | -28.71 | 0.17x |
| ETHUSDT | 53-63% | 60% | -8.89 | 0.10x |
| **TOTAL** | | | **-39.43** | |

**Run 2 — real fees (0.036% / 0.100%), 17.7h:**

| Symbol | WR% | Decay exits | Net PnL | Fee/Gross |
|---|---|---|---|---|
| DOGEUSDT | 57-69% | 50% | -2.07 | 1.7x |
| BTCUSDT | 53-60% | 70% | **-42.78** | 0.36x |
| ETHUSDT | 53-63% | 60% | **-15.76** | 0.22x |
| **TOTAL** | | | **-60.61** | |

**Real fees worsened PnL by ~1.5x** (not 1.8x — because limit closes pay maker only).

**Per-symbol hourly bleed (Run 2):**
- BTCUSDT: **-0.55 USDT/hour**
- ETHUSDT: **-0.20 USDT/hour**
- DOGEUSDT: **-0.06 USDT/hour** (marginal)

**Two independent runs, 2522 trades, 0 invariant violations. Same conclusion: 0 winners out of 12 agents.**

### What this means for you

This harness is a **measurement tool**, not a profitable strategy. Its purpose is to show you the boundary where retail algos stop working — before real money does.

Use it to:
- Test whether your signal has an edge after fees on your symbol.
- Find symbols where 3 x sigma_pct > fee_rt x 1.5 holds.
- Quantify fee bleed before deploying capital.

Do **not** expect it to print money on BTCUSDT at 0.1% taker fees. It won't. That's the point.

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
