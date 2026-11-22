"""Общие котировки с кэшем: цена звезды на Fragment и курс TON (USD/RUB/EUR).

Используется публичным дашбордом, планом автоактивации лотов и расчётом
себестоимости заказов, чтобы везде были одни и те же цифры.
"""

from __future__ import annotations

import asyncio
import re
import time
from decimal import Decimal
from typing import Awaitable, Callable

import httpx

CACHE_TTL = 60
QUOTE_QTY = 1000  # котировку берём за 1000 звёзд и делим
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/131.0 Safari/537.36"

_cache: dict[str, tuple[float, object]] = {}
_locks: dict[str, asyncio.Lock] = {}


async def cached(key: str, fn: Callable[[], Awaitable], ttl: int = CACHE_TTL):
    """Значение из кэша; при ошибке источника отдаёт устаревшее, если оно есть."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            val = await fn()
        except Exception:
            if hit:
                return hit[1]
            raise
        _cache[key] = (time.time(), val)
        return val


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, headers={"User-Agent": UA})


def _num(html: str) -> Decimal:
    return Decimal(re.sub(r"<[^>]+>", "", html).replace(",", "").strip())


async def _fetch_stars_quote() -> dict:
    """Цена звёзд с fragment.com (метод работает без авторизации)."""
    async with _client() as c:
        page = await c.get("https://fragment.com/stars/buy")
        page.raise_for_status()
        h = re.search(r'"apiUrl":"\\?/api\\?\?hash=([0-9a-f]+)"', page.text).group(1)
        r = await c.post(
            "https://fragment.com/api",
            params={"hash": h},
            data={"stars": "", "quantity": str(QUOTE_QTY), "method": "updateStarsPrices"},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://fragment.com/stars/buy"},
        )
        r.raise_for_status()
    cur = r.json()["cur_price"]
    usd = re.search(r'icon-usd">(.*?)</div>', cur, re.S)
    ton = re.search(r'icon-ton">(.*?)</div>', cur, re.S)
    return {
        "ton_per_star": _num(ton.group(1)) / QUOTE_QTY,
        "usd_per_star": _num(usd.group(1)) / QUOTE_QTY if usd else None,
    }


async def _fetch_ton_rates() -> dict[str, Decimal]:
    async with _client() as c:
        r = await c.get("https://tonapi.io/v2/rates", params={"tokens": "ton", "currencies": "usd,rub,eur"})
        r.raise_for_status()
    prices = r.json()["rates"]["TON"]["prices"]
    return {k.lower(): Decimal(str(v)) for k, v in prices.items()}


async def stars_quote() -> dict:
    return await cached("quote", _fetch_stars_quote)


async def ton_rates() -> dict[str, Decimal]:
    """{'usd': ..., 'rub': ..., 'eur': ...} — цена 1 TON."""
    return await cached("rates", _fetch_ton_rates)


async def ton_rates_safe() -> dict[str, Decimal]:
    try:
        return await ton_rates()
    except Exception:
        return {}


def to_rub(amount: Decimal, currency: str, rates: dict[str, Decimal]) -> Decimal | None:
    """Пересчёт суммы в рубли через кросс-курс TON. None — если курса нет."""
    cur = (currency or "rub").lower()
    if cur in ("rub", "₽", "р", "руб"):
        return amount
    rub, other = rates.get("rub"), rates.get(cur)
    if not rub or not other:
        return None
    return amount * rub / other
