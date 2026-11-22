"""Лоты FunPay (для плагина Cardinal): настройки, план автоактивации, статистика продаж.

Плагин присылает список своих лотов (sync) и получает в ответ, какие из них
должны быть включены. Правило: лот активен, если на кошельке хватает TON на
выдачу `max(stars_per_unit × min_units, 50)` звёзд и сессия Fragment жива.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from decimal import Decimal

MIN_STARS = 50
FEE_RESERVE = Decimal("0.01")

_STARS_RE = re.compile(r"(\d[\d\s.,]*)\s*(?:зв[её]зд|звезд|stars?\b|⭐|★)", re.I)
_STARS_HINT_RE = re.compile(r"зв[её]зд|stars?\b|⭐|★", re.I)


def guess_stars_per_unit(title: str) -> int:
    """«400 звёзд» → 400, «1 000 ⭐» → 1000; число не найдено → 1 (1 шт. = 1 звезда)."""
    m = _STARS_RE.search(title or "")
    if not m:
        return 1
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else 1


def looks_like_stars(text: str) -> bool:
    return bool(_STARS_HINT_RE.search(text or ""))


LOT_FIELDS = (
    "lot_id", "title", "subcategory_id", "subcategory", "price", "currency", "stars_per_unit",
    "auto_delivery", "auto_activate", "min_units", "active", "seen_at", "updated_at",
)


class LotStore:
    def __init__(self, path) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS funpay_lots (
                lot_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                subcategory_id TEXT NOT NULL DEFAULT '',
                subcategory TEXT NOT NULL DEFAULT '',
                price REAL,
                currency TEXT NOT NULL DEFAULT '',
                stars_per_unit INTEGER NOT NULL DEFAULT 1,
                auto_delivery INTEGER NOT NULL DEFAULT 1,
                auto_activate INTEGER NOT NULL DEFAULT 1,
                min_units INTEGER NOT NULL DEFAULT 1,
                active INTEGER,
                seen_at INTEGER,
                updated_at INTEGER
            )"""
        )
        self.db.commit()

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("auto_delivery", "auto_activate"):
            d[k] = bool(d[k])
        d["active"] = None if d["active"] is None else bool(d["active"])
        return d

    def list(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM funpay_lots ORDER BY subcategory, stars_per_unit, title").fetchall()
        return [self._row(r) for r in rows]

    def get(self, lot_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM funpay_lots WHERE lot_id=?", (str(lot_id),)).fetchone()
        return self._row(r) if r else None

    def sync(self, lots: list[dict]) -> None:
        """Новые лоты добавляются с угаданным stars_per_unit; у известных обновляется только то,
        что приходит с FunPay (название, цена, active). Настройки из админки не трогаем."""
        now = int(time.time())
        for lot in lots:
            lot_id = str(lot["lot_id"])
            title = str(lot.get("title") or "")
            known = self.get(lot_id)
            if known is None:
                self.db.execute(
                    """INSERT INTO funpay_lots (lot_id, title, subcategory_id, subcategory, price, currency,
                       stars_per_unit, active, seen_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (lot_id, title, str(lot.get("subcategory_id") or ""), str(lot.get("subcategory") or ""),
                     lot.get("price"), str(lot.get("currency") or ""), guess_stars_per_unit(title),
                     _b(lot.get("active")), now, now),
                )
            else:
                self.db.execute(
                    """UPDATE funpay_lots SET title=?, subcategory_id=COALESCE(NULLIF(?, ''), subcategory_id),
                       subcategory=COALESCE(NULLIF(?, ''), subcategory), price=COALESCE(?, price),
                       currency=COALESCE(NULLIF(?, ''), currency), active=COALESCE(?, active), seen_at=?
                       WHERE lot_id=?""",
                    (title or known["title"], str(lot.get("subcategory_id") or ""), str(lot.get("subcategory") or ""),
                     lot.get("price"), str(lot.get("currency") or ""), _b(lot.get("active")), now, lot_id),
                )
        self.db.commit()

    def update(self, lot_id: str, **fields) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in ("stars_per_unit", "auto_delivery", "auto_activate", "min_units") and v is not None}
        if allowed:
            sets = ", ".join(f"{k}=?" for k in allowed)
            vals = [int(v) for v in allowed.values()]
            self.db.execute(f"UPDATE funpay_lots SET {sets}, updated_at=? WHERE lot_id=?", (*vals, int(time.time()), str(lot_id)))
            self.db.commit()
        return self.get(lot_id)

    def delete(self, lot_id: str) -> None:
        self.db.execute("DELETE FROM funpay_lots WHERE lot_id=?", (str(lot_id),))
        self.db.commit()


def _b(v) -> int | None:
    return None if v is None else int(bool(v))


def need_stars(lot: dict) -> int:
    return max(lot["stars_per_unit"] * max(lot["min_units"], 1), MIN_STARS)


def plan(lots: list[dict], balance_ton: Decimal | None, ton_per_star: Decimal | None, fragment_ok: bool) -> dict:
    """Желаемое состояние лотов. Если баланс или цену узнать не удалось — want_active=None
    (плагин ничего не трогает), чтобы сбой котировки не выключил все лоты."""
    affordable = None
    if balance_ton is not None and ton_per_star:
        affordable = math.floor(max(balance_ton - FEE_RESERVE, Decimal(0)) / ton_per_star)
    out = []
    for lot in lots:
        want, reason = None, ""
        if not lot["auto_activate"]:
            reason = "автоактивация выключена"
        elif not fragment_ok:
            want, reason = False, "сессия Fragment не авторизована"
        elif affordable is None:
            reason = "нет данных о балансе/цене"
        else:
            need = need_stars(lot)
            want = affordable >= need
            reason = f"нужно {need} ⭐, хватает на {affordable}"
        out.append({**lot, "want_active": want, "reason": reason})
    return {
        "lots": out,
        "stars_affordable": affordable,
        "balance_ton": None if balance_ton is None else str(balance_ton),
        "ton_per_star": None if ton_per_star is None else str(ton_per_star),
        "fragment_ok": fragment_ok,
    }


# ---------- статистика продаж ----------


def _d(v) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal(0)
    except Exception:
        return Decimal(0)


def _money(v: Decimal) -> str:
    return str(v.quantize(Decimal("0.01")))


def stats(db_path, days: int = 30, now: float | None = None, recent: int = 50) -> dict:
    """Выручка / себестоимость / прибыль по заказам FunPay + расход на ручные выдачи."""
    now = now or time.time()
    day0 = now - now % 86400
    since = day0 - (days - 1) * 86400

    def bucket() -> dict:
        return {"orders": 0, "stars": 0, "revenue_rub": Decimal(0), "cost_rub": Decimal(0), "cost_ton": Decimal(0)}

    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT created_at, status, data FROM orders ORDER BY created_at DESC").fetchall()
        finally:
            db.close()
    except sqlite3.OperationalError:
        rows = []

    periods = {"today": day0, "week": now - 7 * 86400, "month": now - 30 * 86400, "all": 0}
    tot = {k: bucket() for k in periods}
    manual = bucket()
    daily = {int(day0 - i * 86400): bucket() for i in range(days - 1, -1, -1)}
    by_lot: dict[str, dict] = {}
    last: list[dict] = []
    failed = 0
    no_rate = 0

    for created, status, data in rows:
        d = json.loads(data)
        if d.get("kind") != "stars":
            continue
        if status == "failed":
            if d.get("source") == "funpay" or str(d.get("order_id", "")).startswith("fp-"):
                failed += 1
            continue
        if status not in ("sent", "confirmed"):
            continue
        cost_ton = _d(d.get("price_ton"))
        cost_rub = _d(d.get("cost_rub"))
        if d.get("source") != "funpay":
            if created >= since:
                manual["orders"] += 1
                manual["stars"] += d["amount"]
                manual["cost_ton"] += cost_ton
                manual["cost_rub"] += cost_rub
            continue
        rev = _d(d.get("revenue_rub"))
        if not d.get("revenue_rub") or not d.get("cost_rub"):
            no_rate += 1
        for k, t in periods.items():
            if created >= t:
                b = tot[k]
                b["orders"] += 1
                b["stars"] += d["amount"]
                b["revenue_rub"] += rev
                b["cost_rub"] += cost_rub
                b["cost_ton"] += cost_ton
        day = int(created - created % 86400)
        if day in daily:
            b = daily[day]
            b["orders"] += 1
            b["stars"] += d["amount"]
            b["revenue_rub"] += rev
            b["cost_rub"] += cost_rub
            b["cost_ton"] += cost_ton
        if created >= since:
            lb = by_lot.setdefault(d.get("lot_id") or "—", bucket())
            lb["orders"] += 1
            lb["stars"] += d["amount"]
            lb["revenue_rub"] += rev
            lb["cost_rub"] += cost_rub
            lb["cost_ton"] += cost_ton
        if len(last) < recent:
            last.append({
                "at": created, "order_id": d["order_id"], "lot_id": d.get("lot_id"), "buyer": d.get("buyer"),
                "username": d.get("username"), "stars": d["amount"], "status": status,
                "revenue": d.get("revenue"), "currency": d.get("currency"),
                "revenue_rub": d.get("revenue_rub"), "cost_rub": d.get("cost_rub"), "price_ton": d.get("price_ton"),
                "profit_rub": _money(rev - cost_rub) if d.get("revenue_rub") and d.get("cost_rub") else None,
            })

    def fin(b: dict) -> dict:
        profit = b["revenue_rub"] - b["cost_rub"]
        margin = (profit / b["revenue_rub"] * 100) if b["revenue_rub"] else None
        return {
            "orders": b["orders"], "stars": b["stars"],
            "revenue_rub": _money(b["revenue_rub"]), "cost_rub": _money(b["cost_rub"]),
            "cost_ton": str(b["cost_ton"].quantize(Decimal("0.0001"))), "profit_rub": _money(profit),
            "margin_pct": None if margin is None else str(margin.quantize(Decimal("0.1"))),
        }

    return {
        "totals": {k: fin(v) for k, v in tot.items()},
        "daily": [{"day": k, **fin(v)} for k, v in daily.items()],
        "by_lot": {k: fin(v) for k, v in by_lot.items()},
        "manual": fin(manual),
        "recent": last,
        "failed": failed,
        "missing_rate": no_rate,
        "days": days,
    }
