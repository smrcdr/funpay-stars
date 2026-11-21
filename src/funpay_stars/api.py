"""API выдачи (для FunPay-бота и админки). Все ручки под Bearer API_TOKEN.

POST /api/v1/stars     {order_id?, username, quantity, dry_run?}
POST /api/v1/premium   {order_id?, username, months, dry_run?}
GET  /api/v1/recipient?username=&kind=stars|premium&months=3
GET  /api/v1/balance
GET  /api/v1/orders?limit=50
GET  /api/v1/orders/{order_id}
GET  /api/v1/fragment/session
POST /api/v1/fragment/session  {stel_ssid, stel_token, stel_ton_token}

order_id идемпотентен: повторный запрос с тем же id не оплачивает второй раз,
а возвращает уже сохранённый результат. Для FunPay передавай id заказа FunPay.
"""

from __future__ import annotations

import hmac
import uuid
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

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
        return asdict(await s.buy_stars(_oid(body.order_id), body.username, body.quantity, body.dry_run))
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
