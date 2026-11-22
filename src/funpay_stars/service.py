"""Высокоуровневые операции: купить звёзды / подарить Premium.

Защиты:
- order_id идемпотентен: один и тот же заказ не будет оплачен дважды (sqlite);
- MAX_TON_PER_ORDER: если Fragment попросит больше — платёж не уходит;
- dry_run=True: получить цену и транзакцию, ничего не отправляя.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from . import market
from .config import Settings
from .funpay_lots import LotStore
from .fragment import FragmentClient, FragmentError, FragmentTx, Recipient
from .wallet import TonWallet, from_nano

NETWORK_FEE_RESERVE = Decimal("0.01")  # запас на газ (реально ~0.003–0.006 TON)
GAS_TON_EST = Decimal("0.005")  # оценка газа одной покупки для себестоимости

Kind = Literal["stars", "premium"]


@dataclass
class OrderResult:
    order_id: str
    kind: Kind
    username: str
    recipient_name: str
    amount: int  # звёзды или месяцы
    price_ton: str
    status: str  # dry_run | sent | confirmed | failed
    detail: str = ""
    # продажа (FunPay): заполняется, если вызывающий передал данные заказа
    source: str = "manual"  # manual | funpay
    lot_id: str = ""
    buyer: str = ""
    revenue: str = ""  # сумма заказа в его валюте
    currency: str = ""
    revenue_rub: str = ""  # выручка в ₽ (с учётом FUNPAY_FEE_PCT)
    cost_rub: str = ""  # себестоимость в ₽: (цена + газ) × курс TON/RUB на момент выдачи
    ton_rub: str = ""


class OrderStore:
    def __init__(self, path) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                created_at INTEGER,
                status TEXT,
                data TEXT
            )"""
        )
        self.db.commit()

    def get(self, order_id: str) -> OrderResult | None:
        row = self.db.execute("SELECT data FROM orders WHERE order_id=?", (order_id,)).fetchone()
        return OrderResult(**json.loads(row[0])) if row else None

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT created_at, data FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{**json.loads(d), "created_at": c} for c, d in rows]

    def save(self, res: OrderResult) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO orders VALUES (?,?,?,?)",
            (res.order_id, int(time.time()), res.status, json.dumps(asdict(res), ensure_ascii=False)),
        )
        self.db.commit()


class StarsService:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.wallet = TonWallet(settings)
        self.session_file = Path(settings.db_path).parent / "fragment_session.json"
        self.fragment = FragmentClient(self._cookies())
        self.store = OrderStore(settings.db_path)
        self.lots = LotStore(settings.db_path)
        self._order_lock = asyncio.Lock()

    def _cookies(self) -> dict[str, str]:
        """Куки из data/fragment_session.json (сохраняются из админки) перекрывают .env."""
        cookies = dict(self.s.fragment_cookies)
        if self.session_file.exists():
            cookies.update({k: v for k, v in json.loads(self.session_file.read_text()).items() if v})
        return cookies

    async def set_fragment_cookies(self, cookies: dict[str, str]) -> dict:
        data = {k: v.strip() for k, v in cookies.items() if v and v.strip()}
        fd = os.open(self.session_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        old = self.fragment
        self.fragment = FragmentClient(self._cookies())
        market._cache.pop("fragment_status", None)
        await old.__aexit__()
        return await self.fragment.status()

    async def __aenter__(self) -> "StarsService":
        await self.wallet.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.fragment.__aexit__()
        await self.wallet.__aexit__()

    async def recipient(self, username: str, kind: Kind = "stars", months: int = 3) -> dict:
        if kind == "stars":
            r = await self.fragment.find_stars_recipient(username)
        else:
            r = await self.fragment.find_premium_recipient(username, months)
        return {"username": r.username, "name": r.name}

    async def balance(self) -> dict:
        return {"address": self.wallet.address, "balance_ton": str(await self.wallet.balance())}

    async def buy_stars(
        self, order_id: str, username: str, quantity: int, dry_run: bool = False, sale: dict | None = None
    ) -> OrderResult:
        return await self._run(
            order_id, "stars", username, quantity, dry_run,
            lambda acc: self.fragment.stars_tx(username, quantity, acc), sale,
        )

    async def gift_premium(self, order_id: str, username: str, months: int, dry_run: bool = False) -> OrderResult:
        return await self._run(
            order_id, "premium", username, months, dry_run,
            lambda acc: self.fragment.premium_tx(username, months, acc),
        )

    async def _run(self, order_id, kind, username, amount, dry_run, get_tx, sale=None) -> OrderResult:
        if dry_run:
            return await self._run_locked(order_id, kind, username, amount, dry_run, get_tx, sale)
        # реальные выдачи строго по очереди: исключает двойную оплату одного order_id
        async with self._order_lock:
            return await self._run_locked(order_id, kind, username, amount, dry_run, get_tx, sale)

    async def _apply_sale(self, res: OrderResult, sale: dict | None) -> None:
        """Данные продажи и себестоимость в ₽ по курсу на момент выдачи."""
        rates = await market.ton_rates_safe()
        rub = rates.get("rub")
        if rub:
            res.ton_rub = str(rub)
            res.cost_rub = str(((Decimal(res.price_ton) + GAS_TON_EST) * rub).quantize(Decimal("0.01")))
        if not sale:
            return
        res.source = "funpay"
        res.lot_id = str(sale.get("lot_id") or "")
        res.buyer = str(sale.get("buyer") or "")
        if sale.get("revenue") is not None:
            revenue = Decimal(str(sale["revenue"]))
            res.revenue = str(revenue)
            res.currency = str(sale.get("currency") or "rub").lower()
            rev_rub = market.to_rub(revenue, res.currency, rates)
            if rev_rub is not None:
                net = rev_rub * (1 - self.s.funpay_fee_pct / 100)
                res.revenue_rub = str(net.quantize(Decimal("0.01")))

    async def _run_locked(self, order_id, kind, username, amount, dry_run, get_tx, sale=None) -> OrderResult:
        prev = self.store.get(order_id)
        if prev and prev.status in ("sent", "confirmed"):
            return prev  # уже оплачен — повторно не платим

        rec: Recipient
        tx: FragmentTx
        rec, tx = await get_tx(self.wallet.tonconnect_account())
        price = from_nano(tx.amount_nano)
        res = OrderResult(order_id, kind, rec.username, rec.name, amount, str(price), "dry_run")

        if dry_run:
            return res

        if price > self.s.max_ton_per_order:
            res.status, res.detail = "failed", f"Цена {price} TON > лимита {self.s.max_ton_per_order}"
            self.store.save(res)
            return res

        balance = await self.wallet.balance()
        if balance < price + NETWORK_FEE_RESERVE:
            res.status, res.detail = "failed", f"Недостаточно TON: есть {balance}, нужно ~{price + NETWORK_FEE_RESERVE}"
            self.store.save(res)
            return res

        await self._apply_sale(res, sale)
        # фиксируем ДО отправки, чтобы при падении процесса не оплатить повторно
        res.status, res.detail = "sent", f"req_id={tx.req_id}"
        self.store.save(res)
        try:
            pay = await self.wallet.send(tx.address, tx.amount_nano, tx.payload)
        except Exception as e:
            res.status, res.detail = "failed", f"Ошибка отправки: {e!r}"
            self.store.save(res)
            raise
        if pay.confirmed:
            res.status = "confirmed"
        else:
            res.detail += "; подтверждение не дождались — проверь в tonviewer"
        self.store.save(res)
        return res


__all__ = ["StarsService", "OrderResult", "FragmentError"]
