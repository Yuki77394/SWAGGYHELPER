"""Anonymous-post policy shared by the /setanonymous filter and /setgap.

Telegram shows who posted a channel message in one of three ways, chosen in
the channel's settings (core.telegram.org/api/channel):

  * ANONYMOUS (default) — every post is published as the channel. Telegram
    reveals no author at all: no ``from_id``, no signature.
  * SIGN MESSAGES — Telegram adds a text signature with the posting admin's
    name (``post_author``, exposed by Kurigram as ``author_signature``). The
    admin posted from their personal account; Telegram names them but does
    not expose the account (no ``from_id``).
  * SIGN MESSAGES + SHOW AUTHORS' PROFILES ("profiles") — each admin picks,
    per post, their own profile (``from_id`` = the admin's account), the
    channel itself, or another channel they own.

Policy (applied by ``plugins/misc/anonymous.py`` when /setanonymous is on):

* KEEP a post Telegram attributes to an admin's personal account:
    - ``from_id`` is a user (profiles mode, sent as the admin's profile);
    - or Telegram's author signature is present on a post published through
      the channel in Sign Messages mode (``SENDER_SIGNED_ADMIN``). The
      signature is set by Telegram for the admin who posted; the bot uses it
      only as "this post is signed", never as an identity: it does not look
      the name up, compare it, store it or log it.
* DELETE anonymous posts:
    - published as the channel with no signature (``SENDER_OWN_CHANNEL``);
    - in profiles mode, published as the channel even though the admin could
      have posted as their profile — that is the anonymous choice, whatever
      Telegram puts in ``post_author``;
    - sent with ANOTHER channel's / chat's identity (``SENDER_OTHER_CHAT``);
    - sender metadata that cannot be read (``SENDER_UNKNOWN``, fail closed).
* Service messages (pins, title/photo changes ...) are not posts and are
  left alone.
* In ANONYMOUS mode Telegram hides every author, so EVERY post is deleted
  while the protection is on. The /setanonymous wizard therefore refuses to
  enable it until Sign Messages is on, and the filter logs a warning.

History: the original filter kept every post with an author signature. In
round 2 the signature was mistakenly treated as meaningless text, so admins'
signed posts in Sign Messages channels were classified ``own_channel`` and
deleted. The signature rule above restores the original behaviour;
tests/test_anonymous_channel_modes.py pins every mode.

How the sender is read (``sender_identity``): the raw MTProto ``from_id``
that Kurigram keeps in ``Message.raw`` is the primary source — it tells a
user from a chat even when the update omitted the User/Chat object (where
Kurigram's parsed fields fall back to None or could pick the wrong peer
type). Messages without a raw update fall back to ``from_user`` /
``sender_chat`` / ``author_signature``. The channel's mode comes from the
channel object Telegram sends with every update (``Chat.sign_messages`` /
``Chat.show_message_sender_name``).

Coordination with /setgap — the anonymous filter runs in handler group
-30, the gap/delay engine in group 0 (Kurigram runs groups in ascending
order, sequentially, on the same message). A rejected post must never
become an accepted /setgap reference, whatever happens to its deletion:

1. ``mark_rejected`` records the verdict in-process before any Telegram
   call; the gap handler checks ``was_rejected`` and skips the post.
2. ``record_rejection`` persists the verdict in the channel's settings
   document (``anon_rejected_msg_ids``). The gap engine's atomic accept
   refuses any id listed there, so even a second process, a duplicate
   update or a future handler re-ordering cannot record the post.
"""
from collections import OrderedDict
from typing import Optional, Tuple

from pyrogram import raw, utils
from pyrogram.enums import MessageServiceType

from SWAGGYMUSIC.logging import LOGGER

log = LOGGER(__name__)

# How many rejected message ids are kept per channel in MongoDB.
ANON_REJECTED_LIMIT = 200
# In-process verdicts (bounded; oldest dropped first).
_REJECTED_MAX = 10_000
_REJECTED: "OrderedDict[Tuple[int, int], None]" = OrderedDict()

# sender_identity() results
SENDER_PERSONAL = "personal"        # Telegram exposed the personal account
SENDER_SIGNED_ADMIN = "signed_admin"  # Sign Messages: Telegram signed it with
                                      # the posting admin's name (account hidden)
SENDER_OWN_CHANNEL = "own_channel"  # published as this channel, anonymously
SENDER_OTHER_CHAT = "other_chat"    # sent with another channel's/chat's identity
SENDER_UNKNOWN = "unknown"          # no reliable sender metadata
SENDER_SERVICE = "service"          # service message, not a post

_CHAT_IDENTITIES = (SENDER_OWN_CHANNEL, SENDER_OTHER_CHAT)
_KEPT_IDENTITIES = (SENDER_PERSONAL, SENDER_SIGNED_ADMIN, SENDER_SERVICE)

# channel_posting_mode() results — the channel's Telegram settings
MODE_ANONYMOUS = "anonymous"    # Sign Messages off (Telegram's default)
MODE_SIGNATURES = "signatures"  # Sign Messages on, authors' profiles off
MODE_PROFILES = "profiles"      # Sign Messages + Show Authors' Profiles


def _plain_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _raw_sender(message) -> Optional[str]:
    """Classify the sender from the raw MTProto message, if Kurigram kept it.

    Returns None when there is no raw message to read (the caller then uses
    the parsed fields).
    """
    raw_message = getattr(message, "raw", None)
    if not isinstance(raw_message, (raw.types.Message, raw.types.MessageService)):
        return None
    from_id = raw_message.from_id
    if isinstance(from_id, raw.types.PeerUser):
        return SENDER_PERSONAL
    if isinstance(from_id, (raw.types.PeerChannel, raw.types.PeerChat)):
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        try:
            sender_id = utils.get_peer_id(from_id)
        except Exception:
            return SENDER_UNKNOWN
        if _plain_int(chat_id) and sender_id == chat_id:
            return SENDER_OWN_CHANNEL
        return SENDER_OTHER_CHAT
    if from_id is None and isinstance(raw_message.peer_id, raw.types.PeerChannel):
        # Telegram omits from_id for a post sent as the channel itself.
        return SENDER_OWN_CHANNEL
    return SENDER_UNKNOWN


def channel_posting_mode(chat) -> Optional[str]:
    """The channel's posting mode (a MODE_* constant) from the flags Telegram
    sends with the channel object, or None when they are not known.

    Kurigram maps ``channel.signatures`` to ``Chat.sign_messages`` and
    ``channel.signature_profiles`` to ``Chat.show_message_sender_name`` — on
    the chat of every update and on ``get_chat``. "min" channel objects may
    omit flags, so they count as unknown.
    """
    if chat is None or getattr(chat, "is_min", None) is True:
        return None
    if getattr(chat, "show_message_sender_name", None) is True:
        return MODE_PROFILES
    signatures = getattr(chat, "sign_messages", None)
    if signatures is True:
        return MODE_SIGNATURES
    if signatures is False:
        return MODE_ANONYMOUS
    return None


def has_post_signature(message) -> bool:
    """True if Telegram signed the post with its author's name (Sign Messages).

    Only presence is reported: the name itself is never returned, so it
    cannot end up in logs or be used as an identity.
    """
    raw_message = getattr(message, "raw", None)
    if isinstance(raw_message, raw.types.Message):
        value = raw_message.post_author
    else:
        value = getattr(message, "author_signature", None)
    return isinstance(value, str) and bool(value.strip())


def _parsed_sender(message) -> str:
    """Kurigram's parsed fields, for messages without the raw update."""
    if getattr(message, "from_user", None) is not None:
        return SENDER_PERSONAL
    sender_id = getattr(getattr(message, "sender_chat", None), "id", None)
    if not _plain_int(sender_id):
        return SENDER_UNKNOWN
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    return SENDER_OWN_CHANNEL if sender_id == chat_id else SENDER_OTHER_CHAT


def sender_identity(message) -> str:
    """Who Telegram says sent ``message`` — a SENDER_* constant.

    Never resolves or guesses a person. A post published through the channel
    is ``SENDER_SIGNED_ADMIN`` when Telegram signed it (Sign Messages mode):
    an admin's own post whose account Telegram does not expose. In profiles
    mode a post published as the channel stays ``SENDER_OWN_CHANNEL`` even if
    signed — the admin chose the channel over their profile.
    """
    if isinstance(getattr(message, "service", None), MessageServiceType) or isinstance(
        getattr(message, "raw", None), raw.types.MessageService,
    ):
        return SENDER_SERVICE
    sender = _raw_sender(message)
    if sender is None:
        sender = _parsed_sender(message)
    if (
        sender == SENDER_OWN_CHANNEL
        and has_post_signature(message)
        and channel_posting_mode(getattr(message, "chat", None)) != MODE_PROFILES
    ):
        return SENDER_SIGNED_ADMIN
    return sender


def is_channel_identity_post(message) -> bool:
    """True if the post was published anonymously with a chat identity —
    this channel's own (unsigned) or another channel's/chat's."""
    return sender_identity(message) in _CHAT_IDENTITIES


def should_reject_anonymous_post(message) -> bool:
    """Whether /setanonymous protection deletes this message.

    Kept: posts Telegram attributes to an admin's personal account (exposed
    profile, or Telegram's signature in Sign Messages mode) and service
    messages. Deleted: anonymous chat-identity posts and unreadable sender
    metadata (fail closed).
    """
    return sender_identity(message) not in _KEPT_IDENTITIES


def sender_chat_id(message) -> Optional[int]:
    """The chat identity Telegram reported for the post, for logging only."""
    raw_message = getattr(message, "raw", None)
    from_id = getattr(raw_message, "from_id", None)
    if isinstance(from_id, (raw.types.PeerChannel, raw.types.PeerChat)):
        try:
            return utils.get_peer_id(from_id)
        except Exception:
            pass
    sender_id = getattr(getattr(message, "sender_chat", None), "id", None)
    if _plain_int(sender_id):
        return sender_id
    if isinstance(raw_message, raw.types.Message) and from_id is None:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        return chat_id if _plain_int(chat_id) else None
    return None


def mark_rejected(chat_id: int, message_id: int) -> None:
    """Remember (in this process) that the anonymous filter rejected a post."""
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


async def record_rejection(chat_id: int, message_id: int) -> None:
    """Persist the verdict next to the gap state (same settings document).

    Also drops the post from the accepted-post history and from the gap
    reference in case it was ever recorded there (e.g. by another process);
    the gap engine then rebuilds the reference from the remaining accepted
    posts.
    """
    from SWAGGYMUSIC.utils import database

    message_id = int(message_id)
    await database.settings_db.update_one(
        {"chat_id": chat_id},
        {
            "$push": {"anon_rejected_msg_ids": {
                "$each": [message_id], "$slice": -ANON_REJECTED_LIMIT,
            }},
            "$pull": {"gap_history": {"msg_id": message_id}},
        },
    )
    await database.settings_db.update_one(
        {"chat_id": chat_id, "last_gap_post_msg_id": message_id},
        {"$unset": {"last_gap_post_at": "", "last_gap_post_msg_id": ""}},
    )


def is_rejected_in(setting: dict, message_id: int) -> bool:
    """True if the persisted verdicts in ``setting`` include ``message_id``."""
    rejected = (setting or {}).get("anon_rejected_msg_ids") or []
    return any(_plain_int(mid) and mid == message_id for mid in rejected)
