"""Anonymous-post policy shared by the /setanonymous filter and /setgap.

Policy (applied by ``plugins/misc/anonymous.py`` when /setanonymous is on):

* ALLOW posts where Telegram exposes the personal account that sent them:
  the raw message's ``from_id`` is a user (Kurigram then sets
  ``Message.from_user``, e.g. the channel's signatures link to the admins'
  profiles).
* REJECT posts sent with a chat identity, whenever the metadata says so:
    - the channel's OWN identity (``from_id`` omitted or this channel), and
    - ANOTHER channel's / chat's identity (``from_id`` is that chat;
      Kurigram exposes it as ``sender_chat``).
* A visible ``author_signature`` is free text set by Telegram, not an
  account. It does not make a post identifiable, and nothing here ever
  treats it as the identity of the admin who posted. When Telegram does
  not expose the human sender, nobody is "identified": logs name only the
  sender chat Telegram reported, never a person.
* Service messages (pins, title/photo changes ...) are not posts and are
  left alone. A post without reliable sender metadata is rejected while the
  protection is enabled: only a Telegram-exposed personal account is allowed.

How the sender is read (``sender_identity``): the raw MTProto ``from_id``
that Kurigram keeps in ``Message.raw`` is the primary source — it tells a
user from a chat even when the update omitted the User/Chat object (where
Kurigram's parsed fields fall back to None or could pick the wrong peer
type). Messages without a raw update fall back to ``from_user`` /
``sender_chat``.

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
SENDER_OWN_CHANNEL = "own_channel"  # sent with this channel's identity
SENDER_OTHER_CHAT = "other_chat"    # sent with another channel's/chat's identity
SENDER_UNKNOWN = "unknown"          # no reliable sender metadata
SENDER_SERVICE = "service"          # service message, not a post

_CHAT_IDENTITIES = (SENDER_OWN_CHANNEL, SENDER_OTHER_CHAT)


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


def sender_identity(message) -> str:
    """Who Telegram says sent ``message`` — a SENDER_* constant.

    Never resolves or guesses a person: a chat identity stays a chat
    identity, and a signature is ignored.
    """
    if isinstance(getattr(message, "service", None), MessageServiceType) or isinstance(
        getattr(message, "raw", None), raw.types.MessageService,
    ):
        return SENDER_SERVICE
    from_raw = _raw_sender(message)
    if from_raw is not None:
        return from_raw
    # No raw update: Kurigram's parsed fields.
    if getattr(message, "from_user", None) is not None:
        return SENDER_PERSONAL
    sender_id = getattr(getattr(message, "sender_chat", None), "id", None)
    if not _plain_int(sender_id):
        return SENDER_UNKNOWN
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    return SENDER_OWN_CHANNEL if sender_id == chat_id else SENDER_OTHER_CHAT


def is_channel_identity_post(message) -> bool:
    """True if the post was sent with a chat identity — this channel's own
    or another channel's/chat's — rather than an exposed personal account."""
    return sender_identity(message) in _CHAT_IDENTITIES




def should_reject_anonymous_post(message) -> bool:
    """Return whether anonymous protection should reject this message.

    Only posts whose personal sender is exposed by Telegram are allowed.
    Chat identities (this channel or another chat) and unknown sender
    metadata are rejected; service messages are exempt.
    """
    identity = sender_identity(message)
    return identity in _CHAT_IDENTITIES or identity == SENDER_UNKNOWN


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
