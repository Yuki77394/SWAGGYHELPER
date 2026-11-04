"""Forward policy shared by the /setforward filter, its command and its wizard.

Policy (applied by ``plugins/misc/setforward.py`` when /setforward — the
FORWARD POST REMOVER — is enabled for a channel):

* DELETE every forwarded channel post, whatever it carries: text, photos,
  videos, documents, audio, voice notes, stickers, polls, locations and any
  other media — and whoever it was forwarded from: a user, a user hidden by
  their privacy settings, a group, a channel (this one included) or an
  import. Telegram forwards a media album item by item, every item with its
  own forward header, so a forwarded album is removed whole.
* KEEP every post that is not forwarded. Replies, posts sent via an inline
  bot and edited posts are not forwards.
* Service messages (pins, title/photo changes ...) are not posts and are
  never judged.

Detection (``is_forwarded``) — compatible with Kurigram 2.2.25 (the pinned
``kurigram[fast]==2.2.25``), which replaced the old ``forward_from`` /
``forward_date`` fields by ``Message.forward_origin``:

1. The raw MTProto message Kurigram keeps in ``Message.raw``: Telegram marks
   a forwarded message with a ``fwd_from`` header
   (``raw.types.MessageFwdHeader``). This is the primary source — it is
   present even when Kurigram cannot build a ``MessageOrigin`` from it
   (``MessageOrigin._parse`` returns None for a header that has no
   ``from_id``, no ``from_name`` and no ``imported`` flag).
2. Kurigram's parsed ``Message.forward_origin`` (``MessageOriginUser`` /
   ``MessageOriginHiddenUser`` / ``MessageOriginChat`` /
   ``MessageOriginChannel`` / ``MessageOriginImport``) — for messages built
   without the raw update.
3. Older Pyrogram 2.0 forks stored ``forward_date`` / ``forward_from`` /
   ``forward_from_chat`` / ``forward_sender_name`` as plain instance
   attributes. They are read from the instance ``__dict__`` only, so
   Kurigram's deprecated alias PROPERTIES of the same names (each access
   logs a warning) are never touched.

Not detectable by design: a post sent with Telegram's "hide sender name"
forward option (``forward_messages(drop_author=True)``) is a brand-new
message with no forward header — Telegram itself shows it as an ordinary
post, so it is kept like one.

Coordination with /setlink, /setanonymous, /setgap and /setdelay — the
forward filter runs in handler group -25, AFTER the /setlink filter (-40)
and the /setanonymous filter (-30) and BEFORE the channel commands
(-20/-19/-18/-17) and the gap/delay engine (0). A post the link or the
anonymous filter already rejected for this update is left to that filter
(no second delete call). A post the forward filter rejects must never
become an accepted /setgap reference or a /setdelay job, whatever happens
to its deletion:

1. ``mark_rejected`` records the verdict in-process (bounded LRU) before
   any Telegram call; the gap engine checks ``was_rejected`` and skips the
   post (same pattern as ``link_policy`` and ``anonymous_policy``).
2. Deletions Telegram refuses with FloodWait are queued for the persistent
   deletion worker (``autodelete_jobs``, reason ``FORWARD_REJECT_RETRY``)
   instead of being abandoned.
"""
from collections import OrderedDict
from typing import Tuple

from pyrogram import raw, types
from pyrogram.enums import MessageServiceType

from SWAGGYMUSIC.logging import LOGGER

log = LOGGER(__name__)

# How many per-post verdicts are kept (same scale as link_policy and
# anonymous_policy).
_REJECTED_MAX = 10_000
_REJECTED: "OrderedDict[Tuple[int, int], None]" = OrderedDict()

# Pyrogram 2.0-style forward attributes (instance attributes there,
# deprecated warning-logging properties in Kurigram — see the module doc).
_LEGACY_FORWARD_FIELDS = (
    "forward_date",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
)

# forward_origin_kind() results (log labels)
ORIGIN_USER = "user"
ORIGIN_HIDDEN_USER = "hidden_user"
ORIGIN_CHAT = "chat"
ORIGIN_CHANNEL = "channel"
ORIGIN_IMPORT = "import"
ORIGIN_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _raw_forward_header(message):
    """The ``fwd_from`` header of the raw MTProto message, or None."""
    raw_message = getattr(message, "raw", None)
    if not isinstance(raw_message, raw.types.Message):
        return None
    header = raw_message.fwd_from
    return header if isinstance(header, raw.types.MessageFwdHeader) else None


def _legacy_forward_fields(message) -> bool:
    """True if a Pyrogram 2.0-style instance attribute marks a forward."""
    try:
        attributes = vars(message)
    except TypeError:  # no instance __dict__
        return False
    return any(attributes.get(name) is not None for name in _LEGACY_FORWARD_FIELDS)


def is_forwarded(message) -> bool:
    """True if ``message`` is a forwarded post (see the module doc).

    Pure function: no I/O, never raises. Service messages are never
    forwarded posts.
    """
    try:
        if message is None:
            return False
        if isinstance(getattr(message, "service", None), MessageServiceType):
            return False
        if _raw_forward_header(message) is not None:
            return True
        if isinstance(getattr(message, "forward_origin", None), types.MessageOrigin):
            return True
        return _legacy_forward_fields(message)
    except Exception as exc:  # never let detection break the dispatcher
        log.warning("forward_policy.is_forwarded failed: %s: %s",
                    type(exc).__name__, exc)
        return False


def forward_origin_kind(message) -> str:
    """Where a forwarded post came from — an ORIGIN_* label, for logs only.

    Never resolves, stores or logs who the original sender was.
    """
    try:
        origin = getattr(message, "forward_origin", None)
        if isinstance(origin, types.MessageOrigin):
            name = getattr(getattr(origin, "type", None), "name", None)
            if name:
                return name.lower()
        header = _raw_forward_header(message)
        if header is None:
            return ORIGIN_UNKNOWN
        if isinstance(header.from_id, raw.types.PeerUser):
            return ORIGIN_USER
        if isinstance(header.from_id, (raw.types.PeerChannel, raw.types.PeerChat)):
            return ORIGIN_CHANNEL if header.channel_post else ORIGIN_CHAT
        if header.from_name:
            return ORIGIN_HIDDEN_USER
        if header.imported:
            return ORIGIN_IMPORT
    except Exception:
        pass
    return ORIGIN_UNKNOWN


# ---------------------------------------------------------------------------
# In-process verdicts (bounded; oldest dropped first)
# ---------------------------------------------------------------------------

def mark_rejected(chat_id: int, message_id: int) -> None:
    """Remember (in this process) that the forward filter rejected a post."""
    key = (int(chat_id), int(message_id))
    _REJECTED[key] = None
    _REJECTED.move_to_end(key)
    while len(_REJECTED) > _REJECTED_MAX:
        _REJECTED.popitem(last=False)


def was_rejected(chat_id: int, message_id: int) -> bool:
    try:
        return (int(chat_id), int(message_id)) in _REJECTED
    except (TypeError, ValueError):
        return False
