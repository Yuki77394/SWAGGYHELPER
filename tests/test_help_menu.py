"""Help & Commands menu: 2 rows x 2 category buttons, the new
"Anonymous Post" page (/setanonymous), and every existing button/callback.

Callback queries are REAL ``pyrogram.types.CallbackQuery`` objects delivered
through the handlers actually registered on ``app`` (gap_harness.
dispatch_callback); Telegram calls are mocked on ``app``.
"""
from __future__ import annotations

import html
import importlib.util
import re
import sys
import types as pytypes
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from gap_harness import dispatch_callback, registered_groups
from pyrogram import types
from pyrogram.enums import ButtonStyle, ChatType, ParseMode

# (pytest.ini: asyncio_mode = auto — async tests need no marker)

USER = 777
GRID = [["help_callback hb1", "help_callback hb2"],
        ["help_callback hb3", "help_callback hb4"]]


def _strings():
    from strings import get_string
    return get_string("en")


def _help_plugins():
    """Import the Help and Start plugins (registers their handlers).

    start.py imports aiohttp, a production dependency (requirements.txt)
    that the offline test environment may not ship; only then a stub is
    used — no test here reaches the Bot API call that needs it.
    """
    if "aiohttp" not in sys.modules and importlib.util.find_spec("aiohttp") is None:
        stub = pytypes.ModuleType("aiohttp")
        stub.ClientSession = stub.ClientTimeout = object
        sys.modules["aiohttp"] = stub
    import SWAGGYMUSIC.plugins.bot.help as help_mod
    import SWAGGYMUSIC.plugins.bot.start as start_mod
    return help_mod, start_mod


def _query(data: str, *, with_photo: bool = False):
    from SWAGGYMUSIC import app

    chat = types.Chat(id=USER, type=ChatType.PRIVATE, client=app)
    user = types.User(id=USER, first_name="User", client=app)
    kwargs = {}
    if with_photo:
        # Kurigram 2.2.25's Photo also requires file_size and date.
        kwargs["photo"] = types.Photo(
            file_id="photo-file-id", file_unique_id="photo-unique-id", width=1, height=1,
            file_size=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    message = types.Message(id=55, chat=chat, from_user=None, client=app, **kwargs)
    return types.CallbackQuery(client=app, id="q1", from_user=user, chat_instance="ci",
                               message=message, data=data)


@pytest.fixture
def telegram(monkeypatch):
    """Mock every Telegram call the help callbacks make."""
    from SWAGGYMUSIC import app

    calls = {name: AsyncMock() for name in (
        "edit_message_text", "edit_message_caption", "answer_callback_query",
        "edit_message_media", "delete_messages", "send_photo",
    )}
    for name, mock in calls.items():
        monkeypatch.setattr(app, name, mock)
    return calls


def _rows(markup):
    return [[(b.text, b.callback_data, b.style) for b in row] for row in markup.inline_keyboard]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

class TestLayout:
    @pytest.mark.parametrize("start, nav_data, nav_label", [
        (None, "close", "CLOSE_BUTTON"),            # /help, /start help
        (True, "settingsback_helper", "BACK_BUTTON"),  # opened from the Start panel
    ])
    def test_two_rows_of_two_categories_then_navigation(self, start, nav_data, nav_label):
        from SWAGGYMUSIC.utils.inline.help import help_pannel

        _ = _strings()
        rows = _rows(help_pannel(_, start))
        assert len(rows) == 3
        assert [[data for _t, data, _s in row] for row in rows[:2]] == GRID
        assert [[text for text, _d, _s in row] for row in rows[:2]] == [
            [_["H_B_1"], _["H_B_2"]], [_["H_B_3"], _["H_B_4"]]]
        assert all(style == ButtonStyle.PRIMARY for row in rows[:2] for _t, _d, style in row)
        assert rows[2] == [(_[nav_label], nav_data, ButtonStyle.DANGER)]

    def test_labels(self):
        _ = _strings()
        assert (_["H_B_1"], _["H_B_2"], _["H_B_3"]) == ("sєᴛ ᴅєʟᴀʏ", "sєᴛ ɢᴀᴘ", "ᴄʀєᴀᴛє ᴘᴏsᴛ")
        assert _["H_B_4"] == "ᴀɴᴏɴʏᴍᴏᴜs ᴘᴏsᴛ"   # "Anonymous Post" in the menu's small caps

    def test_category_page_back_button_unchanged(self):
        from SWAGGYMUSIC.utils.inline.help import help_back_markup

        _ = _strings()
        assert _rows(help_back_markup(_)) == [[(_["BACK_BUTTON"], "settings_back_helper",
                                                ButtonStyle.DANGER)]]

    def test_callback_data_fits_telegram_limit(self):
        from SWAGGYMUSIC.utils.inline.help import help_pannel

        for row in help_pannel(_strings()).inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

class TestCallbacks:
    async def test_every_button_reaches_its_existing_handler(self, telegram):
        from SWAGGYMUSIC.utils.inline.help import help_back_markup, help_pannel

        help_mod, start_mod = _help_plugins()
        _ = _strings()
        expected = {
            "help_callback hb1": help_mod.helper_cb,
            "help_callback hb2": help_mod.helper_cb,
            "help_callback hb3": help_mod.helper_cb,
            "help_callback hb4": help_mod.helper_cb,
            "close": help_mod.close_helper,
            "settingsback_helper": start_mod.back_to_start_home,   # BACK -> Start panel
            "settings_back_helper": help_mod.helper_private,       # page BACK -> help menu
        }
        shown = {b.callback_data
                 for markup in (help_pannel(_), help_pannel(_, True), help_back_markup(_))
                 for row in markup.inline_keyboard for b in row}
        assert shown == set(expected)
        for data, handler in expected.items():
            assert await dispatch_callback(_query(data)) == [handler], data

    @pytest.mark.parametrize("cb, page", [
        ("hb1", "HELP_1"), ("hb2", "HELP_2"), ("hb3", "HELP_3"), ("hb4", "HELP_4"),
    ])
    async def test_category_shows_its_page_with_back_button(self, telegram, cb, page):
        from strings import helpers

        _help_plugins()
        await dispatch_callback(_query(f"help_callback {cb}"))
        kwargs = telegram["edit_message_text"].await_args.kwargs
        assert kwargs["text"] == getattr(helpers, page)
        assert kwargs["chat_id"] == USER and kwargs["message_id"] == 55
        assert [[b.callback_data for b in row] for row in kwargs["reply_markup"].inline_keyboard] \
            == [["settings_back_helper"]]

    @staticmethod
    def _spy_caption_edits(monkeypatch):
        """Record CallbackQuery.edit_message_caption calls (and run them)."""
        captions = []
        original = types.CallbackQuery.edit_message_caption

        async def spy(self, caption, *args, **kwargs):
            captions.append(caption)
            return await original(self, caption, *args, **kwargs)

        monkeypatch.setattr(types.CallbackQuery, "edit_message_caption", spy)
        return captions

    @pytest.mark.parametrize("cb, page", [
        ("hb1", "HELP_1"), ("hb2", "HELP_2"), ("hb3", "HELP_3"), ("hb4", "HELP_4"),
    ])
    async def test_category_callback_edits_photo_caption_not_text(self, telegram, monkeypatch, cb, page):
        from strings import helpers

        _help_plugins()
        captions = self._spy_caption_edits(monkeypatch)
        await dispatch_callback(_query(f"help_callback {cb}", with_photo=True))
        assert captions == [getattr(helpers, page)], "a photo message takes the caption path"
        # Kurigram 2.2.25 implements CallbackQuery.edit_message_caption() as
        # edit_message_text(caption): ONE messages.editMessage request that
        # replaces the photo's caption and keeps the photo.
        kwargs = telegram["edit_message_text"].await_args.kwargs
        assert kwargs["text"] == getattr(helpers, page)
        assert kwargs["chat_id"] == USER and kwargs["message_id"] == 55
        telegram["edit_message_media"].assert_not_awaited()

    async def test_back_to_help_menu_edits_photo_caption(self, telegram, monkeypatch):
        from strings import get_string

        _help_plugins()
        captions = self._spy_caption_edits(monkeypatch)
        await dispatch_callback(_query("settings_back_helper", with_photo=True))
        expected = get_string("en")["help_1"].format(__import__("config").SUPPORT_CHAT)
        assert captions == [expected]
        kwargs = telegram["edit_message_text"].await_args.kwargs
        assert kwargs["text"] == expected
        assert kwargs["chat_id"] == USER and kwargs["message_id"] == 55
        telegram["edit_message_media"].assert_not_awaited()

    async def test_back_from_a_page_shows_the_new_grid(self, telegram):
        _help_plugins()
        await dispatch_callback(_query("settings_back_helper"))
        markup = telegram["edit_message_text"].await_args.kwargs["reply_markup"]
        assert [[b.callback_data for b in row] for row in markup.inline_keyboard] == \
            GRID + [["settingsback_helper"]]

    async def test_close_deletes_the_menu(self, telegram):
        _help_plugins()
        await dispatch_callback(_query("close"))
        telegram["delete_messages"].assert_awaited_once()

    async def test_help_command_still_opens_the_menu(self, telegram, monkeypatch):
        from SWAGGYMUSIC import app

        help_mod, _start = _help_plugins()
        monkeypatch.setattr(app, "me", types.User(id=app.id, first_name="Bot", username="stub_bot",
                                                  is_bot=True, client=app))
        chat = types.Chat(id=USER, type=ChatType.PRIVATE, client=app)
        command = types.Message(id=9, chat=chat, text="/help", client=app,
                                from_user=types.User(id=USER, first_name="User", client=app))
        handlers = [h for group in (await registered_groups()).values() for h in group
                    if h.callback is help_mod.helper_private and hasattr(h, "filters")]
        assert any([await h.check(app, command) for h in handlers]), "/help routes to helper_private"
        await help_mod.helper_private(app, command)
        markup = telegram["send_photo"].await_args.kwargs["reply_markup"]
        assert [[b.callback_data for b in row] for row in markup.inline_keyboard] == GRID + [["close"]]


# ---------------------------------------------------------------------------
# The Anonymous Post page
# ---------------------------------------------------------------------------

def _visible(page: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", page))


def _ascii_lower_outside_commands(page: str) -> set:
    return set(re.findall(r"[a-z]", re.sub(r"/[a-z]+", "", _visible(page))))


class TestAnonymousPostPage:
    def test_explains_purpose_on_off_and_identity_limits(self):
        from strings import helpers

        text = _visible(helpers.HELP_4)
        assert "/setanonymous" in text
        for words in ("ᴀɴᴏɴʏᴍᴏᴜs ᴘᴏsᴛ",                       # purpose / title
                      "ᴇɴᴀʙʟᴇ", "ᴅɪsᴀʙʟᴇ", "ᴏɴ", "ᴏғғ",          # ON/OFF controls
                      "ᴄʜᴀɴɴᴇʟ ɪᴅ",                           # the wizard's input
                      "ᴀɴᴏᴛʜᴇʀ ᴄʜᴀɴɴᴇʟ",                       # other-channel identity
                      "ᴘᴇʀsᴏɴᴀʟ ᴀᴄᴄᴏᴜɴᴛ",                       # allowed when exposed
                      "sɪɢɴᴀᴛᴜʀᴇ",                             # signature is not identity
                      "ᴀᴅᴍɪɴ"):                                # cannot identify the admin
            assert words in text, words

    def test_same_typography_as_setdelay_and_setgap(self):
        from strings import helpers

        for page in (helpers.HELP_1, helpers.HELP_2, helpers.HELP_4):
            assert page.startswith("<b><u>❖ ʜᴇʟᴘ ғᴏʀ ")
            assert re.search(r"^<b><u>❖ .+ :</u></b>$", page, re.M)       # section title
            assert re.search(r"^<b>๏ /\w+.* -</b> ", page, re.M)          # command lines
            assert re.search(r"^<b>❖ .+ :</b>$", page, re.M)              # sub-sections
            assert re.search(r"^• ", page, re.M)                           # bullets
        # Small caps everywhere except commands: no ASCII lowercase letters
        # beyond those the existing pages already use for the style.
        allowed = (_ascii_lower_outside_commands(helpers.HELP_1)
                   | _ascii_lower_outside_commands(helpers.HELP_2))
        assert _ascii_lower_outside_commands(helpers.HELP_4) <= allowed

    async def test_every_help_page_fits_a_photo_caption(self):
        """The menu is a photo (/help, /start); a category page is shown by
        editing that photo's caption: Telegram allows 1024 characters."""
        from pyrogram.parser import Parser

        from strings import helpers

        for name in ("HELP_1", "HELP_2", "HELP_3", "HELP_4"):
            parsed = await Parser(None).parse(getattr(helpers, name), ParseMode.HTML)
            length = len(parsed["message"].encode("utf-16-le")) // 2
            assert length <= 1024, f"{name} is {length} characters"
