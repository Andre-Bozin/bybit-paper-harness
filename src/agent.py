"""
agent.py — торговый агент.
Один агент = одна стратегия + одна PaperSession + свой конфиг.
Символ берётся из MarketState (может быть любой).
"""
from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from src import config, strategies
from src.session import PaperSession

if TYPE_CHECKING:
    from src.market import MarketState


# --- Type aliases ---
Side = Literal["Buy", "Sell"]
Signal = Literal["Buy", "Sell"] | None


# --- TypedDict structures ---

class AgentSnapshot(TypedDict):
    """Snapshot состояния агента (для status_loop и мониторинга)."""
    name: str
    symbol: str
    strategy: str
    usdt: float
    position_side: str
    position_size: float
    entry_price: float
    open_orders: int
    sl_price: float
    current_tp: float
    in_cooldown: bool


def _setup_agent_logger(name: str) -> logging.Logger:
    """Отдельный logger для агента (JSON-lines в свой файл)."""
    lg = logging.getLogger(f"trade_{name}")
    lg.setLevel(logging.INFO)
    if lg.handlers:
        lg.handlers.clear()
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

    def __init__(
        self,
        cfg: dict[str, Any],
        market: MarketState,
        session: PaperSession,
    ) -> None:
        self.name: str = cfg["name"]
        self.cfg: dict[str, Any] = cfg
        self.market: MarketState = market
        self.session: PaperSession = session

        # Strategy config (все из cfg с fallback на config defaults)
        self.strategy = cfg["signal"]
        self.qty = float(cfg["qty"])
        self.obi_threshold = float(cfg.get("obi_threshold", 0.65))
        self.min_std_dev_pct = float(cfg.get("min_std_dev", 0.05))
        self.tp_sl_ratio = float(cfg.get("tp_sl_ratio", 2.0))
        self.decay_start = float(cfg.get("decay_start", 180))
        self.decay_duration = float(cfg.get("decay_duration", 600))
        self.hard_kill = float(cfg.get("hard_kill", 900))
        self.sl_cooldown = float(cfg.get("sl_cooldown", 300))

        # TP/SL constants из config (per-instance, можно override через cfg)
        self.tp_margin_base_pct = float(
            cfg.get("tp_margin_base_pct", config.TP_MARGIN_BASE_PCT)
        )
        self.tp_margin_sigma_mult = float(
            cfg.get("tp_margin_sigma_mult", config.TP_MARGIN_SIGMA_MULT)
        )
        self.tp_margin_min_pct = float(
            cfg.get("tp_margin_min_pct", config.TP_MARGIN_MIN_PCT)
        )
        self.tp_margin_max_pct = float(
            cfg.get("tp_margin_max_pct", config.TP_MARGIN_MAX_PCT)
        )
        self.decay_exit_min_pct = float(
            cfg.get("decay_exit_min_pct", config.DECAY_EXIT_MIN_PCT)
        )
        self.amend_tp_min_rel = float(
            cfg.get("amend_tp_min_rel", config.AMEND_TP_MIN_REL)
        )

        # Logger for trade events
        self.log = _setup_agent_logger(self.name)

        # Runtime state
        self.daily_block: bool = False    # внешний флаг от harness
        self.position_open: bool = False
        self.position_side: Side | None = None
        self.entry_price: float = 0.0
        self.entry_time: float = 0.0
        self.entry_tp_margin: float = 0.0
        self.current_tp: float = 0.0
        self.sl_price: float = 0.0
        self.active_oid: str | None = None    # входной лимит
        self.tp_oid: str | None = None        # TP-лимит
        self.sl_until: float = 0.0            # cooldown после SL
        self._last_placed_side: Side | None = None
        self._last_side_change: float = 0.0
        self._order_place_cooldown = float(
            cfg.get("order_cooldown", config.DEFAULT_ORDER_COOLDOWN)
        )
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
                # Сбрасываем сигнал, чтобы _on_entry не прочитал устаревшие значения
                self._place_sigma_pct = 0.0
                self._place_obi = 0.0
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

    def snapshot(self) -> AgentSnapshot:
        pos = self.session.position
        return {
            "name": self.name,
            "symbol": self.session.symbol,
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

    def final_snapshot(self) -> None:
        """Логирует финальное состояние при остановке harness.
        Используется deep_analysis.py для точного final_usdt."""
        pos = self.session.position
        self._log({
            "event": "FINAL_SNAPSHOT",
            "symbol": self.session.symbol,
            "usdt": round(self.session.usdt, 6),
            "position_size": pos["size"],
            "position_side": pos["side"] or "",
            "avg_price": pos["avgPrice"],
            "open_orders": len(self.session.orders),
            "position_open": self.position_open,
        })

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
        exit_side: Side = "Sell" if self.position_side == "Buy" else "Buy"
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

        elapsed = max(0.0, now - self.entry_time)

        # --- Fast SL ---
        if self.sl_price > 0:
            hit = (
                (self.position_side == "Buy" and self.market.best_bid <= self.sl_price) or
                (self.position_side == "Sell" and self.market.best_ask >= self.sl_price)
            )
            if hit:
                exit_side: Side = "Sell" if self.position_side == "Buy" else "Buy"
                self.session.cancel_all()
                ok = self.session.place_market(exit_side, self.qty, reduce_only=True)
                if not ok:
                    # Стакан пустой (best_bid/ask = 0) или qty отвалился.
                    # Не сбрасываем состояние — попробуем на следующем тике.
                    self._log({"event": "CLOSE_FAILED", "reason": "stop_loss",
                               "sl": self.sl_price, "elapsed": elapsed})
                    return
                self._log({"event": "STOP_LOSS", "sl": self.sl_price,
                           "elapsed": elapsed})
                self._reset()
                self.sl_until = now + self.sl_cooldown
                return

        # --- HARD_KILL ---
        if elapsed > self.hard_kill:
            exit_side: Side = "Sell" if self.position_side == "Buy" else "Buy"
            self.session.cancel_all()
            ok = self.session.place_market(exit_side, self.qty, reduce_only=True)
            if not ok:
                self._log({"event": "CLOSE_FAILED", "reason": "hard_kill",
                           "elapsed": elapsed})
                return
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
            if new_margin_pct < self.decay_exit_min_pct:
                exit_side: Side = "Sell" if self.position_side == "Buy" else "Buy"
                self.session.cancel_all()
                ok = self.session.place_market(exit_side, self.qty, reduce_only=True)
                if not ok:
                    self._log({"event": "CLOSE_FAILED", "reason": "decay_exit",
                               "elapsed": elapsed, "margin_pct": new_margin_pct})
                    return
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
            amend_threshold = self.entry_price * self.amend_tp_min_rel
            if abs(new_tp - self.current_tp) >= amend_threshold:
                if self.tp_oid is not None:
                    self.session.cancel(self.tp_oid)
                exit_side: Side = "Sell" if self.position_side == "Buy" else "Buy"
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
        """
        Динамический TP-margin в % от цены. Зависит от σ%.
        TP% = clamp(BASE + SIGMA_MULT * sigma_pct, MIN, MAX).
        """
        m = (self.tp_margin_base_pct
             + self.tp_margin_sigma_mult * self.market.current_std_dev_pct)
        return max(self.tp_margin_min_pct, min(self.tp_margin_max_pct, m))

    def _evaluate_signal(self, now: float) -> Signal:
        """
        Вызов стратегии с правильными kwargs.
        Возвращает "Buy" | "Sell" | None.
        При неизвестной стратегии — None + запись в лог (не валит harness).
        """
        try:
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
                self._log({"event": "UNKNOWN_STRATEGY",
                           "strategy": self.strategy})
                return None
        except ValueError as e:
            self._log({"event": "SIGNAL_ERROR",
                       "strategy": self.strategy, "reason": str(e)})
            return None

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

    def _log(self, event: dict[str, Any]) -> None:
        event["ts"] = time.time()
        event["agent"] = self.name
        self.log.info(json.dumps(event, ensure_ascii=False))
