"""Единое приложение: публичный дашборд (/), админка (/admin), API выдачи (/api/v1).

Запуск: uv run uvicorn funpay_stars.server:app --port 8090
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from . import dashboard
from .api import router as api_router
from .config import load_settings
from .service import StarsService

log = logging.getLogger("funpay_stars")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.api_token = settings.api_token
    app.state.svc = None
    if not settings.wallet_mnemonic:
        log.warning("WALLET_MNEMONIC не задан — работает только дашборд")
        yield
        return
    async with StarsService(settings) as svc:
        app.state.svc = svc
        if not dashboard.WALLET_ADDRESS:
            dashboard.WALLET_ADDRESS = svc.wallet.address
        if dashboard.DB_PATH != settings.db_path:
            dashboard.DB_PATH = settings.db_path
        yield


app = FastAPI(title="funpay-stars", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(api_router)
app.include_router(dashboard.router)


@app.get("/admin")
async def admin():
    return FileResponse(
        STATIC / "admin.html",
        headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex", "X-Frame-Options": "DENY"},
    )
