"""
test_market.py — unit-тесты для MarketState.
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.market import MarketState

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def test_snapshot_applied():
    print("\n=== Test: snapshot replaces book ===")
    m = MarketState("DOGEUSDT")
    m.apply_orderbook({
        "b": [["0.10000", "500"], ["0.09999", "300"]],
        "a": [["0.10001", "400"], ["0.10002", "200"]],
    }, is_snapshot=True)
    check("bids has 2 levels", len(m.bids) == 2)
    check("asks has 2 levels", len(m.asks) == 2)

    m.refresh_best()
    check("best_bid = 0.1", abs(m.best_bid - 0.1) < 1e-9, f"got {m.best_bid}")
    check("best_ask = 0.10001", abs(m.best_ask - 0.10001) < 1e-9)


def test_delta_updates():
    print("\n=== Test: delta updates book ===")
    m = MarketState("DOGEUSDT")
    m.apply_orderbook({
        "b": [["0.10000", "500"]],
        "a": [["0.10001", "400"]],
    }, is_snapshot=True)

    # Delta: update bid size, add new ask, remove old
    m.apply_orderbook({
        "b": [["0.10000", "300"]],   # size changed
        "a": [["0.10001", "0"], ["0.10002", "200"]],  # 0.10001 removed
    }, is_snapshot=False)

    check("bid 0.1 size updated", m.bids[0.10000] == 300.0)
    check("ask 0.10001 removed", 0.10001 not in m.asks)
    check("new ask added", m.asks[0.10002] == 200.0)

    m.refresh_best()
    check("best_ask moved to 0.10002", abs(m.best_ask - 0.10002) < 1e-9,
          f"got {m.best_ask}")


def test_obi_positive():
    print("\n=== Test: OBI positive (bid-heavy) ===")
    m = MarketState("DOGEUSDT")
    # Bids sum = 1000, Asks sum = 200
    m.apply_orderbook({
        "b": [["0.10000", "600"], ["0.09999", "400"]],
        "a": [["0.10001", "150"], ["0.10002", "50"]],
    }, is_snapshot=True)
    m.refresh_best()
    m.update_obi()
    # OBI = (1000 - 200) / 1200 = 0.6667
    check("OBI ≈ +0.667", abs(m.current_obi - 0.6667) < 0.01, f"got {m.current_obi}")


def test_obi_negative():
    print("\n=== Test: OBI negative (ask-heavy) ===")
    m = MarketState("DOGEUSDT")
    m.apply_orderbook({
        "b": [["0.10000", "100"]],
        "a": [["0.10001", "900"]],
    }, is_snapshot=True)
    m.refresh_best()
    m.update_obi()
    # OBI = (100 - 900) / 1000 = -0.8
    check("OBI ≈ -0.8", abs(m.current_obi - (-0.8)) < 0.01, f"got {m.current_obi}")


def test_obi_top10_only():
    print("\n=== Test: OBI only top-10 levels ===")
    m = MarketState("DOGEUSDT")
    # 11 bid levels, first 10 sum = 1000, 11th = 100000 (should be ignored)
    bids = [[f"0.1{i:04d}", "100"] for i in range(10)]  # 10 levels, 100 each
    bids.append(["0.05000", "100000"])  # 11th level
    m.apply_orderbook({"b": bids, "a": [["0.20000", "100"]]}, is_snapshot=True)
    m.refresh_best()
    m.update_obi()
    bid_vol = m._top_volume(m.bids, reverse=True)
    check("top-10 bid sum = 1000", bid_vol == 1000.0, f"got {bid_vol}")
    check("11th level ignored", 0.05000 not in sorted(m.bids.keys(), reverse=True)[:10])


def test_sigma_pct():
    print("\n=== Test: sigma percent calculation ===")
    m = MarketState("DOGEUSDT")
    m.apply_orderbook({
        "b": [["0.10000", "100"]],
        "a": [["0.10001", "100"]],
    }, is_snapshot=True)

    # Feed 60 samples with small variation
    base_ts = time.time() - 70
    for i in range(60):
        m.best_bid = 0.10000 + (i % 3) * 0.00001
        m.best_ask = 0.10001 + (i % 3) * 0.00001
        m.update_sigma(base_ts + i)

    check("sigma computed", m.current_std_dev > 0)
    check("sigma_pct > 0", m.current_std_dev_pct > 0)
    # mid ≈ 0.1, sigma ≈ 8e-6, sigma_pct ≈ 0.008%
    check("sigma_pct in range 0.005-0.02",
          0.005 < m.current_std_dev_pct < 0.02,
          f"got {m.current_std_dev_pct}")


def test_sigma_insufficient_data():
    print("\n=== Test: sigma with insufficient data ===")
    m = MarketState("DOGEUSDT")
    m.best_bid = 0.1
    m.best_ask = 0.10001
    for i in range(10):  # only 10 samples, < 50 required
        m.update_sigma(time.time())
    check("sigma stays 0 with <50 samples", m.current_std_dev == 0.0)


def test_trades_window():
    print("\n=== Test: trades trimmed by window ===")
    m = MarketState("DOGEUSDT")
    now = time.time()
    trades = [{"S": "Buy", "v": "100"}, {"S": "Sell", "v": "50"}]
    m.add_trades(trades, now - 5)  # old
    m.add_trades(trades, now)      # fresh

    check("has 4 trades before trim", len(m.trade_history) == 4)
    # TRADE_WINDOW = 10s, old at now-5 is still within
    # Add more trades to trigger trim
    m.add_trades([{"S": "Buy", "v": "1"}], now + 10)  # cutoff = now
    # old trades at now-5 should be gone
    remaining = len(m.trade_history)
    check("old trades trimmed", remaining <= 3, f"got {remaining}")


def test_tick_readiness():
    print("\n=== Test: tick returns False on empty book ===")
    m = MarketState("DOGEUSDT")
    check("tick fails on empty", m.tick(time.time()) is False)

    m.apply_orderbook({"b": [["0.1", "100"]], "a": [["0.10001", "100"]]}, True)
    check("tick succeeds with data", m.tick(time.time()) is True)


def test_sigma_resets_on_insufficient_samples():
    print("\n=== Test: sigma resets when samples < MIN ===")
    m = MarketState("DOGEUSDT")
    m.apply_orderbook({
        "b": [["0.1", "100"]],
        "a": [["0.10001", "100"]],
    }, is_snapshot=True)

    # Прогреваем до 60 samples → σ% должна стать > 0
    base = time.time() - 70
    for i in range(60):
        m.best_bid = 0.1 + (i % 3) * 0.00001
        m.best_ask = 0.10001 + (i % 3) * 0.00001
        m.update_sigma(base + i)
    check("sigma_pct > 0 initially", m.current_std_dev_pct > 0)

    # Теперь "сбой" — приходит лишь 5 samples. Все старые уйдут за 60с.
    later = base + 100
    for i in range(5):
        m.best_bid = 0.1
        m.best_ask = 0.10001
        m.update_sigma(later + i)

    # Старые 60 samples должны быть вытеснены окном 60 сек
    check("sigma_pct reset to 0", m.current_std_dev_pct == 0.0,
          f"got {m.current_std_dev_pct}")
    check("sigma reset to 0", m.current_std_dev == 0.0)


def test_snapshot_clears_history():
    print("\n=== Test: snapshot clears price/trade history ===")
    m = MarketState("DOGEUSDT")
    m.apply_orderbook({
        "b": [["0.1", "100"]],
        "a": [["0.10001", "100"]],
    }, is_snapshot=True)

    # Заполняем историю
    base = time.time() - 70
    for i in range(60):
        m.best_bid = 0.1 + (i % 3) * 0.00001
        m.best_ask = 0.10001 + (i % 3) * 0.00001
        m.update_sigma(base + i)
    m.add_trades([{"S": "Buy", "v": "100"}], time.time())

    check("history before snapshot not empty", len(m.price_history) > 0)
    check("trades before snapshot not empty", len(m.trade_history) > 0)
    check("sigma before snapshot > 0", m.current_std_dev_pct > 0)

    # Новый snapshot
    m.apply_orderbook({
        "b": [["0.2", "500"]],
        "a": [["0.20001", "500"]],
    }, is_snapshot=True)

    check("price_history cleared", len(m.price_history) == 0)
    check("trade_history cleared", len(m.trade_history) == 0)
    check("sigma_pct cleared", m.current_std_dev_pct == 0.0)
    check("std_dev cleared", m.current_std_dev == 0.0)
    check("bids replaced", 0.1 not in m.bids and 0.2 in m.bids)
if __name__ == "__main__":
    print("=" * 60)
    print("MarketState unit tests")
    print("=" * 60)
    test_snapshot_applied()
    test_delta_updates()
    test_obi_positive()
    test_obi_negative()
    test_obi_top10_only()
    test_sigma_pct()
    test_sigma_insufficient_data()
    test_trades_window()
    test_tick_readiness()
    test_sigma_resets_on_insufficient_samples()
    test_snapshot_clears_history()

    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    print("=" * 60)
    sys.exit(0 if FAILED == 0 else 1)


# ============================================================
# Regression tests for market.py patches (27.09)
# ============================================================

