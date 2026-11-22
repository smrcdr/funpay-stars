"""TON-кошелёк W5 (v5r1): создание, баланс, отправка платежей Fragment."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal

from ton_core import Cell, NetworkGlobalID
from tonutils.clients import ToncenterClient
from tonutils.contracts.wallet import TONTransferBuilder, WalletV5R1, seqno_get_method
from tonutils.types import RetryPolicy, RetryRule

from .config import ENV_PATH, Settings

NANO = Decimal(10**9)


def from_nano(value: int) -> Decimal:
    return Decimal(value) / NANO


def to_nano(value: Decimal | str) -> int:
    return int(Decimal(value) * NANO)


def _b64decode(data: str) -> bytes:
    data = data.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(data + "=" * (-len(data) % 4))


RATE_LIMIT_RETRY = RetryPolicy(
    rules=(RetryRule(max_retries=8, base_delay=1.0, max_delay=8.0, backoff_factor=2.0, codes=frozenset({429})),),
    total_timeout=60.0,
)


@dataclass
class PaymentResult:
    destination: str
    amount_ton: Decimal
    seqno_before: int
    confirmed: bool


class TonWallet:
    def __init__(self, settings: Settings) -> None:
        if not settings.wallet_mnemonic:
            raise RuntimeError("WALLET_MNEMONIC пуст — сначала: uv run funpay-stars wallet-create")
        key = settings.toncenter_api_key or None
        self.client = ToncenterClient(
            NetworkGlobalID.MAINNET,
            api_key=key,
            # без ключа toncenter пускает ~1 запрос/с и отвечает 429; при 429 запрос не выполнен,
            # поэтому его безопасно повторить (в том числе sendBoc)
            rps_limit=None if key else 1,
            rps_period=None if key else 1.1,
            retry_policy=RATE_LIMIT_RETRY,
        )
        self.wallet, self.public_key, _, _ = WalletV5R1.from_mnemonic(
            self.client, settings.wallet_mnemonic
        )
        self._lock = asyncio.Lock()  # не даём двум платежам взять один seqno

    async def __aenter__(self) -> "TonWallet":
        await self.client.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.client.close()

    # ---------- инфо ----------

    @property
    def address(self) -> str:
        """Адрес для пополнения (non-bounceable, UQ...)."""
        return self.wallet.address.to_str(is_bounceable=False)

    @property
    def raw_address(self) -> str:
        return self.wallet.address.to_str(is_user_friendly=False)

    def tonconnect_account(self) -> str:
        """JSON-описание кошелька в формате TonConnect — его ждёт Fragment."""
        state_init = self.wallet.state_init.serialize().to_boc()
        return json.dumps(
            {
                "address": self.raw_address,
                "chain": str(NetworkGlobalID.MAINNET.value),
                "walletStateInit": base64.b64encode(state_init).decode(),
                "publicKey": self.public_key.as_hex,
            },
            separators=(",", ":"),
        )

    async def balance(self) -> Decimal:
        await self.wallet.refresh()
        return from_nano(self.wallet.balance)

    async def seqno(self) -> int:
        await self.wallet.refresh()
        if not self.wallet.is_active:
            return 0
        return await seqno_get_method(self.client, self.wallet.address)

    # ---------- платежи ----------

    async def send(
        self,
        destination: str,
        amount_nano: int,
        payload_b64: str | None = None,
        wait_timeout: float = 90.0,
    ) -> PaymentResult:
        """Отправляет TON с телом-payload (как его отдаёт Fragment) и ждёт подтверждения."""
        body = Cell.one_from_boc(_b64decode(payload_b64)) if payload_b64 else None
        async with self._lock:
            seqno_before = await self.seqno()
            await self.wallet.transfer_message(
                TONTransferBuilder(destination=destination, amount=amount_nano, body=body)
            )
            confirmed = await self._wait_seqno(seqno_before, wait_timeout)
        return PaymentResult(destination, from_nano(amount_nano), seqno_before, confirmed)

    async def _wait_seqno(self, seqno_before: int, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(4)
            try:
                if await self.seqno() > seqno_before:
                    return True
            except Exception:
                pass
        return False


def create_wallet_to_env() -> str:
    """Генерирует новый W5-кошелёк, пишет мнемонику в .env (chmod 600). Возвращает адрес."""
    existing = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    if re.search(r"^WALLET_MNEMONIC=\S+", existing, re.M):
        raise RuntimeError(f"В {ENV_PATH} уже есть WALLET_MNEMONIC — не перезаписываю.")

    client = ToncenterClient(NetworkGlobalID.MAINNET)
    wallet, _, _, mnemonic = WalletV5R1.create(client, mnemonic_length=24)
    line = f"WALLET_MNEMONIC={' '.join(mnemonic)}\n"

    if re.search(r"^WALLET_MNEMONIC=\s*$", existing, re.M):
        content = re.sub(r"^WALLET_MNEMONIC=\s*$\n?", line, existing, count=1, flags=re.M)
    else:
        content = existing + ("" if existing.endswith("\n") or not existing else "\n") + line

    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(ENV_PATH, 0o600)
    return wallet.address.to_str(is_bounceable=False)
