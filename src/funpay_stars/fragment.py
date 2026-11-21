"""Клиент внутреннего (неофициального) API fragment.com.

Схема та же, что использует сам сайт:
  stars:   searchStarsRecipient -> initBuyStarsRequest -> getBuyStarsLink
  premium: searchPremiumGiftRecipient -> initGiftPremiumRequest -> getGiftPremiumLink
Последний шаг отдаёт TON-транзакцию (адрес, сумма в нанотонах, payload), которую
мы подписываем своим кошельком.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

BASE = "https://fragment.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEVICE = json.dumps(
    {
        "platform": "mac",
        "appName": "tonkeeper",
        "appVersion": "3.27",
        "maxProtocolVersion": 2,
        "features": ["SendTransaction", {"name": "SendTransaction", "maxMessages": 4}],
    },
    separators=(",", ":"),
)

PREMIUM_MONTHS = (3, 6, 12)
MIN_STARS = 50


class FragmentError(RuntimeError):
    pass


@dataclass
class Recipient:
    recipient: str  # внутренний id получателя Fragment
    name: str
    username: str


@dataclass
class FragmentTx:
    req_id: str
    address: str
    amount_nano: int
    payload: str
    raw: dict[str, Any]


class FragmentClient:
    def __init__(self, cookies: dict[str, str]) -> None:
        if not cookies.get("stel_ssid") or not cookies.get("stel_token"):
            raise FragmentError("Нет кук Fragment (FRAGMENT_STEL_SSID / FRAGMENT_STEL_TOKEN) в .env")
        self._http = httpx.AsyncClient(
            base_url=BASE,
            cookies=cookies,
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=30,
            follow_redirects=True,
        )
        self._hash: str | None = None
        self.authorized = False

    async def __aenter__(self) -> "FragmentClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    # ---------- низкий уровень ----------

    async def _api_hash(self, page: str = "/stars/buy") -> str:
        if self._hash:
            return self._hash
        r = await self._http.get(page)
        r.raise_for_status()
        m = re.search(r'"apiUrl":"\\?/api\\?\?hash=([0-9a-f]+)"', r.text)
        if not m:
            raise FragmentError("Не нашёл api hash на странице — сайт поменялся")
        self.authorized = '"unauth":true' not in r.text
        self._hash = m.group(1)
        return self._hash

    async def call(self, method: str, referer: str = "/stars/buy", **params: Any) -> dict[str, Any]:
        api_hash = await self._api_hash()
        if not self.authorized:
            raise FragmentError("Fragment видит сессию как неавторизованную — обнови куки в .env")
        data ={k: str(v) for k, v in params.items() if v is not None}
        data["method"] = method
        r = await self._http.post(
            "/api",
            params={"hash": api_hash},
            data=data,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": BASE,
                "Referer": BASE + referer,
            },
        )
        r.raise_for_status()
        try:
            resp = r.json()
        except ValueError as e:
            raise FragmentError(f"{method}: не JSON ответ: {r.text[:200]}") from e
        if resp.get("error"):
            raise FragmentError(f"{method}: {resp['error']}")
        return resp

    @staticmethod
    def _parse_tx(req_id: str, resp: dict[str, Any]) -> FragmentTx:
        tx = resp.get("transaction") or {}
        msgs = tx.get("messages") or []
        if not resp.get("ok") or len(msgs) != 1:
            raise FragmentError(f"Неожиданный ответ с транзакцией: {resp}")
        m = msgs[0]
        return FragmentTx(req_id, m["address"], int(m["amount"]), m.get("payload", ""), resp)

    @staticmethod
    def _norm(username: str) -> str:
        return username.strip().lstrip("@")

    # ---------- звёзды ----------

    async def find_stars_recipient(self, username: str) -> Recipient:
        u = self._norm(username)
        resp = await self.call("searchStarsRecipient", query=u, quantity="")
        found = resp.get("found") or {}
        if not found.get("recipient"):
            raise FragmentError(f"Получатель @{u} не найден")
        return Recipient(found["recipient"], found.get("name", ""), u)

    async def stars_tx(self, username: str, quantity: int, wallet_account: str) -> tuple[Recipient, FragmentTx]:
        if quantity < MIN_STARS:
            raise FragmentError(f"Минимум {MIN_STARS} звёзд")
        rec = await self.find_stars_recipient(username)
        init = await self.call("initBuyStarsRequest", recipient=rec.recipient, quantity=quantity)
        req_id = init.get("req_id")
        if not req_id:
            raise FragmentError(f"initBuyStarsRequest без req_id: {init}")
        link = await self.call(
            "getBuyStarsLink",
            account=wallet_account,
            device=DEVICE,
            transaction=1,
            id=req_id,
            show_sender=0,
        )
        return rec, self._parse_tx(req_id, link)

    # ---------- премиум ----------

    async def find_premium_recipient(self, username: str, months: int) -> Recipient:
        u = self._norm(username)
        resp = await self.call(
            "searchPremiumGiftRecipient", referer="/premium/gift", query=u, months=months
        )
        found = resp.get("found") or {}
        if not found.get("recipient"):
            raise FragmentError(f"Получатель @{u} не найден (или у него уже есть Premium)")
        return Recipient(found["recipient"], found.get("name", ""), u)

    async def premium_tx(self, username: str, months: int, wallet_account: str) -> tuple[Recipient, FragmentTx]:
        if months not in PREMIUM_MONTHS:
            raise FragmentError(f"months должен быть одним из {PREMIUM_MONTHS}")
        rec = await self.find_premium_recipient(username, months)
        init = await self.call(
            "initGiftPremiumRequest", referer="/premium/gift", recipient=rec.recipient, months=months
        )
        req_id = init.get("req_id")
        if not req_id:
            raise FragmentError(f"initGiftPremiumRequest без req_id: {init}")
        link = await self.call(
            "getGiftPremiumLink",
            referer="/premium/gift",
            account=wallet_account,
            device=DEVICE,
            transaction=1,
            id=req_id,
            show_sender=0,
        )
        return rec, self._parse_tx(req_id, link)
