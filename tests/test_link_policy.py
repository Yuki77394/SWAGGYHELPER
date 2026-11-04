"""Pure link-detection tests for SWAGGYMUSIC/utils/link_policy.py.

No Telegram I/O, no MongoDB, no dispatcher — only the policy functions.
The handler-level behaviour (deletion, albums, edits, settings) is pinned
by tests/test_setlink.py.
"""
from __future__ import annotations

import itertools
from typing import List, Optional

import pytest
from pyrogram.enums import MessageEntityType

from SWAGGYMUSIC.utils import link_policy

# (pytest.ini: asyncio_mode = auto — async tests need no marker)


# ---------------------------------------------------------------------------
# Message stand-ins for the pure API
# ---------------------------------------------------------------------------

class Entity:
    """A Kurigram-MessageEntity-like object."""

    def __init__(self, etype, offset: int, length: int, url: Optional[str] = None):
        self.type = etype
        self.offset = offset
        self.length = length
        self.url = url


class FakeMessage:
    """Everything find_prohibited_links() reads."""

    def __init__(self, text: str = "", caption: Optional[str] = None,
                 entities: Optional[List[Entity]] = None,
                 caption_entities: Optional[List[Entity]] = None,
                 media_group_id: Optional[int] = None, service=None):
        self.text = text or None
        self.caption = caption
        self.entities = entities or []
        self.caption_entities = caption_entities or []
        self.media_group_id = media_group_id
        self.service = service


def kinds(message) -> List[str]:
    return [kind for kind, _value in link_policy.find_prohibited_links(message)]


def values(message) -> List[str]:
    return [value for _kind, value in link_policy.find_prohibited_links(message)]


# ---------------------------------------------------------------------------
# CORE RULE — plain @username = ALLOW
# ---------------------------------------------------------------------------

class TestPlainUsernamesAllowed:
    @pytest.mark.parametrize("text", [
        "@ABHISHEK77394",
        "follow @ABHISHEK77394 for updates",
        "@user one @Abc_Xyz two",
        "🎉🎉 @ABHISHEK77394 🎉",
        "contact @admin_team today",
    ])
    def test_mention_text_is_never_a_hit(self, text):
        assert link_policy.find_prohibited_links(FakeMessage(text)) == []

    def test_mention_entity_is_never_a_hit(self):
        # Telegram parses "@ABHISHEK77394" into a MENTION entity — that is
        # a username, not a web URL, and must stay allowed.
        msg = FakeMessage("@ABHISHEK77394",
                          entities=[Entity(MessageEntityType.MENTION, 0, 14)])
        assert link_policy.find_prohibited_links(msg) == []

    def test_text_mention_entity_is_never_a_hit(self):
        # An inline user mention links to a PROFILE, not to a web page.
        msg = FakeMessage("our admin",
                          entities=[Entity(MessageEntityType.TEXT_MENTION, 0, 9)])
        assert link_policy.find_prohibited_links(msg) == []

    def test_email_addresses_are_allowed(self):
        for text in ("write to name@example.com",
                     "mail: first.last@sub.domain.org now",
                     "mailto:foo@bar.io"):
            assert link_policy.find_prohibited_links(FakeMessage(text)) == [], text

    def test_ordinary_text_is_never_a_hit(self):
        for text in ("Welcome to the channel everyone",
                     "see chapter 2.5 for details",
                     "v1.2.3 released today",
                     "3.14 is pi", "10:20 AM", "stay.at.home mom",
                     "#win.at.home", "understand.it.all please",
                     "file_v1.2.tar is ready"):
            assert link_policy.find_prohibited_links(FakeMessage(text)) == [], text


# ---------------------------------------------------------------------------
# DIRECT URL = DELETE
# ---------------------------------------------------------------------------

class TestDirectUrlsBlocked:
    @pytest.mark.parametrize("text", [
        "https://spam.com/x",
        "http://spam.in/page",
        "HTTPS://SPAM.COM",                      # case-insensitive
        "www.spam.tv/page",
        "WWW.example.org",
        "spam.com",                              # bare recognizable domain
        "visit SPAM.COM now",
        "sub.example.org/path?q=1",
        "instagram.com/user",
        "instagram.com/p/Cxyz123/",
        "youtu.be/abc123",
        "youtube.com/watch?v=dQw4w9WgXcQ",
        "facebook.com/page",
        "fb.watch/abc123",
        "chat.whatsapp.com/AbCdEf",
        "wa.me/15551234567",
        "bit.ly/xyz",
        "shortLink.io/abc",
    ])
    def test_recognizable_urls_are_hits(self, text):
        hits = link_policy.find_prohibited_links(FakeMessage(text))
        assert hits, text
        assert all(kind in {"url", "domain"} for kind, _v in hits)

    def test_trailing_sentence_punctuation_is_stripped(self):
        msg = FakeMessage("go to example.com, and subscribe")
        assert "example.com" in values(msg)


# ---------------------------------------------------------------------------
# TELEGRAM LINKS = DELETE (invites included)
# ---------------------------------------------------------------------------

class TestTelegramLinksBlocked:
    @pytest.mark.parametrize("text", [
        "t.me/spamchannel",
        "t.me/+AbCdEf12345",                     # private invite link
        "https://t.me/joinchat/XyZ123",          # legacy invite link
        "https://t.me/spamchannel/42",
        "telegram.me/spamchannel",
        "telegram.dog/spamchannel",
        "tg://join?invite=AbCdEf",               # deep invite link
        "tg://resolve?domain=spamchannel",
        "join t.me/spam now",
    ])
    def test_telegram_links_are_hits(self, text):
        hits = link_policy.find_prohibited_links(FakeMessage(text))
        assert hits, text
        assert any(kind in {"telegram_link", "url"} for kind, _v in hits)


# ---------------------------------------------------------------------------
# HIDDEN CLICKABLE URL = DELETE (TEXT_LINK entities)
# ---------------------------------------------------------------------------

class TestHiddenTextLinksBlocked:
    def test_url_hidden_behind_a_username(self):
        # THE core rule: the display text is @ABHISHEK77394, but Telegram
        # makes it clickable with a TEXT_LINK — blocked.
        msg = FakeMessage("@ABHISHEK77394",
                          entities=[Entity(MessageEntityType.TEXT_LINK, 0, 14,
                                           url="https://spam.com")])
        hits = link_policy.find_prohibited_links(msg)
        assert hits and hits[0][0] == "text_link"
        assert hits[0][1] == "https://spam.com"

    def test_url_hidden_behind_plain_words(self):
        msg = FakeMessage("Read more here",
                          entities=[Entity(MessageEntityType.TEXT_LINK, 0, 14,
                                           url="http://spam.io/x")])
        assert kinds(msg) == ["text_link"]

    def test_url_hidden_behind_emojis(self):
        # Entity offsets are UTF-16 code units: two surrogate-pair emoji
        # occupy 4 units, so the entity below spans "Click".
        msg = FakeMessage("🎉🎉 Click 🎉",
                          entities=[Entity(MessageEntityType.TEXT_LINK, 4, 5,
                                           url="https://spam.com")])
        assert kinds(msg) == ["text_link"]

    def test_hidden_tg_invite_is_reported_with_its_url(self):
        msg = FakeMessage("join us",
                          entities=[Entity(MessageEntityType.TEXT_LINK, 0, 7,
                                           url="https://t.me/+AbCdEf")])
        hits = link_policy.find_prohibited_links(msg)
        assert hits and hits[0][1] == "https://t.me/+AbCdEf"

    def test_url_entity_is_a_hit(self):
        msg = FakeMessage("open https://spam.com now",
                          entities=[Entity(MessageEntityType.URL, 5, 18)])
        assert "url_entity" in kinds(msg)

    def test_caption_entities_are_inspected(self):
        msg = FakeMessage(caption="photo caption",
                          caption_entities=[Entity(MessageEntityType.TEXT_LINK,
                                                   0, 14, url="https://spam.com")])
        assert kinds(msg) == ["text_link"]

    def test_malformed_entity_offsets_do_not_crash(self):
        # Out-of-range / negative entities never raise. The verdict stays
        # fail-closed: a URL entity is a prohibited link even when its
        # span cannot be read (the span is only used for logging), while
        # TEXT_LINK carries its URL independently of the span.
        msg = FakeMessage("short",
                          entities=[Entity(MessageEntityType.URL, 999, 50),
                                    Entity(MessageEntityType.TEXT_LINK, -4, 2,
                                           url="https://spam.com")])
        assert kinds(msg) == ["url_entity", "text_link"]
        assert values(msg) == ["", "https://spam.com"]


# ---------------------------------------------------------------------------
# Detection robustness
# ---------------------------------------------------------------------------

class TestDetectionRobustness:
    def test_service_messages_are_ignored(self):
        from pyrogram.enums import MessageServiceType

        msg = FakeMessage("https://spam.com",
                          service=MessageServiceType.NEW_CHAT_TITLE)
        assert link_policy.find_prohibited_links(msg) == []

    def test_media_group_id_helper(self):
        assert link_policy.media_group_id(FakeMessage("x", media_group_id=13_500)) == 13_500
        assert link_policy.media_group_id(FakeMessage("x", media_group_id=None)) is None
        assert link_policy.media_group_id(FakeMessage("x", media_group_id="777")) == 777
        assert link_policy.media_group_id(FakeMessage("x", media_group_id=True)) is None

    def test_non_string_text_never_crashes(self):
        msg = FakeMessage()
        msg.text = 12345
        assert link_policy.find_prohibited_links(msg) == []

    def test_message_has_prohibited_link_wrapper(self):
        assert link_policy.message_has_prohibited_link(FakeMessage("clean")) is False
        assert link_policy.message_has_prohibited_link(FakeMessage("t.me/spam")) is True

    def test_caption_used_when_text_missing(self):
        msg = FakeMessage(caption="check spam.com today")
        assert "domain" in kinds(msg)


# ---------------------------------------------------------------------------
# In-process verdict + album registries
# ---------------------------------------------------------------------------

class TestRegistries:
    def test_mark_and_was_rejected(self):
        link_policy.mark_rejected(-100111, 5)
        assert link_policy.was_rejected(-100111, 5)
        assert not link_policy.was_rejected(-100111, 6)
        assert not link_policy.was_rejected(-100222, 5)

    def test_album_block_and_items(self):
        chat, group = -100333, 555_001
        assert not link_policy.album_is_blocked(chat, group)
        link_policy.remember_album_item(chat, group, 1)
        link_policy.remember_album_item(chat, group, 2)
        link_policy.block_album(chat, group)
        assert link_policy.album_is_blocked(chat, group)
        assert link_policy.take_album_items(chat, group) == [1, 2]
        assert link_policy.take_album_items(chat, group) == []  # taken = gone
        # blocking keeps the verdict even after the items were taken
        assert link_policy.album_is_blocked(chat, group)

    def test_album_items_are_bounded(self):
        chat, group = -100444, 555_002
        for mid in range(100):
            link_policy.remember_album_item(chat, group, mid)
        assert len(link_policy.take_album_items(chat, group)) == link_policy._ALBUM_ITEMS_MAX

    def test_registries_are_bounded_lrus(self):
        for n in range(link_policy._REJECTED_MAX + 50):
            link_policy.mark_rejected(-100555, n)
        assert len(link_policy._REJECTED) == link_policy._REJECTED_MAX
        for n in range(link_policy._ALBUM_TRACK_MAX + 10):
            link_policy.block_album(-100666, n)
        assert len(link_policy._BLOCKED_ALBUMS) == link_policy._ALBUM_TRACK_MAX

    def test_persisted_blocked_album_lookup(self):
        setting = {"link_blocked_albums": [111, "222", 333]}
        assert link_policy.persisted_blocked_album(setting, 222) is True
        assert link_policy.persisted_blocked_album(setting, 999) is False
        assert link_policy.persisted_blocked_album(None, 111) is False
        assert link_policy.persisted_blocked_album({}, 111) is False

    def test_bad_ids_never_crash(self):
        assert not link_policy.was_rejected("x", None)
        assert not link_policy.album_is_blocked(None, "x")
