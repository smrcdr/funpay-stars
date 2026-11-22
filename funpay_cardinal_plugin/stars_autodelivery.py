"""Плагин FunPay Cardinal: автовыдача Telegram Stars через сервер funpay-stars.

Как работает:
1. Новый заказ на лот со звёздами → считаем звёзды (кол-во × «звёзд за 1 шт.» из админки),
   берём username из заказа и спрашиваем покупателя: «верно указан?».
2. «да» → выдаём звёзды через POST /api/v1/stars (order_id = id заказа FunPay, повтор не платит дважды).
   «нет» → просим прислать username, проверяем его через Fragment и снова спрашиваем «верно?».
3. Фоновый поток раз в несколько минут синхронизирует лоты с сервером и включает или выключает
   их по балансу TON (решение принимает сервер), а также повторяет отложенные выдачи.

Установка: положить файл в папку plugins/ Cardinal, перезапустить, затем в Telegram-боте Cardinal:
  /stars_url https://stars.modelport.org
  /stars_token <API_TOKEN>
Или вписать их в storage/plugins/<UUID>/settings.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import requests

NAME = "Stars Autodelivery"
VERSION = "1.1.1"
DESCRIPTION = "Автовыдача Telegram Stars через stars.modelport.org: подтверждение username, автоактивация лотов по балансу, статистика"
CREDITS = "funpay-stars"
UUID = "5b0e9a52-8a3f-4a51-9c4e-1f7d2c6b3a90"
SETTINGS_PAGE = False

logger = logging.getLogger("FPC.stars_autodelivery")
LOG = "[STARS]"

MIN_STARS = 50
LOOP_TICK = 30  # сек
LOTS_EVERY = 240  # сек между синхронизациями лотов
RETRY_EVERY = 300  # сек между повторами отложенных выдач
MAX_RETRIES = 48  # ~4 часа
STATE_KEEP_DAYS = 14

DEFAULT_SETTINGS: dict[str, Any] = {
    "api_url": "https://stars.modelport.org",
    "api_token": "",
    "enabled": True,  # выдача
    "auto_activate": True,  # включать/выключать лоты по балансу
    "subcategory_ids": [],  # доп. подкатегории со звёздами (если не определились сами)
    "texts": {
        "confirm": "Заказ #{order_id}: {stars} ⭐ на @{username}{name}.\n"
                   "Username указан верно? Ответьте «да» или «нет».",
        "ask_username": "Пришлите ваш Telegram username (например, @durov), на который отправить {stars} ⭐.",
        "ask_username_again": "Пришлите правильный Telegram username (например, @durov).",
        "not_found": "Не нашёл пользователя @{username} в Telegram. Проверьте username и пришлите его ещё раз.",
        "yes_or_no": "Ответьте, пожалуйста, «да», если @{username} верный, или «нет», чтобы указать другой.",
        "sending": "Отправляю {stars} ⭐ на @{username}…",
        "done": "✅ Готово! {stars} ⭐ отправлены на @{username}.\n\n"
                "Проверьте Telegram: звёзды уже на балансе 🎉\n\n"
                "🙏 Пожалуйста, подтвердите заказ: кнопка «Подтвердить выполнение» внизу страницы заказа.\n"
                "⭐ И оставьте отзыв, это очень помогает нам и другим покупателям!\n\n"
                "Если звёзды не пришли, напишите сюда, разберёмся 💬",
        "thanks": "🎉 Спасибо, что подтвердили заказ #{order_id}!\n\n"
                  "Будем очень благодарны за отзыв ⭐⭐⭐⭐⭐: он помогает нам продавать дешевле и быстрее.\n"
                  "Оставить отзыв можно на странице заказа: https://funpay.com/orders/{order_id}/\n\n"
                  "Нужны ещё звёзды? Заходите, бот выдаст их автоматически за пару минут ⚡",
        "review_reply_5": "Спасибо за отзыв! ❤️ Рады были помочь, ждём снова ⭐",
        "delayed": "Выдача немного задерживается — звёзды придут автоматически, ничего делать не нужно. "
                   "Если что-то пойдёт не так, продавец свяжется с вами.",
    },
}

# прежние тексты по умолчанию: если в settings.json лежит такой, он заменяется на новый
OLD_DEFAULT_TEXTS = {
    "done": {"✅ {stars} ⭐ отправлены на @{username}. Проверьте Telegram и, пожалуйста, подтвердите заказ."},
}
ORDER_ID_RE = re.compile(r"#([A-Z0-9]{8})\b")

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_EXPLICIT_RE = re.compile(r"(?:t(?:elegram)?\.me/|@)([A-Za-z][A-Za-z0-9_]{3,31})\b", re.I)
_YES_RE = re.compile(r"^(да+|ага|угу|верно|всё верно|все верно|правильно|подтверждаю|yes|yep|y|ok|ок|окей|\+|✅|👍)(\s|[.!,)]|$)", re.I)
_NO_RE = re.compile(r"^(нет|не|неверно|не верно|неправильно|no|nope|n|-|❌)(\s|[.!,]|$)", re.I)
_USERNAME_FIELD_RE = re.compile(r"user|ник|telegram|телеграм|тг|tg|логин|получател|аккаунт", re.I)
_STARS_HINT_RE = re.compile(r"зв[её]зд|stars?\b|⭐|★", re.I)

# состояния заказа
AWAIT_CONFIRM = "await_confirm"
AWAIT_USERNAME = "await_username"
DELIVERING = "delivering"
RETRY = "retry"
DONE = "done"
MANUAL = "manual"  # выдача остановлена, нужен продавец
ACTIVE_STATES = (AWAIT_CONFIRM, AWAIT_USERNAME)


# ---------- разбор текста ----------


def norm_username(text: str | None) -> str | None:
    """'@Durov' / 't.me/durov' / 'durov' → 'durov'-как-есть; иначе None."""
    if not text:
        return None
    text = text.strip()
    m = _EXPLICIT_RE.search(text)
    if m:
        return m.group(1)
    word = text.strip(" .,!;:()[]\"'«»")
    return word if USERNAME_RE.match(word) else None


def is_yes(text: str) -> bool:
    return bool(_YES_RE.match(text.strip()))


def is_no(text: str) -> bool:
    return bool(_NO_RE.match(text.strip()))


def looks_like_stars(text: str) -> bool:
    return bool(_STARS_HINT_RE.search(text or ""))


# слова-заглушки, которые встречаются в текстах лотов вроде «пришлите ваш @username»
_PLACEHOLDERS = {"username", "user", "yourname", "your_username", "nickname", "telegram"}


def _real(u: str | None) -> str | None:
    return u if u and u.lower() not in _PLACEHOLDERS else None


def username_from_order(fields: dict[str, str], texts: list[str]) -> str | None:
    """Username, который ввёл покупатель при оплате.

    FunPay кладёт его в поле «player» (в форме заказа это «Telegram username»). Потом смотрим поля
    с «username/ник/telegram» в названии и любое @username / t.me/ в полях покупателя.
    Тексты продавца (описание лота) сюда не передаются: там бывают примеры вроде «@username».
    """
    if u := _real(norm_username(fields.get("player"))):
        return u
    for name, value in fields.items():
        if _USERNAME_FIELD_RE.search(name or "") and (u := _real(norm_username(value))):
            return u
    for value in list(fields.values()) + texts:
        for m in _EXPLICIT_RE.finditer(value or ""):
            if u := _real(m.group(1)):
                return u
    return None


def match_lot(lots: list[dict], title: str, subcategory_id: str | None = None) -> dict | None:
    """Лот заказа по названию: точное совпадение, затем самое длинное вхождение."""
    t = (title or "").strip().lower()
    if not t:
        return None
    pool = [l for l in lots if not subcategory_id or not l.get("subcategory_id") or str(l["subcategory_id"]) == str(subcategory_id)] or lots
    for lot in pool:
        if (lot.get("title") or "").strip().lower() == t:
            return lot
    best = None
    for lot in pool:
        lt = (lot.get("title") or "").strip().lower()
        if lt and (lt in t or t in lt) and (best is None or len(lt) > len(best["title"])):
            best = lot
    return best


# ---------- HTTP к серверу ----------


class ApiError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class StarsApi:
    def __init__(self, get_settings: Callable[[], dict]):
        self._settings = get_settings
        self.http = requests.Session()

    def _req(self, method: str, path: str, timeout: float = 30, **kw) -> Any:
        s = self._settings()
        if not s.get("api_url") or not s.get("api_token"):
            raise ApiError("не заданы api_url / api_token (команды /stars_url и /stars_token)")
        url = s["api_url"].rstrip("/") + "/api/v1" + path
        try:
            r = self.http.request(method, url, headers={"Authorization": f"Bearer {s['api_token']}"}, timeout=timeout, **kw)
        except requests.RequestException as e:
            raise ApiError(f"сеть: {e}") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text[:200]
            raise ApiError(str(detail), r.status_code)
        return r.json()

    def recipient(self, username: str) -> dict:
        return self._req("GET", "/recipient", params={"username": username, "kind": "stars"})

    def stars(self, body: dict) -> dict:
        # сервер ждёт подтверждения сети — до пары минут
        return self._req("POST", "/stars", json=body, timeout=240)

    def sync_lots(self, lots: list[dict]) -> dict:
        return self._req("POST", "/funpay/lots/sync", json={"lots": lots}, timeout=60)

    def balance(self) -> dict:
        return self._req("GET", "/balance")


# ---------- состояние ----------


@dataclass
class Pending:
    order_id: str
    chat_id: str
    chat_name: str
    buyer: str
    buyer_id: int | None
    stars: int
    lot_id: str
    revenue: float | None
    currency: str
    username: str = ""
    rec_name: str = ""
    state: str = AWAIT_USERNAME
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    attempts: int = 0
    next_try: float = 0
    error: str = ""
    thanked: bool = False  # отправили «спасибо за подтверждение»
    reviewed: int = 0  # оценка отзыва, на который уже отреагировали


class StarsBot:
    """Логика без зависимостей от Cardinal (её проверяют тесты).

    send(chat_id, text, chat_name) — сообщение покупателю; notify(text) — продавцу в TG.
    """

    def __init__(self, storage_dir: str, send: Callable[[str, str, str], Any], notify: Callable[[str], Any],
                 api: StarsApi | None = None, spawn: Callable[[Callable], Any] | None = None):
        self.dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)
        self.send_raw = send
        self.notify = notify
        self.lock = threading.RLock()
        self.settings = self._load("settings.json", {})
        self._fill_defaults()
        raw = self._load("state.json", {})
        self.orders: dict[str, Pending] = {k: Pending(**v) for k, v in raw.get("orders", {}).items()}
        self.lots: list[dict] = raw.get("lots", [])
        self.subcats: list[str] = raw.get("subcats", [])
        self.api = api or StarsApi(lambda: self.settings)
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._toggle_errors: set[str] = set()
        self._claimed: set[str] = set()

    # ----- файлы -----

    def _load(self, name: str, default):
        try:
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _dump(self, name: str, data) -> None:
        path = os.path.join(self.dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _fill_defaults(self) -> None:
        for k, v in DEFAULT_SETTINGS.items():
            if k == "texts":
                self.settings.setdefault("texts", {})
                for tk, tv in v.items():
                    if self.settings["texts"].get(tk) in OLD_DEFAULT_TEXTS.get(tk, ()):
                        self.settings["texts"][tk] = tv
                    self.settings["texts"].setdefault(tk, tv)
            else:
                self.settings.setdefault(k, v)

    def save_settings(self) -> None:
        with self.lock:
            self._dump("settings.json", self.settings)

    def save_state(self) -> None:
        with self.lock:
            cutoff = time.time() - STATE_KEEP_DAYS * 86400
            self.orders = {k: v for k, v in self.orders.items() if v.state not in (DONE,) or v.updated > cutoff}
            # заказ на подтверждении и отзыв приходят позже выдачи, поэтому DONE держим STATE_KEEP_DAYS
            self._dump("state.json", {
                "orders": {k: asdict(v) for k, v in self.orders.items()},
                "lots": self.lots,
                "subcats": self.subcats,
            })

    def text(self, key: str, p: Pending, **extra) -> str:
        name = f" ({p.rec_name})" if p.rec_name else ""
        return self.settings["texts"][key].format(
            order_id=p.order_id, stars=p.stars, username=p.username, name=name, **extra)

    def send(self, p: Pending, text: str) -> None:
        try:
            self.send_raw(p.chat_id, text, p.chat_name)
        except Exception as e:
            logger.warning(f"{LOG} не отправил сообщение в чат {p.chat_id}: {e}")

    # ----- подтверждение заказа и отзыв -----

    def on_order_closed(self, order_id: str) -> None:
        """Покупатель подтвердил заказ: благодарим и просим отзыв (один раз, только выданные ботом)."""
        with self.lock:
            p = self.orders.get(order_id)
            if p is None or p.state != DONE or p.thanked:
                return
            p.thanked, p.updated = True, time.time()
            self.save_state()
        self.send(p, self.text("thanks", p))

    def on_review(self, order_id: str, stars: int | None, text: str, reply: Callable[[str], Any]) -> None:
        """Новый или изменённый отзыв. 5★ → отвечаем на отзыв, 1–4★ → уведомляем продавца."""
        if not stars:
            return
        with self.lock:
            p = self.orders.get(order_id)
            if p is None or p.reviewed == stars:
                return
            p.reviewed, p.updated = stars, time.time()
            if not p.thanked:
                p.thanked = True  # отзыв уже оставлен, просить его не нужно
            self.save_state()
        if stars == 5:
            try:
                reply(self.text("review_reply_5", p))
            except Exception as e:
                logger.warning(f"{LOG} не ответил на отзыв #{order_id}: {e}")
                self.notify(f"⚠️ Не удалось ответить на отзыв 5★ по заказу #{order_id}: {e}")
        else:
            self.notify(f"⚠️ Отзыв {'⭐' * stars} на заказ #{order_id} (@{p.username}, {p.stars} ⭐):\n"
                        f"{text or '(без текста)'}\n\nОтветьте сами: https://funpay.com/orders/{order_id}/")

    # ----- новый заказ -----

    def on_new_order(self, order: dict) -> None:
        """order: {id, title, amount, sum, currency, buyer, buyer_id, chat_id, subcategory_id,
        subcategory, fields: {name: value}, texts: [..]}"""
        if not self.settings.get("enabled"):
            return
        oid = str(order["id"])
        with self.lock:
            # заказ может прийти и событием Cardinal, и из догонялки: обрабатываем один раз
            if oid in self.orders or oid in self._claimed:
                return
            self._claimed.add(oid)
        title = order.get("title") or ""
        lot = match_lot(self.lots, title, order.get("subcategory_id"))
        if lot is None and (looks_like_stars(title) or looks_like_stars(order.get("subcategory") or "")):
            self.refresh_lots_quiet()  # новый лот, которого ещё нет в списке
            lot = match_lot(self.lots, title, order.get("subcategory_id"))
        if lot is None:
            if looks_like_stars(title) or looks_like_stars(order.get("subcategory") or ""):
                self.notify(f"⚠️ Заказ #{oid} похож на звёзды, но лот не найден в списке сервера: «{title}». "
                            f"Выдайте вручную или проверьте лоты в админке.")
            return
        if not lot.get("auto_delivery", True):
            return
        amount = int(order.get("amount") or 1)
        stars = amount * int(lot.get("stars_per_unit") or 1)
        p = Pending(
            order_id=oid, chat_id=str(order.get("chat_id") or ""), chat_name=order.get("buyer") or "",
            buyer=order.get("buyer") or "", buyer_id=order.get("buyer_id"), stars=stars,
            lot_id=str(lot["lot_id"]), revenue=order.get("sum"), currency=order.get("currency") or "rub",
        )
        if stars < MIN_STARS:
            p.state, p.error = MANUAL, f"{stars} ⭐ меньше минимума Fragment ({MIN_STARS})"
            with self.lock:
                self.orders[oid] = p
                self.save_state()
            self.notify(f"⚠️ Заказ #{oid}: {amount} шт. × {lot.get('stars_per_unit')} = {stars} ⭐ — меньше {MIN_STARS}, "
                        f"Fragment столько не продаёт. Выдайте вручную или верните деньги.")
            return
        with self.lock:
            self.orders[oid] = p
        username = username_from_order(order.get("fields") or {}, order.get("texts") or [])
        logger.info(f"{LOG} заказ #{oid}: {stars} ⭐, username из заказа: {username}")
        if username:
            self._propose(p, username)
        else:
            p.state = AWAIT_USERNAME
            self.send(p, self.text("ask_username", p))
        self.save_state()

    def _propose(self, p: Pending, username: str) -> None:
        """Проверить username через Fragment и спросить «верно?»."""
        try:
            rec = self.api.recipient(username)
            p.username, p.rec_name = rec.get("username") or username, rec.get("name") or ""
        except ApiError as e:
            if e.status == 400:  # Fragment не нашёл такого пользователя
                p.username, p.rec_name, p.state = username, "", AWAIT_USERNAME
                self.send(p, self.text("not_found", p))
                return
            p.username, p.rec_name = username, ""  # сервер недоступен: спросим без имени
            logger.warning(f"{LOG} не смог проверить @{username}: {e}")
        p.state, p.updated = AWAIT_CONFIRM, time.time()
        self.send(p, self.text("confirm", p))

    # ----- сообщения покупателя -----

    def pending_for_chat(self, chat_id: str, author_id: int | None = None) -> Pending | None:
        """Заказ, ждущий ответа в этом чате. У заказа chat_id бывает вида «users-1-2», а у сообщения
        числовой, поэтому сверяем и по id покупателя."""
        with self.lock:
            items = [p for p in self.orders.values() if p.state in ACTIVE_STATES and (
                p.chat_id == str(chat_id) or (author_id is not None and p.buyer_id == author_id))]
        return min(items, key=lambda p: p.created) if items else None

    def on_message(self, chat_id: str, text: str, author_id: int | None = None) -> None:
        p = self.pending_for_chat(chat_id, author_id)
        if p is None or not text:
            return
        text = text.strip()
        with self.lock:
            if p.state == AWAIT_CONFIRM:
                explicit = _EXPLICIT_RE.search(text)
                if is_yes(text) and not explicit:
                    self._start_delivery(p)
                elif explicit:
                    self._propose(p, explicit.group(1))
                elif is_no(text):
                    p.state, p.updated = AWAIT_USERNAME, time.time()
                    self.send(p, self.text("ask_username_again", p))
                elif u := norm_username(text):
                    self._propose(p, u)
                else:
                    self.send(p, self.text("yes_or_no", p))
            elif p.state == AWAIT_USERNAME:
                u = norm_username(text)
                if u and not (is_yes(text) or is_no(text)):
                    self._propose(p, u)
                else:
                    self.send(p, self.text("ask_username_again", p))
            self.save_state()

    # ----- выдача -----

    def _start_delivery(self, p: Pending) -> None:
        p.state, p.updated = DELIVERING, time.time()
        self.send(p, self.text("sending", p))
        self.save_state()
        self.spawn(lambda: self.deliver(p.order_id))

    def deliver(self, order_id: str) -> None:
        with self.lock:
            p = self.orders.get(order_id)
            if p is None or p.state not in (DELIVERING, RETRY):
                return
            p.state, p.attempts = DELIVERING, p.attempts + 1
            self.save_state()
        body = {
            "order_id": f"fp-{p.order_id}", "username": p.username, "quantity": p.stars,
            "revenue": p.revenue, "currency": p.currency, "lot_id": p.lot_id, "buyer": p.buyer,
        }
        try:
            res = self.api.stars(body)
            status, detail = res.get("status"), res.get("detail") or ""
        except ApiError as e:
            status, detail = "error", str(e)
        with self.lock:
            p.updated = time.time()
            if status in ("sent", "confirmed"):
                p.state, p.error = DONE, ""
                self.send(p, self.text("done", p))
                self.notify(f"✅ #{p.order_id}: {p.stars} ⭐ → @{p.username} ({res.get('price_ton')} TON"
                            f"{', ждём подтверждения сети' if status == 'sent' else ''})")
            else:
                p.error = detail
                first = p.attempts == 1
                if p.attempts >= MAX_RETRIES:
                    p.state = MANUAL
                    self.notify(f"❌ #{p.order_id}: выдача {p.stars} ⭐ → @{p.username} остановлена после "
                                f"{p.attempts} попыток: {detail}\nПовторить: /stars_retry {p.order_id}")
                else:
                    p.state, p.next_try = RETRY, time.time() + RETRY_EVERY
                    if first:
                        self.send(p, self.text("delayed", p))
                        self.notify(f"⏳ #{p.order_id}: не удалось выдать {p.stars} ⭐ → @{p.username}: {detail}\n"
                                    f"Повторяю каждые {RETRY_EVERY // 60} мин. Если не хватает TON — пополните кошелёк.")
            self.save_state()

    def retry_due(self) -> None:
        now = time.time()
        with self.lock:
            due = [p.order_id for p in self.orders.values()
                   if (p.state == RETRY and p.next_try <= now)
                   or (p.state == DELIVERING and now - p.updated > 600)]  # завис после рестарта
        for oid in due:
            self.deliver(oid)

    def force_retry(self, order_id: str) -> str:
        with self.lock:
            p = self.orders.get(order_id.lstrip("#"))
            if p is None:
                return "заказ не найден"
            if not p.username:
                return "у заказа ещё нет подтверждённого username"
            p.state, p.attempts, p.next_try = RETRY, 0, 0
            self.save_state()
        self.spawn(lambda: self.deliver(p.order_id))
        return f"повторяю выдачу #{p.order_id}: {p.stars} ⭐ → @{p.username}"

    # ----- лоты -----

    def refresh_lots(self, fetch_lots: Callable[[list[str]], list[dict]] | None = None,
                     set_active: Callable[[str, bool], bool] | None = None) -> list[str]:
        """Синхронизация лотов с сервером и автоактивация. Возвращает список изменений."""
        fetch_lots = fetch_lots or self._fetch_lots
        set_active = set_active or self._set_active
        if fetch_lots is None:
            return []
        subcats = sorted(set(self.subcats) | {str(x) for x in self.settings.get("subcategory_ids") or []})
        found = fetch_lots(subcats)
        stars_lots = [l for l in found if looks_like_stars(l.get("title", "")) or looks_like_stars(l.get("subcategory", ""))
                      or str(l.get("subcategory_id")) in subcats]
        plan = self.api.sync_lots(stars_lots)
        with self.lock:
            self.lots = plan.get("lots", [])
            self.subcats = sorted({str(l["subcategory_id"]) for l in self.lots if l.get("subcategory_id")} | set(self.subcats))
            self.save_state()
        changes = []
        if not self.settings.get("auto_activate") or set_active is None:
            return changes
        seen = {str(l["lot_id"]): l for l in stars_lots}
        for lot in self.lots:
            lot_id = str(lot["lot_id"])
            want = lot.get("want_active")
            # лота нет среди найденных → его не видно в профиле, т.е. он выключен (или удалён)
            cur = seen[lot_id].get("active") if lot_id in seen else False
            if want is None or cur is None or bool(cur) == bool(want):
                self._toggle_errors.discard(lot_id)
                continue
            try:
                if set_active(lot_id, bool(want)):
                    self._toggle_errors.discard(lot_id)
                    changes.append(f"{'🟢 включил' if want else '🔴 выключил'} «{lot.get('title')}» ({lot.get('reason')})")
            except Exception as e:
                logger.warning(f"{LOG} лот {lot_id}: {e}")
                if lot_id not in self._toggle_errors:  # не спамим каждые 4 минуты
                    self._toggle_errors.add(lot_id)
                    changes.append(f"⚠️ не смог {'включить' if want else 'выключить'} «{lot.get('title')}»: {e} "
                                   f"(если лот удалён — уберите его в админке)")
        if changes:
            self.notify("Лоты звёзд:\n" + "\n".join(changes))
        return changes

    # подставляются адаптером Cardinal
    _fetch_lots: Callable[[list[str]], list[dict]] | None = None
    _set_active: Callable[[str, bool], bool] | None = None

    def refresh_lots_quiet(self) -> None:
        try:
            self.refresh_lots()
        except Exception as e:
            logger.warning(f"{LOG} синхронизация лотов: {e}")

    def status_text(self) -> str:
        lines = [f"⭐ {NAME} v{VERSION}", f"Сервер: {self.settings.get('api_url')}",
                 f"Выдача: {'вкл' if self.settings.get('enabled') else 'ВЫКЛ'}, "
                 f"автоактивация: {'вкл' if self.settings.get('auto_activate') else 'ВЫКЛ'}"]
        try:
            b = self.api.balance()
            lines.append(f"Баланс: {b.get('balance_ton')} TON")
        except ApiError as e:
            lines.append(f"Сервер недоступен: {e}")
        lines.append(f"Лотов звёзд: {len(self.lots)}")
        with self.lock:
            active = [p for p in self.orders.values() if p.state not in (DONE,)]
        if active:
            lines.append("Незавершённые заказы:")
            for p in sorted(active, key=lambda x: x.created)[:20]:
                lines.append(f"• #{p.order_id} {p.stars} ⭐ @{p.username or '?'} — {p.state}{(': ' + p.error[:80]) if p.error else ''}")
        return "\n".join(lines)


# ======================================================================
# Адаптер FunPay Cardinal
# ======================================================================

BOT: StarsBot | None = None


def _currency(c) -> str:
    try:
        code = c.code
    except Exception:
        code = str(c or "")
    code = str(code).lower()
    return {"₽": "rub", "$": "usd", "€": "eur"}.get(code, code or "rub")


_SELLER_FIELDS = {"desc", "payment_msg", "images", "summary"}


def _field_values(order) -> tuple[dict[str, str], list[str]]:
    """Все поля заказа, где покупатель мог указать username (разные версии FunPayAPI)."""
    fields: dict[str, str] = {}
    for attr in ("fields", "buyer_params", "lot_params"):
        val = getattr(order, attr, None)
        if isinstance(val, dict):
            for k, v in val.items():
                if str(k) in _SELLER_FIELDS:
                    continue  # описание и сообщение после оплаты пишет продавец, не покупатель
                name = getattr(v, "name", None) or str(k)
                value = getattr(v, "value", v)
                if isinstance(value, dict):
                    value = value.get("ru") or value.get("en") or ""
                fields[str(name)] = str(value or "")
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    fields[str(item[0])] = str(item[1] or "")
    if getattr(order, "player", None):
        fields["player"] = str(order.player)
    return fields, []


def _notify_factory(cardinal):
    def notify(text: str) -> None:
        logger.info(f"{LOG} {text}")
        tg = getattr(cardinal, "telegram", None)
        if not tg:
            return
        users = getattr(tg, "authorized_users", None) or []
        for uid in list(users):
            try:
                tg.bot.send_message(uid, text)
            except Exception as e:
                logger.warning(f"{LOG} TG {uid}: {e}")
    return notify


def _fetch_lots_factory(cardinal):
    def fetch(subcats: list[str]) -> list[dict]:
        acc = cardinal.account
        out: dict[str, dict] = {}

        def add(lot, active):
            sub = getattr(lot, "subcategory", None)
            out[str(lot.id)] = {
                "lot_id": str(lot.id),
                "title": getattr(lot, "description", None) or getattr(lot, "title", None) or "",
                "subcategory_id": str(getattr(sub, "id", "") or ""),
                "subcategory": getattr(sub, "fullname", None) or getattr(sub, "name", None) or "",
                "price": getattr(lot, "price", None),
                "currency": _currency(getattr(lot, "currency", None)),
                "active": active,
            }

        # публичный профиль показывает только активные лоты
        for lot in acc.get_user(acc.id).get_lots():
            add(lot, True)
            sub = getattr(lot, "subcategory", None)
            if sub is not None and (looks_like_stars(getattr(sub, "name", "") or "") or looks_like_stars(getattr(lot, "description", "") or "")):
                subcats = list({*subcats, str(sub.id)})
        # свои лоты в подкатегории (включая выключенные) — если версия FunPayAPI умеет
        if hasattr(acc, "get_my_subcategory_lots"):
            for sid in subcats:
                try:
                    for lot in acc.get_my_subcategory_lots(int(sid)):
                        add(lot, bool(getattr(lot, "active", True)))
                    time.sleep(1)
                except Exception as e:
                    logger.warning(f"{LOG} лоты подкатегории {sid}: {e}")
        return list(out.values())
    return fetch


def _set_active_factory(cardinal):
    def set_active(lot_id: str, active: bool) -> bool:
        acc = cardinal.account
        fields = acc.get_lot_fields(int(lot_id))
        if hasattr(fields, "active"):
            fields.active = active
        if hasattr(fields, "edit_fields"):
            fields.edit_fields({"active": "on" if active else ""})
        acc.save_lot(fields)
        time.sleep(1)
        return True
    return set_active


CATCH_UP_EVERY = 180  # с
CATCH_UP_WINDOW = timedelta(hours=24)  # state хранит выданные заказы 14 дней, так что повторов не будет
MSK = timezone(timedelta(hours=3))  # FunPay показывает время заказов по Москве
_catch_up_skip: set[str] = set()


def _catch_up(cardinal) -> None:
    """Оплаченные заказы на звёзды, которые плагин не видел (Cardinal был выключен или перезапускался,
    событие NEW_ORDER не пришло). Обрабатываем их как новые."""
    res = cardinal.account.get_sales(state="paid", include_closed=False, include_refunded=False)
    sales = next((x for x in res if isinstance(x, list)), []) if isinstance(res, tuple) else res
    now = datetime.now(MSK).replace(tzinfo=None)
    for short in sales:
        oid = short.id
        if oid in BOT.orders or oid in BOT._claimed or oid in _catch_up_skip:
            continue
        date = getattr(short, "date", None)
        if date and now - date > CATCH_UP_WINDOW:
            continue
        text = f"{getattr(short, 'description', '')} {getattr(short, 'subcategory_name', '')}"
        if not looks_like_stars(text):
            _catch_up_skip.add(oid)
            continue
        logger.info(f"{LOG} догоняю пропущенный заказ #{oid}")
        BOT.notify(f"🔁 Нашёл пропущенный заказ #{oid} ({short.description[:60]}), обрабатываю.")
        on_new_order(cardinal, type("Ev", (), {"order": short})())


def _loop(cardinal) -> None:
    last_lots = 0.0
    last_catch = 0.0
    while True:
        try:
            if BOT.settings.get("enabled"):
                BOT.retry_due()
                if time.time() - last_catch > CATCH_UP_EVERY:
                    last_catch = time.time()
                    _catch_up(cardinal)
            if time.time() - last_lots > LOTS_EVERY and BOT.settings.get("api_token"):
                last_lots = time.time()
                BOT.refresh_lots()
        except Exception as e:
            logger.warning(f"{LOG} фоновый цикл: {e}")
        time.sleep(LOOP_TICK)


def init(cardinal, *args):
    global BOT
    storage = os.path.join("storage", "plugins", UUID)
    BOT = StarsBot(
        storage,
        send=lambda chat_id, text, chat_name: cardinal.send_message(chat_id, text, chat_name),
        notify=_notify_factory(cardinal),
    )
    BOT.save_settings()
    _init_telegram(cardinal)
    logger.info(f"{LOG} плагин загружен, настройки: {os.path.abspath(storage)}/settings.json")


def post_init(cardinal, *args):
    BOT._fetch_lots = _fetch_lots_factory(cardinal)
    BOT._set_active = _set_active_factory(cardinal)
    threading.Thread(target=_loop, args=(cardinal,), daemon=True, name="stars-loop").start()


def _init_telegram(cardinal) -> None:
    tg = getattr(cardinal, "telegram", None)
    if not tg:
        return
    bot = tg.bot

    def reply(m, text):
        bot.send_message(m.chat.id, text, disable_web_page_preview=True)

    def arg(m) -> str:
        parts = (m.text or "").split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def h_status(m):
        reply(m, BOT.status_text())

    def h_url(m):
        if a := arg(m):
            BOT.settings["api_url"] = a.rstrip("/")
            BOT.save_settings()
        reply(m, f"api_url: {BOT.settings['api_url']}")

    def h_token(m):
        if a := arg(m):
            BOT.settings["api_token"] = a
            BOT.save_settings()
            try:
                bot.delete_message(m.chat.id, m.message_id)  # не держим токен в истории чата
            except Exception:
                pass
        reply(m, "api_token сохранён" if BOT.settings.get("api_token") else "api_token не задан")

    def h_toggle(key, label):
        def h(m):
            BOT.settings[key] = not BOT.settings.get(key)
            BOT.save_settings()
            reply(m, f"{label}: {'вкл' if BOT.settings[key] else 'ВЫКЛ'}")
        return h

    def h_retry(m):
        reply(m, BOT.force_retry(arg(m)) if arg(m) else "Формат: /stars_retry <id заказа>")

    def h_lots(m):
        try:
            changes = BOT.refresh_lots()
            reply(m, f"Лотов звёзд: {len(BOT.lots)}\n" + ("\n".join(changes) if changes else "Изменений нет"))
        except Exception as e:
            reply(m, f"Ошибка: {e}")

    handlers = [
        ("stars", "статус автовыдачи звёзд", h_status),
        ("stars_url", "адрес сервера звёзд", h_url),
        ("stars_token", "API-токен сервера звёзд", h_token),
        ("stars_toggle", "вкл/выкл автовыдачу звёзд", h_toggle("enabled", "Автовыдача")),
        ("stars_autolots", "вкл/выкл автоактивацию лотов", h_toggle("auto_activate", "Автоактивация лотов")),
        ("stars_lots", "синхронизировать лоты звёзд", h_lots),
        ("stars_retry", "повторить выдачу заказа", h_retry),
    ]
    for cmd, _, fn in handlers:
        tg.msg_handler(fn, commands=[cmd])
    try:
        cardinal.add_telegram_commands(UUID, [(cmd, desc, True) for cmd, desc, _ in handlers])
    except Exception as e:
        logger.warning(f"{LOG} не добавил команды в меню: {e}")


def on_new_order(cardinal, event, *args):
    if BOT is None:
        return
    try:
        short = event.order
        order = cardinal.account.get_order(short.id)
        sub = getattr(order, "subcategory", None) or getattr(short, "subcategory", None)
        fields, texts = _field_values(order)
        title = getattr(short, "description", None) or getattr(order, "short_description", None) or getattr(order, "title", "") or ""
        BOT.on_new_order({
            "id": short.id,
            "title": title,
            "amount": getattr(order, "amount", None) or getattr(short, "amount", None) or 1,
            "sum": getattr(order, "sum", None) or getattr(short, "price", None),
            "currency": _currency(getattr(order, "currency", None) or getattr(short, "currency", None)),
            "buyer": getattr(order, "buyer_username", None) or getattr(short, "buyer_username", "") or "",
            "buyer_id": getattr(order, "buyer_id", None) or getattr(short, "buyer_id", None),
            "chat_id": getattr(order, "chat_id", None) or getattr(short, "chat_id", None),
            "subcategory_id": str(getattr(sub, "id", "") or "") or None,
            "subcategory": getattr(sub, "fullname", None) or getattr(sub, "name", None) or getattr(short, "subcategory_name", "") or "",
            "fields": fields,
            "texts": texts,
        })
    except Exception as e:
        logger.exception(f"{LOG} ошибка обработки заказа: {e}")
        _notify_factory(cardinal)(f"💥 Ошибка обработки заказа {getattr(event.order, 'id', '?')}: {e}")


def _on_system_message(cardinal, msg, kind: str) -> None:
    m = ORDER_ID_RE.search(str(msg.text or msg) or "")
    if not m or BOT is None or m.group(1) not in BOT.orders:
        return
    order_id = m.group(1)
    if kind == "ORDER_CONFIRMED":
        BOT.on_order_closed(order_id)
    elif kind in ("NEW_FEEDBACK", "FEEDBACK_CHANGED"):
        def work():
            try:
                order = cardinal.account.get_order(order_id)
                review = getattr(order, "review", None)
                BOT.on_review(order_id, getattr(review, "stars", None), getattr(review, "text", "") or "",
                              lambda t: cardinal.account.send_review(order_id, t))
            except Exception as e:
                logger.exception(f"{LOG} ошибка обработки отзыва #{order_id}: {e}")
        BOT.spawn(work)


def on_order_status_changed(cardinal, event, *args):
    if BOT is None:
        return
    try:
        status = getattr(getattr(event.order, "status", None), "name", "")
        if status == "CLOSED":
            BOT.on_order_closed(event.order.id)
    except Exception as e:
        logger.exception(f"{LOG} ошибка обработки статуса заказа: {e}")


def on_new_message(cardinal, event, *args):
    if BOT is None:
        return
    try:
        msg = event.message
        kind = getattr(getattr(msg, "type", None), "name", "NON_SYSTEM")
        if kind in ("ORDER_CONFIRMED", "NEW_FEEDBACK", "FEEDBACK_CHANGED"):
            return _on_system_message(cardinal, msg, kind)
        if not msg.author_id or msg.author_id == cardinal.account.id:
            return
        if any(getattr(msg, a, False) for a in ("is_support", "is_moderation", "is_arbitration")):
            return
        mtype = getattr(msg, "type", None)
        if mtype is not None and getattr(mtype, "name", "NON_SYSTEM") != "NON_SYSTEM":
            return
        BOT.on_message(str(msg.chat_id), msg.text or "", msg.author_id)
    except Exception as e:
        logger.exception(f"{LOG} ошибка обработки сообщения: {e}")


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_INIT = [post_init]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_ORDER_STATUS_CHANGED = [on_order_status_changed]
BIND_TO_DELETE = None
