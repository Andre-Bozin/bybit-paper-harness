"""
test_agent.py — unit-тесты для Agent.
Использует синтетический MarketState, реальный PaperSession.
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.market import MarketState
from src.session import PaperSession
from src.agent import Agent

PASSED = 0
FAILED = 0

DOGE_INSTRUMENT = {
    "symbol": "DOGEUSDT",
    "tick_size": 0.00001,
    "qty_step": 1.0,
    "min_qty": 1.0,
    "min_notional": 5.0,
    "maker_fee": 0.0002,
    "taker_fee": 0.00055,
}


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def make_agent(cfg_overrides=None, market_overrides=None,
               start_usdt=1000.0, obi=0.0, sigma_pct=0.1):
    """Создаёт agent с market в заданном состоянии."""
    cfg = {
        "name": "test_agent",
        "signal": "fade_obi",
        "symbol": "DOGEUSDT",
        "qty": 100,
        "obi_threshold": 0.65,
        "min_std_dev": 0.05,
        "tp_sl_ratio": 2.0,
        "decay_start": 180,
        "decay_duration": 600,
        "hard_kill": 900,
        "sl_cooldown": 300,
    }
    if cfg_overrides:
        cfg.update(cfg_overrides)

    market = MarketState("DOGEUSDT")
    market.current_obi = obi
    market.current_std_dev_pct = sigma_pct
    market.best_bid = 0.1
    market.best_ask = 0.10001
    market.bids = {0.1: 1000, 0.09999: 500}
    market.asks = {0.10001: 1000, 0.10002: 500}
    if market_overrides:
        for k, v in market_overrides.items():
            setattr(market, k, v)

    session = PaperSession("test_agent", DOGE_INSTRUMENT, start_usdt)
    agent = Agent(cfg, market, session)
    return agent, market, session


def test_entry_on_signal():
    print("\n=== Test: entry on fade_obi signal ===")
    agent, market, session = make_agent(obi=-0.7)  # signal: Buy
    now = time.time()
    agent.on_tick(now)

    check("active_oid set", agent.active_oid is not None)
    check("order in session", len(session.orders) == 1)
    order = list(session.orders.values())[0]
    check("side=Buy", order["side"] == "Buy")
    check("price=best_bid", order["price"] == 0.1)


def test_entry_fill_and_tp_sl():
    print("\n=== Test: entry fill → TP and SL set ===")
    agent, market, session = make_agent(obi=-0.7)
    now = time.time()
    agent.on_tick(now)
    check("order placed", agent.active_oid is not None)

    # Trigger fill: bid drops below our 0.1 → level eaten
    market.best_bid = 0.09999
    market.best_ask = 0.1
    now += 0.1
    agent.on_tick(now)

    check("position opened", agent.position_open is True)
    check("side=Buy", agent.position_side == "Buy")
    check("entry_price=0.1", abs(agent.entry_price - 0.1) < 1e-9)
    check("sl_price < entry", agent.sl_price < agent.entry_price,
          f"sl={agent.sl_price}")
    check("current_tp > entry", agent.current_tp > agent.entry_price,
          f"tp={agent.current_tp}")
    # tp_sl_ratio=2.0: sl at entry - 2*margin, tp at entry + margin
    margin = agent.entry_tp_margin
    check("sl = entry - 2*margin", abs((agent.entry_price - agent.sl_price) - 2 * margin) < 1e-9)
    check("tp = entry + margin", abs((agent.current_tp - agent.entry_price) - margin) < 1e-9)
    check("tp_oid set", agent.tp_oid is not None)


def test_stop_loss_fires():
    print("\n=== Test: stop-loss fires ===")
    agent, market, session = make_agent(obi=-0.7)
    now = time.time()
    agent.on_tick(now)
    # Fill entry
    market.best_bid = 0.09999
    market.best_ask = 0.1
    now += 0.1
    agent.on_tick(now)
    check("position opened", agent.position_open)

    # Crash below SL
    market.best_bid = agent.sl_price - 0.001
    market.best_ask = agent.sl_price - 0.0009
    now += 1
    agent.on_tick(now)

    check("position closed", not agent.position_open)
    check("sl_cooldown active", now < agent.sl_until)


def test_sl_cooldown_blocks_entry():
    print("\n=== Test: SL cooldown blocks new entry ===")
    agent, market, session = make_agent(obi=-0.7)
    now = time.time()
    agent.sl_until = now + 300
    agent.on_tick(now)
    check("no order placed during cooldown", agent.active_oid is None)


def test_hard_kill():
    print("\n=== Test: HARD_KILL after 900s ===")
    agent, market, session = make_agent(obi=-0.7)
    now = time.time()
    agent.on_tick(now)
    market.best_bid = 0.09999
    market.best_ask = 0.1
    now += 0.1
    agent.on_tick(now)
    check("position opened", agent.position_open)

    # Advance 901 seconds
    now += 901
    agent.on_tick(now)
    check("position closed by hard_kill", not agent.position_open)


def test_decay_exit():
    print("\n=== Test: decay exit when margin < 1 ===")
    agent, market, session = make_agent(obi=-0.7)
    now = time.time()
    agent.on_tick(now)
    market.best_bid = 0.09999
    market.best_ask = 0.1
    now += 0.1
    agent.on_tick(now)

    # decay_start=180, decay_duration=600 → margin=0 at t=780
    now += 800
    agent.on_tick(now)
    check("position closed by decay", not agent.position_open)


def test_decay_tightens_tp():
    print("\n=== Test: decay tightens TP ===")
    agent, market, session = make_agent(obi=-0.7, cfg_overrides={"decay_start": 10, "decay_duration": 600})
    now = time.time()
    agent.on_tick(now)
    market.best_bid = 0.09999
    market.best_ask = 0.1
    now += 0.1
    agent.on_tick(now)
    tp_initial = agent.current_tp

    # Advance to 50% decay
    now += 310  # elapsed ≈ 310, ratio ≈ 1 - (310-10)/600 = 0.5
    agent.on_tick(now)
    tp_after = agent.current_tp
    check("TP tightened", tp_after < tp_initial, f"before={tp_initial}, after={tp_after}")


def test_cancel_on_signal_flip():
    print("\n=== Test: cancel order when signal flips ===")
    agent, market, session = make_agent(obi=-0.7)  # Buy signal
    now = time.time()
    agent.on_tick(now)
    check("order placed", agent.active_oid is not None)

    # OBI flips to +0.7 (Sell signal)
    market.current_obi = 0.7
    now += 0.1
    agent.on_tick(now)
    check("Buy order cancelled, Sell order placed",
          agent.active_oid is not None and agent._last_placed_side == "Sell")


def test_daily_block():
    print("\n=== Test: daily_block stops entries ===")
    agent, market, session = make_agent(obi=-0.7)
    agent.daily_block = True
    now = time.time()
    agent.on_tick(now)
    check("no order when daily_block", agent.active_oid is None)


def test_min_sigma_gate():
    print("\n=== Test: min_sigma gate blocks entries ===")
    agent, market, session = make_agent(obi=-0.7, sigma_pct=0.01)
    now = time.time()
    agent.on_tick(now)
    check("no order when sigma below threshold", agent.active_oid is None)


def test_no_signal_no_order():
    print("\n=== Test: no signal → no order ===")
    agent, market, session = make_agent(obi=0.1)  # neutral OBI
    now = time.time()
    agent.on_tick(now)
    check("no order when OBI neutral", agent.active_oid is None)


def test_snapshot():
    print("\n=== Test: agent snapshot ===")
    agent, market, session = make_agent(obi=-0.7)
    snap = agent.snapshot()
    check("snapshot has name", snap["name"] == "test_agent")
    check("snapshot has strategy", snap["strategy"] == "fade_obi")
    check("snapshot usdt", snap["usdt"] == 1000.0)
    check("snapshot position flat", snap["position_side"] == "flat")


def test_entry_uses_place_time_signal():
    """
    Regression test: ENTRY должен логировать sigma_pct/obi
    на момент PLACE, а не на момент fill.
    """
    print("\n=== Test: ENTRY uses PLACE-time signal (regression) ===")
    agent, market, session = make_agent(obi=-0.7, sigma_pct=0.10)
    now = time.time()
    agent.on_tick(now)

    # Проверяем что значения сохранены
    check("place_sigma saved", abs(agent._place_sigma_pct - 0.10) < 1e-9,
          f"got {agent._place_sigma_pct}")
    check("place_obi saved", abs(agent._place_obi - (-0.7)) < 1e-9,
          f"got {agent._place_obi}")

    # Меняем рынок: OBI и σ% теперь совсем другие
    market.current_obi = +0.05
    market.current_std_dev_pct = 0.01

    # Активируем fill — bid drops below our order
    market.best_bid = 0.09999
    market.best_ask = 0.1
    now += 0.1
    agent.on_tick(now)

    check("position opened", agent.position_open is True)
    # Значения в agent должны остаться старыми (place-time)
    check("place_sigma unchanged after fill",
          abs(agent._place_sigma_pct - 0.10) < 1e-9,
          f"got {agent._place_sigma_pct}")
    check("place_obi unchanged after fill",
          abs(agent._place_obi - (-0.7)) < 1e-9,
          f"got {agent._place_obi}")


def test_place_signal_reset_after_close():
    """После закрытия позиции значения PLACE сбрасываются."""
    print("\n=== Test: PLACE signal reset after close ===")
    agent, market, session = make_agent(obi=-0.7, sigma_pct=0.10)
    now = time.time()
    agent.on_tick(now)
    market.best_bid = 0.09999
    market.best_ask = 0.1
    now += 0.1
    agent.on_tick(now)
    check("position opened", agent.position_open)

    # Trigger SL
    market.best_bid = agent.sl_price - 0.001
    market.best_ask = agent.sl_price - 0.0009
    now += 1
    agent.on_tick(now)

    check("position closed", not agent.position_open)
    check("place_sigma reset", agent._place_sigma_pct == 0.0)
    check("place_obi reset", agent._place_obi == 0.0)


if __name__ == "__main__":
    print("=" * 60)
    print("Agent unit tests")
    print("=" * 60)
    test_entry_on_signal()
    test_entry_fill_and_tp_sl()
    test_stop_loss_fires()
    test_sl_cooldown_blocks_entry()
    test_hard_kill()
    test_decay_exit()
    test_decay_tightens_tp()
    test_cancel_on_signal_flip()
    test_daily_block()
    test_min_sigma_gate()
    test_no_signal_no_order()
    test_snapshot()
    test_entry_uses_place_time_signal()
    test_place_signal_reset_after_close()

    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    print("=" * 60)
    sys.exit(0 if FAILED == 0 else 1)
