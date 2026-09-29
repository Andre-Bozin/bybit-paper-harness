"""
deep_analysis.py — глубокий анализ логов завершённого прогона.
Проверяет 6 инвариантов, считает exit-type distribution и time-to-close.

Использование:
    python3 -m scripts.deep_analysis
    python3 -m scripts.deep_analysis --archive archive/final_run_20260927
"""
from __future__ import annotations

import json
import statistics
from argparse import ArgumentParser
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, TypedDict

from src import config


# Path to archive root
ARCHIVE_DIR: Path = config.PROJECT_ROOT / "archive"


# --- TypedDict structures ---

class Trade(TypedDict):
    """Реконструированная сделка: ENTRY + exit event."""
    entry_ts: float | None
    side: str | None
    entry_price: float | None
    tp: float | None
    sl: float | None
    sigma_pct: float | None
    obi: float | None
    exit_ts: float | None
    exit_type: str | None
    usdt_at_close: float | None


class AgentResult(TypedDict):
    """Метрики одного агента после deep analysis."""
    name: str
    symbol: str
    n_trades: int
    exit_types: dict[str, int]
    median_time_all: float | None
    median_time_by_type: dict[str, float]
    median_sigma: float | None
    mean_obi: float | None
    errors: list[str]


def find_latest_archive() -> Path:  # noqa: D401
    """Ищет последний archive/final_run_* по имени."""
    if not ARCHIVE_DIR.exists():
        raise FileNotFoundError(f"No archive dir: {ARCHIVE_DIR}")
    candidates = sorted(ARCHIVE_DIR.glob("final_run_*"))
    if not candidates:
        raise FileNotFoundError(f"No final_run_* in {ARCHIVE_DIR}")
    return candidates[-1]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Читает JSON-lines, игнорирует мусор, сортирует по ts."""
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
    events.sort(key=lambda e: e.get("ts", 0))
    return events


def list_agents(logs_dir: Path) -> list[str]:
    """Список агентов по trade_*.log (slice префикса, не replace)."""
    prefix = "trade_"
    names = []
    for f in logs_dir.glob(f"{prefix}*.log"):
        names.append(f.stem[len(prefix):])
    return sorted(names)


def reconstruct_trades(trade_events: list[dict[str, Any]]) -> list[Trade]:
    """
    Пара (ENTRY → exit) формирует одну сделку.
    exit = STOP_LOSS | HARD_KILL | DECAY_EXIT | CLOSE_DETECTED
    """
    trades = []
    current = None
    for e in trade_events:
        ev = e.get("event")
        if ev == "ENTRY":
            current = {
                "entry_ts": e.get("ts"),
                "side": e.get("side"),
                "entry_price": e.get("price"),
                "tp": e.get("tp"),
                "sl": e.get("sl"),
                "sigma_pct": e.get("sigma_pct"),
                "obi": e.get("obi"),
                "exit_ts": None,
                "exit_type": None,
                "usdt_at_close": None,
            }
        elif ev in ("STOP_LOSS", "HARD_KILL", "DECAY_EXIT") and current:
            current["exit_ts"] = e.get("ts")
            current["exit_type"] = ev.lower()
            trades.append(current)
            current = None
        elif ev == "CLOSE_DETECTED" and current:
            current["exit_ts"] = e.get("ts")
            current["exit_type"] = "tp"
            current["usdt_at_close"] = e.get("usdt")
            trades.append(current)
            current = None
    return trades


def check_invariants(
    trades: list[Trade],
    trade_events: list[dict[str, Any]],
    agent_log: list[dict[str, Any]],
    start_balance: float,
) -> list[str]:
    """Возвращает список нарушенных инвариантов.
    trade_events — сырые события из trade_*.log (для FINAL_SNAPSHOT).
    agent_log — события из agent_*.log (для fee/pnl)."""
    errors = []

    for i, t in enumerate(trades, 1):
        # I1: направление TP/SL
        side, entry = t["side"], t["entry_price"]
        sl, tp = t["sl"], t["tp"]
        if side == "Buy" and not (sl < entry < tp):
            errors.append(f"trade#{i} I1: Buy sl={sl} entry={entry} tp={tp}")
        if side == "Sell" and not (sl > entry > tp):
            errors.append(f"trade#{i} I1: Sell sl={sl} entry={entry} tp={tp}")

        # I2: exit_time > entry_time
        if t["exit_ts"] and t["exit_ts"] < t["entry_ts"]:
            errors.append(f"trade#{i} I2: exit before entry")

        # I3: время > 0 и < разумного предела
        if t["exit_ts"]:
            dt = t["exit_ts"] - t["entry_ts"]
            if dt <= 0 or dt > 3600:
                errors.append(f"trade#{i} I3: time-to-close={dt:.0f}s")

    # I4: opens == closes (проверка на уровне агента)
    # I5: арифметика баланса (использует FINAL_SNAPSHOT если есть)
    total_fee = sum(e.get("fee", 0) for e in agent_log if "fee" in e)
    total_pnl = sum(
        e.get("pnl", 0) for e in agent_log
        if e.get("event") in ("CLOSE", "REDUCE") and "pnl" in e
    )
    net_calc = total_pnl - total_fee

    # Приоритет: FINAL_SNAPSHOT (точное состояние на момент shutdown)
    # Fallback: последний CLOSE_DETECTED (может быть неточным из-за открытой позиции)
    final_usdt = None
    final_source = None
    for e in reversed(trade_events):
        if e.get("event") == "FINAL_SNAPSHOT" and "usdt" in e:
            final_usdt = e["usdt"]
            final_source = "final_snapshot"
            break

    if final_usdt is None:
        # Старые логи без FINAL_SNAPSHOT — fallback на CLOSE_DETECTED
        for t in reversed(trades):
            if t.get("usdt_at_close") is not None:
                final_usdt = t["usdt_at_close"]
                final_source = "close_detected_fallback"
                break

    if final_usdt is not None:
        actual_delta = final_usdt - start_balance
        # Допуск 0.05 только для fallback (FINAL_SNAPSHOT должен точно совпадать)
        tolerance = 0.05 if final_source == "close_detected_fallback" else 0.001
        if abs(actual_delta - net_calc) > tolerance:
            errors.append(
                f"I5 accounting drift ({final_source}): actual={actual_delta:+.4f}, "
                f"calc={net_calc:+.4f}, diff={actual_delta - net_calc:+.4f}"
            )

    # I6: exit_type известен всегда
    for i, t in enumerate(trades, 1):
        if t["exit_type"] is None:
            errors.append(f"trade#{i} I6: exit_type unknown")

    return errors


def analyze_agent(
    name: str,
    logs_dir: Path,
    start_balance: float,
) -> AgentResult:
    trade_log: list[dict[str, Any]] = read_jsonl(logs_dir / f"trade_{name}.log")
    agent_log: list[dict[str, Any]] = read_jsonl(logs_dir / f"agent_{name}.log")

    trades = reconstruct_trades(trade_log)
    errors = check_invariants(trades, trade_log, agent_log, start_balance)

    # Exit-type distribution
    exit_types = Counter(t["exit_type"] for t in trades if t["exit_type"])
    n = len(trades)

    # Time-to-close
    times_all = []
    times_by_type = defaultdict(list)
    for t in trades:
        if t["exit_ts"] and t["entry_ts"]:
            dt = t["exit_ts"] - t["entry_ts"]
            times_all.append(dt)
            if t["exit_type"]:
                times_by_type[t["exit_type"]].append(dt)

    # Entry conditions
    sigmas = [t["sigma_pct"] for t in trades if t["sigma_pct"] is not None]
    obis = [t["obi"] for t in trades if t["obi"] is not None]

    return {
        "name": name,
        "symbol": "",   # заполняется в main() из config.json
        "n_trades": n,
        "exit_types": dict(exit_types),
        "median_time_all": statistics.median(times_all) if times_all else None,
        "median_time_by_type": {
            k: statistics.median(v) for k, v in times_by_type.items() if v
        },
        "median_sigma": statistics.median(sigmas) if sigmas else None,
        "mean_obi": statistics.mean(obis) if obis else None,
        "errors": errors,
    }


def format_report(results: list[AgentResult], archive_name: str) -> str:
    lines: list[str] = []
    lines.append("=" * 100)
    lines.append(f"DEEP ANALYSIS — {archive_name}")
    lines.append("=" * 100)
    lines.append("")

    # Aggregates
    total_trades: int = 0
    total_exit_types: Counter[str] = Counter()
    total_times: list[float] = []

    for r in results:
        symbol = r.get("symbol", "?")
        lines.append(f"### Agent: {r['name']} ({symbol})")
        lines.append(f"  Trades reconstructed: {r['n_trades']}")

        if r["exit_types"]:
            exit_str = ", ".join(f"{k}={v}" for k, v in sorted(r["exit_types"].items()))
            lines.append(f"  Exit-type distribution: {exit_str}")
            total_exit_types.update(r["exit_types"])
        else:
            lines.append("  Exit-type distribution: (none)")

        if r["median_time_all"] is not None:
            lines.append(f"  Median time-to-close: {r['median_time_all']:.1f}s")
        else:
            lines.append("  Median time-to-close: n/a")

        for etype, mt in sorted(r["median_time_by_type"].items()):
            lines.append(f"    {etype:12} → median {mt:.1f}s")

        if r["median_sigma"] is not None:
            lines.append(f"  Median σ% on entry: {r['median_sigma']:.4f}")
        else:
            lines.append("  Median σ% on entry: n/a")

        if r["mean_obi"] is not None:
            lines.append(f"  Mean OBI on entry: {r['mean_obi']:+.3f}")
        else:
            lines.append("  Mean OBI on entry: n/a")

        if r["errors"]:
            lines.append(f"  ⚠️  Invariant violations: {len(r['errors'])}")
            for err in r["errors"][:5]:
                lines.append(f"    - {err}")
            if len(r["errors"]) > 5:
                lines.append(f"    ... and {len(r['errors']) - 5} more")
        else:
            lines.append(f"  ✅ All invariants OK")
        lines.append("")

        total_trades += r["n_trades"]
        if r["median_time_all"] is not None:
            total_times.append(r["median_time_all"])

    # Aggregate summary
    lines.append("=" * 100)
    lines.append(f"TOTAL TRADES: {total_trades}")
    if total_exit_types:
        agg = ", ".join(f"{k}={v}" for k, v in sorted(total_exit_types.items()))
        lines.append(f"TOTAL exit types: {agg}")
    if total_times:
        lines.append(f"Median of medians (time-to-close): {statistics.median(total_times):.1f}s")

    total_errors = sum(len(r["errors"]) for r in results)
    lines.append(f"TOTAL INVARIANT VIOLATIONS: {total_errors}")
    lines.append("=" * 100)

    return "\n".join(lines)


def main() -> None:
    """
    Читает последний (или указанный) archive/final_run_*, проверяет
    инварианты, генерирует отчёт в reports/deep_analysis.txt.

    CLI:
        python3 -m scripts.deep_analysis
        python3 -m scripts.deep_analysis --archive archive/final_run_20260927
    """
    parser: ArgumentParser = ArgumentParser(
        description="Deep analysis of a completed harness run"
    )
    parser.add_argument(
        "--archive", "-a",
        default=None,
        help="Path to archive/final_run_* (default: latest)",
    )
    args = parser.parse_args()

    if args.archive:
        archive = Path(args.archive).resolve()
        if not archive.exists():
            print(f"[deep_analysis] ERROR: archive not found: {archive}")
            return
    else:
        archive = find_latest_archive()

    logs_dir = archive / "logs"
    if not logs_dir.exists():
        print(f"[deep_analysis] ERROR: no logs dir in {archive}")
        return

    print(f"[deep_analysis] Archive: {archive.name}")
    print(f"[deep_analysis] Logs dir: {logs_dir}")
    print()

    # Config из archive (start_balance + symbol map)
    start_balance: float = 1000.0
    symbol_map: dict[str, str] = {}
    cfg_path: Path = archive / "config.json"
    if cfg_path.exists():
        try:
            cfg: dict[str, Any] = json.loads(cfg_path.read_text(encoding="utf-8"))
            start_balance = float(cfg.get("meta", {}).get("start_balance", 1000.0))
            symbol_map = {
                a.get("name", "?"): a.get("symbol", "?")
                for a in cfg.get("agents", [])
            }
        except json.JSONDecodeError as e:
            print(f"[deep_analysis] WARN: cannot parse config.json: {e}")

    agents: list[str] = list_agents(logs_dir)
    print(f"[deep_analysis] Agents: {agents}")
    print()

    results: list[AgentResult] = []
    for name in agents:
        r: AgentResult = analyze_agent(name, logs_dir, start_balance)
        r["symbol"] = symbol_map.get(name, "?")
        results.append(r)

    report = format_report(results, archive.name)
    print(report)

    out_path = config.REPORTS_DIR / "deep_analysis.txt"
    out_path.write_text(report, encoding="utf-8")
    print(f"\n[deep_analysis] Report saved: {out_path}")


if __name__ == "__main__":
    main()
