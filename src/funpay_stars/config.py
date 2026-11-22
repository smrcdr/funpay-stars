"""Настройки из .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"

load_dotenv(ENV_PATH)


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # TON-кошелёк (W5 / v5r1 — самая дешёвая по комиссиям версия)
    wallet_mnemonic: str
    toncenter_api_key: str

    # Куки авторизованной сессии fragment.com
    fragment_stel_ssid: str
    fragment_stel_token: str
    fragment_stel_ton_token: str
    fragment_stel_dt: str

    # Защита: максимум TON на один заказ и токен для HTTP API
    max_ton_per_order: Decimal
    api_token: str

    # Комиссия FunPay в %, вычитается из выручки (0 — если сумма заказа уже «на руки»)
    funpay_fee_pct: Decimal

    db_path: Path

    @property
    def fragment_cookies(self) -> dict[str, str]:
        cookies = {
            "stel_ssid": self.fragment_stel_ssid,
            "stel_token": self.fragment_stel_token,
            "stel_ton_token": self.fragment_stel_ton_token,
            "stel_dt": self.fragment_stel_dt,
        }
        return {k: v for k, v in cookies.items() if v}


def load_settings() -> Settings:
    return Settings(
        wallet_mnemonic=_get("WALLET_MNEMONIC"),
        toncenter_api_key=_get("TONCENTER_API_KEY"),
        fragment_stel_ssid=_get("FRAGMENT_STEL_SSID"),
        fragment_stel_token=_get("FRAGMENT_STEL_TOKEN"),
        fragment_stel_ton_token=_get("FRAGMENT_STEL_TON_TOKEN"),
        fragment_stel_dt=_get("FRAGMENT_STEL_DT", "-180"),
        max_ton_per_order=Decimal(_get("MAX_TON_PER_ORDER", "10")),
        api_token=_get("API_TOKEN"),
        funpay_fee_pct=Decimal(_get("FUNPAY_FEE_PCT", "0")),
        db_path=ROOT / _get("DB_PATH", "orders.sqlite3"),
    )
