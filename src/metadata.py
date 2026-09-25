"""
metadata.py — загрузка спецификаций инструмента и комиссий с Bybit.
Кэширует ответы, чтобы не дёргать API при каждом запуске.
"""
import json
import time
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlencode

from src import config


def _fetch_json(url: str) -> dict:
    with urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def _cache_path(name: str) -> Path:
    return config.CACHE_DIR / f"{name}.json"


def _load_cache(name: str, ttl: int):
    p = _cache_path(name)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("_cached_at", 0) > ttl:
        return None
    return data


def _save_cache(name: str, data: dict) -> None:
    data["_cached_at"] = time.time()
    _cache_path(name).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def fetch_instrument_info(symbol: str, category: str = "linear") -> dict:
    """
    Возвращает {tick_size, qty_step, min_qty, min_notional, base_precision}.
    Кэш 24 часа.
    """
    cache_name = f"instrument_{category}_{symbol}"
    cached = _load_cache(cache_name, config.METADATA_CACHE_TTL)
    if cached:
        return cached

    url = "https://api.bybit.com/v5/market/instruments-info?" + urlencode(
        {"category": category, "symbol": symbol}
    )
    resp = _fetch_json(url)
    if resp.get("retCode") != 0:
        raise RuntimeError(f"Bybit instruments-info error: {resp.get('retMsg')}")

    items = resp["result"]["list"]
    if not items:
        raise RuntimeError(f"Symbol {symbol} not found in category {category}")

    i = items[0]
    lot = i["lotSizeFilter"]
    prc = i["priceFilter"]

    # Spot uses minOrderAmt, linear uses minNotionalValue
    min_notional = float(
        lot.get("minNotionalValue") or lot.get("minOrderAmt") or 0
    )

    info = {
        "symbol": symbol,
        "category": category,
        "tick_size": float(prc["tickSize"]),
        "qty_step": float(lot["qtyStep"]),
        "min_qty": float(lot["minOrderQty"]),
        "min_notional": min_notional,
        "base_precision": lot.get("basePrecision"),
        "quote_precision": lot.get("quotePrecision"),
    }
    _save_cache(cache_name, info)
    return info


def fetch_fees(api_key: str, api_secret: str, symbol: str,
               category: str = "linear") -> dict:
    """
    Возвращает {maker_fee, taker_fee} для аккаунта.
    Требует API-ключи. Кэш 1 час. Если недоступно — fallback на defaults.
    """
    cache_name = f"fees_{category}_{symbol}"
    cached = _load_cache(cache_name, config.FEES_CACHE_TTL)
    if cached:
        return cached

    try:
        # Lazy import — pybit не обязателен если ключей нет
        from pybit.unified_trading import HTTP
        session = HTTP(
            testnet=False, demo=False,
            api_key=api_key, api_secret=api_secret,
            recv_window=10000,
        )
        resp = session.get_fee_rates(category=category, symbol=symbol)
        if resp.get("retCode") != 0:
            raise RuntimeError(resp.get("retMsg"))
        item = resp["result"]["list"][0]
        fees = {
            "maker_fee": float(item["makerFeeRate"]),
            "taker_fee": float(item["takerFeeRate"]),
        }
        _save_cache(cache_name, fees)
        return fees
    except Exception as e:
        # Fallback на defaults с предупреждением
        print(f"[metadata] fee fetch failed ({e}), using defaults")
        return {
            "maker_fee": config.DEFAULT_MAKER_FEE,
            "taker_fee": config.DEFAULT_TAKER_FEE,
        }


def get_instrument(symbol: str, category: str = "linear",
                   maker_fee: float = None, taker_fee: float = None) -> dict:
    """
    Единая точка: instrument + fees.
    maker_fee/taker_fee из config override'ят fetch.
    """
    info = fetch_instrument_info(symbol, category)

    # Если override в config — используем его, иначе defaults
    if maker_fee is not None:
        info["maker_fee"] = maker_fee
    else:
        info["maker_fee"] = config.DEFAULT_MAKER_FEE

    if taker_fee is not None:
        info["taker_fee"] = taker_fee
    else:
        info["taker_fee"] = config.DEFAULT_TAKER_FEE

    return info
