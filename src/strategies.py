"""
strategies.py — торговые сигналы.
Каждая стратегия: (market_state, now) -> "Buy" | "Sell" | None.
Стратегии symbol-agnostic: работают с OBI и σ% (безразмерные величины).
"""
from collections import deque


def fade_obi(market, now, obi_threshold=0.65):
    """Контр-трендовый: Sell при OBI > +thr, Buy при OBI < -thr."""
    obi = market.current_obi
    if obi > obi_threshold:
        return "Sell"
    if obi < -obi_threshold:
        return "Buy"
    return None


def direct_obi(market, now, obi_threshold=0.65):
    """Прямой: Buy при OBI > +thr, Sell при OBI < -thr."""
    obi = market.current_obi
    if obi > obi_threshold:
        return "Buy"
    if obi < -obi_threshold:
        return "Sell"
    return None


def _momentum_signal(market, now, window_sec, threshold_mult):
    """Базовый momentum: сравнить текущий mid с mid window_sec назад."""
    window = [
        (ts, price) for ts, price in market.price_history
        if now - ts <= window_sec
    ]
    if len(window) < 5:
        return None
    first_price = window[0][1]
    last_price = window[-1][1]
    if first_price <= 0:
        return None
    move_pct = (last_price - first_price) / first_price * 100.0
    threshold_pct = max(0.02, threshold_mult * market.current_std_dev_pct)
    if abs(move_pct) < threshold_pct:
        return None
    return move_pct


def momentum_10s(market, now, threshold_mult=0.25):
    """Momentum: Buy если цена выросла за 10s, Sell если упала."""
    move = _momentum_signal(market, now, 10.0, threshold_mult)
    if move is None:
        return None
    return "Buy" if move > 0 else "Sell"


def momentum_60s(market, now, threshold_mult=0.25):
    """Momentum на 60s окне."""
    move = _momentum_signal(market, now, 60.0, threshold_mult)
    if move is None:
        return None
    return "Buy" if move > 0 else "Sell"


def meanrev_10s(market, now, threshold_mult=0.25):
    """Контр-моментум: Sell если цена выросла, Buy если упала."""
    move = _momentum_signal(market, now, 10.0, threshold_mult)
    if move is None:
        return None
    return "Sell" if move > 0 else "Buy"


def meanrev_60s(market, now, threshold_mult=0.25):
    """Контр-моментум на 60s окне."""
    move = _momentum_signal(market, now, 60.0, threshold_mult)
    if move is None:
        return None
    return "Sell" if move > 0 else "Buy"


# --- Registry ---

STRATEGIES = {
    "fade_obi": fade_obi,
    "direct_obi": direct_obi,
    "momentum_10s": momentum_10s,
    "momentum_60s": momentum_60s,
    "meanrev_10s": meanrev_10s,
    "meanrev_60s": meanrev_60s,
}


def evaluate(strategy_name: str, market, now, **kwargs):
    """
    Универсальная точка вызова.
    kwargs передаются в стратегию (например obi_threshold=0.65).
    """
    fn = STRATEGIES.get(strategy_name)
    if fn is None:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    return fn(market, now, **kwargs)
