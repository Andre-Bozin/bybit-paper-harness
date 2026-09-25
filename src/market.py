"""
market.py — состояние рынка для одного символа.
Держит: orderbook cache, trade_history, σ%, OBI.
Один экземпляр на символ. Потокобезопасен в рамках asyncio.
"""
import time
import statistics
from collections import deque


class MarketState:
    """Состояние одного символа: стакан, σ, OBI."""

    SIGMA_WINDOW_SEC = 60.0
    TRADE_WINDOW_SEC = 10.0
    SIGMA_MIN_SAMPLES = 50
    TOP_LEVELS = 10

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids = {}  # price -> size
        self.asks = {}  # price -> size
        self.price_history = deque()   # (ts, mid_price)
        self.trade_history = deque()   # (ts, side, volume)
        self.current_std_dev = 0.0
        self.current_std_dev_pct = 0.0
        self.current_obi = 0.0
        self.best_bid = 0.0
        self.best_ask = 0.0
        self.last_update_ts = 0.0

    # --- Orderbook updates ---

    def apply_orderbook(self, data: dict, is_snapshot: bool) -> None:
        """Применяет snapshot или delta к локальному стакану."""
        if is_snapshot:
            self.bids.clear()
            self.asks.clear()

        for price_str, size_str in data.get("b", []):
            price = float(price_str)
            size = float(size_str)
            if size == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = size

        for price_str, size_str in data.get("a", []):
            price = float(price_str)
            size = float(size_str)
            if size == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = size

    def has_data(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    def refresh_best(self) -> bool:
        """Обновляет best_bid/best_ask. True если есть данные."""
        if not self.has_data():
            return False
        self.best_bid = max(self.bids.keys())
        self.best_ask = min(self.asks.keys())
        self.last_update_ts = time.time()
        return True

    # --- Metrics ---

    def update_sigma(self, ts: float) -> None:
        """Обновляет σ и σ%. Требует уже обновлённых best_bid/best_ask."""
        if self.best_bid <= 0 or self.best_ask <= 0:
            return
        mid = (self.best_bid + self.best_ask) / 2.0
        self.price_history.append((ts, mid))

        # Trim old
        cutoff = ts - self.SIGMA_WINDOW_SEC
        while self.price_history and self.price_history[0][0] < cutoff:
            self.price_history.popleft()

        if len(self.price_history) >= self.SIGMA_MIN_SAMPLES:
            prices = [p[1] for p in self.price_history]
            self.current_std_dev = statistics.stdev(prices)
            self.current_std_dev_pct = (
                (self.current_std_dev / mid * 100.0) if mid > 0 else 0.0
            )

    def add_trades(self, trades: list, ts: float) -> None:
        """Добавляет публичные сделки. trades = [{S, v}, ...]."""
        for t in trades:
            try:
                self.trade_history.append((ts, t["S"], float(t["v"])))
            except (KeyError, ValueError, TypeError):
                continue

        cutoff = ts - self.TRADE_WINDOW_SEC
        while self.trade_history and self.trade_history[0][0] < cutoff:
            self.trade_history.popleft()

    def update_obi(self) -> None:
        """Обновляет OBI по топ-N уровням стакана."""
        bid_vol = self._top_volume(self.bids, reverse=True)
        ask_vol = self._top_volume(self.asks, reverse=False)
        total = bid_vol + ask_vol
        if total > 0:
            self.current_obi = (bid_vol - ask_vol) / total
        else:
            self.current_obi = 0.0

    def _top_volume(self, book: dict, reverse: bool) -> float:
        if not book:
            return 0.0
        prices = sorted(book.keys(), reverse=reverse)[: self.TOP_LEVELS]
        return sum(book[p] for p in prices)

    # --- Snapshot for agent ---

    def tick(self, ts: float) -> bool:
        """
        Полный апдейт: refresh best, обновить σ%, OBI.
        Вызывается после apply_orderbook + add_trades.
        Возвращает True если рынок готов (есть стакан и σ посчитана).
        """
        if not self.refresh_best():
            return False
        self.update_sigma(ts)
        self.update_obi()
        return True

    def snapshot(self) -> dict:
        return {
            "symbol": self.symbol,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "sigma_pct": round(self.current_std_dev_pct, 4),
            "obi": round(self.current_obi, 3),
            "bids_levels": len(self.bids),
            "asks_levels": len(self.asks),
            "trades_buffered": len(self.trade_history),
        }
