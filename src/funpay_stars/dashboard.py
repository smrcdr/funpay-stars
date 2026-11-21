"""Публичный дашборд: баланс, на сколько звёзд хватит, статистика выдачи, адрес пополнения.

Работает БЕЗ мнемоники — нужен только публичный адрес кошелька (WALLET_ADDRESS)
и (необязательно) база заказов orders.sqlite3.

Запуск: uv run uvicorn funpay_stars.dashboard:app --port 8090
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse

WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "").strip()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "orders.sqlite3"))
CACHE_TTL = 60
FEE_RESERVE = Decimal("0.01")  # газ на одну покупку
QUOTE_QTY = 1000  # котировку берём за 1000 звёзд и делим
MIN_STARS = 50

STATIC = Path(__file__).parent / "static"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/131.0 Safari/537.36"

app = FastAPI(title="stars dashboard", docs_url=None, redoc_url=None)
_cache: dict[str, tuple[float, object]] = {}
_lock = asyncio.Lock()


async def _cached(key: str, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        val = await fn()
    except Exception:
        if hit:
            return hit[1]  # отдаём устаревшее, если источник лёг
        raise
    _cache[key] = (time.time(), val)
    return val


async def _balance(c: httpx.AsyncClient) -> Decimal:
    params = {"address": WALLET_ADDRESS}
    if TONCENTER_API_KEY:
        params["api_key"] = TONCENTER_API_KEY
    r = await c.get("https://toncenter.com/api/v2/getAddressBalance", params=params)
    r.raise_for_status()
    return Decimal(r.json()["result"]) / Decimal(10**9)


async def _ton_usd(c: httpx.AsyncClient) -> Decimal:
    r = await c.get("https://tonapi.io/v2/rates", params={"tokens": "ton", "currencies": "usd"})
    r.raise_for_status()
    return Decimal(str(r.json()["rates"]["TON"]["prices"]["USD"]))


def _num(html: str) -> Decimal:
    text = re.sub(r"<[^>]+>", "", html).replace(",", "").strip()
    return Decimal(text)


async def _stars_quote(c: httpx.AsyncClient) -> dict:
    """Цена звёзд с fragment.com (метод работает без авторизации)."""
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


def _stats() -> dict:
    empty = {
        "orders": 0, "stars": 0, "premium_months": 0, "premium_orders": 0, "spent_ton": "0",
        "today_stars": 0, "week_stars": 0, "failed": 0, "daily": [], "recent": [],
    }
    if not DB_PATH.exists():
        return empty
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT created_at, status, data FROM orders ORDER BY created_at DESC").fetchall()
    except sqlite3.OperationalError:
        return empty
    finally:
        db.close()

    now = time.time()
    day0 = now - now % 86400
    days = {int(day0 - i * 86400): {"stars": 0, "orders": 0} for i in range(29, -1, -1)}
    s = dict(empty, daily=[], recent=[])
    spent = Decimal(0)
    for created, status, data in rows:
        d = json.loads(data)
        if status == "failed":
            s["failed"] += 1
            continue
        if status not in ("sent", "confirmed"):
            continue
        s["orders"] += 1
        spent += Decimal(d.get("price_ton") or 0)
        if d["kind"] == "stars":
            s["stars"] += d["amount"]
            if created >= day0:
                s["today_stars"] += d["amount"]
            if created >= now - 7 * 86400:
                s["week_stars"] += d["amount"]
        else:
            s["premium_orders"] += 1
            s["premium_months"] += d["amount"]
        bucket = int(created - created % 86400)
        if bucket in days:
            days[bucket]["orders"] += 1
            if d["kind"] == "stars":
                days[bucket]["stars"] += d["amount"]
        if len(s["recent"]) < 12:
            u = d.get("username") or ""
            s["recent"].append({
                "at": created,
                "kind": d["kind"],
                "amount": d["amount"],
                "price_ton": d.get("price_ton"),
                "status": status,
                "user": (u[:2] + "•••" + u[-1:]) if len(u) > 3 else "•••",
            })
    s["spent_ton"] = str(spent.quantize(Decimal("0.01")))
    s["daily"] = [{"day": k, **v} for k, v in days.items()]
    return s


@app.get("/api/summary")
async def summary():
    async with _lock, httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
        out: dict = {"address": WALLET_ADDRESS, "errors": []}

        async def safe(key, fn):
            try:
                return await _cached(key, lambda: fn(c))
            except Exception as e:
                out["errors"].append(f"{key}: {type(e).__name__}")
                return None

        bal, rate, quote = await asyncio.gather(
            safe("balance", _balance), safe("rate", _ton_usd), safe("quote", _stars_quote)
        )

    out["balance_ton"] = str(bal.quantize(Decimal("0.0001"))) if bal is not None else None
    out["ton_usd"] = str(rate.quantize(Decimal("0.0001"))) if rate is not None else None
    out["balance_usd"] = str((bal * rate).quantize(Decimal("0.01"))) if bal is not None and rate else None
    if quote:
        out["ton_per_star"] = str(quote["ton_per_star"])
        out["usd_per_star"] = str(quote["usd_per_star"]) if quote["usd_per_star"] else None
        out["price_50_ton"] = str((quote["ton_per_star"] * MIN_STARS).quantize(Decimal("0.001")))
        if bal is not None:
            avail = max(bal - FEE_RESERVE, Decimal(0))
            out["stars_affordable"] = math.floor(avail / quote["ton_per_star"])
    out["stats"] = await asyncio.to_thread(_stats)
    out["updated_at"] = int(time.time())
    return out


@app.get("/")
async def index():
    return FileResponse(STATIC / "dashboard.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
async def healthz():
    return {"ok": True}
