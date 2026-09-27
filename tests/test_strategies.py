"""
test_strategies.py — unit-тесты для strategies.
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.market import MarketState
from src import strategies

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


def make_market(obi: float, sigma_pct: float = 0.05,
                price_history: list = None) -> MarketState:
    """Быстрое создание market с заданными параметрами."""
    m = MarketState("DOGEUSDT")
    m.current_obi = obi
    m.current_std_dev_pct = sigma_pct
    if price_history:
        m.price_history.extend(price_history)
    return m


def test_fade_obi():
    print("\n=== Test: fade_obi ===")
    now = time.time()
    check("Buy at OBI=-0.7", strategies.fade_obi(make_market(-0.7), now) == "Buy")
    check("Sell at OBI=+0.7", strategies.fade_obi(make_market(0.7), now) == "Sell")
    check("None at OBI=0.5", strategies.fade_obi(make_market(0.5), now) is None)
    check("None at OBI=-0.5", strategies.fade_obi(make_market(-0.5), now) is None)
    check("None at boundary OBI=-0.65 (strict <)",
          strategies.fade_obi(make_market(-0.65), now) is None)
    check("Buy just past boundary OBI=-0.6501",
          strategies.fade_obi(make_market(-0.6501), now) == "Buy")


def test_direct_obi():
    print("\n=== Test: direct_obi ===")
    now = time.time()
    check("Buy at OBI=+0.7", strategies.direct_obi(make_market(0.7), now) == "Buy")
    check("Sell at OBI=-0.7", strategies.direct_obi(make_market(-0.7), now) == "Sell")
    check("None at OBI=0.3", strategies.direct_obi(make_market(0.3), now) is None)


def test_momentum_10s():
    print("\n=== Test: momentum_10s ===")
    now = time.time()
    # Price rose 0.5% over 10s, sigma_pct = 0.05 → threshold = max(0.02, 0.25*0.05) = 0.02
    hist_up = [(now - 9 + i, 0.1 * (1 + 0.005 * i / 10)) for i in range(10)]
    hist_down = [(now - 9 + i, 0.1 * (1 - 0.005 * i / 10)) for i in range(10)]

    m_up = make_market(0.0, sigma_pct=0.05, price_history=hist_up)
    m_down = make_market(0.0, sigma_pct=0.05, price_history=hist_down)
    m_flat = make_market(0.0, sigma_pct=0.05,
                         price_history=[(now - 9 + i, 0.1) for i in range(10)])

    check("Buy when price rising", strategies.momentum_10s(m_up, now) == "Buy")
    check("Sell when price falling", strategies.momentum_10s(m_down, now) == "Sell")
    check("None when flat", strategies.momentum_10s(m_flat, now) is None)


def test_meanrev_10s():
    print("\n=== Test: meanrev_10s ===")
    now = time.time()
    hist_up = [(now - 9 + i, 0.1 * (1 + 0.005 * i / 10)) for i in range(10)]
    hist_down = [(now - 9 + i, 0.1 * (1 - 0.005 * i / 10)) for i in range(10)]

    m_up = make_market(0.0, sigma_pct=0.05, price_history=hist_up)
    m_down = make_market(0.0, sigma_pct=0.05, price_history=hist_down)

    check("Sell when price rose (fade)", strategies.meanrev_10s(m_up, now) == "Sell")
    check("Buy when price fell (fade)", strategies.meanrev_10s(m_down, now) == "Buy")


def test_momentum_60s():
    print("\n=== Test: momentum_60s ===")
    now = time.time()
    # 30 samples over 60s with clear move up
    hist = [(now - 59 + 2 * i, 0.1 * (1 + 0.01 * i / 30)) for i in range(30)]
    m = make_market(0.0, sigma_pct=0.05, price_history=hist)
    check("Buy on 60s uptrend", strategies.momentum_60s(m, now) == "Buy")


def test_insufficient_data():
    print("\n=== Test: insufficient price history ===")
    now = time.time()
    m = make_market(0.0, sigma_pct=0.05,
                    price_history=[(now - 1, 0.1), (now - 0.5, 0.1001)])
    check("None with <5 samples", strategies.momentum_10s(m, now) is None)
    check("None with <5 samples (meanrev)", strategies.meanrev_10s(m, now) is None)


def test_evaluate_registry():
    print("\n=== Test: evaluate() dispatcher ===")
    now = time.time()
    m = make_market(-0.7)
    result = strategies.evaluate("fade_obi", m, now)
    check("evaluate('fade_obi') returns Buy", result == "Buy")

    try:
        strategies.evaluate("nonexistent", m, now)
        check("raises on unknown strategy", False)
    except ValueError:
        check("raises on unknown strategy", True)


def test_threshold_scaling():
    print("\n=== Test: threshold scales with sigma ===")
    now = time.time()
    # Move = 0.1% over 10s
    hist = [(now - 9 + i, 0.1 * (1 + 0.001 * i / 10)) for i in range(10)]

    # With sigma_pct = 0.05, threshold = max(0.02, 0.0125) = 0.02 → 0.1% < 0.02% → signal
    # Actually 0.1% = 0.1 percent, threshold 0.02 percent. move 0.1 > 0.02 → signal
    m_low_sigma = make_market(0.0, sigma_pct=0.05, price_history=hist)
    check("Signal with low sigma", strategies.momentum_10s(m_low_sigma, now) == "Buy")

    # With sigma_pct = 1.0, threshold = max(0.02, 0.25) = 0.25 → 0.1% < 0.25% → None
    m_high_sigma = make_market(0.0, sigma_pct=1.0, price_history=hist)
    check("No signal with high sigma", strategies.momentum_10s(m_high_sigma, now) is None)


# ============================================================
# Regression tests for strategies patches (27.09)
# ============================================================

def test_threshold_validation():
    print("\n=== Test: obi_threshold must be in (0, 1) ===")
    now = time.time()
    for bad in (0, 1, 1.5, -0.5, -1, 2):
        m = make_market(obi=-0.7)
        try:
            strategies.fade_obi(m, now, obi_threshold=bad)
            check(f"fade rejects {bad}", False)
        except ValueError:
            check(f"fade rejects {bad}", True)

    for bad in (0, 1, 1.5, -0.5):
        m = make_market(obi=0.7)
        try:
            strategies.direct_obi(m, now, obi_threshold=bad)
            check(f"direct rejects {bad}", False)
        except ValueError:
            check(f"direct rejects {bad}", True)


def test_momentum_60s_min_samples():
    print("\n=== Test: momentum_60s requires 30 samples (0.5/sec) ===")
    now = time.time()
    # 15 samples за 60 сек — мало
    sparse = [(now - 59 + 4 * i, 0.1 * (1 + 0.005 * i / 15)) for i in range(15)]
    m_sparse = make_market(0.0, sigma_pct=0.05, price_history=sparse)
    check("None with 15 samples for 60s", strategies.momentum_60s(m_sparse, now) is None)

    # 35 samples — достаточно
    dense = [(now - 59 + 1.7 * i, 0.1 * (1 + 0.01 * i / 35)) for i in range(35)]
    m_dense = make_market(0.0, sigma_pct=0.05, price_history=dense)
    check("Signal with 35 samples", strategies.momentum_60s(m_dense, now) == "Buy")


def test_momentum_10s_min_samples():
    print("\n=== Test: momentum_10s requires 5 samples ===")
    now = time.time()
    sparse = [(now - 9 + 2 * i, 0.1) for i in range(4)]
    m_sparse = make_market(0.0, sigma_pct=0.05, price_history=sparse)
    check("None with 4 samples", strategies.momentum_10s(m_sparse, now) is None)

    enough = [(now - 9 + 1 * i, 0.1 * (1 + 0.01 * i / 9)) for i in range(9)]
    m_enough = make_market(0.0, sigma_pct=0.05, price_history=enough)
    check("Signal with 9 samples", strategies.momentum_10s(m_enough, now) == "Buy")


def test_evaluate_kwargs_validation():
    print("\n=== Test: evaluate rejects unknown kwargs ===")
    now = time.time()
    m = make_market(obi=-0.7)

    # Правильный kwarg
    result = strategies.evaluate("fade_obi", m, now, obi_threshold=0.7)
    check("valid kwarg works", result is None or result in ("Buy", "Sell"))

    # Неизвестный kwarg для fade_obi
    try:
        strategies.evaluate("fade_obi", m, now, wrong_kwarg=1)
        check("rejects unknown kwarg for fade", False)
    except ValueError as e:
        check("rejects unknown kwarg for fade", "wrong_kwarg" in str(e))

    # obi_threshold не входит в сигнатуру momentum_10s
    try:
        m2 = make_market(0.0, sigma_pct=0.05)
        strategies.evaluate("momentum_10s", m2, now, obi_threshold=0.7)
        check("rejects obi_threshold for momentum", False)
    except ValueError as e:
        check("rejects obi_threshold for momentum", "obi_threshold" in str(e))
if __name__ == "__main__":
    print("=" * 60)
    print("Strategies unit tests")
    print("=" * 60)
    test_fade_obi()
    test_direct_obi()
    test_momentum_10s()
    test_meanrev_10s()
    test_momentum_60s()
    test_insufficient_data()
    test_evaluate_registry()
    test_threshold_scaling()
    test_threshold_validation()
    test_momentum_60s_min_samples()
    test_momentum_10s_min_samples()
    test_evaluate_kwargs_validation()

    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    print("=" * 60)
    sys.exit(0 if FAILED == 0 else 1)
