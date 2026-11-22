"""Публичный дашборд: баланс, на сколько звёзд хватит, статистика выдачи, адрес пополнения.

Работает БЕЗ мнемоники — нужен только публичный адрес кошелька (WALLET_ADDRESS)
и (необязательно) база заказов orders.sqlite3.

Подключается в server.py.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import APIRouter
from fastapi.responses import FileResponse

from . import market

WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "").strip()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "orders.sqlite3"))
FEE_RESERVE = Decimal("0.01")  # газ на одну покупку
MIN_STARS = 50

STATIC = Path(__file__).parent / "static"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/131.0 Safari/537.36"

router = APIRouter()
_lock = asyncio.Lock()


async def _balance() -> Decimal:
    params = {"address": WALLET_ADDRESS}
    if TONCENTER_API_KEY:
        params["api_key"] = TONCENTER_API_KEY
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
        r = await c.get("https://toncenter.com/api/v2/getAddressBalance", params=params)
        r.raise_for_status()
    return Decimal(r.json()["result"]) / Decimal(10**9)


async def _ton_usd() -> Decimal:
    return (await market.ton_rates())["usd"]


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


@router.get("/api/summary")
async def summary():
    out: dict = {"address": WALLET_ADDRESS, "errors": []}

    async def safe(key, fn):
        try:
            return await market.cached(key, fn)
        except Exception as e:
            out["errors"].append(f"{key}: {type(e).__name__}")
            return None

    async with _lock:
        bal, rate, quote = await asyncio.gather(
            safe("balance", _balance), safe("rate_usd", _ton_usd), safe("quote", market._fetch_stars_quote)
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


@router.get("/")
async def index():
    return FileResponse(STATIC / "dashboard.html", headers={"Cache-Control": "no-cache"})


@router.get("/healthz")
async def healthz():
    return {"ok": True}
