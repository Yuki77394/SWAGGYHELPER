"""Link policy shared by the /setlink filter, its commands and its wizard.

Policy (applied by ``plugins/misc/setlink.py`` when /setlink is enabled for
a channel):

* DELETE posts containing a prohibited link:
    - HTTP/HTTPS URLs, ``www.`` links and recognizable bare-domain URLs
      (``instagram.com/...``, ``youtu.be/...``, ``facebook.com/...``,
      ``chat.whatsapp.com/...`` ...);
    - Telegram links: ``t.me``, ``telegram.me``, ``telegram.dog`` —
      including invite links (``t.me/+hash``, ``t.me/joinchat/hash``) —
      and ``tg://`` deep links (``tg://join?invite=...``, ``tg://resolve``);
    - hidden clickable URLs: a Telegram TEXT_LINK entity makes its display
      text (a word, an @username, an emoji ...) clickable, so the display
      text is judged by what it links to, never by what it shows;
    - the same rules apply to message text, captions, forwarded posts,
      edited posts and the caption/items of media albums.
* ALLOW plain @usernames (``@ABHISHEK77394``) when no URL is attached.
  A Telegram MENTION entity is a username, not a web URL; Telegram
  usernames never contain dots, so the bare-domain scan cannot produce a
  false positive from one. TEXT_MENTION (an inline user mention) links to
  a profile, not to a web page, and is allowed as well.
* Detection uses BOTH the message entities (authoritative — this is what
  Telegram renders as clickable) and a conservative text scan (URLs inside
  CODE/PRE blocks and other text Telegram did not entity-tag). The text
  scan guards against false positives from ordinary text:
    - an e-mail address (``name@example.com``) is not a web link and is
      allowed — the domain part is preceded by ``@``/``mailto:``;
    - a domain-like token followed by ``.`` + more characters
      (``see.at.home``, ``v1.2.3``) is not matched: only a label plus a
      KNOWN top-level domain counts as a recognizable URL.

Albums: Telegram delivers every item of a media group as a separate
update. While the feature is enabled, clean items are remembered in
``_ALBUM_ITEMS`` (bounded, in-process). The first item found to carry a
prohibited link condemns the whole album: the verdict is recorded BEFORE
any Telegram call (so a concurrent item always sees it), the current item
and every item already delivered are deleted, and every later item of the
same ``media_group_id`` is deleted on arrival. The condemned album id is
also persisted in the channel's settings document
(``link_blocked_albums``, bounded) so a restart between item deliveries
cannot leave a partial album alive. This mirrors how the /setgap engine
keeps album verdicts across a restart (``gap_albums``).

Coordination with /setgap and /setdelay — the link filter runs in handler
group -40, BEFORE the /setanonymous filter (-30), the /setdelay and
/setgap commands (-20/-19) and the gap/delay engine (0). A post the link
filter rejects must never become an accepted /setgap reference or a
/setdelay job, whatever happens to its deletion:

1. ``mark_rejected`` records the verdict in-process (bounded LRU) before
   any Telegram call; the gap engine checks ``was_rejected`` and skips
   the post (same pattern as ``anonymous_policy``).
2. Deletions that Telegram refuses with FloodWait are queued for the
   persistent deletion worker (``autodelete_jobs``, reason
   ``LINK_REJECT_RETRY``) instead of being abandoned.
"""
import re
from collections import OrderedDict
from typing import List, Optional, Tuple

from pyrogram.enums import MessageEntityType, MessageServiceType

from SWAGGYMUSIC.logging import LOGGER

log = LOGGER(__name__)

# ---------------------------------------------------------------------------
# In-process verdicts and album registries (bounded; oldest dropped first)
# ---------------------------------------------------------------------------

# How many per-post verdicts are kept (same scale as anonymous_policy).
_REJECTED_MAX = 10_000
_REJECTED: "OrderedDict[Tuple[int, int], None]" = OrderedDict()

# How many (chat, media_group_id) albums are tracked at once. Telegram
# albums deliver within seconds; the bound is generous for safety.
_ALBUM_TRACK_MAX = 512
# Items remembered per album (Telegram media groups hold 2..10 items).
_ALBUM_ITEMS_MAX = 32
# Items seen per album: (chat_id, media_group_id) -> {msg_id: None}
_ALBUM_ITEMS: "OrderedDict[Tuple[int, int], OrderedDict[int, None]]" = OrderedDict()
# Albums condemned by the link filter: (chat_id, media_group_id) -> None
_BLOCKED_ALBUMS: "OrderedDict[Tuple[int, int], None]" = OrderedDict()

# How many condemned album ids are kept per channel in MongoDB — the
# bound lives in database.LINK_BLOCKED_ALBUMS_LIMIT (single source of
# truth for the storage layer).


def _touch(book: OrderedDict, key, max_entries: int) -> None:
    book[key] = None
    book.move_to_end(key)
    while len(book) > max_entries:
        book.popitem(last=False)


def mark_rejected(chat_id: int, message_id: int) -> None:
    """Remember (in this process) that the link filter rejected a post."""
    _touch(_REJECTED, (int(chat_id), int(message_id)), _REJECTED_MAX)


def was_rejected(chat_id: int, message_id: int) -> bool:
    try:
        return (int(chat_id), int(message_id)) in _REJECTED
    except (TypeError, ValueError):
        return False


def remember_album_item(chat_id: int, media_group_id: int, message_id: int) -> None:
    """Track a delivered (clean) item of a media album."""
    key = (int(chat_id), int(media_group_id))
    items = _ALBUM_ITEMS.get(key)
    if items is None:
        items = OrderedDict()
        _touch(_ALBUM_ITEMS, key, _ALBUM_TRACK_MAX)
        _ALBUM_ITEMS[key] = items
    _touch(items, int(message_id), _ALBUM_ITEMS_MAX)


def block_album(chat_id: int, media_group_id: int) -> None:
    """Condemn a whole album (verdict recorded before any Telegram call)."""
    _touch(_BLOCKED_ALBUMS, (int(chat_id), int(media_group_id)), _ALBUM_TRACK_MAX)


def album_is_blocked(chat_id: int, media_group_id: int) -> bool:
    try:
        return (int(chat_id), int(media_group_id)) in _BLOCKED_ALBUMS
    except (TypeError, ValueError):
        return False


def take_album_items(chat_id: int, media_group_id: int) -> List[int]:
    """Return the items delivered so far for one album and forget them."""
    key = (int(chat_id), int(media_group_id))
    items = _ALBUM_ITEMS.pop(key, None)
    return list(items) if items else []


def media_group_id(message) -> Optional[int]:
    """The message's media-group id (albums), as a plain int, or None."""
    value = getattr(message, "media_group_id", None)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def persisted_blocked_album(setting: Optional[dict], media_group_id: int) -> bool:
    """True if ``setting``'s persisted ``link_blocked_albums`` lists the id."""
    blocked = (setting or {}).get("link_blocked_albums") or []
    for value in blocked:
        try:
            if int(value) == int(media_group_id):
                return True
        except (TypeError, ValueError):
            continue
    return False


# ---------------------------------------------------------------------------
# Detection — entities first (what Telegram renders as clickable), then a
# conservative text scan for URLs Telegram did not entity-tag.
# ---------------------------------------------------------------------------

# URL schemes: HTTP/HTTPS, www-prefixed hosts and Telegram deep links.
_URL_SCHEME_RE = re.compile(
    r"(?<![\w@.])(?:https?://|www\.|tg://)[^\s<>\"'`]{0,1024}",
    re.IGNORECASE,
)

# Telegram links in every spelling, with invite links covered by the path:
# t.me/+hash, t.me/joinchat/hash, t.me/username, telegram.me/..., telegram.dog/...
_TELEGRAM_LINK_RE = re.compile(
    r"(?<![\w@.])(?:t\.me|telegram\.me|telegram\.dog)(?:/[^\s<>\"'`]{0,1024})?",
    re.IGNORECASE,
)

# Recognizable bare domains: one or more labels followed by a KNOWN
# top-level domain. The lookbehind keeps @usernames, e-mail addresses and
# longer dotted tokens out; the trailing lookahead rejects a match that is
# only the prefix of a longer dotted token ("see.at.home"), so ordinary
# text and version strings ("v1.2.3") never match.
_TLDS = (
    "com|net|org|io|co|me|xyz|info|biz|online|site|website|club|link|live|"
    "news|tv|app|dev|gg|ai|ru|in|uk|us|de|fr|it|es|br|pl|nl|se|no|fi|dk|"
    "ch|at|be|pt|cz|ua|tr|ir|id|ph|my|sg|jp|kr|cn|au|nz|za|ng|ar|mx|cl|pe|"
    "ca|ly|watch|store|shop|blog|page|tech|pro|vip|icu|top|space|fun|"
    "press|rocks|city|tools|wiki|life|world|today|zone"
)
_BARE_DOMAIN_RE = re.compile(
    rf"(?<![\w@.])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+({_TLDS}))"
    rf"(?!\.[a-z0-9])(?:/[^\s<>\"'`]{{0,1024}})?",
    re.IGNORECASE,
)

# Trailing punctuation that belongs to the sentence, not to the URL.
_TRAILING_PUNCTUATION = ".,;:!?)\"'»…}]"


def _entity_span(text: str, entity) -> str:
    """The text an entity spans.

    Telegram entity offsets/lengths are UTF-16 code units, so the plain
    Python string indices are WRONG whenever emoji or other non-BMP
    characters precede the entity. Decode through UTF-16 to slice safely;
    return "" for malformed/mismatched entities.
    """
    try:
        data = text.encode("utf-16-le")
        start = int(entity.offset) * 2
        end = start + int(entity.length) * 2
        if start < 0 or end > len(data) or start > end:
            return ""
        return data[start:end].decode("utf-16-le", errors="replace")
    except Exception:
        return ""


def _clean_url(value: str) -> str:
    return (value or "").rstrip(_TRAILING_PUNCTUATION)


def _entity_hits(message, text: str) -> List[Tuple[str, str]]:
    """Prohibited links carried by the message's entities."""
    hits: List[Tuple[str, str]] = []
    entities = list(getattr(message, "entities", None) or [])
    entities += list(getattr(message, "caption_entities", None) or [])
    for entity in entities:
        etype = getattr(entity, "type", None)
        if etype is MessageEntityType.TEXT_LINK:
            hits.append(("text_link", _clean_url(getattr(entity, "url", None) or "")))
        elif etype is MessageEntityType.URL:
            hits.append(("url_entity", _clean_url(_entity_span(text, entity))))
    return hits


def _text_hits(text: str) -> List[Tuple[str, str]]:
    """Prohibited links found by the conservative text scan."""
    hits: List[Tuple[str, str]] = []
    for match in _URL_SCHEME_RE.finditer(text):
        hits.append(("url", _clean_url(match.group(0))))
    for match in _TELEGRAM_LINK_RE.finditer(text):
        hits.append(("telegram_link", _clean_url(match.group(0))))
    for match in _BARE_DOMAIN_RE.finditer(text):
        hits.append(("domain", _clean_url(match.group(0))))
    return hits


def find_prohibited_links(message) -> List[Tuple[str, str]]:
    """Every prohibited link in ``message`` as ``(kind, value)`` pairs.

    Kinds: ``text_link`` (hidden clickable URL), ``url_entity`` (Telegram
    URL entity), ``url`` / ``telegram_link`` / ``domain`` (text scan).
    Empty list = the message is clean. Pure function: no I/O, never raises.
    """
    try:
        if isinstance(getattr(message, "service", None), MessageServiceType):
            return []
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if not isinstance(text, str):
            text = str(text)
        hits = _entity_hits(message, text)
        if text:
            seen = {value for _kind, value in hits if value}
            for kind, value in _text_hits(text):
                if not value or value in seen:
                    continue
                seen.add(value)
                hits.append((kind, value))
        return hits
    except Exception as exc:  # never let detection break the dispatcher
        log.warning("link_policy.find_prohibited_links failed: %s: %s",
                    type(exc).__name__, exc)
        return []


def message_has_prohibited_link(message) -> bool:
    """True if the message carries any prohibited link (pure check)."""
    return bool(find_prohibited_links(message))
