"""
harness.py — Multi-agent paper trading orchestrator.
Один WebSocket, N MarketState (по одному на символ), M Agent'ов.
"""
import asyncio
import json
import logging
import sys
import time
from argparse import ArgumentParser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import websockets

from src import config, metadata
from src.market import MarketState
from src.session import PaperSession
from src.agent import Agent


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


class Harness:
    """Главный orchestrator."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.events = setup_events_logger()
        self.meta = config.get_meta(cfg) if hasattr(config, "get_meta") else cfg["meta"]
        self.symbols = config.get_symbols(cfg)
        self.category = config.get_category(cfg)

        self.markets = {}   # symbol -> MarketState
        self.agents = []    # list of Agent

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
        for agent_cfg in self.cfg["agents"]:
            symbol = agent_cfg["symbol"]
            market = self.markets[symbol]

            # Получаем instrument metadata (с кэшем)
            instrument = metadata.get_instrument(
                symbol=symbol,
                category=self.category,
                maker_fee=agent_cfg.get("maker_fee"),
                taker_fee=agent_cfg.get("taker_fee"),
            )

            # Session с initial balance из config
            start_usdt = float(self.cfg["meta"].get("start_balance", 1000.0))
            session = PaperSession(agent_cfg["name"], instrument, start_usdt)
            agent = Agent(agent_cfg, market, session)
            self.agents.append(agent)

            self.events.info(
                f"[SYSTEM] Agent {agent_cfg['name']} ready: "
                f"strategy={agent_cfg['signal']} symbol={symbol} qty={agent_cfg['qty']}"
            )

    async def keep_alive(self, ws) -> None:
        """Bybit требует ping каждые ~20s."""
        while True:
            try:
                await asyncio.sleep(20)
                await ws.send(json.dumps({
                    "req_id": str(int(time.time())),
                    "op": "ping",
                }))
            except Exception as e:
                self.events.error(f"[SYSTEM] heartbeat error: {e}")
                break

    async def ws_loop(self) -> None:
        """Подписки на orderbook + publicTrade для всех символов."""
        url = "wss://stream.bybit.com/v5/public/linear"

        # Формируем подписки
        args = []
        for sym in self.symbols:
            args.append(f"orderbook.50.{sym}")
            args.append(f"publicTrade.{sym}")

        while True:
            try:
                async with websockets.connect(url, ping_interval=None) as ws:
                    asyncio.create_task(self.keep_alive(ws))
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    self.events.info(f"[SYSTEM] WS connected, subscribed to {len(args)} channels")

                    async for raw in ws:
                        await self._handle_message(raw)
            except Exception as e:
                self.events.error(f"[SYSTEM] WS error: {e}. Reconnecting in 3s.")
                await asyncio.sleep(3)

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        topic = data.get("topic", "")
        if not topic:
            return

        now = time.time()

        # publicTrade.<symbol>
        if topic.startswith("publicTrade."):
            symbol = topic.split(".", 1)[1]
            market = self.markets.get(symbol)
            if market is None:
                return
            market.add_trades(data.get("data", []), now)
            return

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
            parts = []
            for a in self.agents:
                snap = a.snapshot()
                parts.append(
                    f"{snap['name']}=${snap['usdt']:.2f}"
                    f"[{snap['position_side']}:{snap['position_size']}]"
                    f"o{snap['open_orders']}"
                )
            self.events.info(f"[STATUS] {' | '.join(parts)}")

    async def run(self) -> None:
        self.events.info("[SYSTEM] Harness v3 starting async tasks...")
        await asyncio.gather(
            self.ws_loop(),
            self.status_loop(interval=300),
        )


def main():
    parser = ArgumentParser(description="Bybit paper trading harness")
    parser.add_argument(
        "--config", "-c",
        default=str(config.DEFAULT_CONFIG),
        help="Path to JSON config",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config)
    print(f"[harness] Loading config: {cfg_path}")
    cfg = config.load_config(cfg_path)

    print(f"[harness] Building {len(cfg['agents'])} agents...")
    h = Harness(cfg)
    print(f"[harness] Ready. Symbols: {h.symbols}. Agents: {len(h.agents)}.")
    print("[harness] Trading in PAPER mode. Ctrl+C to stop.")
    print("[harness] Logs: logs/events.log + logs/trade_*.log")
    print()

    try:
        asyncio.run(h.run())
    except KeyboardInterrupt:
        print("\n[harness] Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
