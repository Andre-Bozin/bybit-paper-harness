"""
strategies.py — торговые сигналы.
Каждая стратегия: (market_state, now) -> "Buy" | "Sell" | None.
Стратегии symbol-agnostic: работают с OBI и σ% (безразмерные величины).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from src.market import MarketState


# Тип возвращаемого сигнала
Signal = Literal["Buy", "Sell"] | None


def fade_obi(
    market: MarketState,
    now: float,
    obi_threshold: float = 0.65,
) -> Signal:
    """
    Контр-трендовая стратегия (adverse selection).

    Идея: когда OBI сильно перекошен (много taker-покупок или продаж),
    крупный игрок завершает позицию — и цена откатывается. Мы занимаем
    противоположную сторону.

    Sell при OBI > +thr (перекос в покупки → продаём)
    Buy  при OBI < -thr (перекос в продажи → покупаем)
    """
    if not 0 < obi_threshold < 1:
        raise ValueError(
            f"obi_threshold must be in (0, 1), got {obi_threshold}"
        )
    obi = market.current_obi
    if obi > obi_threshold:
        return "Sell"
    if obi < -obi_threshold:
        return "Buy"
    return None


def direct_obi(
    market: MarketState,
    now: float,
    obi_threshold: float = 0.65,
) -> Signal:
    """
    Прямая стратегия (momentum на OBI).

    Buy  при OBI > +thr (следуем за агрессивным покупателем)
    Sell при OBI < -thr (следуем за агрессивным продавцом)

    В бектестах показывает худший результат, чем fade_obi — по всей
    видимости, из-за adverse selection.
    """
    if not 0 < obi_threshold < 1:
        raise ValueError(
            f"obi_threshold must be in (0, 1), got {obi_threshold}"
        )
    obi = market.current_obi
    if obi > obi_threshold:
        return "Buy"
    if obi < -obi_threshold:
        return "Sell"
    return None


def _momentum_signal(
    market: MarketState,
    now: float,
    window_sec: float,
    threshold_mult: float,
) -> float | None:
    """
    Базовый momentum: сравнить текущий mid с mid window_sec назад.

    Идём по deque в обратном порядке и останавливаемся при выходе за окно.
    Это O(k), где k — количество samples в окне (не O(n) по всему deque).

    Возвращает signed move_pct или None если недостаточно данных.
    """
    first_price = None
    last_price = None
    count = 0
    for ts, price in reversed(market.price_history):
        if now - ts > window_sec:
            break
        first_price = price
        if last_price is None:
            last_price = price
        count += 1

    # M3: min_samples масштабируется с окном (0.5 sample/sec floor)
    min_samples = max(5, int(window_sec * 0.5))
    if count < min_samples:
        return None
    if first_price is None or last_price is None or first_price <= 0:
        return None

    move_pct = (last_price - first_price) / first_price * 100.0
    threshold_pct = threshold_mult * market.current_std_dev_pct
    if threshold_pct <= 0:
        return None
    if abs(move_pct) < threshold_pct:
        return None
    return move_pct


def momentum_10s(
    market: MarketState,
    now: float,
    threshold_mult: float = 0.25,
) -> Signal:
    """Momentum: Buy если цена выросла за 10s, Sell если упала."""
    move = _momentum_signal(market, now, 10.0, threshold_mult)
    if move is None:
        return None
    return "Buy" if move > 0 else "Sell"


def momentum_60s(
    market: MarketState,
    now: float,
    threshold_mult: float = 0.25,
) -> Signal:
    """Momentum на 60s окне."""
    move = _momentum_signal(market, now, 60.0, threshold_mult)
    if move is None:
        return None
    return "Buy" if move > 0 else "Sell"


def meanrev_10s(
    market: MarketState,
    now: float,
    threshold_mult: float = 0.25,
) -> Signal:
    """Контр-моментум: Sell если цена выросла, Buy если упала."""
    move = _momentum_signal(market, now, 10.0, threshold_mult)
    if move is None:
        return None
    return "Sell" if move > 0 else "Buy"


def meanrev_60s(
    market: MarketState,
    now: float,
    threshold_mult: float = 0.25,
) -> Signal:
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


def evaluate(
    strategy_name: str,
    market: MarketState,
    now: float,
    **kwargs: Any,
) -> Signal:
    """
    Универсальная точка вызова.

    kwargs передаются в стратегию (например obi_threshold=0.65).
    Неизвестные kwargs → ValueError — защита от опечаток в config.
    """
    import inspect

    fn = STRATEGIES.get(strategy_name)
    if fn is None:
        raise ValueError(
            f"Unknown strategy: {strategy_name}. "
            f"Allowed: {sorted(STRATEGIES.keys())}"
        )

    # M1: валидация kwargs против сигнатуры функции
    sig = inspect.signature(fn)
    allowed = set(sig.parameters.keys()) - {"market", "now"}
    extra = set(kwargs.keys()) - allowed
    if extra:
        raise ValueError(
            f"Unknown kwargs for {strategy_name}: {sorted(extra)}. "
            f"Allowed: {sorted(allowed)}"
        )

    return fn(market, now, **kwargs)
