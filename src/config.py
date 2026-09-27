"""
config.py — конфигурация проекта.
Единая точка для путей, загрузки конфига, defaults.
"""
import json
from pathlib import Path

# --- Project paths ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
CONFIGS_DIR = PROJECT_ROOT / "configs"
LOGS_DIR = PROJECT_ROOT / "logs"
REPORTS_DIR = PROJECT_ROOT / "reports"
CACHE_DIR = CONFIGS_DIR / "cache"

# Runtime dirs
for d in (LOGS_DIR, REPORTS_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- Defaults ---
DEFAULT_MAKER_FEE = 0.0002
DEFAULT_TAKER_FEE = 0.00055
DEFAULT_CATEGORY = "linear"  # linear perps
METADATA_CACHE_TTL = 86400   # 24h
FEES_CACHE_TTL = 3600        # 1h
DEFAULT_CONFIG = CONFIGS_DIR / "example_doge.json"  # public default


VALID_CATEGORIES = {"linear", "inverse", "spot"}


def load_config(path: Path = None) -> dict:
    """
    Загружает JSON-конфиг, валидирует структуру и значения агентов.

    Поднимает ValueError с понятным сообщением при:
    - отсутствии meta/agents
    - неверной category
    - отсутствии name/signal/symbol у агента
    - дубликатах name
    - неизвестной signal
    - неположительном qty
    """
    # Отложенный импорт — избегаем cyclic import (strategies не зависит от config)
    from src import strategies

    path = Path(path) if path else DEFAULT_CONFIG
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # --- Structure validation ---
    if "meta" not in raw or "agents" not in raw:
        raise ValueError("Config must have 'meta' and 'agents'")
    if not isinstance(raw["agents"], list) or not raw["agents"]:
        raise ValueError("'agents' must be non-empty list")

    # --- Meta defaults ---
    meta = dict(raw["meta"])  # копия, не мутируем оригинал
    meta.setdefault("category", DEFAULT_CATEGORY)
    meta.setdefault("start_balance", 1000.0)
    meta.setdefault("maker_fee", DEFAULT_MAKER_FEE)
    meta.setdefault("taker_fee", DEFAULT_TAKER_FEE)

    # Category whitelist
    if meta["category"] not in VALID_CATEGORIES:
        raise ValueError(
            f"Invalid category '{meta['category']}'. "
            f"Allowed: {sorted(VALID_CATEGORIES)}"
        )

    # --- Agents validation + defaults ---
    valid_strategies = set(strategies.STRATEGIES.keys())
    seen_names = set()
    agents = []

    for i, a in enumerate(raw["agents"]):
        a = dict(a)  # копия
        label = a.get("name") or f"#{i}"

        # name
        name = a.get("name")
        if not name or not isinstance(name, str):
            raise ValueError(f"Agent {label}: 'name' must be non-empty string")
        if name in seen_names:
            raise ValueError(f"Agent {label}: duplicate name '{name}'")
        seen_names.add(name)

        # signal
        sig = a.get("signal")
        if sig not in valid_strategies:
            raise ValueError(
                f"Agent '{name}': unknown signal '{sig}'. "
                f"Allowed: {sorted(valid_strategies)}"
            )

        # symbol
        sym = a.get("symbol") or meta.get("symbol")
        if not sym or not isinstance(sym, str):
            raise ValueError(f"Agent '{name}': missing 'symbol'")
        a["symbol"] = sym

        # qty
        qty = a.get("qty")
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
            raise ValueError(
                f"Agent '{name}': qty must be positive number, got {qty!r}"
            )

        # Apply other defaults
        a.setdefault("obi_threshold", 0.65)
        a.setdefault("min_std_dev", 0.05)
        a.setdefault("tp_sl_ratio", 2.0)
        a.setdefault("decay_start", 180)
        a.setdefault("decay_duration", 600)
        a.setdefault("hard_kill", 900)
        a.setdefault("sl_cooldown", 300)

        agents.append(a)

    # meta.symbol остаётся как fallback для будущих агентов, но не обязателен
    meta.setdefault("symbol", agents[0]["symbol"])

    return {"meta": meta, "agents": agents}


def get_symbols(cfg: dict) -> list:
    """Уникальные символы среди всех агентов."""
    return sorted({a["symbol"] for a in cfg["agents"]})


def get_category(cfg: dict) -> str:
    return cfg["meta"].get("category", DEFAULT_CATEGORY)


def get_agents_for_symbol(cfg: dict, symbol: str) -> list:
    return [a for a in cfg["agents"] if a["symbol"] == symbol]
