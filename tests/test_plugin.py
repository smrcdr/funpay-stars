import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "funpay_cardinal_plugin"))
import stars_autodelivery as sa  # noqa: E402


class FakeApi:
    def __init__(self, known=("durov", "good_user"), stars_status="confirmed"):
        self.known = set(known)
        self.stars_status = stars_status
        self.stars_calls = []
        self.lots = [{"lot_id": "11", "title": "400 звёзд Telegram", "stars_per_unit": 400, "auto_delivery": True,
                      "subcategory_id": "2418"}]

    def recipient(self, username):
        if username.lower() not in self.known:
            raise sa.ApiError("not found", 400)
        return {"username": username, "name": username.title()}

    def stars(self, body):
        self.stars_calls.append(body)
        if self.stars_status == "error":
            raise sa.ApiError("Недостаточно TON", 400)
        return {"status": self.stars_status, "price_ton": "1.23", "detail": ""}

    def sync_lots(self, lots):
        return {"lots": [dict(l, want_active=True, reason="ok") for l in self.lots]}

    def balance(self):
        return {"balance_ton": "10"}


@pytest.fixture
def bot(tmp_path):
    sent, notes = [], []
    api = FakeApi()
    b = sa.StarsBot(str(tmp_path), send=lambda c, t, n: sent.append(t), notify=notes.append, api=api,
                    spawn=lambda fn: fn())
    b.lots = api.lots
    b.sent, b.notes, b.fake = sent, notes, api
    return b


def order(**kw):
    base = {"id": "ABC123", "title": "400 звёзд Telegram", "amount": 5, "sum": 1500.0, "currency": "rub",
            "buyer": "buyer1", "buyer_id": 1, "chat_id": "777", "subcategory_id": "2418", "subcategory": "Звёзды",
            "fields": {"Telegram username": "@durov"}, "texts": []}
    base.update(kw)
    return base


def test_norm_username():
    assert sa.norm_username("@Durov") == "Durov"
    assert sa.norm_username("https://t.me/good_user") == "good_user"
    assert sa.norm_username("  durov. ") == "durov"
    assert sa.norm_username("привет") is None
    assert sa.norm_username("1abc") is None


def test_yes_no():
    assert sa.is_yes("Да") and sa.is_yes("да, верно") and sa.is_yes("+") and sa.is_yes("ok")
    assert sa.is_no("нет") and sa.is_no("Нет, другой") and sa.is_no("не")
    assert not sa.is_yes("данные") and not sa.is_no("нету")


def test_match_lot():
    lots = [{"lot_id": 1, "title": "100 звёзд"}, {"lot_id": 2, "title": "1000 звёзд"}]
    assert sa.match_lot(lots, "1000 звёзд")["lot_id"] == 2
    assert sa.match_lot(lots, "1000 звёзд, моментально")["lot_id"] == 2
    assert sa.match_lot(lots, "Premium") is None


def test_confirm_yes_delivers_amount_times_unit(bot):
    bot.on_new_order(order())
    assert "2000 ⭐ на @durov" in bot.sent[-1]
    bot.on_message("777", "да")
    call = bot.fake.stars_calls[-1]
    assert call["quantity"] == 2000 and call["username"] == "durov" and call["order_id"] == "fp-ABC123"
    assert call["revenue"] == 1500.0 and call["lot_id"] == "11"
    assert bot.orders["ABC123"].state == sa.DONE
    assert "отправлены" in bot.sent[-1] and "отзыв" in bot.sent[-1]


def test_no_then_new_username_then_yes(bot):
    bot.on_new_order(order())
    bot.on_message("777", "нет")
    assert bot.orders["ABC123"].state == sa.AWAIT_USERNAME
    bot.on_message("777", "@unknown_guy")
    assert "Не нашёл" in bot.sent[-1] and bot.orders["ABC123"].state == sa.AWAIT_USERNAME
    bot.on_message("777", "good_user")
    assert bot.orders["ABC123"].state == sa.AWAIT_CONFIRM and "@good_user" in bot.sent[-1]
    assert not bot.fake.stars_calls
    bot.on_message("777", "Да!")
    assert bot.fake.stars_calls[-1]["username"] == "good_user"


def test_no_username_in_order_asks(bot):
    bot.on_new_order(order(fields={}))
    assert bot.orders["ABC123"].state == sa.AWAIT_USERNAME
    bot.on_message("777", "да")  # «да» не username
    assert bot.orders["ABC123"].state == sa.AWAIT_USERNAME
    bot.on_message("777", "t.me/durov")
    assert bot.orders["ABC123"].state == sa.AWAIT_CONFIRM


def test_gibberish_reasks(bot):
    bot.on_new_order(order())
    bot.on_message("777", "а когда придёт?")
    assert "Ответьте" in bot.sent[-1] and bot.orders["ABC123"].state == sa.AWAIT_CONFIRM


def test_failed_delivery_goes_to_retry_and_state_persists(bot, tmp_path):
    bot.fake.stars_status = "error"
    bot.on_new_order(order())
    bot.on_message("777", "да")
    p = bot.orders["ABC123"]
    assert p.state == sa.RETRY and "задерживается" in bot.sent[-1] and bot.notes
    again = sa.StarsBot(str(tmp_path), send=lambda *a: None, notify=lambda t: None, api=bot.fake, spawn=lambda f: f())
    assert again.orders["ABC123"].state == sa.RETRY
    bot.fake.stars_status = "confirmed"
    again.orders["ABC123"].next_try = 0
    again.retry_due()
    assert again.orders["ABC123"].state == sa.DONE


def test_duplicate_and_foreign_orders_ignored(bot):
    bot.on_new_order(order())
    n = len(bot.sent)
    bot.on_new_order(order())
    assert len(bot.sent) == n
    bot.on_new_order(order(id="X2", title="Аккаунт Steam", subcategory="Steam"))
    assert "X2" not in bot.orders


def test_below_minimum_goes_manual(bot):
    bot.lots = [{"lot_id": "12", "title": "1 звезда", "stars_per_unit": 1, "auto_delivery": True}]
    bot.on_new_order(order(title="1 звезда", amount=10))
    assert bot.orders["ABC123"].state == sa.MANUAL and bot.notes


def test_refresh_lots_toggles(bot):
    toggled = []
    found = [{"lot_id": "11", "title": "400 звёзд Telegram", "subcategory_id": "2418", "active": False}]
    changes = bot.refresh_lots(lambda subcats: found, lambda lot_id, a: toggled.append((lot_id, a)) or True)
    assert toggled == [("11", True)] and changes


def test_username_prefers_player_and_ignores_placeholders():
    fields = {"Количество звёзд": "50 звёзд", "Способ получения": "По username", "player": "Sasher809"}
    assert sa.username_from_order(fields, ["нужен только ваш @username"]) == "Sasher809"
    assert sa.username_from_order({"x": "пришлите @username"}, []) is None


def test_field_values_skip_seller_text():
    class F:
        def __init__(self, name, value): self.name, self.value = name, value
    class O:
        fields = {"quantity": F("Количество звёзд", "50 звёзд"), "desc": F("Описание", {"ru": "ваш @username"})}
        player = "Sasher809"
    fields, texts = sa._field_values(O())
    assert "Описание" not in fields and fields["player"] == "Sasher809" and texts == []


def test_message_matches_by_buyer_id(bot):
    bot.on_new_order(order(chat_id="users-1-2", buyer_id=42))
    bot.on_message("987654", "да", author_id=42)
    assert bot.orders["ABC123"].state == sa.DONE


def test_thanks_once_after_close_and_review_reply(bot):
    bot.on_new_order(order())
    bot.on_message("777", "да")
    n = len(bot.sent)
    bot.on_order_closed("ABC123")
    bot.on_order_closed("ABC123")
    assert len(bot.sent) == n + 1 and "Спасибо, что подтвердили" in bot.sent[-1]
    bot.on_order_closed("UNKNOWN1")
    replies = []
    bot.on_review("ABC123", 5, "огонь", replies.append)
    bot.on_review("ABC123", 5, "огонь", replies.append)
    assert replies == [bot.settings["texts"]["review_reply_5"]]
    notes = len(bot.notes)
    bot.on_review("ABC123", 3, "долго", replies.append)
    assert len(replies) == 1 and len(bot.notes) == notes + 1 and "долго" in bot.notes[-1]


def test_no_thanks_for_undelivered(bot):
    bot.on_new_order(order())
    n = len(bot.sent)
    bot.on_order_closed("ABC123")
    assert len(bot.sent) == n


def test_old_default_text_migrates(tmp_path):
    import json
    d = tmp_path / "st"; d.mkdir()
    (d / "settings.json").write_text(json.dumps({"texts": {"done": next(iter(sa.OLD_DEFAULT_TEXTS["done"])), "sending": "my"}}))
    b = sa.StarsBot(str(d), send=lambda *a: None, notify=lambda t: None, api=FakeApi(), spawn=lambda f: f())
    assert b.settings["texts"]["done"] == sa.DEFAULT_SETTINGS["texts"]["done"] and b.settings["texts"]["sending"] == "my"
