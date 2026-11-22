import json
import sqlite3
import time
from decimal import Decimal

from funpay_stars import funpay_lots as fl
from funpay_stars import market


def test_guess_stars_per_unit():
    assert fl.guess_stars_per_unit("400 звёзд Telegram") == 400
    assert fl.guess_stars_per_unit("⭐ 1 000 звезд моментально") == 1000
    assert fl.guess_stars_per_unit("Telegram Stars 2500 stars") == 2500
    assert fl.guess_stars_per_unit("Звёзды Telegram по username") == 1


def test_sync_keeps_admin_settings(tmp_path):
    s = fl.LotStore(tmp_path / "db.sqlite3")
    s.sync([{"lot_id": 1, "title": "100 звёзд", "active": True}])
    assert s.get("1")["stars_per_unit"] == 100
    s.update("1", stars_per_unit=150, auto_activate=False)
    s.sync([{"lot_id": 1, "title": "100 звёзд!", "active": False}])
    lot = s.get("1")
    assert lot["stars_per_unit"] == 150 and lot["auto_activate"] is False
    assert lot["title"] == "100 звёзд!" and lot["active"] is False


def test_plan():
    lots = [
        {"lot_id": "a", "stars_per_unit": 1000, "min_units": 1, "auto_activate": True},
        {"lot_id": "b", "stars_per_unit": 1, "min_units": 1, "auto_activate": True},
        {"lot_id": "c", "stars_per_unit": 100, "min_units": 1, "auto_activate": False},
    ]
    p = fl.plan(lots, Decimal("1.01"), Decimal("0.002"), True)  # хватает на 500
    want = {l["lot_id"]: l["want_active"] for l in p["lots"]}
    assert want == {"a": False, "b": True, "c": None}
    assert fl.plan(lots, Decimal("100"), Decimal("0.002"), False)["lots"][0]["want_active"] is False
    assert fl.plan(lots, None, Decimal("0.002"), True)["lots"][0]["want_active"] is None


def test_to_rub():
    rates = {"rub": Decimal("300"), "usd": Decimal("3")}
    assert market.to_rub(Decimal("10"), "usd", rates) == Decimal("1000")
    assert market.to_rub(Decimal("10"), "rub", {}) == Decimal("10")
    assert market.to_rub(Decimal("10"), "eur", rates) is None


def test_stats(tmp_path):
    path = tmp_path / "orders.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY, created_at INTEGER, status TEXT, data TEXT)")
    now = int(time.time())
    rows = [
        ("fp-1", "confirmed", {"order_id": "fp-1", "kind": "stars", "amount": 1000, "price_ton": "2", "source": "funpay",
                               "revenue_rub": "1500", "cost_rub": "1000", "lot_id": "11", "username": "a"}),
        ("fp-2", "failed", {"order_id": "fp-2", "kind": "stars", "amount": 50, "price_ton": "0.1", "source": "funpay"}),
        ("m-1", "confirmed", {"order_id": "m-1", "kind": "stars", "amount": 50, "price_ton": "0.1", "username": "b"}),
    ]
    for oid, st, d in rows:
        db.execute("INSERT INTO orders VALUES (?,?,?,?)", (oid, now, st, json.dumps(d)))
    db.commit()
    s = fl.stats(path)
    t = s["totals"]["today"]
    assert t["orders"] == 1 and t["revenue_rub"] == "1500.00" and t["profit_rub"] == "500.00" and t["margin_pct"] == "33.3"
    assert s["manual"]["orders"] == 1 and s["failed"] == 1
    assert s["recent"][0]["profit_rub"] == "500.00"
    assert s["by_lot"]["11"]["stars"] == 1000
