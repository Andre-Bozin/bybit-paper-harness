"""
agent.py — торговый агент.
Один агент = одна стратегия + одна PaperSession + свой конфиг.
Символ берётся из MarketState (может быть любой).
"""
import json
import time
import logging
from pathlib import Path

from src import config, strategies
from src.session import PaperSession


def _setup_agent_logger(name: str) -> logging.Logger:
    """Отдельный logger для агента (JSON-lines в свой файл)."""
    lg = logging.getLogger(f"trade_{name}")
    lg.setLevel(logging.INFO)
    if lg.handlers:
        lg.handlers.clear()
    from logging.handlers import RotatingFileHandler
    h = RotatingFileHandler(
        config.LOGS_DIR / f"trade_{name}.log",
        maxBytes=10 * 1024 * 1024, backupCount=7,
    )
    h.setFormatter(logging.Formatter('%(message)s'))
    lg.addHandler(h)
    lg.propagate = False
    return lg


class Agent:
    """Торговый агент. Один экземпляр на конфиг агента."""

    def __init__(self, cfg: dict, market, session: PaperSession):
        self.name = cfg["name"]
        self.cfg = cfg
        self.market = market
        self.session = session

        # Config
        self.strategy = cfg["signal"]
        self.qty = float(cfg["qty"])
        self.obi_threshold = float(cfg.get("obi_threshold", 0.65))
        self.min_std_dev_pct = float(cfg.get("min_std_dev", 0.05))
        self.tp_sl_ratio = float(cfg.get("tp_sl_ratio", 2.0))
        self.decay_start = float(cfg.get("decay_start", 180))
        self.decay_duration = float(cfg.get("decay_duration", 600))
        self.hard_kill = float(cfg.get("hard_kill", 900))
        self.sl_cooldown = float(cfg.get("sl_cooldown", 300))

        # Logger for trade events
        self.log = _setup_agent_logger(self.name)

        # Runtime state
        self.daily_block = False    # внешний флаг от harness
        self.position_open = False
        self.position_side = None
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.entry_tp_margin = 0.0
        self.current_tp = 0.0
        self.sl_price = 0.0
        self.active_oid = None      # входной лимит
        self.tp_oid = None          # TP-лимит
        self.sl_until = 0.0         # cooldown после SL
        self._last_placed_side = None
        self._last_side_change = 0.0
        self._order_place_cooldown = 2.0  # не переставлять чаще 2s
        # Значения сигнала на момент решения (PLACE). Используются в ENTRY.
        self._place_sigma_pct = 0.0
        self._place_obi = 0.0

    # --- Public API ---

    def on_tick(self, now: float) -> None:
        """Основной цикл агента. Вызывается harness'ом на каждом тике."""
        # 1. Обновляем market в session (для fills)
        self.session.update_market(self.market.best_bid, self.market.best_ask)

        # 2. Детект входа (session перешла 0 → >0)
        if not self.position_open and self.session.position["size"] > 0:
            self._on_entry(now)

        # 3. Управление позицией
        if self.position_open:
            self._manage(now)
            return

        # 4. Гейты для входа
        if self.daily_block:
            return
        if self.market.current_std_dev_pct < self.min_std_dev_pct:
            return
        if now < self.sl_until:
            return

        # 5. Сигнал
        side = self._evaluate_signal(now)

        # 6. Отмена при исчезновении / смене сигнала
        if self.active_oid is not None:
            if side is None or side != self._last_placed_side:
                self.session.cancel(self.active_oid)
                self.active_oid = None
                self._last_placed_side = None
        if side is None:
            return
        if self.active_oid is not None:
            return  # уже стоит ордер на ту же сторону

        # 7. Rate-limit на однотипные постановки
        if (self._last_placed_side == side and
                (now - self._last_side_change) < self._order_place_cooldown):
            return

        # 8. Постановка лимита
        price = self.market.best_bid if side == "Buy" else self.market.best_ask
        try:
            self.active_oid = self.session.place_limit(side, price, self.qty)
            self._last_placed_side = side
            self._last_side_change = now
            # Фиксируем сигнал на момент решения — именно его логируем и в ENTRY
            self._place_sigma_pct = self.market.current_std_dev_pct
            self._place_obi = self.market.current_obi
            self._log({"event": "PLACE", "side": side, "price": price,
                       "sigma_pct": self._place_sigma_pct,
                       "obi": self._place_obi})
        except ValueError as e:
            # qty/min_notional/tick validation fail — не критично, пропускаем
            self._log({"event": "PLACE_REJECTED", "side": side, "price": price,
                       "reason": str(e)})

    def snapshot(self) -> dict:
        pos = self.session.position
        return {
            "name": self.name,
            "strategy": self.strategy,
            "usdt": round(self.session.usdt, 4),
            "position_side": pos["side"] or "flat",
            "position_size": pos["size"],
            "entry_price": self.entry_price,
            "open_orders": len(self.session.orders),
            "sl_price": self.sl_price,
            "current_tp": self.current_tp,
            "in_cooldown": time.time() < self.sl_until,
        }

    # --- Entry / Manage / Close ---

    def _on_entry(self, now: float) -> None:
        """Сработал входной лимит — устанавливаем TP и SL."""
        # Отменяем остатки входного ордера (если остались)
        if self.active_oid is not None:
            self.session.cancel(self.active_oid)
            self.active_oid = None

        self.position_open = True
        self.position_side = self.session.position["side"]
        self.entry_price = self.session.position["avgPrice"]
        self.entry_time = now

        # Расчёт TP/SL
        tp_pct = self._tp_margin_pct()
        margin = self.entry_price * tp_pct / 100.0
        self.entry_tp_margin = margin

        if self.position_side == "Buy":
            self.sl_price = self.entry_price - margin * self.tp_sl_ratio
            self.current_tp = self.entry_price + margin
        else:
            self.sl_price = self.entry_price + margin * self.tp_sl_ratio
            self.current_tp = self.entry_price - margin

        # Ставим TP-лимит
        exit_side = "Sell" if self.position_side == "Buy" else "Buy"
        try:
            self.tp_oid = self.session.place_limit(exit_side, self.current_tp, self.qty)
        except ValueError as e:
            self._log({"event": "TP_PLACE_FAILED", "reason": str(e)})
            self.tp_oid = None

        self._log({
            "event": "ENTRY",
            "side": self.position_side,
            "price": self.entry_price,
            # Значения сигнала на момент решения (PLACE), а не на момент fill
            "sigma_pct": self._place_sigma_pct,
            "obi": self._place_obi,
            "tp": self.current_tp,
            "sl": self.sl_price,
            "tp_pct": tp_pct,
        })

    def _manage(self, now: float) -> None:
        """Управление открытой позицией: SL, HARD_KILL, decay."""
        # Позиция уже закрылась (TP-лимит исполнился или что-то)
        if self.session.position["size"] == 0:
            self._on_close(now)
            return

        elapsed = now - self.entry_time

        # --- Fast SL ---
        if self.sl_price > 0:
            hit = (
                (self.position_side == "Buy" and self.market.best_bid <= self.sl_price) or
                (self.position_side == "Sell" and self.market.best_ask >= self.sl_price)
            )
            if hit:
                exit_side = "Sell" if self.position_side == "Buy" else "Buy"
                self.session.cancel_all()
                self.session.place_market(exit_side, self.qty, reduce_only=True)
                self._log({"event": "STOP_LOSS", "sl": self.sl_price,
                           "elapsed": elapsed})
                self._reset()
                self.sl_until = now + self.sl_cooldown
                return

        # --- HARD_KILL ---
        if elapsed > self.hard_kill:
            exit_side = "Sell" if self.position_side == "Buy" else "Buy"
            self.session.cancel_all()
            self.session.place_market(exit_side, self.qty, reduce_only=True)
            self._log({"event": "HARD_KILL", "elapsed": elapsed})
            self._reset()
            return

        # --- Time Decay ---
        if elapsed > self.decay_start:
            ratio = max(0.0, 1.0 - (elapsed - self.decay_start) / self.decay_duration)
            base_margin_pct = self._tp_margin_pct()
            new_margin_pct = base_margin_pct * ratio

            # Decay exit fires when remaining margin < 0.02% of price.
            # (Previously was absolute "< 1.0 USDT" — broke all non-BTC symbols.)
            if new_margin_pct < 0.02:
                exit_side = "Sell" if self.position_side == "Buy" else "Buy"
                self.session.cancel_all()
                self.session.place_market(exit_side, self.qty, reduce_only=True)
                self._log({"event": "DECAY_EXIT", "elapsed": elapsed,
                           "margin_pct": new_margin_pct})
                self._reset()
                return

            new_margin = self.entry_price * new_margin_pct / 100.0

            if self.position_side == "Buy":
                new_tp = self.entry_price + new_margin
            else:
                new_tp = self.entry_price - new_margin

            # Amend TP only if change is significant relative to price.
            # Previous absolute "< 1.0 USDT" broke all non-BTC symbols.
            amend_threshold = self.entry_price * 0.0001  # 0.01% of price
            if abs(new_tp - self.current_tp) >= amend_threshold:
                if self.tp_oid is not None:
                    self.session.cancel(self.tp_oid)
                exit_side = "Sell" if self.position_side == "Buy" else "Buy"
                try:
                    self.tp_oid = self.session.place_limit(exit_side, new_tp, self.qty)
                    self.current_tp = new_tp
                    self._log({"event": "DECAY", "new_tp": new_tp,
                               "margin": new_margin, "elapsed": elapsed})
                except ValueError as e:
                    self._log({"event": "DECAY_FAILED", "reason": str(e)})

    def _on_close(self, now: float) -> None:
        """Позиция закрылась (TP или внешнее)."""
        self._log({
            "event": "CLOSE_DETECTED",
            "side": self.position_side,
            "entry": self.entry_price,
            "usdt": round(self.session.usdt, 4),
            "elapsed": now - self.entry_time if self.entry_time else 0,
        })
        self._reset()

    # --- Helpers ---

    def _tp_margin_pct(self) -> float:
        """Динамический TP-margin в % от цены. Зависит от σ%. """
        m = 0.05 + 3.0 * self.market.current_std_dev_pct
        return max(0.15, min(0.75, m))

    def _evaluate_signal(self, now: float):
        """Вызов стратегии с правильными kwargs."""
        if self.strategy in ("fade_obi", "direct_obi"):
            return strategies.evaluate(
                self.strategy, self.market, now,
                obi_threshold=self.obi_threshold,
            )
        elif self.strategy in ("momentum_10s", "momentum_60s",
                               "meanrev_10s", "meanrev_60s"):
            return strategies.evaluate(
                self.strategy, self.market, now,
                threshold_mult=0.25,
            )
        else:
            raise ValueError(f"Unknown strategy: {self.strategy}")

    def _reset(self) -> None:
        self.position_open = False
        self.position_side = None
        self.entry_price = 0.0
        self.entry_time = 0.0
        self.entry_tp_margin = 0.0
        self.current_tp = 0.0
        self.sl_price = 0.0
        self.active_oid = None
        self.tp_oid = None
        self._last_placed_side = None
        self._place_sigma_pct = 0.0
        self._place_obi = 0.0

    def _log(self, event: dict) -> None:
        event["ts"] = time.time()
        event["agent"] = self.name
        self.log.info(json.dumps(event, ensure_ascii=False))
