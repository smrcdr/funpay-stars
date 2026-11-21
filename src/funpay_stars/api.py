"""HTTP API (FastAPI). Запуск: uv run funpay-stars serve"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import load_settings
from .fragment import MIN_STARS, FragmentError
from .service import StarsService

settings = load_settings()
svc: StarsService | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global svc
    async with StarsService(settings) as s:
        svc = s
        yield


app = FastAPI(title="funpay-stars", lifespan=lifespan)


def auth(authorization: str = Header(default="")) -> None:
    if not settings.api_token or authorization != f"Bearer {settings.api_token}":
        raise HTTPException(401, "bad token")


class StarsIn(BaseModel):
    order_id: str
    username: str
    quantity: int = Field(ge=MIN_STARS)
    dry_run: bool = False


class PremiumIn(BaseModel):
    order_id: str
    username: str
    months: int = Field(description="3, 6 или 12")
    dry_run: bool = False


@app.get("/balance", dependencies=[Depends(auth)])
async def balance():
    return await svc.balance()


@app.post("/stars", dependencies=[Depends(auth)])
async def stars(body: StarsIn):
    try:
        return asdict(await svc.buy_stars(body.order_id, body.username, body.quantity, body.dry_run))
    except FragmentError as e:
        raise HTTPException(400, str(e))


@app.post("/premium", dependencies=[Depends(auth)])
async def premium(body: PremiumIn):
    try:
        return asdict(await svc.gift_premium(body.order_id, body.username, body.months, body.dry_run))
    except FragmentError as e:
        raise HTTPException(400, str(e))


@app.get("/orders/{order_id}", dependencies=[Depends(auth)])
async def order(order_id: str):
    res = svc.store.get(order_id)
    if not res:
        raise HTTPException(404, "not found")
    return asdict(res)
