"""
analyze.py — парсер логов harness, генерирует сводный отчёт.

Читает JSON-lines из logs/trade_<name>.log и logs/agent_<name>.log.
Считает метрики по каждому агенту:
- entries (из ENTRY)
- closes (из CLOSE_DETECTED)
- winrate (по CLOSE/REDUCE pnl)
- SL/HARD_KILL/DECAY_EXIT counts
- final_usdt (приоритет FINAL_SNAPSHOT, fallback CLOSE_DETECTED)
- PnL относительно start_balance
- разбивка closes: TP (limit) vs market

Использование:
    python3 -m src.analyze
    python3 -m src.analyze --config configs/12agents_doge.json
"""
from __future__ import annotations

import json
from argparse import ArgumentParser
from pathlib import Path
from typing import Any, TypedDict

from src import config


# --- TypedDict structures ---

class AgentSummary(TypedDict):
    """Метрики одного агента после парсинга логов."""
    name: str
    entries: int
    closes: int
    closed_trades: int
    wins: int
    losses: int
    winrate: float
    sl: int
    hard_kill: int
    decay_exit: int
    close_failed: int
    place_rejected: int
    limit_closes: int
    market_closes: int
    final_usdt: float | None
    final_source: str | None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Читает JSON-lines, пропускает невалидные строки."""
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def parse_agent_log(name: str) -> list[dict[str, Any]]:
    """Объединяет события из trade_<name>.log и agent_<name>.log, сортирует по ts."""
    events = (
        _read_jsonl(config.LOGS_DIR / f"trade_{name}.log") +
        _read_jsonl(config.LOGS_DIR / f"agent_{name}.log")
    )
    events.sort(key=lambda e: e.get("ts", 0))
    return events


def summarize(name: str, start_usdt: float) -> AgentSummary:
    """Считает метрики по одному агенту."""
    events: list[dict[str, Any]] = parse_agent_log(name)

    entries: list[dict[str, Any]] = [e for e in events if e.get("event") == "ENTRY"]
    closes_detected = [e for e in events if e.get("event") == "CLOSE_DETECTED"]
    sl = [e for e in events if e.get("event") == "STOP_LOSS"]
    hk = [e for e in events if e.get("event") == "HARD_KILL"]
    dc = [e for e in events if e.get("event") == "DECAY_EXIT"]
    close_failed = [e for e in events if e.get("event") == "CLOSE_FAILED"]
    place_rejected = [e for e in events if e.get("event") == "PLACE_REJECTED"]

    # Closed trades (для winrate) — из PaperSession (agent_*.log)
    closed_trades = [
        e for e in events
        if e.get("event") in ("CLOSE", "REDUCE") and "pnl" in e
    ]
    wins = [e for e in closed_trades if e.get("pnl", 0) > 0]
    losses = [e for e in closed_trades if e.get("pnl", 0) <= 0]
    winrate = (len(wins) / len(closed_trades) * 100.0) if closed_trades else 0.0

    # Разбивка причин закрытия (limit vs market)
    limit_closes = [e for e in closed_trades if e.get("src") != "market"]
    market_closes = [e for e in closed_trades if e.get("src") == "market"]

    # final_usdt: приоритет FINAL_SNAPSHOT, fallback CLOSE_DETECTED
    final_usdt = None
    final_source = None
    for e in reversed(events):
        if e.get("event") == "FINAL_SNAPSHOT" and "usdt" in e:
            final_usdt = e["usdt"]
            final_source = "final_snapshot"
            break
    if final_usdt is None and closes_detected:
        final_usdt = closes_detected[-1].get("usdt")
        final_source = "close_detected"

    return {
        "name": name,
        "entries": len(entries),
        "closes": len(closes_detected),
        "closed_trades": len(closed_trades),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": winrate,
        "sl": len(sl),
        "hard_kill": len(hk),
        "decay_exit": len(dc),
        "close_failed": len(close_failed),
        "place_rejected": len(place_rejected),
        "limit_closes": len(limit_closes),
        "market_closes": len(market_closes),
        "final_usdt": final_usdt,
        "final_source": final_source,
    }


def build_report(cfg: dict[str, Any]) -> str:
    """Строит текстовый отчёт. Возвращает строку."""
    start_usdt: float = float(cfg["meta"].get("start_balance", 1000.0))
    agents: list[dict[str, Any]] = cfg["agents"]

    lines: list[str] = []
    lines.append("=" * 130)
    lines.append(
        f"{'Agent':16} {'Symbol':10} {'Ent':>5} {'Cls':>5} {'WR%':>6} "
        f"{'SL':>4} {'HK':>4} {'DC':>4} {'CF':>4} "
        f"{'Limit':>6} {'Mkt':>5} {'Final$':>10} {'PnL':>10}"
    )
    lines.append("-" * 130)

    total_pnl = 0.0
    total_entries = 0
    total_closes = 0
    total_sl = 0
    total_hk = 0
    total_dc = 0
    total_limit = 0
    total_market = 0

    rows: list[dict[str, Any]] = []
    for a in agents:
        name: str = a["name"]
        symbol: str = a.get("symbol", "?")
        s: AgentSummary = summarize(name, start_usdt)

        pnl = (s["final_usdt"] - start_usdt) if s["final_usdt"] is not None else 0.0
        pnl_str = f"{pnl:+.4f}" if s["final_usdt"] is not None else "n/a"
        final_str = f"{s['final_usdt']:.4f}" if s["final_usdt"] is not None else "n/a"

        lines.append(
            f"{name:16} {symbol:10} {s['entries']:>5} {s['closes']:>5} "
            f"{s['winrate']:>5.1f}% {s['sl']:>4} {s['hard_kill']:>4} "
            f"{s['decay_exit']:>4} {s['close_failed']:>4} "
            f"{s['limit_closes']:>6} {s['market_closes']:>5} "
            f"{final_str:>10} {pnl_str:>10}"
        )

        total_pnl += pnl
        total_entries += s["entries"]
        total_closes += s["closes"]
        total_sl += s["sl"]
        total_hk += s["hard_kill"]
        total_dc += s["decay_exit"]
        total_limit += s["limit_closes"]
        total_market += s["market_closes"]

        rows.append({"name": name, "symbol": symbol, "pnl": pnl, **s})

    lines.append("-" * 130)
    lines.append(
        f"{'TOTAL':16} {'':10} {total_entries:>5} {total_closes:>5} "
        f"{'':>6} {total_sl:>4} {total_hk:>4} {total_dc:>4} {'':>4} "
        f"{total_limit:>6} {total_market:>5} {'':>10} {total_pnl:+.4f}"
    )
    lines.append("")

    # Сводка
    with_pnl = [r for r in rows if r["final_usdt"] is not None]
    winners = sorted([r for r in with_pnl if r["pnl"] > 0], key=lambda x: -x["pnl"])
    losers = sorted([r for r in with_pnl if r["pnl"] <= 0], key=lambda x: x["pnl"])
    lines.append(f"Winners: {len(winners)} | Losers: {len(losers)} | Agents: {len(rows)}")
    if winners:
        lines.append(f"Best:  {winners[0]['name']} ({winners[0]['symbol']}) {winners[0]['pnl']:+.4f}")
    if losers:
        lines.append(f"Worst: {losers[0]['name']} ({losers[0]['symbol']}) {losers[0]['pnl']:+.4f}")

    return "\n".join(lines)


def main() -> None:
    parser: ArgumentParser = ArgumentParser(description="Analyze harness logs")
    parser.add_argument(
        "--config", "-c",
        default=str(config.DEFAULT_CONFIG),
        help="Path to JSON config (must match the harness run)",
    )
    args = parser.parse_args()

    cfg = config.load_config(Path(args.config))
    report = build_report(cfg)
    print(report)

    # Сохраняем в reports/summary.txt
    out = config.REPORTS_DIR / "summary.txt"
    out.write_text(report + "\n", encoding="utf-8")
    print(f"\n[Report saved to {out}]")


if __name__ == "__main__":
    main()
