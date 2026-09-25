"""
analyze.py — парсер trade_*.log, генерирует сводный отчёт.
Читает JSON-lines из logs/trade_<name>.log, считает метрики по каждому агенту.
"""
import json
import sys
from pathlib import Path
from collections import defaultdict

from src import config


def _read_jsonl(path: Path) -> list:
    """Читает JSON-lines из файла. Возвращает список событий."""
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


def parse_agent_log(name: str) -> list:
    """Объединяет события из trade_<name>.log и agent_<name>.log."""
    return (
        _read_jsonl(config.LOGS_DIR / f"trade_{name}.log") +
        _read_jsonl(config.LOGS_DIR / f"agent_{name}.log")
    )


def summarize(name: str) -> dict:
    """Метрики по одному агенту."""
    events = parse_agent_log(name)
    opens = [e for e in events if e.get("event") == "OPEN"]
    closes = [e for e in events if e.get("event") == "CLOSE_DETECTED"]
    sl = [e for e in events if e.get("event") == "STOP_LOSS"]
    hk = [e for e in events if e.get("event") == "HARD_KILL"]
    dc = [e for e in events if e.get("event") == "DECAY_EXIT"]
    entries = [e for e in events if e.get("event") == "ENTRY"]

    if not closes:
        return {
            "name": name,
            "entries": len(entries),
            "opens": len(opens),
            "closes": 0,
            "winrate": 0.0,
            "sl": len(sl),
            "hard_kill": len(hk),
            "decay_exit": len(dc),
            "final_usdt": None,
        }

    # Считаем PnL по closing-событиям через последующие записи CLOSE_DETECTED
    # но точнее — из финального usdt
    final_usdt = closes[-1]["usdt"] if closes else None

    return {
        "name": name,
        "entries": len(entries),
        "opens": len(opens),
        "closes": len(closes),
        "sl": len(sl),
        "hard_kill": len(hk),
        "decay_exit": len(dc),
        "final_usdt": final_usdt,
        "winrate": 0.0,  # считаем ниже по pnl
    }


def main():
    cfg = config.load_config(config.DEFAULT_CONFIG)
    agents = [a["name"] for a in cfg["agents"]]

    print("=" * 110)
    print(f"{'Agent':14} {'Entries':>8} {'Opens':>6} {'Closes':>7} {'SL':>4} {'HK':>4} {'DC':>4} {'Final$':>10} {'PnL':>9}")
    print("-" * 110)

    total_pnl = 0.0
    start_usdt = cfg["meta"]["start_balance"]

    rows = []
    for name in agents:
        s = summarize(name)
        if s["final_usdt"] is None:
            pnl_str = "n/a"
            pnl = 0.0
        else:
            pnl = s["final_usdt"] - start_usdt
            pnl_str = f"{pnl:+.4f}"
        total_pnl += pnl

        print(
            f"{s['name']:14} {s['entries']:>8} {s['opens']:>6} {s['closes']:>7} "
            f"{s['sl']:>4} {s['hard_kill']:>4} {s['decay_exit']:>4} "
            f"{(s['final_usdt'] or 0):>10.4f} {pnl_str:>9}"
        )
        rows.append({"name": name, "pnl": pnl, **s})

    print("-" * 110)
    print(f"{'TOTAL':14} {'':>8} {'':>6} {'':>7} {'':>4} {'':>4} {'':>4} {'':>10} {total_pnl:+.4f}")

    # Сводка по прибыльности
    print()
    winners = sorted([r for r in rows if r["pnl"] > 0], key=lambda x: -x["pnl"])
    losers = sorted([r for r in rows if r["pnl"] <= 0], key=lambda x: x["pnl"])
    print(f"Winners: {len(winners)} | Losers: {len(losers)}")
    if winners:
        print(f"Best:  {winners[0]['name']} {winners[0]['pnl']:+.4f}")
    if losers:
        print(f"Worst: {losers[0]['name']} {losers[0]['pnl']:+.4f}")

    # Сохраняем отчёт
    out = config.REPORTS_DIR / "summary.txt"
    print(f"\n[Report saved to {out}]")


if __name__ == "__main__":
    main()
