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
# Способ оплаты на Fragment: ton (нативный TON — самая низкая комиссия), usdt_ton, usdt_bsc, usdt_tron...
PAYMENT_METHOD = "ton"
MIN_STARS = 50


class FragmentError(RuntimeError):
    pass


def _raw(addr: str) -> str:
    from ton_core import Address

    return Address(addr).to_str(is_user_friendly=False)


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
        self.has_cookies = bool(cookies.get("stel_ssid") and cookies.get("stel_token"))
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

    async def status(self) -> dict[str, Any]:
        """Проверка сессии: есть ли куки и считает ли Fragment нас залогиненными."""
        self._hash = None
        try:
            await self._api_hash()
        except Exception as e:
            return {"has_cookies": self.has_cookies, "authorized": False, "error": str(e)}
        return {"has_cookies": self.has_cookies, "authorized": self.authorized}

    async def call(self, method: str, referer: str = "/stars/buy", **params: Any) -> dict[str, Any]:
        if not self.has_cookies:
            raise FragmentError("Нет кук Fragment — вставь их в админке (раздел «Сессия Fragment»)")
        try:
            return await self._call(method, referer, **params)
        except (FragmentError, httpx.HTTPError) as e:
            # hash страницы мог смениться — пробуем один раз со свежим
            if "Access denied" in str(e) or isinstance(e, httpx.HTTPError):
                self._hash = None
                return await self._call(method, referer, **params)
            raise

    async def _call(self, method: str, referer: str, **params: Any) -> dict[str, Any]:
        api_hash = await self._api_hash()
        if not self.authorized:
            raise FragmentError("Fragment видит сессию как неавторизованную — обнови куки в админке")
        data = {k: str(v) for k, v in params.items() if v is not None}
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
        if resp.get("need_verify"):
            raise FragmentError("Fragment требует верификацию аккаунта — пройди её на fragment.com в браузере")
        if resp.get("need_ton"):
            raise FragmentError("В сессии Fragment не подключён TON-кошелёк — нажми Connect TON на fragment.com и обнови куки")
        return resp

    @staticmethod
    def _parse_tx(req_id: str, resp: dict[str, Any], wallet_account: str) -> FragmentTx:
        if resp.get("evm"):
            raise FragmentError("Fragment выставил счёт в EVM-сети — нужен payment_method=ton")
        tx = resp.get("transaction") or {}
        msgs = tx.get("messages") or []
        if not resp.get("ok") or len(msgs) != 1:
            raise FragmentError(f"Неожиданный ответ с транзакцией: {str(resp)[:300]}")
        # счёт должен быть выставлен нашему кошельку (тому, что подключён к сессии Fragment)
        sender = tx.get("from")
        if sender and wallet_account:
            ours = json.loads(wallet_account)["address"]
            if _raw(sender) != _raw(ours):
                raise FragmentError(
                    "В Fragment подключён другой кошелёк, не кошелёк сервиса. "
                    "Импортируй мнемонику сервиса в Tonkeeper, подключи его на fragment.com и обнови куки"
                )
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
        init = await self.call(
            "initBuyStarsRequest", recipient=rec.recipient, quantity=quantity, payment_method=PAYMENT_METHOD
        )
        req_id = init.get("req_id")
        if not req_id:
            raise FragmentError(f"initBuyStarsRequest без req_id: {init}")
        link = await self.call(
            "getBuyStarsLink",
            transaction=1,
            id=req_id,
            show_sender=0,
        )
        return rec, self._parse_tx(req_id, link, wallet_account)

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
            "initGiftPremiumRequest",
            referer="/premium/gift",
            recipient=rec.recipient,
            months=months,
            payment_method=PAYMENT_METHOD,
        )
        req_id = init.get("req_id")
        if not req_id:
            raise FragmentError(f"initGiftPremiumRequest без req_id: {init}")
        link = await self.call(
            "getGiftPremiumLink",
            referer="/premium/gift",
            transaction=1,
            id=req_id,
            show_sender=0,
        )
        return rec, self._parse_tx(req_id, link, wallet_account)
