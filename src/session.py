"""
session.py — PaperSession.
Симуляция биржи: держит баланс, ордера, позицию.
Валидирует ордера по metadata (tick_size, qty_step, min_notional).
"""
import json
import time
import logging
from logging.handlers import RotatingFileHandler

from src import config


def _setup_agent_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(f"agent_{name}")
    lg.setLevel(logging.INFO)
    if lg.handlers:
        lg.handlers.clear()
    h = RotatingFileHandler(
        config.LOGS_DIR / f"agent_{name}.log",
        maxBytes=10 * 1024 * 1024, backupCount=7,
    )
    h.setFormatter(logging.Formatter('%(message)s'))
    lg.addHandler(h)
    lg.propagate = False
    return lg


# Global events logger (shared between agents)
_events_logger = logging.getLogger("events")
if not _events_logger.handlers:
    _eh = RotatingFileHandler(
        config.LOGS_DIR / "events.log",
        maxBytes=20 * 1024 * 1024, backupCount=5,
    )
    _eh.setFormatter(logging.Formatter(
        '%(asctime)s.%(msecs)03d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    _events_logger.addHandler(_eh)
    _events_logger.propagate = False


class PaperSession:
    """
    Симулирует биржевую сессию для одного агента.

    Принимает instrument metadata — не хардкодит ни qty, ни fees, ни symbol.
    """
    def __init__(self, name: str, instrument: dict, start_usdt: float):
        self.name = name
        self.symbol = instrument["symbol"]
        self.tick_size = instrument["tick_size"]
        self.qty_step = instrument["qty_step"]
        self.min_qty = instrument["min_qty"]
        self.min_notional = instrument["min_notional"]
        self.maker_fee = instrument["maker_fee"]
        self.taker_fee = instrument["taker_fee"]

        self.usdt = float(start_usdt)
        self.orders = {}
        self.position = {"size": 0.0, "side": "", "avgPrice": 0.0}
        self.best_bid = 0.0
        self.best_ask = 0.0

        self._next_id = 0
        self.log = _setup_agent_logger(name)

    # --- Order placement ---

    def _gen_id(self) -> str:
        self._next_id += 1
        return f"{self.name}-{self._next_id}"

    def _round_price(self, price: float) -> float:
        """Округляет цену к tick_size."""
        return round(round(price / self.tick_size) * self.tick_size, 10)

    def _round_qty(self, qty: float) -> float:
        """Округляет qty к qty_step."""
        return round(round(qty / self.qty_step) * self.qty_step, 10)

    def _validate(self, side: str, price: float, qty: float) -> tuple:
        """Валидирует side/price/qty и округляет к tick_size/qty_step.
        Поднимает ValueError при невалидных параметрах."""
        if side not in ("Buy", "Sell"):
            raise ValueError(f"side must be 'Buy' or 'Sell', got {side!r}")
        if not isinstance(price, (int, float)) or price <= 0:
            raise ValueError(f"price must be > 0, got {price!r}")
        if not isinstance(qty, (int, float)) or qty <= 0:
            raise ValueError(f"qty must be > 0, got {qty!r}")

        price = self._round_price(price)
        qty = self._round_qty(qty)

        if price <= 0:
            raise ValueError(f"price after rounding <= 0: {price}")
        if qty < self.min_qty:
            raise ValueError(f"qty {qty} < min_qty {self.min_qty}")
        if self.min_notional > 0 and price * qty < self.min_notional:
            raise ValueError(f"notional {price*qty} < min_notional {self.min_notional}")
        return price, qty

    def place_limit(self, side: str, price: float, qty: float) -> str:
        """Ставит лимитный ордер. Возвращает order_id."""
        price, qty = self._validate(side, price, qty)
        oid = self._gen_id()
        # Считаем ордер "на лучшем уровне" если цена совпадает с точностью до половины tick.
        # Иначе order на tick ниже уже не считается "на best".
        half_tick = self.tick_size / 2
        was_at_best = (
            (side == "Buy" and self.best_bid > 0 and abs(self.best_bid - price) < half_tick) or
            (side == "Sell" and self.best_ask > 0 and abs(self.best_ask - price) < half_tick)
        )
        self.orders[oid] = {
            "side": side, "price": price, "qty": qty,
            "type": "Limit", "was_at_best": was_at_best,
        }
        return oid

    def cancel(self, oid: str) -> bool:
        if oid in self.orders:
            del self.orders[oid]
            return True
        return False

    def cancel_all(self) -> int:
        n = len(self.orders)
        self.orders.clear()
        return n

    def place_market(self, side: str, qty: float, reduce_only: bool = False) -> bool:
        """Market-ордер. Возвращает True если исполнен, False при no-op."""
        if side not in ("Buy", "Sell"):
            return False
        price = self.best_ask if side == "Buy" else self.best_bid
        if price <= 0:
            return False
        if not isinstance(qty, (int, float)) or qty <= 0:
            return False

        qty = self._round_qty(qty)
        if qty < self.min_qty:
            return False
        if self.min_notional > 0 and price * qty < self.min_notional:
            return False

        try:
            self._execute_market(side, qty, price, reduce_only)
        except ValueError:
            return False
        return True

    # --- Market updates & fills ---

    def update_market(self, bid: float, ask: float) -> None:
        self.best_bid = bid
        self.best_ask = ask
        self._try_fills()

    def _try_fills(self) -> None:
        """
        Проверяет все активные лимитные ордера на fill.

        Правила fill (симуляция Bybit):
        - Buy @ P: filled если best_ask <= P (touch) ИЛИ
                   was_at_best и best_bid < P (уровень съеден).
        - Sell @ P: filled если best_bid >= P (touch) ИЛИ
                    was_at_best и best_ask > P (уровень съеден).
        """
        for oid, o in list(self.orders.items()):
            side = o["side"]
            price = o["price"]
            was_at_best = o["was_at_best"]

            if side == "Buy":
                # Touch: ask <= our buy price
                if 0 < self.best_ask <= price:
                    self._fill_limit(oid, o, min(price, self.best_ask))
                # Level eaten: our bid was at best, now best_bid went below
                elif was_at_best and 0 < self.best_bid < price:
                    self._fill_limit(oid, o, price)
            elif side == "Sell":
                if 0 < self.best_bid >= price:
                    self._fill_limit(oid, o, max(price, self.best_bid))
                elif was_at_best and 0 < self.best_ask > price:
                    self._fill_limit(oid, o, price)

    def _fill_limit(self, oid: str, order: dict, price: float) -> None:
        """
        Исполняет лимитный ордер по указанной цене.
        Различает три случая: OPEN (нет позиции), ADD (усреднение),
        REDUCE/CLOSE (закрытие противоположной).
        """
        qty = order["qty"]
        side = order["side"]

        cs = self.position["size"]
        cside = self.position["side"]
        cavg = self.position["avgPrice"]

        if cs == 0:
            fee = qty * price * self.maker_fee
            self.usdt -= fee
            self.position = {"size": qty, "side": side, "avgPrice": price}
            self._emit({"event": "OPEN", "side": side, "qty": qty, "price": price, "fee": fee})

        elif cside == side:
            fee = qty * price * self.maker_fee
            self.usdt -= fee
            ns = cs + qty
            na = (cavg * cs + price * qty) / ns
            self.position = {"size": ns, "side": side, "avgPrice": na}
            self._emit({"event": "ADD", "side": side, "qty": qty, "price": price, "fee": fee})

        else:
            # Truncate — ордер не может исполнить больше, чем размер позиции
            exec_qty = min(qty, cs)
            if qty > cs:
                self._emit({"event": "TRUNCATED", "side": side,
                            "requested": qty, "executed": exec_qty, "price": price})

            fee = exec_qty * price * self.maker_fee
            self.usdt -= fee
            pnl = (price - cavg) * exec_qty if cside == "Buy" else (cavg - price) * exec_qty
            self.usdt += pnl

            if exec_qty >= cs:
                self.position = {"size": 0.0, "side": "", "avgPrice": 0.0}
                self._emit({"event": "CLOSE", "side": side, "qty": exec_qty,
                            "price": price, "pnl": pnl, "fee": fee})
            else:
                self.position["size"] = cs - exec_qty
                self._emit({"event": "REDUCE", "side": side, "qty": exec_qty,
                            "price": price, "pnl": pnl, "fee": fee})

        del self.orders[oid]

    def _execute_market(self, side: str, qty: float, price: float, reduce_only: bool) -> None:
        """
        Market-исполнение. Taker fee.
        - reduce_only=True: закрыть/уменьшить позицию. No-op если нечего закрывать.
        - reduce_only=False: открыть новую позицию. Raise если позиция уже открыта
          (используйте place_limit для входа).
        """
        cs = self.position["size"]
        cside = self.position["side"]
        cavg = self.position["avgPrice"]

        if reduce_only:
            if cs == 0 or cside == side:
                # No-op — нечего закрывать
                return

            exec_qty = min(qty, cs)
            if qty > cs:
                self._emit({"event": "TRUNCATED_MARKET", "side": side,
                            "requested": qty, "executed": exec_qty, "price": price})

            fee = exec_qty * price * self.taker_fee
            self.usdt -= fee
            pnl = (price - cavg) * exec_qty if cside == "Buy" else (cavg - price) * exec_qty
            self.usdt += pnl

            if exec_qty >= cs:
                self.position = {"size": 0.0, "side": "", "avgPrice": 0.0}
                self._emit({"event": "CLOSE", "side": side, "qty": exec_qty, "price": price,
                            "pnl": pnl, "fee": fee, "src": "market"})
            else:
                self.position["size"] = cs - exec_qty
                self._emit({"event": "REDUCE", "side": side, "qty": exec_qty, "price": price,
                            "pnl": pnl, "fee": fee, "src": "market"})
        else:
            if cs > 0:
                raise ValueError(
                    f"place_market(reduce_only=False) with open position "
                    f"(size={cs}, side={cside}). Use place_limit to enter."
                )
            fee = qty * price * self.taker_fee
            self.usdt -= fee
            self.position = {"size": qty, "side": side, "avgPrice": price}
            self._emit({"event": "OPEN", "side": side, "qty": qty, "price": price,
                        "fee": fee, "src": "market"})

    # --- Logging ---

    def _emit(self, event: dict) -> None:
        event["ts"] = time.time()
        self.log.info(json.dumps(event, ensure_ascii=False))
        side = event.get("side", "")
        qty = event.get("qty", "")
        price = event.get("price", 0)
        pnl = event.get("pnl", 0)
        fee = event.get("fee", 0)
        _events_logger.info(
            f"[{self.name}][{self.symbol}] {event['event']:12} {side:5} "
            f"{qty:>10} @ {price:.5f} pnl={pnl:+.4f} fee={fee:.4f}"
        )

    # --- Info ---

    def snapshot(self) -> dict:
        """Текущее состояние сессии."""
        return {
            "name": self.name,
            "symbol": self.symbol,
            "usdt": self.usdt,
            "position_size": self.position["size"],
            "position_side": self.position["side"],
            "avg_price": self.position["avgPrice"],
            "open_orders": len(self.orders),
        }
