"""
metadata.py — загрузка спецификаций инструмента и комиссий с Bybit.
Кэширует ответы, чтобы не дёргать API при каждом запуске.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlencode
from urllib.request import urlopen

from pybit.unified_trading import HTTP

from src import config


# --- TypedDict structures ---

class Fees(TypedDict):
    """Maker/Taker fees для аккаунта."""
    maker_fee: float
    taker_fee: float


class InstrumentInfo(TypedDict):
    """Спецификация торгового инструмента (без fees)."""
    symbol: str
    category: str
    tick_size: float
    qty_step: float
    min_qty: float
    min_notional: float


class InstrumentWithFees(InstrumentInfo):
    """Instrument info + resolved fees."""
    maker_fee: float
    taker_fee: float


def _fetch_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=config.HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def _mask(s: str) -> str:
    """Маскирует секрет для безопасного логирования. tYKsc3...WYG5"""
    if not s or len(s) < 10:
        return "***"
    return f"{s[:6]}...{s[-4:]}"


def _sanitize_error(msg: str, *secrets: str) -> str:
    """Заменяет все вхождения секретов в строке на маскированные версии."""
    result = str(msg)
    for s in secrets:
        if s and len(s) >= 10:
            result = result.replace(s, _mask(s))
    return result


def _cache_path(name: str) -> Path:
    return config.CACHE_DIR / f"{name}.json"


def _load_cache(name: str, ttl: int) -> dict[str, Any] | None:
    """Читает кэш. Возвращает dict без служебного поля _cached_at."""
    p = _cache_path(name)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("_cached_at", 0) > ttl:
        return None
    # Убираем служебное поле — не выходит за пределы модуля
    data.pop("_cached_at", None)
    return data


def _save_cache(name: str, data: dict[str, Any]) -> None:
    """Atomic write кэша. Не мутирует входной dict."""
    payload = {**data, "_cached_at": time.time()}
    path = _cache_path(name)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # Атомарная замена (POSIX rename atomic)
    os.replace(tmp, path)


def fetch_instrument_info(
    symbol: str,
    category: str = "linear",
) -> InstrumentInfo:
    """
    Возвращает спецификацию инструмента с Bybit.
    Кэш 24 часа.
    """
    cache_name = f"instrument_{config.CACHE_VERSION}_{category}_{symbol}"
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
    }
    _save_cache(cache_name, info)
    return info


def fetch_fees(
    api_key: str,
    api_secret: str,
    symbol: str,
    category: str = "linear",
) -> Fees | None:
    """
    Возвращает {maker_fee, taker_fee} для аккаунта или None при ошибке.
    Требует API-ключи. Кэш 1 час.

    НЕ возвращает defaults — этим занимается resolve_fees().
    Это позволяет вызывающей стороне различать "API ответил defaults"
    и "API не ответил, fallback на defaults".
    """
    if not api_key or not api_secret:
        return None

    cache_name = f"fees_{config.CACHE_VERSION}_{category}_{symbol}"
    cached = _load_cache(cache_name, config.FEES_CACHE_TTL)
    if cached:
        return cached

    try:
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
        safe_msg = _sanitize_error(str(e), api_key, api_secret)
        print(f"[metadata] fee fetch failed for {symbol}: {safe_msg}")
        return None


def resolve_fees(
    symbol: str,
    category: str = "linear",
    maker_fee: float | None = None,
    taker_fee: float | None = None,
) -> Fees:
    """
    Определяет maker/taker fees для символа с приоритетом:
      1. Явный override (аргументы maker_fee/taker_fee)
      2. fetch_fees из Bybit API (если BYBIT_API_KEY/SECRET в env)
      3. config.DEFAULT_*

    Возвращает {maker_fee, taker_fee}.
    """
    if maker_fee is not None and taker_fee is not None:
        return {"maker_fee": maker_fee, "taker_fee": taker_fee}

    api_key = os.environ.get("BYBIT_API_KEY")
    api_secret = os.environ.get("BYBIT_API_SECRET")
    if api_key and api_secret:
        fetched = fetch_fees(api_key, api_secret, symbol, category)
        if fetched:
            # Если только один из override задан — частичное применение
            if maker_fee is not None:
                fetched["maker_fee"] = maker_fee
            if taker_fee is not None:
                fetched["taker_fee"] = taker_fee
            return fetched

    return {
        "maker_fee": maker_fee if maker_fee is not None else config.DEFAULT_MAKER_FEE,
        "taker_fee": taker_fee if taker_fee is not None else config.DEFAULT_TAKER_FEE,
    }


def get_instrument(
    symbol: str,
    category: str = "linear",
    maker_fee: float | None = None,
    taker_fee: float | None = None,
) -> InstrumentWithFees:
    """
    Единая точка: instrument spec + resolved fees.

    Приоритет fees: override → API fetch (если env key) → defaults.
    """
    info = fetch_instrument_info(symbol, category)
    info.update(resolve_fees(symbol, category, maker_fee, taker_fee))
    return info
