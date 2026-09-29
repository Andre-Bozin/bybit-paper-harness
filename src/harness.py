"""
harness.py — Multi-agent paper trading orchestrator.
Один WebSocket, N MarketState (по одному на символ), M Agent'ов.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from argparse import ArgumentParser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TYPE_CHECKING

import websockets

from src import config, metadata
from src.agent import Agent
from src.market import MarketState
from src.session import PaperSession

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection


def setup_events_logger() -> logging.Logger:
    """Общий events.log для всех агентов."""
    lg = logging.getLogger("events")
    lg.setLevel(logging.INFO)
    if lg.handlers:
        lg.handlers.clear()
    h = RotatingFileHandler(
        config.LOGS_DIR / "events.log",
        maxBytes=20 * 1024 * 1024, backupCount=5,
    )
    h.setFormatter(logging.Formatter(
        '%(asctime)s.%(msecs)03d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    lg.addHandler(h)
    lg.propagate = False
    return lg


def _startup_check() -> None:
    """
    Проверяет безопасность и логирует состояние ключей.

    1. .env не отслеживается git (защита от утечки).
    2. Ключи загружены (маскированные).
    3. Пингует Bybit API через fee fetch (если ключи есть).
    """
    import os
    import subprocess

    print("[harness] === Startup security check ===")

    # 1. .env в git?
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print("[harness] ⚠️  WARNING: .env IS tracked by git!")
            print("[harness] ⚠️  API keys could leak. Run: git rm --cached .env")
        else:
            print("[harness] OK: .env not tracked by git")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("[harness] WARN: cannot check git tracking (git not available?)")

    # 2. Ключи загружены?
    key = os.environ.get("BYBIT_API_KEY", "")
    secret = os.environ.get("BYBIT_API_SECRET", "")
    if key and secret and len(key) >= 10:
        masked = f"{key[:6]}...{key[-4:]}"
        print(f"[harness] API key loaded: {masked}")
        print("[harness] Real fees will be fetched from Bybit")
    else:
        print("[harness] No API keys in env — using DEFAULT fees")
        print("[harness]   maker 0.00036 / taker 0.001 (retail estimate)")

    print("[harness] === End of startup check ===")
    print()


class Harness:
    """Главный orchestrator."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg: dict[str, Any] = cfg
        self.events: logging.Logger = setup_events_logger()
        self.meta: dict[str, Any] = cfg["meta"]
        self.symbols: list[str] = config.get_symbols(cfg)
        self.category: str = config.get_category(cfg)

        self.markets: dict[str, MarketState] = {}   # symbol -> MarketState
        self.agents: list[Agent] = []               # list of Agent
        self._shutdown_done: bool = False

        self._build()

    def _build(self) -> None:
        """Создаёт markets, sessions, agents."""
        self.events.info(
            f"[SYSTEM] Harness starting, {len(self.cfg['agents'])} agents, "
            f"symbols={self.symbols}"
        )

        # 1. Создаём MarketState для каждого символа
        for sym in self.symbols:
            self.markets[sym] = MarketState(sym)
            self.events.info(f"[SYSTEM] MarketState created for {sym}")

        # 2. Создаём агентов
        start_usdt: float = float(self.cfg["meta"].get("start_balance", 1000.0))
        instrument_cache: dict[str, dict[str, Any]] = {}

        for agent_cfg in self.cfg["agents"]:
            symbol = agent_cfg["symbol"]
            market = self.markets[symbol]

            # Instrument metadata кэшируется per-symbol (12 агентов → 1 fetch)
            if symbol not in instrument_cache:
                instrument_cache[symbol] = metadata.get_instrument(
                    symbol=symbol,
                    category=self.category,
                    maker_fee=agent_cfg.get("maker_fee"),
                    taker_fee=agent_cfg.get("taker_fee"),
                )
            instrument = instrument_cache[symbol]

            session = PaperSession(agent_cfg["name"], instrument, start_usdt)
            agent = Agent(agent_cfg, market, session)
            self.agents.append(agent)

            self.events.info(
                f"[SYSTEM] Agent {agent_cfg['name']} ready: "
                f"strategy={agent_cfg['signal']} symbol={symbol} qty={agent_cfg['qty']}"
            )

    async def keep_alive(self, ws: ClientConnection) -> None:
        """
        Bybit требует ping каждые ~20s.
        При ошибке — закрывает WS, чтобы ws_loop сделал реконнект.
        """
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send(json.dumps({
                    "req_id": str(int(time.time())),
                    "op": "ping",
                }))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.events.error(f"[SYSTEM] heartbeat error: {e}, closing WS")
            try:
                await ws.close()
            except Exception:
                pass

    async def ws_loop(self) -> None:
        """
        Подписывается на orderbook.50.<symbol> для всех символов.
        Exponential backoff при реконнекте (3s → 6s → 12s → ... → max 60s).
        keep_alive task отменяется при каждом реконнекте (без leak).
        """
        url = "wss://stream.bybit.com/v5/public/linear"

        # Формируем подписки (publicTrade не используется — не подписываемся)
        args = [f"orderbook.50.{sym}" for sym in self.symbols]

        backoff: int = 3
        reconnect_count: int = 0
        ka_task: asyncio.Task[None] | None = None

        while True:
            try:
                async with websockets.connect(url, ping_interval=None) as ws:
                    # Отменить предыдущий keep_alive
                    if ka_task is not None and not ka_task.done():
                        ka_task.cancel()
                    ka_task = asyncio.create_task(self.keep_alive(ws))

                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    reconnect_count += 1
                    self.events.info(
                        f"[SYSTEM] WS connected (#{reconnect_count}), "
                        f"subscribed to {len(args)} channels"
                    )
                    backoff = 3  # reset on successful connect

                    async for raw in ws:
                        await self._handle_message(raw)
            except asyncio.CancelledError:
                if ka_task is not None:
                    ka_task.cancel()
                raise
            except Exception as e:
                self.events.error(
                    f"[SYSTEM] WS error: {e}. Reconnecting in {backoff}s."
                )
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)

    async def _handle_message(self, raw: str) -> None:
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            return

        topic = data.get("topic", "")
        if not topic:
            return

        now = time.time()

        # orderbook.50.<symbol>
        if topic.startswith("orderbook.50."):
            symbol = topic.split(".", 2)[2]
            market = self.markets.get(symbol)
            if market is None:
                return
            payload = data.get("data", {})
            if not payload.get("b") and not payload.get("a"):
                return
            is_snapshot = data.get("type") == "snapshot"
            market.apply_orderbook(payload, is_snapshot)
            if not market.tick(now):
                return

            # Все агенты этого символа обрабатывают тик
            for agent in self.agents:
                if agent.market is market:
                    try:
                        agent.on_tick(now)
                    except Exception as e:
                        self.events.error(f"[{agent.name}] tick error: {e}")
            return

    async def status_loop(self, interval: int = 300) -> None:
        """Периодический статус всех агентов."""
        while True:
            await asyncio.sleep(interval)
            parts: list[str] = []
            for a in self.agents:
                snap = a.snapshot()
                parts.append(
                    f"{snap['name']}=${snap['usdt']:.2f}"
                    f"[{snap['position_side']}:{snap['position_size']}]"
                    f"o{snap['open_orders']}"
                )
            self.events.info(f"[STATUS] {' | '.join(parts)}")

    def shutdown(self) -> None:
        """Логирует финальный snapshot каждого агента. Идемпотентно."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.events.info("[SYSTEM] SHUTDOWN: capturing final snapshots...")
        for agent in self.agents:
            try:
                agent.final_snapshot()
            except Exception as e:
                self.events.error(f"[SYSTEM] final_snapshot error for {agent.name}: {e}")
        self.events.info("[SYSTEM] SHUTDOWN: snapshots saved.")

    async def run(self) -> None:
        self.events.info("[SYSTEM] Harness v3 starting async tasks...")
        try:
            await asyncio.gather(
                self.ws_loop(),
                self.status_loop(interval=300),
            )
        except asyncio.CancelledError:
            self.events.info("[SYSTEM] Tasks cancelled, running shutdown sequence...")
            self.shutdown()
            raise
        finally:
            # Гарантия: если gather завершился нормально (без cancel), всё равно shutdown
            self.shutdown()


async def _run_with_signals(h: Harness) -> None:
    """Запускает Harness, реагирует на SIGTERM/SIGINT, чистит handlers."""
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    stop_event: asyncio.Event = asyncio.Event()
    installed: list[str] = []

    def _handle_signal(sig_name: str):
        print(f"\n[harness] Signal {sig_name} received, stopping...")
        stop_event.set()

    for sig_name in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(
                getattr(signal, sig_name), _handle_signal, sig_name
            )
            installed.append(sig_name)
        except (AttributeError, NotImplementedError):
            pass

    try:
        main_task = asyncio.create_task(h.run())
        stop_task = asyncio.create_task(stop_event.wait())

        done, _ = await asyncio.wait(
            [main_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done and not main_task.done():
            main_task.cancel()
            try:
                await main_task
            except asyncio.CancelledError:
                pass

        if main_task.done() and not main_task.cancelled():
            try:
                await main_task
            except Exception as e:
                print(f"[harness] Fatal error: {e}")
    finally:
        for sig_name in installed:
            try:
                loop.remove_signal_handler(getattr(signal, sig_name))
            except (AttributeError, NotImplementedError):
                pass


def main() -> None:
    parser: ArgumentParser = ArgumentParser(description="Bybit paper trading harness")
    parser.add_argument(
        "--config", "-c",
        default=str(config.DEFAULT_CONFIG),
        help="Path to JSON config",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config)
    print(f"[harness] Loading config: {cfg_path}")
    cfg = config.load_config(cfg_path)

    _startup_check()

    print(f"[harness] Building {len(cfg['agents'])} agents...")
    h = Harness(cfg)
    print(f"[harness] Ready. Symbols: {h.symbols}. Agents: {len(h.agents)}.")
    print("[harness] Trading in PAPER mode. Ctrl+C to stop.")
    print("[harness] Logs: logs/events.log + logs/trade_*.log")
    print()

    try:
        asyncio.run(_run_with_signals(h))
    except KeyboardInterrupt:
        print("\n[harness] Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
