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
DEFAULT_CONFIG = CONFIGS_DIR / "12agents_doge.json"


def load_config(path: Path = None) -> dict:
    """Загружает JSON-конфиг, валидирует структуру."""
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # Validation
    if "meta" not in cfg or "agents" not in cfg:
        raise ValueError("Config must have 'meta' and 'agents'")
    if not isinstance(cfg["agents"], list) or not cfg["agents"]:
        raise ValueError("'agents' must be non-empty list")

    # Apply defaults
    meta = cfg["meta"]
    meta.setdefault("category", DEFAULT_CATEGORY)
    meta.setdefault("symbol", "DOGEUSDT")  # default symbol if agent omits
    meta.setdefault("start_balance", 1000.0)
    meta.setdefault("maker_fee", DEFAULT_MAKER_FEE)
    meta.setdefault("taker_fee", DEFAULT_TAKER_FEE)

    for a in cfg["agents"]:
        a.setdefault("symbol", meta["symbol"])
        a.setdefault("obi_threshold", 0.65)
        a.setdefault("min_std_dev", 0.05)
        a.setdefault("tp_sl_ratio", 2.0)
        a.setdefault("decay_start", 180)
        a.setdefault("decay_duration", 600)
        a.setdefault("hard_kill", 900)
        a.setdefault("sl_cooldown", 300)

    return cfg


def get_symbols(cfg: dict) -> list:
    """Уникальные символы среди всех агентов."""
    return sorted({a["symbol"] for a in cfg["agents"]})


def get_category(cfg: dict) -> str:
    return cfg["meta"].get("category", DEFAULT_CATEGORY)


def get_agents_for_symbol(cfg: dict, symbol: str) -> list:
    return [a for a in cfg["agents"] if a["symbol"] == symbol]
