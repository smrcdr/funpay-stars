"""CLI: uv run funpay-stars <команда>"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import uuid
from dataclasses import asdict


def main() -> None:
    p = argparse.ArgumentParser(prog="funpay-stars")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("wallet-create", help="создать TON-кошелёк W5, мнемоника пишется в .env")
    sub.add_parser("balance", help="адрес и баланс кошелька")

    st = sub.add_parser("stars", help="купить звёзды")
    st.add_argument("username")
    st.add_argument("quantity", type=int)
    st.add_argument("--order-id", default=None)
    st.add_argument("--pay", action="store_true", help="реально оплатить (без флага — только цена)")

    pr = sub.add_parser("premium", help="подарить Telegram Premium")
    pr.add_argument("username")
    pr.add_argument("months", type=int, choices=[3, 6, 12])
    pr.add_argument("--order-id", default=None)
    pr.add_argument("--pay", action="store_true", help="реально оплатить (без флага — только цена)")

    sv = sub.add_parser("serve", help="запустить HTTP API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)

    sub.add_parser("gen-token", help="сгенерировать API_TOKEN")

    a = p.parse_args()

    if a.cmd == "wallet-create":
        from .wallet import create_wallet_to_env

        addr = create_wallet_to_env()
        print("Кошелёк создан, мнемоника сохранена в .env (сделай бэкап!)")
        print("Адрес для пополнения (сеть TON):", addr)
        return

    if a.cmd == "gen-token":
        print(secrets.token_urlsafe(32))
        return

    if a.cmd == "serve":
        import uvicorn

        uvicorn.run("funpay_stars.server:app", host=a.host, port=a.port)
        return

    asyncio.run(_async_cmd(a))


async def _async_cmd(a) -> None:
    from .config import load_settings
    from .service import StarsService

    s = load_settings()
    if a.cmd == "balance":
        from .wallet import TonWallet

        async with TonWallet(s) as w:
            print(json.dumps({"address": w.address, "balance_ton": str(await w.balance())}, indent=2))
        return

    async with StarsService(s) as svc:
        order_id = a.order_id or f"cli-{uuid.uuid4().hex[:8]}"
        if a.cmd == "stars":
            res = await svc.buy_stars(order_id, a.username, a.quantity, dry_run=not a.pay)
        else:
            res = await svc.gift_premium(order_id, a.username, a.months, dry_run=not a.pay)
        print(json.dumps(asdict(res), ensure_ascii=False, indent=2))
