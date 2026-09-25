"""
test_session.py — unit-тесты для PaperSession.
Запуск: python3 tests/test_session.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.session import PaperSession


# DOGEUSDT metadata (из реального API)
DOGE = {
    "symbol": "DOGEUSDT",
    "tick_size": 0.00001,
    "qty_step": 1.0,
    "min_qty": 1.0,
    "min_notional": 5.0,
    "maker_fee": 0.0002,
    "taker_fee": 0.00055,
}

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


def test_validation():
    print("\n=== Test: validation ===")
    s = PaperSession("test_val", DOGE, 1000.0)

    # min qty
    try:
        s.place_limit("Buy", 0.099, 0.5)
        check("rejects qty < min_qty", False)
    except ValueError as e:
        check("rejects qty < min_qty", "min_qty" in str(e))

    # min notional
    try:
        s.place_limit("Buy", 0.001, 1.0)
        check("rejects notional < min_notional", False)
    except ValueError as e:
        check("rejects notional < min_notional", "min_notional" in str(e))

    # tick rounding
    oid = s.place_limit("Buy", 0.099123456, 100)
    price = s.orders[oid]["price"]
    check("tick rounding: 0.099123456 → 0.09912", abs(price - 0.09912) < 1e-9,
          f"got {price}")

    # qty rounding
    oid2 = s.place_limit("Buy", 0.10, 100.7)
    qty = s.orders[oid2]["qty"]
    check("qty rounding: 100.7 → 101", qty == 101.0, f"got {qty}")


def test_fill_on_touch():
    print("\n=== Test: fill on touch ===")
    s = PaperSession("test_touch", DOGE, 1000.0)
    s.update_market(0.10000, 0.10001)
    oid = s.place_limit("Buy", 0.10000, 100)  # buy at best_bid

    # Ask drops to 0.10000 → touch
    s.update_market(0.09999, 0.10000)
    check("order filled", oid not in s.orders)
    check("position opened Buy 100", s.position["size"] == 100.0 and s.position["side"] == "Buy")
    check("avgPrice = 0.10000", abs(s.position["avgPrice"] - 0.10000) < 1e-9)

    # fee = 100 * 0.10000 * 0.0002 = 0.002
    expected_fee = 100 * 0.10000 * 0.0002
    actual_usdt = s.usdt
    check("maker fee deducted", abs((1000.0 - expected_fee) - actual_usdt) < 1e-9,
          f"expected {1000.0 - expected_fee}, got {actual_usdt}")


def test_fill_level_eaten():
    print("\n=== Test: fill on level eaten ===")
    s = PaperSession("test_eaten", DOGE, 1000.0)
    s.update_market(0.10000, 0.10001)
    oid = s.place_limit("Buy", 0.10000, 100)  # at best_bid

    # Market moves down: best_bid becomes 0.09999 → our bid was eaten
    s.update_market(0.09999, 0.10000)
    check("order filled via level-eaten", oid not in s.orders)
    check("position opened", s.position["size"] == 100.0)


def test_close_pnl_win():
    print("\n=== Test: close with profit (Buy then Sell higher) ===")
    s = PaperSession("test_win", DOGE, 1000.0)
    s.update_market(0.10000, 0.10001)
    s.place_limit("Buy", 0.10000, 100)
    s.update_market(0.09999, 0.10000)  # bid drops → level-eaten fill Buy @ 0.1

    # Now Buy position open @ 0.1, usdt = 999.998
    # Place Sell higher
    s.update_market(0.10499, 0.10500)
    s.place_limit("Sell", 0.10500, 100)
    s.update_market(0.10500, 0.10501)  # bid crosses → Sell fill @ 0.105

    check("position closed", s.position["size"] == 0.0)
    # Buy fee = 0.002, Sell fee = 100 * 0.105 * 0.0002 = 0.0021
    # PnL = (0.105 - 0.1) * 100 = 0.5
    # Final = 1000 - 0.002 + 0.5 - 0.0021 = 1000.4959
    expected = 1000.4959
    check("PnL correct", abs(s.usdt - expected) < 1e-6,
          f"expected {expected}, got {s.usdt}")


def test_close_pnl_loss():
    print("\n=== Test: close with loss (Buy then Sell lower) ===")
    s = PaperSession("test_loss", DOGE, 1000.0)
    s.update_market(0.10000, 0.10001)
    s.place_limit("Buy", 0.10000, 100)
    s.update_market(0.09999, 0.10000)  # Buy fill @ 0.1

    # Sell at 0.09500 (below market)
    s.update_market(0.09499, 0.09500)
    s.place_limit("Sell", 0.09500, 100)
    s.update_market(0.09500, 0.09501)  # Sell fill @ 0.095

    check("position closed", s.position["size"] == 0.0)
    # PnL = (0.095 - 0.1) * 100 = -0.5
    # Buy fee = 0.002, Sell fee = 100 * 0.095 * 0.0002 = 0.0019
    # Final = 1000 - 0.002 - 0.5 - 0.0019 = 999.4961
    expected = 999.4961
    check("PnL correct", abs(s.usdt - expected) < 1e-6,
          f"expected {expected}, got {s.usdt}")


def test_market_close():
    print("\n=== Test: market close with taker fee ===")
    s = PaperSession("test_market", DOGE, 1000.0)
    s.update_market(0.10000, 0.10001)
    s.place_limit("Buy", 0.10000, 100)
    s.update_market(0.09999, 0.10000)  # Buy fill @ 0.1
    s.update_market(0.10000, 0.10001)  # restore bid=0.1 for market close
    # Position: Buy 100 @ 0.1, usdt = 1000 - 0.002 = 999.998

    # Market sell to close @ best_bid = 0.1
    s.place_market("Sell", 100, reduce_only=True)
    check("position closed", s.position["size"] == 0.0)
    # Taker fee = 100 * 0.1 * 0.00055 = 0.0055
    # PnL = 0 → usdt = 999.998 - 0.0055 = 999.9925
    expected = 999.9925
    check("taker fee applied", abs(s.usdt - expected) < 1e-6,
          f"expected {expected}, got {s.usdt}")


def test_snapshot():
    print("\n=== Test: snapshot ===")
    s = PaperSession("test_snap", DOGE, 500.0)
    s.update_market(0.10000, 0.10001)
    s.place_limit("Buy", 0.10000, 100)
    snap = s.snapshot()
    check("snapshot has usdt", "usdt" in snap and snap["usdt"] == 500.0)
    check("snapshot has open_orders", snap["open_orders"] == 1)
    check("snapshot has position_size 0", snap["position_size"] == 0.0)


if __name__ == "__main__":
    print("=" * 60)
    print("PaperSession unit tests")
    print("=" * 60)
    test_validation()
    test_fill_on_touch()
    test_fill_level_eaten()
    test_close_pnl_win()
    test_close_pnl_loss()
    test_market_close()
    test_snapshot()

    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    print("=" * 60)
    sys.exit(0 if FAILED == 0 else 1)
