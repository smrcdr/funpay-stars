"""API выдачи (для FunPay-бота и админки). Все ручки под Bearer API_TOKEN.

POST /api/v1/stars     {order_id?, username, quantity, dry_run?}
POST /api/v1/premium   {order_id?, username, months, dry_run?}
GET  /api/v1/recipient?username=&kind=stars|premium&months=3
GET  /api/v1/balance
GET  /api/v1/orders?limit=50
GET  /api/v1/orders/{order_id}
GET  /api/v1/fragment/session
POST /api/v1/fragment/session  {stel_ssid, stel_token, stel_ton_token}

FunPay (плагин Cardinal + админка):
POST  /api/v1/funpay/lots/sync   {lots:[{lot_id,title,subcategory_id?,subcategory?,price?,currency?,active?}]} → план
GET   /api/v1/funpay/lots        лоты + план автоактивации
PATCH /api/v1/funpay/lots/{id}   {stars_per_unit?, auto_delivery?, auto_activate?, min_units?}
DELETE /api/v1/funpay/lots/{id}
GET   /api/v1/stats?days=30      выручка / себестоимость / прибыль

POST /stars принимает данные продажи: revenue, currency, lot_id, buyer.

order_id идемпотентен: повторный запрос с тем же id не оплачивает второй раз,
а возвращает уже сохранённый результат. Для FunPay передавай id заказа FunPay.
"""

from __future__ import annotations

import asyncio
import hmac
import uuid
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import funpay_lots, market
from .fragment import MIN_STARS, PREMIUM_MONTHS, FragmentError
from .service import StarsService

router = APIRouter(prefix="/api/v1")


def svc(request: Request) -> StarsService:
    s = request.app.state.svc
    if s is None:
        raise HTTPException(503, "Сервис выдачи не настроен: нет WALLET_MNEMONIC на сервере")
    return s


def auth(request: Request, authorization: str = Header(default="")) -> None:
    token = request.app.state.api_token
    if not token or not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "bad token")


class StarsIn(BaseModel):
    order_id: str | None = Field(default=None, max_length=128)
    username: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=MIN_STARS, le=1_000_000)
    dry_run: bool = False
    # данные продажи (FunPay)
    revenue: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)
    lot_id: str | None = Field(default=None, max_length=32)
    buyer: str | None = Field(default=None, max_length=64)

    def sale(self) -> dict | None:
        if self.revenue is None and not self.lot_id and not self.buyer:
            return None
        return {"revenue": self.revenue, "currency": self.currency, "lot_id": self.lot_id, "buyer": self.buyer}


class PremiumIn(BaseModel):
    order_id: str | None = Field(default=None, max_length=128)
    username: str = Field(min_length=1, max_length=64)
    months: Literal[3, 6, 12]
    dry_run: bool = False


class SessionIn(BaseModel):
    stel_ssid: str = ""
    stel_token: str = ""
    stel_ton_token: str = ""


def _oid(order_id: str | None) -> str:
    return order_id or f"manual-{uuid.uuid4().hex[:10]}"


def _fail(e: Exception):
    raise HTTPException(400, str(e))


deps = [Depends(auth)]


@router.get("/balance", dependencies=deps)
async def balance(s: StarsService = Depends(svc)):
    return await s.balance()


@router.get("/recipient", dependencies=deps)
async def recipient(
    username: str,
    kind: Literal["stars", "premium"] = "stars",
    months: int = Query(3),
    s: StarsService = Depends(svc),
):
    if kind == "premium" and months not in PREMIUM_MONTHS:
        _fail(ValueError("months: 3, 6 или 12"))
    try:
        return await s.recipient(username, kind, months)
    except FragmentError as e:
        _fail(e)


@router.post("/stars", dependencies=deps)
async def stars(body: StarsIn, s: StarsService = Depends(svc)):
    try:
        return asdict(await s.buy_stars(_oid(body.order_id), body.username, body.quantity, body.dry_run, body.sale()))
    except FragmentError as e:
        _fail(e)


@router.post("/premium", dependencies=deps)
async def premium(body: PremiumIn, s: StarsService = Depends(svc)):
    try:
        return asdict(await s.gift_premium(_oid(body.order_id), body.username, body.months, body.dry_run))
    except FragmentError as e:
        _fail(e)


@router.get("/orders", dependencies=deps)
async def orders(limit: int = Query(50, ge=1, le=500), s: StarsService = Depends(svc)):
    return s.store.list(limit)


@router.get("/orders/{order_id}", dependencies=deps)
async def order(order_id: str, s: StarsService = Depends(svc)):
    res = s.store.get(order_id)
    if not res:
        raise HTTPException(404, "not found")
    return asdict(res)


@router.get("/fragment/session", dependencies=deps)
async def session_status(s: StarsService = Depends(svc)):
    return await s.fragment.status()


@router.post("/fragment/session", dependencies=deps)
async def session_set(body: SessionIn, s: StarsService = Depends(svc)):
    return await s.set_fragment_cookies(body.model_dump())


# ---------- FunPay: лоты и статистика ----------


class LotIn(BaseModel):
    lot_id: str | int
    title: str = Field(default="", max_length=500)
    subcategory_id: str | int | None = None
    subcategory: str | None = Field(default=None, max_length=200)
    price: float | None = None
    currency: str | None = Field(default=None, max_length=8)
    active: bool | None = None


class SyncIn(BaseModel):
    lots: list[LotIn] = Field(default_factory=list, max_length=500)


class LotPatch(BaseModel):
    stars_per_unit: int | None = Field(default=None, ge=1, le=1_000_000)
    auto_delivery: bool | None = None
    auto_activate: bool | None = None
    min_units: int | None = Field(default=None, ge=1, le=10_000)


async def _plan(s: StarsService) -> dict:
    async def bal():
        try:
            return await market.cached("wallet_balance", s.wallet.balance, ttl=30)
        except Exception:
            return None

    async def quote():
        try:
            return (await market.stars_quote())["ton_per_star"]
        except Exception:
            return None

    async def frag():
        try:
            st = await market.cached("fragment_status", s.fragment.status, ttl=120)
            return bool(st.get("authorized"))
        except Exception:
            return False

    balance, tps, ok = await asyncio.gather(bal(), quote(), frag())
    return funpay_lots.plan(s.lots.list(), balance, tps, ok)


@router.post("/funpay/lots/sync", dependencies=deps)
async def lots_sync(body: SyncIn, s: StarsService = Depends(svc)):
    s.lots.sync([lot.model_dump() for lot in body.lots])
    return await _plan(s)


@router.get("/funpay/lots", dependencies=deps)
async def lots_list(s: StarsService = Depends(svc)):
    return await _plan(s)


@router.patch("/funpay/lots/{lot_id}", dependencies=deps)
async def lot_patch(lot_id: str, body: LotPatch, s: StarsService = Depends(svc)):
    lot = s.lots.update(lot_id, **body.model_dump())
    if not lot:
        raise HTTPException(404, "not found")
    return lot


@router.delete("/funpay/lots/{lot_id}", dependencies=deps)
async def lot_delete(lot_id: str, s: StarsService = Depends(svc)):
    s.lots.delete(lot_id)
    return {"ok": True}


@router.get("/stats", dependencies=deps)
async def stats(days: int = Query(30, ge=1, le=365), s: StarsService = Depends(svc)):
    return await asyncio.to_thread(funpay_lots.stats, s.s.db_path, days)
