"""SCHEDULED POST MANAGER — the hidden /Schedule command.

Owner / SUDO only, in the bot's private chat. Not listed in the Help menu
or the Start message. Everyone else gets no answer at all (the command's
filter includes SUDOERS, exactly like /logger), and every button press
re-checks the permission (never trust the UI).

FLOW
    /Schedule -> 📌 One-Time Schedule | 🔁 Daily Schedule | 📋 Manage Schedules
      (sending a Channel ID right away opens that channel's schedules too)
    📌 One-Time / 🔁 Daily -> send the Channel ID -> compose the post in
       Telegram's own editor -> exact preview -> 📅 date (One-Time: the post
       date; Daily: the start date — calendar or DD-MM-YYYY) -> ⏰ hour ->
       minutes (or a typed hh:mm AM/PM) -> confirmation: Schedule Type,
       Channel, Start Date, Time, Next Execution, with 📅 Change Date and
       🕒 Change Time (each keeps the other) -> saved. A daily start date is
       never changed: if today's time has passed, the confirmation says the
       first post goes out tomorrow.
    📋 Manage -> send the Channel ID -> the channel's schedules: type,
       next run and status (📜 History: completed / cancelled / missed
       one-time posts); ➕ Add New -> choose One-Time or Daily -> as above
      a one-time post -> ✏️ Edit / 🕒 Reschedule / 👁 Preview /
         🚫 Cancel Schedule / 🗑 Delete
      a daily schedule -> ✏️ Edit / 🕒 Change Time / ⏸ Pause / ▶️ Resume /
         👁 Preview / 🗑 Delete
      (destructive actions ask for confirmation first)
    Every step has ❌ Cancel (and ⬅️ Back where it makes sense). Times are
    shown and typed in IST (utils/schedule_time.py).

PERMISSIONS
    * the admin must be in SUDOERS (the owner always is);
    * the channel must be a channel where the bot is an admin with Post
      Messages, and the admin must be an admin of that channel — the
      owner bypasses this last check (utils/permissions.verify_channel_admin,
      the same rule /setgap, /setlink and /createpost use). Every change is
      re-verified at the moment it is saved.

FORMATTING (utils/schedule_content.py)
    The post is stored as text + Telegram message entities (custom emoji
    ids included) and sent back with those same entities — no HTML or
    Markdown round trip. 📋 Plain Text mode removes formatting on purpose.
    Nothing is ever added to a post. 🛡 Protection (from a separate cleanup
    script) is ON only when the admin's own text contains "Radhe Radhe"
    (utils/protection_phrase.py) — it is shown, never written into the post.

DELIVERY (worker below, utils/scheduled_posts.py)
    One background task inside the bot process (started at boot, restored
    from MongoDB after every restart) sleeps until the next post is due —
    woken early when an admin creates or changes a schedule, and checking
    MongoDB at least every MAX_IDLE_WAIT seconds. A due post is claimed
    atomically (scheduled -> sending) before it is sent, so two workers,
    two processes or a restart can never send it twice. The result is
    recorded only after Telegram answers: sent (+ message id), failed
    (+ error), or deferred after a FloodWait. A post left in "sending" by a
    stopped bot is never auto-resent (its outcome is unknown).

    One-time: published once, then "sent" (shown as ✅ Completed). If the
    bot was offline at the post time and finds it more than
    SCHEDULE_MISSED_GRACE_MINUTES late, config.SCHEDULE_MISSED_POLICY
    decides: "send" (default) publishes it once now; "skip" marks it Missed.
    Daily: after each occurrence (published, failed or missed) the next one
    is computed for the following day; a missed occurrence is always
    skipped (no backlog), and at most one post goes out per IST day. Paused
    schedules are never claimed. The creator is told in private chat when a
    one-time post is sent, and when any post fails or is skipped.

HANDLER GROUPS
    -12  /Schedule (SUDO, private) — runs before any wizard's input handler
         so the command always works, then stops propagation.
    -11  input from an admin inside a /Schedule session (consumed, then
         propagation stops — no other wizard sees it). Any other command
         ends the session and continues to its own handler; /cancel ends
         it with a confirmation.
      0  callbacks "sch:..." (no other handler's pattern can match them:
         they never contain "close", "help_callback" or "settings_back_helper").
"""

import asyncio
import html
import uuid
from contextlib import suppress
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import StopPropagation, filters, types
from pyrogram.enums import ButtonStyle, ChatType
from pyrogram.errors import (FloodWait, InternalServerError, RPCError,
                             ServiceUnavailable)
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

import config
from SWAGGYMUSIC import app
from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.misc import SUDOERS
from SWAGGYMUSIC.utils import schedule_time as st
from SWAGGYMUSIC.utils import scheduled_posts as store
from SWAGGYMUSIC.utils.activity_logger import log_schedule_change
from SWAGGYMUSIC.utils.permissions import is_sudo, verify_channel_admin
from SWAGGYMUSIC.utils.schedule_content import (ContentError,
                                                custom_emoji_delivery,
                                                content_hash, entity_counts,
                                                extract_content,
                                                formatting_label, is_protected,
                                                kind_label, make_plain,
                                                preview_line, protection_label,
                                                send_content)

log = LOGGER(__name__)

PREFIX = "sch"
PAGE_SIZE = 5
SESSION_TTL = 60 * 60          # an idle wizard expires after an hour
VERIFY_CACHE_TTL = 30          # read-only views reuse a channel check this long

# Delivery worker tuning
MAX_IDLE_WAIT = 30             # longest sleep between MongoDB checks (catches
                               # schedules written by another process)
ERROR_BACKOFF = 5              # pause after an unexpected worker error
RECOVERY_INTERVAL = 120        # how often stale "sending" posts are checked
STALE_SENDING_AFTER = 300      # a claim older than this was interrupted
SEND_TIMEOUT = 120             # upper bound for one Telegram send
MAX_ATTEMPTS = 8               # claims (FloodWait retries) before giving up
BATCH = 20                     # posts handled before re-checking at once


def missed_policy() -> str:
    policy = str(getattr(config, "SCHEDULE_MISSED_POLICY", "send") or "send").lower()
    return policy if policy in ("send", "skip") else "send"


def missed_grace_seconds() -> int:
    try:
        minutes = int(getattr(config, "SCHEDULE_MISSED_GRACE_MINUTES", 10))
    except (TypeError, ValueError):
        minutes = 10
    return max(minutes, 1) * 60


# ---------------------------------------------------------------------------
# Lettering — the Create Post panel's style ("Pᴏsᴛ Cʀᴇᴀᴛᴏʀ", "❌ Cᴀɴᴄᴇʟ"):
# the first letter stays a capital, the rest become small capitals. "s" and
# "x" stay ASCII there too ("Pᴏsᴛ", "Tᴇxᴛ").
# ---------------------------------------------------------------------------

_SMALL_CAPS = str.maketrans({
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ",
    "f": "ғ", "g": "ɢ", "h": "ʜ", "i": "ɪ", "j": "ᴊ",
    "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ", "o": "ᴏ",
    "p": "ᴘ", "q": "ǫ", "r": "ʀ", "t": "ᴛ", "u": "ᴜ",
    "v": "ᴠ", "w": "ᴡ", "y": "ʏ", "z": "ᴢ",
})


def _sc(text: str) -> str:
    return text.translate(_SMALL_CAPS)


def _btn(label: str, data: str, style: Optional[ButtonStyle] = None) -> InlineKeyboardButton:
    kwargs: Dict[str, Any] = {"text": label, "callback_data": f"{PREFIX}:{data}"}
    if style is not None:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def _cancel_btn() -> InlineKeyboardButton:
    return _btn("❌ " + _sc("Cancel"), "abort", ButtonStyle.DANGER)


def _back_btn(data: str = "bk") -> InlineKeyboardButton:
    return _btn("⬅️ " + _sc("Back"), data, ButtonStyle.DANGER)


STATUS_LABELS = {
    store.STATUS_SCHEDULED: "🟢 Scheduled",
    store.STATUS_SENDING: "⏳ Sending",
    store.STATUS_SENT: "✅ Completed",
    store.STATUS_FAILED: "🔴 Failed",
    store.STATUS_CANCELLED: "🚫 Cancelled",
    store.STATUS_MISSED: "⌛ Missed",
    store.STATUS_PAUSED: "⏸ Paused",
}
TYPE_LABELS = {store.TYPE_ONCE: "📌 One-Time", store.TYPE_DAILY: "🔁 Daily"}


def _status_label(doc: Dict[str, Any]) -> str:
    status = doc.get("status")
    if status == store.STATUS_SCHEDULED and store.is_daily(doc):
        return "🟢 Active"
    return STATUS_LABELS.get(status, str(status))


def _daily_label(doc_or_time: Any) -> str:
    """"10:30 PM IST" for a daily schedule document or an (hour, minute)."""
    hour, minute = (store.daily_time_of(doc_or_time) if isinstance(doc_or_time, dict)
                    else doc_or_time)
    return f"{st.fmt_hhmm(hour, minute)} IST"

ASK_CHANNEL_TEXT = (
    "🗓 <b>" + _sc("Scheduled Posts") + "</b>\n\n"
    "Send the Channel ID whose scheduled posts you want to manage.\n\n"
    "<i>Example: <code>-1001234567890</code></i>"
)
HOME_TEXT = (
    "🗓 <b>" + _sc("Scheduled Posts") + "</b>\n\n"
    "📌 <b>One-Time</b> — publish a post once, at the date and time you pick.\n"
    "🔁 <b>Daily</b> — publish a post every day at the same time, from the date you pick.\n"
    "📋 <b>Manage</b> — see a channel's schedules: edit, pause, resume, "
    "reschedule, cancel or delete them.\n\n"
    "All times are IST (Asia/Kolkata).\n"
    "<i>Tip: send a Channel ID now to open its schedules directly.</i>"
)
_KIND_HEADERS = {
    store.TYPE_ONCE: "📌 <b>" + _sc("One-Time Schedule") + "</b>",
    store.TYPE_DAILY: "🔁 <b>" + _sc("Daily Schedule") + "</b>",
}
CLOSED_TEXT = "❌ <b>" + _sc("Schedule Manager Closed") + ".</b>"
EXPIRED_TEXT = "⌛ This step has expired. Send /Schedule to start again."
# Panels and notices never need a link preview (Kurigram 2.2.25 deprecates
# the old disable_web_page_preview flag in favour of LinkPreviewOptions).
_NO_PREVIEW = types.LinkPreviewOptions(is_disabled=True)


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def _when(ts: int) -> str:
    return f"{st.fmt_date(ts)} · {st.fmt_time(ts)}"


# ---------------------------------------------------------------------------
# Sessions (in memory; a confirmed post is persisted immediately)
# ---------------------------------------------------------------------------

SESSIONS: Dict[int, Dict[str, Any]] = {}
_LOCKS: Dict[int, Tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
_VERIFIED: Dict[Tuple[int, int], int] = {}


def _lock(user_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    holder = _LOCKS.get(user_id)
    if holder is None or holder[0] is not loop:
        holder = (loop, asyncio.Lock())
        _LOCKS[user_id] = holder
    return holder[1]


def _session(user_id: int) -> Optional[Dict[str, Any]]:
    session = SESSIONS.get(user_id)
    if session is None:
        return None
    if st.now_ts() - session.get("touched", 0) > SESSION_TTL:
        SESSIONS.pop(user_id, None)
        return None
    session["touched"] = st.now_ts()
    return session


def _new_session(user_id: int, **fields: Any) -> Dict[str, Any]:
    session: Dict[str, Any] = {
        "step": "channel", "channel_id": None, "channel_title": None,
        "mode": None, "target": None, "draft": None, "original": None,
        "plain_mode": False, "day": None, "hour": None, "scheduled_at": None,
        "cal": None, "draft_id": None, "done": False, "albums": set(),
        "kind": None, "purpose": "manage", "daily_time": None,
        "change_from": None, "confirmed": None,
        "touched": st.now_ts(),
    }
    session.update(fields)
    SESSIONS[user_id] = session
    return session


def _end_session(user_id: int) -> None:
    SESSIONS.pop(user_id, None)


def _reset_wizard(session: Dict[str, Any], step: str = "list") -> None:
    session.update({
        "step": step, "mode": None, "target": None, "draft": None,
        "original": None, "plain_mode": False, "day": None, "hour": None,
        "scheduled_at": None, "cal": None, "draft_id": None, "done": False,
        "kind": None, "daily_time": None, "change_from": None, "confirmed": None,
    })
    session["albums"] = set()


def _snapshot(session: Dict[str, Any]) -> None:
    """Remember the date/time shown on the confirmation screen, so ⬅️ Back
    from 📅 Change Date / 🕒 Change Time returns to it unchanged."""
    session["confirmed"] = {key: session.get(key)
                            for key in ("day", "cal", "hour", "daily_time", "scheduled_at")}
    session["change_from"] = None


def _restore_confirm(session: Dict[str, Any]) -> bool:
    snapshot = session.get("confirmed") or {}
    if snapshot.get("scheduled_at") is None:
        return False
    session.update(snapshot)
    session.update({"step": "confirm", "change_from": None})
    return True


def _selected_time(session: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """The (hour, minute) already chosen in this wizard, if any."""
    if session.get("daily_time"):
        return tuple(session["daily_time"])
    if session.get("scheduled_at"):
        return st.ist_hm(int(session["scheduled_at"]))
    return None


def _is_daily_wizard(session: Dict[str, Any]) -> bool:
    """The wizard picks a daily time (new daily schedule, or a time change)."""
    return session.get("mode") == "retime" or (
        session.get("mode") == "new" and session.get("kind") == store.TYPE_DAILY)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def _authorized(user: Any) -> bool:
    user_id = getattr(user, "id", None)
    return isinstance(user_id, int) and is_sudo(user_id)


def _parse_channel_id(text: str) -> Optional[int]:
    raw = (text or "").strip()
    if not raw.startswith("-100") or not raw[1:].isdigit() or len(raw) < 8:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def _check_channel(channel_id: int, user_id: int) -> Tuple[bool, Any, Optional[str]]:
    """(ok, chat, reason): a channel where the bot posts and the admin manages."""
    try:
        chat = await app.get_chat(channel_id)
    except Exception as exc:
        return False, None, (
            "❌ <b>I can't access that channel.</b>\n\n"
            "Add me to the channel as an admin with <b>Post Messages</b>, "
            "then try again.\n\n"
            f"<code>{_esc(type(exc).__name__)}</code>"
        )
    if getattr(chat, "type", None) != ChatType.CHANNEL:
        return False, None, (
            "❌ <b>That ID is not a channel.</b>\n\n"
            "Scheduled posts work in channels. Send a channel ID such as "
            "<code>-1001234567890</code>."
        )
    ok, reason = await verify_channel_admin(
        channel_id, user_id, require_delete=False, require_post=True,
    )
    if not ok:
        return False, None, reason
    _VERIFIED[(int(user_id), int(channel_id))] = st.now_ts() + VERIFY_CACHE_TTL
    return True, chat, None


async def _can_view(channel_id: int, user_id: int) -> Tuple[bool, Optional[str]]:
    """Read-only access: a channel check from the last few seconds counts."""
    if _VERIFIED.get((int(user_id), int(channel_id)), 0) >= st.now_ts():
        return True, None
    ok, _chat, reason = await _check_channel(channel_id, user_id)
    return ok, reason


def _chat_title(chat: Any, channel_id: int) -> str:
    return str(getattr(chat, "title", None) or channel_id)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _retry_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn("🔄 " + _sc("Retry"), "retry", ButtonStyle.PRIMARY), _cancel_btn(),
    ]])


def render_home() -> Tuple[str, InlineKeyboardMarkup]:
    """/Schedule: choose the schedule type, or manage existing schedules."""
    return HOME_TEXT, InlineKeyboardMarkup([
        [_btn("📌 " + _sc("One-Time Schedule"), "ty:once", ButtonStyle.PRIMARY),
         _btn("🔁 " + _sc("Daily Schedule"), "ty:daily", ButtonStyle.PRIMARY)],
        [_btn("📋 " + _sc("Manage Schedules"), "mg", ButtonStyle.SUCCESS)],
        [_cancel_btn()],
    ])


def render_ask_channel(session: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    kind = session.get("kind")
    if session.get("purpose") == "create" and kind in _KIND_HEADERS:
        text = (
            f"{_KIND_HEADERS[kind]}\n\n"
            "Send the Channel ID where the post should be published.\n\n"
            "<i>Example: <code>-1001234567890</code></i>"
        )
    else:
        text = ASK_CHANNEL_TEXT
    return text, InlineKeyboardMarkup([[_back_btn("hm"), _cancel_btn()]])


def render_types(session: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    """➕ Add New for a known channel: One-Time or Daily?"""
    text = (
        "➕ <b>" + _sc("New Schedule") + f"</b> — <b>{_esc(session.get('channel_title'))}</b>\n\n"
        "📌 <b>One-Time</b> — publish the post once, at the date and time you pick.\n"
        "🔁 <b>Daily</b> — publish it every day at the same time (IST), from the start "
        "date you pick until you pause or delete it.\n\n"
        "Choose the schedule type:"
    )
    return text, InlineKeyboardMarkup([
        [_btn("📌 " + _sc("One-Time Schedule"), "ty:once", ButtonStyle.PRIMARY),
         _btn("🔁 " + _sc("Daily Schedule"), "ty:daily", ButtonStyle.PRIMARY)],
        [_back_btn(), _cancel_btn()],
    ])


async def render_list(channel_id: int, title: str, view: str = "p", page: int = 0,
                      notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    view = "h" if view == "h" else "p"
    pending = view == "p"
    page = max(int(page), 0)
    posts, total = await store.list_schedules(
        channel_id, "pending" if pending else "history", page, PAGE_SIZE,
    )
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    if page >= total_pages and total:
        page = total_pages - 1
        posts, total = await store.list_schedules(
            channel_id, "pending" if pending else "history", page, PAGE_SIZE,
        )

    header = (
        ("🗓 <b>" + _sc("Scheduled Posts") + "</b>") if pending
        else ("📜 <b>" + _sc("Post History") + "</b>")
    )
    lines = []
    if notice:
        lines += [notice, ""]
    lines += [f"{header} — <b>{_esc(title)}</b>",
              f"🆔 <b>Channel ID:</b> <code>{int(channel_id)}</code>", ""]
    if not posts:
        lines.append("No scheduled posts found for this channel." if pending
                     else "No completed, cancelled or missed posts yet.")
    rows: List[List[InlineKeyboardButton]] = []
    number_row: List[InlineKeyboardButton] = []
    for index, doc in enumerate(posts, start=page * PAGE_SIZE + 1):
        sid = int(doc["schedule_id"])
        ts = int(doc["scheduled_at"])
        lines.append(f"<b>{index}.</b> {_esc(preview_line(doc.get('content') or {}))} · <code>#{sid}</code>")
        if store.is_daily(doc):
            paused = doc.get("status") == store.STATUS_PAUSED
            lines += [
                "   📅 Next: — (paused)" if paused else f"   📅 Next: {st.fmt_date(ts)}",
                f"   ⏰ {_daily_label(doc)} · every day",
            ]
        else:
            lines += [f"   📅 {st.fmt_date(ts)}", f"   ⏰ {st.fmt_time(ts)}"]
        lines += [
            f"   Type: {TYPE_LABELS[store.schedule_type(doc)]}",
            f"   Status: {_status_label(doc)}",
            "",
        ]
        number_row.append(_btn(str(index), f"it:{sid}:{view}:{page}"))
        if len(number_row) == 5:
            rows.append(number_row)
            number_row = []
    if number_row:
        rows.append(number_row)
    if total_pages > 1:
        lines.append(f"<i>Page {page + 1}/{total_pages} · {total} posts</i>")
        nav = []
        if page > 0:
            nav.append(_btn("◀️", f"ls:{channel_id}:{view}:{page - 1}", ButtonStyle.PRIMARY))
        nav.append(_btn(f"{page + 1}/{total_pages}", "nop"))
        if page + 1 < total_pages:
            nav.append(_btn("▶️", f"ls:{channel_id}:{view}:{page + 1}", ButtonStyle.PRIMARY))
        rows.append(nav)
    if pending:
        rows.append([
            _btn("➕ " + _sc("Add New"), f"new:{channel_id}", ButtonStyle.SUCCESS),
            _btn("🔄 " + _sc("Refresh"), f"ls:{channel_id}:p:{page}", ButtonStyle.PRIMARY),
        ])
        rows.append([
            _btn("📜 " + _sc("History"), f"ls:{channel_id}:h:0", ButtonStyle.PRIMARY),
            _btn("❌ " + _sc("Close"), "x", ButtonStyle.DANGER),
        ])
    else:
        rows.append([
            _back_btn(f"ls:{channel_id}:p:0"),
            _btn("🔄 " + _sc("Refresh"), f"ls:{channel_id}:h:{page}", ButtonStyle.PRIMARY),
        ])
        rows.append([_btn("❌ " + _sc("Close"), "x", ButtonStyle.DANGER)])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(rows)


def render_daily_item(doc: Dict[str, Any], view: str = "p", page: int = 0,
                      notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    sid = int(doc["schedule_id"])
    ts = int(doc["scheduled_at"])
    status = doc.get("status")
    content = doc.get("content") or {}
    lines = [notice, ""] if notice else []
    lines += [
        "🔁 <b>" + _sc("Daily Schedule") + f"</b> <code>#{sid}</code>",
        "",
        f"📌 <b>Channel:</b> {_esc(doc.get('channel_title') or doc['channel_id'])}",
        f"🔁 <b>Repeats:</b> Every day at {_daily_label(doc)}",
        f"📅 <b>Start Date:</b> {st.fmt_day(store.start_date_of(doc))}",
        "⏭ <b>Next post:</b> — paused" if status == store.STATUS_PAUSED
        else f"⏭ <b>Next post:</b> {_when(ts)}",
        f"📊 <b>Status:</b> {_status_label(doc)}",
        f"📝 <b>Type:</b> {kind_label(content)}",
        f"✨ <b>Formatting:</b> {formatting_label(content)}",
        f"🛡 <b>Protection:</b> {protection_label(content)}",
        f"💬 <b>Preview:</b> {_esc(preview_line(content, 48))}",
        f"👤 <b>Created by:</b> <code>{int(doc.get('creator_id') or 0)}</code>",
    ]
    if doc.get("last_run_at"):
        last = f"✅ <b>Last post:</b> {_when(int(doc['last_run_at']))}"
        if doc.get("last_sent_message_id"):
            last += f" · message <code>{int(doc['last_sent_message_id'])}</code>"
        lines.append(last)
    lines.append(f"🔢 <b>Posts published:</b> {int(doc.get('run_count') or 0)}")
    skip = doc.get("last_skip") or {}
    if skip.get("occurrence"):
        lines.append(f"⌛ <b>Last skipped:</b> {_when(int(skip['occurrence']))} (the bot was offline)")
    error = doc.get("error") or {}
    if error:
        when = f" ({_when(int(error['occurrence']))})" if error.get("occurrence") else ""
        lines.append(f"⚠️ <b>Last error{when}:</b> <code>{_esc(error.get('code'))}</code> — "
                     f"{_esc(error.get('message'))}")
    for warning in doc.get("delivery_warnings") or []:
        lines.append(f"⚠️ {_esc(warning)}")

    rows: List[List[InlineKeyboardButton]] = []
    if status in store.RETIMABLE_STATUSES:
        rows.append([
            _btn("✏️ " + _sc("Edit"), f"ed:{sid}", ButtonStyle.PRIMARY),
            _btn("🕒 " + _sc("Change Time"), f"rs:{sid}", ButtonStyle.PRIMARY),
        ])
    if status == store.STATUS_SCHEDULED:
        rows.append([_btn("⏸ " + _sc("Pause"), f"ps:{sid}", ButtonStyle.PRIMARY)])
    elif status == store.STATUS_PAUSED:
        rows.append([_btn("▶️ " + _sc("Resume"), f"rm:{sid}", ButtonStyle.SUCCESS)])
    preview_row = [_btn("👁 " + _sc("Preview"), f"pv:{sid}", ButtonStyle.PRIMARY)]
    if status in store.DELETABLE_STATUSES:
        preview_row.append(_btn("🗑 " + _sc("Delete"), f"da:{sid}", ButtonStyle.DANGER))
    rows.append(preview_row)
    if status == store.STATUS_SENDING:
        rows.append([_btn("🔄 " + _sc("Refresh"), f"it:{sid}:{view}:{page}", ButtonStyle.PRIMARY)])
    rows.append([_back_btn(f"ls:{int(doc['channel_id'])}:{view}:{page}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def render_item(doc: Dict[str, Any], view: str = "p", page: int = 0,
                notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    if store.is_daily(doc):
        return render_daily_item(doc, view, page, notice)
    sid = int(doc["schedule_id"])
    ts = int(doc["scheduled_at"])
    status = doc.get("status")
    content = doc.get("content") or {}
    lines = []
    if notice:
        lines += [notice, ""]
    lines += [
        "🗓 <b>" + _sc("Scheduled Post") + f"</b> <code>#{sid}</code>",
        "",
        f"📌 <b>Channel:</b> {_esc(doc.get('channel_title') or doc['channel_id'])}",
        f"🗂 <b>Schedule:</b> {TYPE_LABELS[store.TYPE_ONCE]}",
        f"📅 <b>Date:</b> {st.fmt_date(ts)}",
        f"⏰ <b>Time:</b> {st.fmt_time(ts)}",
        f"📊 <b>Status:</b> {_status_label(doc)}",
        f"📝 <b>Type:</b> {kind_label(content)}",
        f"✨ <b>Formatting:</b> {formatting_label(content)}",
        f"🛡 <b>Protection:</b> {protection_label(content)}",
        f"💬 <b>Preview:</b> {_esc(preview_line(content, 48))}",
        f"👤 <b>Created by:</b> <code>{int(doc.get('creator_id') or 0)}</code>",
    ]
    if status == store.STATUS_SENT:
        sent_at = doc.get("sent_at")
        if sent_at:
            lines.append(f"✅ <b>Sent:</b> {_when(int(sent_at))}")
        if doc.get("sent_message_id"):
            lines.append(f"🔢 <b>Message ID:</b> <code>{int(doc['sent_message_id'])}</code>")
        if (doc.get("late_by") or 0) >= 60:
            lines.append(f"⏱ <b>Late by:</b> {int(doc['late_by']) // 60} min (the bot was offline)")
    error = doc.get("error") or {}
    if status in (store.STATUS_FAILED, store.STATUS_MISSED) and error:
        lines.append(f"⚠️ <b>Error:</b> <code>{_esc(error.get('code'))}</code> — {_esc(error.get('message'))}")
    for warning in doc.get("delivery_warnings") or []:
        lines.append(f"⚠️ {_esc(warning)}")

    rows: List[List[InlineKeyboardButton]] = []
    if status in store.EDITABLE_STATUSES:
        rows.append([
            _btn("✏️ " + _sc("Edit"), f"ed:{sid}", ButtonStyle.PRIMARY),
            _btn("🕒 " + _sc("Reschedule"), f"rs:{sid}", ButtonStyle.PRIMARY),
        ])
    preview_row = [_btn("👁 " + _sc("Preview"), f"pv:{sid}", ButtonStyle.PRIMARY)]
    if status in store.CANCELLABLE_STATUSES:
        preview_row.append(_btn("🚫 " + _sc("Cancel Schedule"), f"ca:{sid}", ButtonStyle.DANGER))
    rows.append(preview_row)
    if status in store.DELETABLE_STATUSES:
        rows.append([_btn("🗑 " + _sc("Delete"), f"da:{sid}", ButtonStyle.DANGER)])
    if status == store.STATUS_SENDING:
        rows.append([_btn("🔄 " + _sc("Refresh"), f"it:{sid}:{view}:{page}", ButtonStyle.PRIMARY)])
    rows.append([_back_btn(f"ls:{int(doc['channel_id'])}:{view}:{page}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def render_compose(session: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    if session.get("mode") == "edit":
        header = "✏️ <b>" + _sc("Edit Post") + f"</b> <code>#{session['target']}</code>"
    else:
        header = "✍️ <b>" + _sc("Compose Post") + "</b>"
        if session.get("kind") in TYPE_LABELS:
            header += f" — {TYPE_LABELS[session['kind']]}"
    plain = session.get("plain_mode")
    mode_line = (
        "📋 <b>Plain Text Mode:</b> ON — formatting will be removed from the "
        "message you send; the text stays exactly the same."
        if plain else "📋 <b>Plain Text Mode:</b> OFF"
    )
    text = (
        f"{header}\n"
        f"📌 <b>Channel:</b> {_esc(session.get('channel_title'))}\n\n"
        "Send the post exactly as it should appear in the channel. Format it "
        "with Telegram's own editor — <b>bold</b>, <i>italic</i>, <u>underline</u>, "
        "<s>strikethrough</s>, <tg-spoiler>spoiler</tg-spoiler>, "
        "<code>monospace</code>, links and Premium custom emoji are all kept.\n\n"
        "You can also send ONE photo, video, GIF, document, audio or voice note "
        "with a caption.\n\n"
        f"{mode_line}"
    )
    markup = InlineKeyboardMarkup([
        [_btn("📋 " + _sc("Plain Text") + (": ON" if plain else ": OFF"), "pm", ButtonStyle.PRIMARY)],
        [_back_btn(), _cancel_btn()],
    ])
    return text, markup


def render_preview_controls(session: Dict[str, Any], *, kept: Optional[int] = None,
                            wanted: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
    draft = session["draft"]
    lines = [
        "👁 <b>" + _sc("Preview") + "</b> ☝️ — the message above is exactly how "
        "the post will look.",
        "",
        f"📝 <b>Type:</b> {kind_label(draft)}",
        f"✨ <b>Formatting:</b> {formatting_label(draft)}",
        f"🛡 <b>Protection:</b> {protection_label(draft)}",
    ]
    if draft.get("plain_text"):
        lines.append("📋 <b>Plain text mode is active</b> — all formatting was removed.")
    _total, custom = entity_counts(draft)
    if custom:
        if kept is not None and kept < wanted:
            lines.append(
                f"⚠️ Telegram did not show {wanted - kept} of {wanted} custom emoji even in "
                "this private preview: the bot can only use Premium emoji if its owner "
                "has Telegram Premium."
            )
        lines.append(
            "⚠️ <b>Premium emoji in channels:</b> Telegram lets bots post custom "
            "emoji in channels only if the bot owns a Fragment username; otherwise "
            "Telegram may replace them with standard emoji or refuse the post. "
            "After delivery the bot tells you what Telegram did."
        )
    if session.get("mode") == "edit":
        primary = _btn("💾 " + _sc("Save Changes"), "nx", ButtonStyle.SUCCESS)
    else:
        primary = _btn("➡️ " + _sc("Continue"), "nx", ButtonStyle.SUCCESS)
    if draft.get("plain_text") and session.get("original") and session["original"].get("entities"):
        toggle = _btn("✨ " + _sc("Restore Formatting"), "pt", ButtonStyle.PRIMARY)
    elif draft.get("entities"):
        toggle = _btn("📋 " + _sc("Plain Text"), "pt", ButtonStyle.PRIMARY)
    else:
        toggle = None
    edit_row = [_btn("✏️ " + _sc("Edit Message"), "cm", ButtonStyle.PRIMARY)]
    if toggle is not None:
        edit_row.append(toggle)
    markup = InlineKeyboardMarkup([[primary], edit_row, [_cancel_btn()]])
    return "\n".join(lines), markup


def render_calendar(session: Dict[str, Any], notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    now = st.now_ts()
    today = st.ist_today(now)
    last_day = st.last_allowed_day(now)
    year, month = session.get("cal") or (today.year, today.month)
    lines = []
    if notice:
        lines += [notice, ""]
    if session.get("mode") == "reschedule":
        lines.append("🕒 <b>" + _sc("Reschedule") + f"</b> <code>#{session['target']}</code>")
        current = session.get("current_at")
        if current:
            lines.append(f"Currently: {_when(int(current))}")
        lines.append("")
    daily_new = session.get("mode") == "new" and session.get("kind") == store.TYPE_DAILY
    lines += [
        ("📅 <b>" + _sc("Start Date") + "</b>") if daily_new else ("📅 <b>" + _sc("Select Date") + "</b>"),
        f"🕒 Now: {_when(now)}",
        "",
    ]
    if daily_new:
        lines.append("Tap the day the daily schedule starts (IST), or send it as "
                     "<code>DD-MM-YYYY</code> — optionally with the time, e.g. "
                     "<code>06-11-2026 10:30 AM</code>.")
    else:
        lines.append("Tap a date (IST), or send it as <code>DD-MM-YYYY</code> — optionally with "
                     "the time, e.g. <code>05-11-2026 10:30 PM</code>.")
    if session.get("change_from") == "date" and session.get("scheduled_at"):
        start = _session_day(session)
        if daily_new and session.get("daily_time") and start is not None:
            current = (f"starts {st.fmt_day(start)}, every day at "
                       f"{_daily_label(tuple(session['daily_time']))}")
        else:
            current = st.fmt_datetime(int(session["scheduled_at"]))
        lines.append(f"<i>Currently: {current} — the time stays the same.</i>")
    selected = session.get("day")
    rows: List[List[InlineKeyboardButton]] = []
    prev_ym = st.shift_month(year, month, -1)
    next_ym = st.shift_month(year, month, 1)
    nav = []
    if (prev_ym[0], prev_ym[1]) >= (today.year, today.month):
        nav.append(_btn("◀️", f"cal:{prev_ym[0]:04d}{prev_ym[1]:02d}", ButtonStyle.PRIMARY))
    nav.append(_btn(st.month_label(year, month), "nop"))
    if (next_ym[0], next_ym[1]) <= (last_day.year, last_day.month):
        nav.append(_btn("▶️", f"cal:{next_ym[0]:04d}{next_ym[1]:02d}", ButtonStyle.PRIMARY))
    rows.append(nav)
    rows.append([_btn(name, "nop") for name in st.WEEKDAYS])
    for week in st.month_weeks(year, month):
        row = []
        for day_number in week:
            if not day_number:
                row.append(_btn(" ", "nop"))
                continue
            day = date(year, month, day_number)
            if st.date_error(day, now):
                row.append(_btn("·", "nop"))
                continue
            style = None
            if selected == day.isoformat():
                style = ButtonStyle.PRIMARY
            elif day == today:
                style = ButtonStyle.SUCCESS
            row.append(_btn(str(day_number), f"day:{day.strftime('%Y%m%d')}", style))
        rows.append(row)
    rows.append([_back_btn(), _cancel_btn()])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _session_day(session: Dict[str, Any]) -> Optional[date]:
    with suppress(TypeError, ValueError):
        return date.fromisoformat(session.get("day") or "")
    return None


def _daily_start(session: Dict[str, Any]) -> Optional[date]:
    """Start date of the daily schedule being created / re-timed."""
    if session.get("mode") == "retime":
        with suppress(TypeError, ValueError):
            return date.fromisoformat(session.get("start_date") or "")
        return None
    return _session_day(session)


def _daily_header(session: Dict[str, Any]) -> List[str]:
    if session.get("mode") == "retime":
        lines = ["🕒 <b>" + _sc("Change Time") + f"</b> <code>#{session['target']}</code>"]
        if session.get("current_daily"):
            lines.append(f"Currently: every day at {_daily_label(tuple(session['current_daily']))}")
        return lines
    return ["🔁 <b>" + _sc("Daily Time") + "</b>"]


def render_hours(session: Dict[str, Any], notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    daily = _is_daily_wizard(session)
    day = None if daily else _session_day(session)
    now = st.now_ts()
    lines = []
    if notice:
        lines += [notice, ""]
    if daily:
        lines += _daily_header(session)
        start = _daily_start(session)
        if start is not None:
            lines.append(f"📅 <b>Start Date:</b> {st.fmt_day(start)}")
        lines += [
            "",
            "Tap the hour (IST) at which the post goes out <b>every day</b>, or send "
            "the time as <code>hh:mm AM/PM</code> (e.g. <code>10:30 PM</code>).",
        ]
    else:
        lines += [
            "⏰ <b>" + _sc("Select Time") + "</b>",
            f"📅 <b>Date:</b> {st.fmt_day(day)}",
            "",
            "Tap the hour (IST), or send the time as <code>hh:mm AM/PM</code> "
            "(e.g. <code>10:30 PM</code>).",
        ]
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for hour in range(24):
        if daily or st.hour_is_open(day, hour, now):
            row.append(_btn(st.fmt_hour(hour), f"hr:{hour:02d}"))
        else:
            row.append(_btn("·", "nop"))
        if len(row) == 4:
            rows.append(row)
            row = []
    rows.append([_back_btn(), _cancel_btn()])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def render_minutes(session: Dict[str, Any], notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    daily = _is_daily_wizard(session)
    day = None if daily else _session_day(session)
    hour = int(session["hour"])
    now = st.now_ts()
    lines = []
    if notice:
        lines += [notice, ""]
    start = _daily_start(session) if daily else None
    if daily:
        where = (f"🔁 <b>Every day</b> from {st.fmt_day(start)} · ⏰ <b>Hour:</b> {st.fmt_hour(hour)}"
                 if start is not None else f"🔁 <b>Every day</b> · ⏰ <b>Hour:</b> {st.fmt_hour(hour)}")
    else:
        where = f"📅 <b>Date:</b> {st.fmt_day(day)} · ⏰ <b>Hour:</b> {st.fmt_hour(hour)}"
    lines += [
        "⏰ <b>" + _sc("Select Minutes") + "</b>",
        where,
        "",
        "Tap the minutes, or send an exact time as <code>hh:mm AM/PM</code>.",
    ]
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for minute in range(0, 60, 5):
        if daily or st.minute_is_open(day, hour, minute, now):
            row.append(_btn(st.fmt_hhmm(hour, minute), f"mn:{minute:02d}"))
        else:
            row.append(_btn("·", "nop"))
        if len(row) == 4:
            rows.append(row)
            row = []
    rows.append([_back_btn(), _cancel_btn()])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _first_run_note(start: date, hour: int, minute: int, first: int) -> Optional[str]:
    """Why a daily schedule's first post is later than its start date's time
    (never silent: the start date itself stays as picked)."""
    planned = st.ist_timestamp(start, hour, minute)
    if first == planned:
        return None
    now = st.now_ts()
    if start == st.ist_today(now):
        reason = ("has already passed" if planned <= now else "is less than a minute away")
        return (f"ℹ️ Today's {_daily_label((hour, minute))} {reason}, so the first post will go "
                f"out <b>tomorrow, {st.fmt_datetime(first)}</b>. The start date stays "
                f"{st.fmt_day(start)}.")
    return (f"ℹ️ {st.fmt_day(start)} at {_daily_label((hour, minute))} is no longer possible, so the "
            f"first post will go out on <b>{st.fmt_datetime(first)}</b>. The start date stays "
            f"{st.fmt_day(start)}.")


def _date_time_buttons(confirm_label: str) -> List[List[InlineKeyboardButton]]:
    return [
        [_btn("✅ " + _sc(confirm_label), "ok", ButtonStyle.SUCCESS)],
        [_btn("📅 " + _sc("Change Date"), "cd", ButtonStyle.PRIMARY),
         _btn("🕒 " + _sc("Change Time"), "ct", ButtonStyle.PRIMARY)],
    ]


def render_confirm(session: Dict[str, Any], notice: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    ts = int(session["scheduled_at"])
    channel = f"<code>{int(session['channel_id'])}</code> · {_esc(session.get('channel_title'))}"
    lines = []
    if notice:
        lines += [notice, ""]
    if session.get("mode") == "retime":
        lines += [
            "🕒 <b>" + _sc("Confirm New Time") + f"</b> <code>#{session['target']}</code>",
            "",
            f"📌 <b>Channel:</b> {channel}",
        ]
        if session.get("start_date"):
            lines.append(f"📅 <b>Start Date:</b> {st.fmt_day(date.fromisoformat(session['start_date']))}")
        if session.get("current_daily"):
            lines.append(f"⏰ <b>Old time:</b> every day at {_daily_label(tuple(session['current_daily']))}")
        lines.append(f"⏰ <b>New time:</b> every day at {_daily_label(tuple(session['daily_time']))}")
        lines.append("⏸ <b>Paused</b> — it restarts at the new time when you resume it."
                     if session.get("target_paused") else f"⏭ <b>Next post:</b> {_when(ts)}")
        markup = InlineKeyboardMarkup([
            [_btn("✅ " + _sc("Confirm New Time"), "ok", ButtonStyle.SUCCESS)],
            [_btn("🕒 " + _sc("Change Time"), "ct", ButtonStyle.PRIMARY), _cancel_btn()],
        ])
        return "\n".join(lines), markup
    if session.get("mode") == "reschedule":
        lines += [
            "🕒 <b>" + _sc("Confirm Reschedule") + f"</b> <code>#{session['target']}</code>",
            "",
            f"📌 <b>Channel:</b> {channel}",
        ]
        if session.get("current_at"):
            lines.append(f"📅 <b>Old:</b> {_when(int(session['current_at']))}")
        lines += [f"📅 <b>Date:</b> {st.fmt_date(ts)}", f"⏰ <b>Time:</b> {st.fmt_time(ts)}"]
        markup = InlineKeyboardMarkup(_date_time_buttons("Confirm Reschedule") + [[_cancel_btn()]])
        return "\n".join(lines), markup

    # A new schedule — the same screen for both types.
    daily = session.get("kind") == store.TYPE_DAILY
    draft = session["draft"]
    start = _session_day(session) or st.to_ist(ts).date()
    hour, minute = tuple(session["daily_time"]) if daily else st.ist_hm(ts)
    lines += [
        ("🔁 <b>" + _sc("Confirm Daily Schedule") + "</b>") if daily
        else ("🗓 <b>" + _sc("Confirm Scheduled Post") + "</b>"),
        "",
        f"🗂 <b>Schedule Type:</b> {TYPE_LABELS[store.TYPE_DAILY if daily else store.TYPE_ONCE]}",
        f"📌 <b>Channel:</b> {channel}",
        f"📅 <b>Start Date:</b> {st.fmt_day(start)}",
        f"⏰ <b>Time:</b> {_daily_label((hour, minute))}" + (" · every day" if daily else ""),
        f"⏭ <b>Next Execution:</b> {st.fmt_datetime(ts)}",
        f"📝 <b>Type:</b> {kind_label(draft)}",
        f"✨ <b>Formatting:</b> {formatting_label(draft)}",
        f"🛡 <b>Protection:</b> {protection_label(draft)}",
    ]
    note = _first_run_note(start, hour, minute, ts) if daily else None
    if note:
        lines += ["", note]
    markup = InlineKeyboardMarkup(_date_time_buttons("Confirm Schedule") + [
        [_btn("✏️ " + _sc("Edit Message"), "cm", ButtonStyle.PRIMARY), _cancel_btn()],
    ])
    return "\n".join(lines), markup


def render_scheduled(doc: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    ts = int(doc["scheduled_at"])
    if store.is_daily(doc):
        text = (
            "✅ <b>" + _sc("Daily Schedule Created") + "</b>\n\n"
            f"🆔 <b>Schedule:</b> <code>#{int(doc['schedule_id'])}</code>\n"
            f"📌 <b>Channel:</b> <code>{int(doc['channel_id'])}</code> · {_esc(doc.get('channel_title'))}\n"
            f"🔁 <b>Repeats:</b> Every day at {_daily_label(doc)}\n"
            f"📅 <b>Start Date:</b> {st.fmt_day(store.start_date_of(doc))}\n"
            f"⏭ <b>Next Execution:</b> {st.fmt_datetime(ts)}"
        )
    else:
        text = (
            "✅ <b>" + _sc("Post Scheduled") + "</b>\n\n"
            f"🆔 <b>Schedule:</b> <code>#{int(doc['schedule_id'])}</code>\n"
            f"📌 <b>Channel:</b> <code>{int(doc['channel_id'])}</code> · {_esc(doc.get('channel_title'))}\n"
            f"📅 <b>Date:</b> {st.fmt_date(ts)}\n"
            f"⏰ <b>Time:</b> {st.fmt_time(ts)}"
        )
    markup = InlineKeyboardMarkup([
        [_btn("📋 " + _sc("View Schedules"), f"ls:{int(doc['channel_id'])}:p:0", ButtonStyle.PRIMARY),
         _btn("➕ " + _sc("Add Another"), f"new:{int(doc['channel_id'])}", ButtonStyle.SUCCESS)],
        [_btn("❌ " + _sc("Close"), "x", ButtonStyle.DANGER)],
    ])
    return text, markup


def render_ask(doc: Dict[str, Any], action: str) -> Tuple[str, InlineKeyboardMarkup]:
    sid = int(doc["schedule_id"])
    when = _when(int(doc["scheduled_at"]))
    if action == "delete" and store.is_daily(doc):
        text = (
            "🗑 <b>" + _sc("Delete Daily Schedule") + f"</b> <code>#{sid}</code>?\n\n"
            f"🔁 Every day at {_daily_label(doc)}\n"
            f"💬 {_esc(preview_line(doc.get('content') or {}, 48))}\n\n"
            "The schedule stops for good and is removed. Posts already published "
            "stay in the channel. This can't be undone."
        )
        yes = _btn("✅ " + _sc("Yes, Delete"), f"dy:{sid}", ButtonStyle.DANGER)
    elif action == "delete":
        text = (
            "🗑 <b>" + _sc("Delete Post") + f"</b> <code>#{sid}</code>?\n\n"
            f"📅 {when}\n💬 {_esc(preview_line(doc.get('content') or {}, 48))}\n\n"
            "The schedule is removed for good. This can't be undone."
        )
        yes = _btn("✅ " + _sc("Yes, Delete"), f"dy:{sid}", ButtonStyle.DANGER)
    else:
        text = (
            "🚫 <b>" + _sc("Cancel Schedule") + f"</b> <code>#{sid}</code>?\n\n"
            f"📅 {when}\n💬 {_esc(preview_line(doc.get('content') or {}, 48))}\n\n"
            "The post will not be sent. It stays in 📜 History, where you can "
            "reschedule or delete it."
        )
        yes = _btn("✅ " + _sc("Yes, Cancel It"), f"cy:{sid}", ButtonStyle.DANGER)
    no = _btn("⬅️ " + _sc("No, Go Back"), f"it:{sid}:p:0", ButtonStyle.PRIMARY)
    return text, InlineKeyboardMarkup([[yes], [no]])


async def _edit(query: CallbackQuery, rendered: Tuple[str, InlineKeyboardMarkup]) -> None:
    text, markup = rendered
    with suppress(Exception):
        await query.message.edit_text(text, reply_markup=markup, link_preview_options=_NO_PREVIEW)


async def _reply(message: Message, rendered: Tuple[str, InlineKeyboardMarkup]):
    text, markup = rendered
    return await message.reply_text(text, reply_markup=markup, link_preview_options=_NO_PREVIEW)


# ---------------------------------------------------------------------------
# /Schedule — the hidden command (group -12)
# ---------------------------------------------------------------------------

@app.on_message(filters.command("schedule") & filters.private & SUDOERS, group=-12)
async def schedule_command(_, message: Message):
    """Start the manager. SUDOERS only — anyone else gets no answer."""
    if not _authorized(message.from_user):
        return
    await _start_worker()
    async with _lock(message.from_user.id):
        _new_session(message.from_user.id, step="home")
        await _reply(message, render_home())
    raise StopPropagation


# ---------------------------------------------------------------------------
# Admin input inside a session (group -11)
# ---------------------------------------------------------------------------

async def _in_session(_, __, message) -> bool:
    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    return bool(
        user is not None
        and getattr(chat, "type", None) == ChatType.PRIVATE
        and getattr(user, "id", None) in SESSIONS
    )


_IN_SESSION = filters.create(_in_session, name="ScheduleSessionFilter")


@app.on_message(_IN_SESSION & ~filters.service, group=-11)
async def schedule_input(_, message: Message):
    if await handle_input(message):
        raise StopPropagation


async def handle_input(message: Message) -> bool:
    """Process one message from an admin in a session; True if consumed."""
    user = message.from_user
    session = _session(user.id)
    if session is None:
        return False
    if not _authorized(user):
        _end_session(user.id)
        return False
    text = message.text if isinstance(message.text, str) else None
    if text and text.startswith("/"):
        command = text[1:].split(maxsplit=1)[0].split("@", 1)[0].lower() if len(text) > 1 else ""
        _end_session(user.id)
        if command == "cancel":
            with suppress(Exception):
                await message.reply_text(CLOSED_TEXT + "\n\nNothing was saved.")
            return True
        return False  # another command: it reaches its own handler
    async with _lock(user.id):
        step = session.get("step")
        if step == "home":  # a Channel ID sent right away: manage it
            session.update({"step": "channel", "purpose": "manage", "kind": None})
            await _on_channel_input(message, session)
        elif step == "channel":
            await _on_channel_input(message, session)
        elif step in ("compose", "preview"):
            await _on_compose_input(message, session)
        elif step == "date":
            await _on_typed_date(message, session)
        elif step in ("hour", "minute"):
            await _on_typed_time(message, session)
        else:
            with suppress(Exception):
                await message.reply_text("ℹ️ Use the buttons above, or send /Schedule to start again.")
    return True


async def _on_channel_input(message: Message, session: Dict[str, Any]) -> None:
    channel_id = _parse_channel_id(message.text or message.caption or "")
    if channel_id is None:
        await message.reply_text(
            "❌ <b>Invalid Channel ID.</b>\n\nSend the numeric ID of a channel, for "
            "example <code>-1001234567890</code>.",
            reply_markup=_retry_markup(),
        )
        return
    ok, chat, reason = await _check_channel(channel_id, message.from_user.id)
    if not ok:
        await message.reply_text(reason, reply_markup=_retry_markup())
        return
    session.update({"channel_id": channel_id, "channel_title": _chat_title(chat, channel_id)})
    if session.get("purpose") == "create" and session.get("kind") in TYPE_LABELS:
        _start_compose(session, session["kind"])
        await _reply(message, render_compose(session))
        return
    _reset_wizard(session, "list")
    await _reply(message, await render_list(channel_id, session["channel_title"]))


def _start_compose(session: Dict[str, Any], kind: str) -> None:
    """Begin composing a new schedule of ``kind`` for the session's channel."""
    _reset_wizard(session, "compose")
    session.update({"mode": "new", "kind": kind, "draft_id": uuid.uuid4().hex})


async def _send_preview(chat_id: int, session: Dict[str, Any]):
    """Send the exact post to the admin, then the preview controls."""
    draft = session["draft"]
    try:
        sent, warnings = await send_content(app, chat_id, draft, context={"feature": "schedule", "step": "preview"})
    except Exception as exc:
        log.warning("schedule preview failed for %s: %s: %s", chat_id, type(exc).__name__, exc)
        session["step"] = "compose"
        await app.send_message(
            chat_id,
            "❌ <b>I couldn't build a preview of that post.</b>\n\n"
            f"<code>{_esc(type(exc).__name__)}: {_esc(str(exc)[:300])}</code>\n\n"
            "Send the post again (or a different one).",
            reply_markup=render_compose(session)[1],
        )
        return
    wanted, kept = custom_emoji_delivery(draft, sent)
    text, markup = render_preview_controls(session, kept=kept, wanted=wanted)
    if warnings:
        text += "\n" + "\n".join(f"⚠️ {_esc(w)}" for w in warnings)
    await app.send_message(chat_id, text, reply_markup=markup, link_preview_options=_NO_PREVIEW)


async def _on_compose_input(message: Message, session: Dict[str, Any]) -> None:
    group_id = getattr(message, "media_group_id", None)
    if group_id:
        if group_id not in session["albums"]:
            session["albums"].add(group_id)
            await message.reply_text(
                "❌ <b>Albums can't be scheduled.</b>\n\nSend ONE photo, video, GIF, "
                "document, audio or voice note (with an optional caption), or text."
            )
        return
    try:
        original = extract_content(message, plain_text=False)
    except ContentError as exc:
        await message.reply_text(str(exc))
        return
    session["original"] = original
    session["draft"] = make_plain(original) if session.get("plain_mode") else original
    session["step"] = "preview"
    await _send_preview(message.chat.id, session)


async def _show_hours(message_or_query: Any, session: Dict[str, Any], notice: Optional[str] = None) -> None:
    session["step"] = "hour"
    rendered = render_hours(session, notice)
    if isinstance(message_or_query, CallbackQuery):
        await _edit(message_or_query, rendered)
    else:
        await _reply(message_or_query, rendered)


async def _pick_daily(target: Any, session: Dict[str, Any], hour: int, minute: int) -> None:
    """A daily time was chosen (new schedule or time change): confirmation."""
    is_query = isinstance(target, CallbackQuery)
    skip_key = None
    start = _session_day(session)
    if session.get("mode") == "retime":
        doc = await store.get_schedule(int(session["target"]))
        if doc is None:
            _reset_wizard(session, "list")
            text = "⚠️ That schedule no longer exists."
            if is_query:
                await _answer(query=target, text=text, alert=True)
            else:
                await target.reply_text(text)
            return
        skip_key = doc.get("last_run_key")
        start = store.start_date_of(doc)
        session["start_date"] = start.isoformat()
        session["target_paused"] = doc.get("status") == store.STATUS_PAUSED
    if start is None:
        start = st.ist_today()
    session.update({
        "hour": hour, "daily_time": (hour, minute), "step": "confirm",
        "scheduled_at": st.daily_first_run(start, hour, minute, skip_key=skip_key),
    })
    _snapshot(session)
    rendered = render_confirm(session)
    await (_edit(target, rendered) if is_query else _reply(target, rendered))


async def _pick_time(target: Any, session: Dict[str, Any], hour: int, minute: int) -> None:
    """A complete date + time was chosen: validate and show the confirmation."""
    if _is_daily_wizard(session):
        await _pick_daily(target, session, hour, minute)
        return
    day = _session_day(session)
    ts = st.ist_timestamp(day, hour, minute)
    error = st.time_error(ts)
    is_query = isinstance(target, CallbackQuery)
    if error:
        session["hour"] = hour
        if st.hour_is_open(day, hour):
            session["step"] = "minute"
            rendered = render_minutes(session, f"❌ {error}")
        else:
            session["step"] = "hour"
            rendered = render_hours(session, f"❌ {error}")
        await (_edit(target, rendered) if is_query else _reply(target, rendered))
        return
    session.update({"hour": hour, "scheduled_at": ts, "step": "confirm"})
    _snapshot(session)
    rendered = render_confirm(session)
    await (_edit(target, rendered) if is_query else _reply(target, rendered))


async def _on_typed_date(message: Message, session: Dict[str, Any]) -> None:
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1)
    day = st.parse_date(parts[0]) if parts else None
    if day is None:
        await _reply(message, render_calendar(
            session, "❌ <b>Invalid date.</b> Send it as <code>DD-MM-YYYY</code>, e.g. <code>05-11-2026</code>."))
        return
    error = st.date_error(day)
    if error:
        await _reply(message, render_calendar(session, f"❌ {error}"))
        return
    known = _selected_time(session) if session.get("change_from") == "date" else None
    session.update({"day": day.isoformat(), "cal": (day.year, day.month)})
    if len(parts) == 2:
        parsed = st.parse_time(parts[1])
        if parsed is None:
            session["hour"] = None
            await _show_hours(message, session, "❌ <b>Invalid time.</b> Send it as <code>hh:mm AM/PM</code>.")
            return
        await _pick_time(message, session, *parsed)
        return
    if known is not None:  # 📅 Change Date: keep the time already chosen
        await _pick_time(message, session, *known)
        return
    session["hour"] = None
    await _show_hours(message, session)


async def _on_typed_time(message: Message, session: Dict[str, Any]) -> None:
    parsed = st.parse_time(message.text or "")
    if parsed is None:
        rendered = (render_minutes if session.get("step") == "minute" else render_hours)(
            session, "❌ <b>Invalid time.</b> Send it as <code>hh:mm AM/PM</code>, e.g. <code>10:30 PM</code>.")
        await _reply(message, rendered)
        return
    await _pick_time(message, session, *parsed)


# ---------------------------------------------------------------------------
# Callbacks ("sch:...")
# ---------------------------------------------------------------------------

@app.on_callback_query(filters.regex(rf"^{PREFIX}:"))
async def schedule_callbacks(_, query: CallbackQuery):
    user = query.from_user
    if not _authorized(user):
        with suppress(Exception):
            await query.answer("Not available.", show_alert=True)
        return
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    args = parts[2:]
    async with _lock(user.id):
        try:
            handler = _ACTIONS.get(action)
            if handler is None:
                await query.answer("Unknown option.", show_alert=True)
                return
            await handler(query, args)
        except (ValueError, IndexError):
            with suppress(Exception):
                await query.answer("Invalid option.", show_alert=True)


async def _answer(query: CallbackQuery, text: Optional[str] = None, alert: bool = False) -> None:
    with suppress(Exception):
        if text:
            await query.answer(text, show_alert=alert)
        else:
            await query.answer()


def _wizard(query: CallbackQuery, *steps: str) -> Optional[Dict[str, Any]]:
    session = _session(query.from_user.id)
    if session is None or (steps and session.get("step") not in steps):
        return None
    return session


async def _expired(query: CallbackQuery) -> None:
    await _answer(query, EXPIRED_TEXT, alert=True)


async def _channel_context(query: CallbackQuery, channel_id: int) -> Optional[Dict[str, Any]]:
    """Session bound to ``channel_id`` (created after a restart if needed)."""
    ok, reason = await _can_view(channel_id, query.from_user.id)
    if not ok:
        await _answer(query, "You can't manage this channel.", alert=True)
        await _edit(query, (reason, _retry_markup()))
        return None
    session = _session(query.from_user.id)
    if session is None or session.get("channel_id") != channel_id:
        title = str(channel_id)
        with suppress(Exception):
            title = _chat_title(await app.get_chat(channel_id), channel_id)
        session = _new_session(query.from_user.id, channel_id=channel_id, channel_title=title)
    return session


async def _load_post(query: CallbackQuery, sid: int) -> Optional[Dict[str, Any]]:
    doc = await store.get_schedule(sid)
    if doc is None:
        await _answer(query, "That post no longer exists.", alert=True)
        return None
    if await _channel_context(query, int(doc["channel_id"])) is None:
        return None
    return doc


# — navigation ---------------------------------------------------------------

async def _act_nop(query: CallbackQuery, args: List[str]) -> None:
    await _answer(query)


async def _act_close(query: CallbackQuery, args: List[str]) -> None:
    _end_session(query.from_user.id)
    await _answer(query, "Closed")
    try:
        await query.message.delete()
    except Exception:
        with suppress(Exception):
            await query.message.edit_text(CLOSED_TEXT)


async def _act_retry(query: CallbackQuery, args: List[str]) -> None:
    old = _session(query.from_user.id) or {}
    session = _new_session(query.from_user.id, step="channel",
                           purpose=old.get("purpose") or "manage", kind=old.get("kind"))
    await _answer(query)
    await _edit(query, render_ask_channel(session))


async def _act_home(query: CallbackQuery, args: List[str]) -> None:
    session = _session(query.from_user.id)
    if session is None:
        session = _new_session(query.from_user.id)
    _reset_wizard(session, "home")
    session["purpose"] = "manage"
    await _answer(query)
    await _edit(query, render_home())


async def _act_manage(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "home")
    if session is None:
        return await _expired(query)
    session.update({"step": "channel", "purpose": "manage", "kind": None})
    await _answer(query)
    await _edit(query, render_ask_channel(session))


async def _act_type(query: CallbackQuery, args: List[str]) -> None:
    kind = args[0]
    if kind not in TYPE_LABELS:
        raise ValueError(kind)
    session = _session(query.from_user.id)
    if session is None:
        return await _expired(query)
    if session.get("step") == "home":
        session.update({"step": "channel", "purpose": "create", "kind": kind})
        await _answer(query)
        await _edit(query, render_ask_channel(session))
        return
    if session.get("step") == "types" and session.get("channel_id") is not None:
        if await _channel_context(query, int(session["channel_id"])) is None:
            return
        _start_compose(session, kind)
        await _answer(query)
        await _edit(query, render_compose(session))
        return
    await _expired(query)


async def _act_abort(query: CallbackQuery, args: List[str]) -> None:
    session = _session(query.from_user.id)
    if session is None or session.get("channel_id") is None:
        _end_session(query.from_user.id)
        await _answer(query, "Closed")
        await _edit(query, (CLOSED_TEXT, None))
        return
    _reset_wizard(session, "list")
    await _answer(query, "Cancelled")
    await _edit(query, await render_list(
        session["channel_id"], session["channel_title"],
        notice="❌ <b>Cancelled</b> — nothing was saved."))


async def _act_list(query: CallbackQuery, args: List[str]) -> None:
    channel_id, view, page = int(args[0]), args[1], int(args[2])
    session = await _channel_context(query, channel_id)
    if session is None:
        return
    _reset_wizard(session, "list")
    await _answer(query)
    await _edit(query, await render_list(channel_id, session["channel_title"], view, page))


async def _act_item(query: CallbackQuery, args: List[str]) -> None:
    sid = int(args[0])
    view = args[1] if len(args) > 1 else "p"
    page = int(args[2]) if len(args) > 2 else 0
    doc = await _load_post(query, sid)
    if doc is None:
        return
    await _answer(query)
    await _edit(query, render_item(doc, view, page))


async def _act_preview_post(query: CallbackQuery, args: List[str]) -> None:
    doc = await _load_post(query, int(args[0]))
    if doc is None:
        return
    await _answer(query, "Preview sent below")
    chat_id = query.message.chat.id
    try:
        await send_content(app, chat_id, doc.get("content") or {}, context={"feature": "schedule", "step": "preview"})
    except Exception as exc:
        await app.send_message(
            chat_id, "❌ <b>I couldn't build a preview of that post.</b>\n\n"
            f"<code>{_esc(type(exc).__name__)}: {_esc(str(exc)[:300])}</code>")
        return
    await app.send_message(
        chat_id, f"👁 <b>{_sc('Preview')}</b> ☝️ — post <code>#{int(doc['schedule_id'])}</code> "
        f"({_when(int(doc['scheduled_at']))}).")


# — composing (Add New / Edit) -----------------------------------------------

async def _act_new(query: CallbackQuery, args: List[str]) -> None:
    session = await _channel_context(query, int(args[0]))
    if session is None:
        return
    _reset_wizard(session, "types")
    await _answer(query)
    await _edit(query, render_types(session))


async def _act_edit(query: CallbackQuery, args: List[str]) -> None:
    doc = await _load_post(query, int(args[0]))
    if doc is None:
        return
    if doc.get("status") not in store.EDITABLE_STATUSES:
        await _answer(query, "This post can't be edited any more.", alert=True)
        await _edit(query, render_item(doc))
        return
    session = _session(query.from_user.id)
    _reset_wizard(session, "compose")
    session.update({"mode": "edit", "target": int(doc["schedule_id"])})
    await _answer(query)
    await _edit(query, render_compose(session))


async def _act_plain_mode(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "compose")
    if session is None:
        return await _expired(query)
    session["plain_mode"] = not session.get("plain_mode")
    await _answer(query, "Plain text mode is ON — formatting will be removed."
                  if session["plain_mode"] else "Plain text mode is OFF.")
    await _edit(query, render_compose(session))


async def _act_plain_draft(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "preview")
    if session is None or not session.get("draft"):
        return await _expired(query)
    if session["draft"].get("plain_text") and session.get("original"):
        session["draft"] = session["original"]
        session["plain_mode"] = False
        await _answer(query, "Formatting restored.")
    else:
        session["draft"] = make_plain(session["draft"])
        session["plain_mode"] = True
        await _answer(query, "Plain text mode is active — formatting removed.")
    with suppress(Exception):
        await query.message.edit_reply_markup(None)
    await _send_preview(query.message.chat.id, session)


async def _act_compose_again(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "preview", "confirm")
    if session is None or session.get("mode") not in ("new", "edit"):
        return await _expired(query)
    session["step"] = "compose"
    await _answer(query)
    await _edit(query, render_compose(session))


async def _act_next(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "preview")
    if session is None or not session.get("draft"):
        return await _expired(query)
    if session.get("mode") == "edit":
        await _save_edit(query, session)
        return
    await _answer(query)
    session["step"] = "date"  # 📅 date first (a daily schedule's start date), then 🕒 time
    await _edit(query, render_calendar(session))


async def _save_edit(query: CallbackQuery, session: Dict[str, Any]) -> None:
    sid = int(session["target"])
    ok, _chat, reason = await _check_channel(int(session["channel_id"]), query.from_user.id)
    if not ok:
        _reset_wizard(session, "list")
        await _answer(query, "You can't manage this channel any more.", alert=True)
        await _edit(query, (reason, _retry_markup()))
        return
    draft = session["draft"]
    doc = await store.update_content(sid, draft, content_hash(draft), query.from_user.id)
    _reset_wizard(session, "list")
    if doc is None:
        current = await store.get_schedule(sid)
        await _answer(query, "Not saved — the post is being sent or was already sent.", alert=True)
        if current:
            await _edit(query, render_item(current, notice="⚠️ <b>Not saved</b> — this post is being sent or was already sent."))
        return
    await _answer(query, "Saved")
    await _edit(query, render_item(doc, notice="✅ <b>Changes saved.</b>"))
    log.info("schedule #%s: content edited by %s", sid, query.from_user.id)
    with suppress(Exception):
        await log_schedule_change(doc["channel_id"], doc.get("channel_title"), query.from_user,
                                  "EDITED", schedule_id=sid,
                                  when=(f"every day at {_daily_label(doc)}" if store.is_daily(doc)
                                        else _when(int(doc["scheduled_at"]))),
                                  kind=store.schedule_type(doc))


# — date and time --------------------------------------------------------------

async def _act_calendar(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "date")
    if session is None:
        return await _expired(query)
    value = args[0]
    if len(value) != 6 or not value.isdigit():
        raise ValueError(value)
    year, month = int(value[:4]), int(value[4:6])
    now = st.now_ts()
    first, last = st.ist_today(now), st.last_allowed_day(now)
    if not 1 <= month <= 12 or not (first.year, first.month) <= (year, month) <= (last.year, last.month):
        raise ValueError(value)  # only months that contain selectable days
    session["cal"] = (year, month)
    await _answer(query)
    await _edit(query, render_calendar(session))


async def _act_day(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "date")
    if session is None:
        return await _expired(query)
    value = args[0]
    if len(value) != 8 or not value.isdigit():
        raise ValueError(value)
    day = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    error = st.date_error(day)
    if error:
        await _answer(query, error, alert=True)
        await _edit(query, render_calendar(session, f"❌ {error}"))
        return
    known = _selected_time(session) if session.get("change_from") == "date" else None
    session.update({"day": day.isoformat(), "cal": (day.year, day.month)})
    await _answer(query)
    if known is not None:  # 📅 Change Date: keep the time already chosen
        await _pick_time(query, session, *known)
        return
    session["hour"] = None
    await _show_hours(query, session)


async def _act_hour(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "hour")
    if session is None:
        return await _expired(query)
    hour = int(args[0])
    if not 0 <= hour <= 23:
        raise ValueError(hour)
    if not _is_daily_wizard(session) and not st.hour_is_open(_session_day(session), hour):
        await _answer(query, "That time is in the past.", alert=True)
        await _edit(query, render_hours(session, "❌ That time is in the past."))
        return
    session.update({"hour": hour, "step": "minute"})
    await _answer(query)
    await _edit(query, render_minutes(session))


async def _act_minute(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "minute")
    if session is None:
        return await _expired(query)
    minute = int(args[0])
    if not 0 <= minute <= 59:
        raise ValueError(minute)
    await _answer(query)
    await _pick_time(query, session, int(session["hour"]), minute)


async def _act_change_time(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "confirm")
    if session is None:
        return await _expired(query)
    _snapshot(session)
    session.update({"step": "hour", "change_from": "hour"})  # the date stays as chosen
    await _answer(query)
    await _edit(query, render_hours(session))


async def _act_change_date(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "confirm")
    if session is None or session.get("mode") == "retime":
        return await _expired(query)
    _snapshot(session)
    session.update({"step": "date", "change_from": "date"})  # the time stays as chosen
    await _answer(query)
    await _edit(query, render_calendar(session))


async def _act_back(query: CallbackQuery, args: List[str]) -> None:
    session = _session(query.from_user.id)
    if session is None:
        return await _expired(query)
    step, mode = session.get("step"), session.get("mode")
    await _answer(query)
    if session.get("change_from") and session.get("change_from") == step and _restore_confirm(session):
        await _edit(query, render_confirm(session))
    elif step == "minute":
        session["step"] = "hour"
        await _edit(query, render_hours(session))
    elif step == "hour" and mode == "retime":
        doc = await store.get_schedule(int(session["target"]))
        _reset_wizard(session, "list")
        if doc:
            await _edit(query, render_item(doc))
    elif step == "hour":
        session["step"] = "date"
        await _edit(query, render_calendar(session))
    elif step == "date" and mode == "reschedule":
        doc = await store.get_schedule(int(session["target"]))
        _reset_wizard(session, "list")
        if doc:
            await _edit(query, render_item(doc))
    elif step == "date" and session.get("draft"):
        session["step"] = "preview"
        await _edit(query, render_preview_controls(session))
    elif step in ("compose", "preview") and mode == "edit":
        doc = await store.get_schedule(int(session["target"]))
        _reset_wizard(session, "list")
        if doc:
            await _edit(query, render_item(doc))
    elif step == "compose" and mode == "new" and session.get("channel_id") is not None:
        _reset_wizard(session, "types")
        await _edit(query, render_types(session))
    elif step in ("channel", "home"):
        _reset_wizard(session, "home")
        await _edit(query, render_home())
    elif session.get("channel_id") is not None:
        _reset_wizard(session, "list")
        await _edit(query, await render_list(session["channel_id"], session["channel_title"]))
    else:
        _reset_wizard(session, "home")
        await _edit(query, render_home())


# — confirmation ---------------------------------------------------------------

async def _act_confirm(query: CallbackQuery, args: List[str]) -> None:
    session = _wizard(query, "confirm")
    if session is None:
        if (_session(query.from_user.id) or {}).get("done"):
            return await _answer(query, "Already scheduled.", alert=True)
        return await _expired(query)
    channel_id = int(session["channel_id"])
    ok, chat, reason = await _check_channel(channel_id, query.from_user.id)
    if not ok:
        _reset_wizard(session, "list")
        await _answer(query, "You can't manage this channel any more.", alert=True)
        await _edit(query, (reason, _retry_markup()))
        return
    session["channel_title"] = _chat_title(chat, channel_id)
    if session.get("mode") == "retime":
        await _confirm_retime(query, session)
        return
    if session.get("mode") == "new" and session.get("kind") == store.TYPE_DAILY:
        await _confirm_daily(query, session, channel_id)
        return
    ts = int(session["scheduled_at"])
    error = st.time_error(ts)
    if error:
        day = _session_day(session)
        session.update({"hour": None, "scheduled_at": None, "change_from": None, "confirmed": None})
        await _answer(query, error, alert=True)
        if day is None or st.date_error(day):
            session["step"] = "date"
            await _edit(query, render_calendar(session, f"❌ {error} Pick a new date and time."))
        else:
            session["step"] = "hour"
            await _edit(query, render_hours(session, f"❌ {error} Pick a new time."))
        return
    if session.get("mode") == "reschedule":
        await _confirm_reschedule(query, session, ts)
        return
    draft = session["draft"]
    digest = content_hash(draft)
    twin = await store.find_identical(channel_id, digest, ts)
    if twin is not None:
        await _answer(query, "An identical post is already scheduled for that time.", alert=True)
        await _edit(query, render_confirm(
            session, f"⚠️ <b>Not scheduled</b> — this exact post is already scheduled for "
                     f"{_when(ts)} (<code>#{int(twin['schedule_id'])}</code>). Change the time "
                     "or cancel."))
        return
    try:
        doc = await store.create_schedule(
            channel_id=channel_id, channel_title=session["channel_title"],
            creator_id=query.from_user.id, content=draft, digest=digest,
            scheduled_at=ts, draft_id=session["draft_id"],
        )
    except store.DuplicateDraft as exc:
        session["done"] = True
        await _answer(query, "Already scheduled.", alert=True)
        if exc.existing:
            await _edit(query, render_scheduled(exc.existing))
        return
    _reset_wizard(session, "list")
    session["done"] = True
    await _answer(query, "Scheduled!")
    await _edit(query, render_scheduled(doc))
    wake_worker()
    log.info("schedule #%s: one-time post for %s at %s created by %s (protection %s)",
             doc["schedule_id"], channel_id, _when(ts), query.from_user.id,
             "ON" if is_protected(draft) else "OFF")
    with suppress(Exception):
        await log_schedule_change(channel_id, session["channel_title"], query.from_user, "CREATED",
                                  schedule_id=int(doc["schedule_id"]), when=_when(ts),
                                  kind=store.TYPE_ONCE, protected=is_protected(draft))


async def _confirm_daily(query: CallbackQuery, session: Dict[str, Any], channel_id: int) -> None:
    hour, minute = session["daily_time"]
    start = _session_day(session) or st.ist_today()
    # Recomputed now: the admin may have waited on this screen. If the first
    # post moved, show it and ask again — never move it silently.
    first = st.daily_first_run(start, hour, minute)
    if first != int(session["scheduled_at"]):
        session["scheduled_at"] = first
        _snapshot(session)
        await _answer(query, "The first post time changed — please check it and confirm again.", alert=True)
        await _edit(query, render_confirm(
            session, f"⚠️ <b>Please check again</b> — while this screen was open, the first post "
                     f"moved to {st.fmt_datetime(first)}."))
        return
    draft = session["draft"]
    digest = content_hash(draft)
    twin = await store.find_identical_daily(channel_id, digest, hour, minute)
    if twin is not None:
        await _answer(query, "An identical daily schedule already exists.", alert=True)
        await _edit(query, render_confirm(
            session, f"⚠️ <b>Not scheduled</b> — this exact post is already scheduled every day "
                     f"at {_daily_label((hour, minute))} (<code>#{int(twin['schedule_id'])}</code>). "
                     "Change the time or cancel."))
        return
    try:
        doc = await store.create_schedule(
            channel_id=channel_id, channel_title=session["channel_title"],
            creator_id=query.from_user.id, content=draft, digest=digest,
            scheduled_at=first, draft_id=session["draft_id"],
            kind=store.TYPE_DAILY, daily_time=(hour, minute), start_date=start.isoformat(),
        )
    except store.DuplicateDraft as exc:
        session["done"] = True
        await _answer(query, "Already scheduled.", alert=True)
        if exc.existing:
            await _edit(query, render_scheduled(exc.existing))
        return
    _reset_wizard(session, "list")
    session["done"] = True
    await _answer(query, "Scheduled!")
    await _edit(query, render_scheduled(doc))
    wake_worker()
    log.info("schedule #%s: daily schedule for %s every day at %s created by %s (first post %s, protection %s)",
             doc["schedule_id"], channel_id, _daily_label((hour, minute)), query.from_user.id, _when(first),
             "ON" if is_protected(draft) else "OFF")
    with suppress(Exception):
        await log_schedule_change(channel_id, session["channel_title"], query.from_user, "CREATED",
                                  schedule_id=int(doc["schedule_id"]),
                                  when=f"every day at {_daily_label((hour, minute))}",
                                  details=f"first post {_when(first)}", kind=store.TYPE_DAILY,
                                  protected=is_protected(draft))


async def _confirm_retime(query: CallbackQuery, session: Dict[str, Any]) -> None:
    sid = int(session["target"])
    hour, minute = session["daily_time"]
    doc = await store.get_schedule(sid)
    if doc is None:
        _reset_wizard(session, "list")
        await _answer(query, "That schedule no longer exists.", alert=True)
        return
    skip_key = doc.get("last_run_key")
    first = st.daily_first_run(store.start_date_of(doc), hour, minute, skip_key=skip_key)
    updated = await store.change_daily_time(sid, hour, minute, first, query.from_user.id, skip_key)
    _reset_wizard(session, "list")
    if updated is None:
        await _answer(query, "Not changed — the schedule is being sent right now. Try again in a moment.",
                      alert=True)
        current = await store.get_schedule(sid)
        if current:
            await _edit(query, render_item(current))
        return
    label = _daily_label((hour, minute))
    if updated.get("status") == store.STATUS_PAUSED:
        notice = f"🕒 <b>Time changed</b> — every day at {label}. It restarts at that time when you resume it."
    else:
        notice = f"🕒 <b>Time changed</b> — every day at {label}; next post {_when(int(updated['scheduled_at']))}."
        wake_worker()
    await _answer(query, "Time changed")
    await _edit(query, render_item(updated, notice=notice))
    log.info("schedule #%s: daily time changed to %s by %s", sid, label, query.from_user.id)
    with suppress(Exception):
        await log_schedule_change(updated["channel_id"], updated.get("channel_title"), query.from_user,
                                  "TIME_CHANGED", schedule_id=sid, when=f"every day at {label}",
                                  kind=store.TYPE_DAILY)


async def _confirm_reschedule(query: CallbackQuery, session: Dict[str, Any], ts: int) -> None:
    sid = int(session["target"])
    doc = await store.reschedule(sid, ts, query.from_user.id)
    _reset_wizard(session, "list")
    if doc is None:
        current = await store.get_schedule(sid)
        await _answer(query, "Not rescheduled — the post is being sent or was already sent.", alert=True)
        if current:
            await _edit(query, render_item(current, notice="⚠️ <b>Not rescheduled</b> — this post is being sent or was already sent."))
        return
    await _answer(query, "Rescheduled")
    await _edit(query, render_item(doc, notice=f"✅ <b>Rescheduled</b> to {_when(ts)}."))
    wake_worker()
    log.info("schedule #%s: rescheduled to %s by %s", sid, _when(ts), query.from_user.id)
    with suppress(Exception):
        await log_schedule_change(doc["channel_id"], doc.get("channel_title"), query.from_user,
                                  "RESCHEDULED", schedule_id=sid, when=_when(ts), kind=store.TYPE_ONCE)


# — reschedule / cancel / delete -----------------------------------------------

async def _act_reschedule(query: CallbackQuery, args: List[str]) -> None:
    doc = await _load_post(query, int(args[0]))
    if doc is None:
        return
    if store.is_daily(doc):
        if doc.get("status") not in store.RETIMABLE_STATUSES:
            await _answer(query, "The schedule is being sent right now. Try again in a moment.", alert=True)
            await _edit(query, render_item(doc))
            return
        session = _session(query.from_user.id)
        _reset_wizard(session, "hour")
        session.update({
            "mode": "retime", "target": int(doc["schedule_id"]), "kind": store.TYPE_DAILY,
            "current_daily": list(store.daily_time_of(doc)),
            "start_date": store.start_date_of(doc).isoformat(),
            "target_paused": doc.get("status") == store.STATUS_PAUSED,
        })
        await _answer(query)
        await _edit(query, render_hours(session))
        return
    if doc.get("status") not in store.RESCHEDULABLE_STATUSES:
        await _answer(query, "This post can't be rescheduled any more.", alert=True)
        await _edit(query, render_item(doc))
        return
    session = _session(query.from_user.id)
    _reset_wizard(session, "date")
    session.update({"mode": "reschedule", "target": int(doc["schedule_id"]),
                    "current_at": int(doc["scheduled_at"])})
    await _answer(query)
    await _edit(query, render_calendar(session))


async def _act_ask(query: CallbackQuery, args: List[str], action: str) -> None:
    doc = await _load_post(query, int(args[0]))
    if doc is None:
        return
    allowed = store.DELETABLE_STATUSES if action == "delete" else store.CANCELLABLE_STATUSES
    if doc.get("status") not in allowed or (action == "cancel" and store.is_daily(doc)):
        await _answer(query, "Not possible for this post right now.", alert=True)
        await _edit(query, render_item(doc))
        return
    await _answer(query)
    await _edit(query, render_ask(doc, action))


async def _act_cancel_ask(query: CallbackQuery, args: List[str]) -> None:
    await _act_ask(query, args, "cancel")


async def _act_delete_ask(query: CallbackQuery, args: List[str]) -> None:
    await _act_ask(query, args, "delete")


async def _destructive(query: CallbackQuery, args: List[str], action: str) -> None:
    doc = await _load_post(query, int(args[0]))
    if doc is None:
        return
    sid, channel_id = int(doc["schedule_id"]), int(doc["channel_id"])
    ok, _chat, reason = await _check_channel(channel_id, query.from_user.id)
    if not ok:
        await _answer(query, "You can't manage this channel any more.", alert=True)
        await _edit(query, (reason, _retry_markup()))
        return
    session = _session(query.from_user.id)
    when = _when(int(doc["scheduled_at"]))
    if action == "delete":
        done = await store.delete_schedule(sid)
        if not done:
            await _answer(query, "Not deleted — the post is being sent right now.", alert=True)
            current = await store.get_schedule(sid)
            if current:
                await _edit(query, render_item(current))
            return
        await _answer(query, "Deleted")
        await _edit(query, await render_list(channel_id, session["channel_title"],
                                             notice=f"🗑 <b>Post <code>#{sid}</code> deleted.</b>"))
        log_action = "DELETED"
    else:
        updated = await store.cancel_schedule(sid, query.from_user.id)
        if updated is None:
            await _answer(query, "Not cancelled — the post is being sent or was already sent.", alert=True)
            current = await store.get_schedule(sid)
            if current:
                await _edit(query, render_item(current))
            return
        await _answer(query, "Cancelled")
        await _edit(query, render_item(updated, notice="🚫 <b>Schedule cancelled</b> — the post will not be sent."))
        log_action = "CANCELLED"
    log.info("schedule #%s: %s by %s", sid, log_action.lower(), query.from_user.id)
    with suppress(Exception):
        await log_schedule_change(channel_id, doc.get("channel_title"), query.from_user,
                                  log_action, schedule_id=sid, when=when,
                                  kind=store.schedule_type(doc))


async def _act_cancel_yes(query: CallbackQuery, args: List[str]) -> None:
    await _destructive(query, args, "cancel")


async def _act_delete_yes(query: CallbackQuery, args: List[str]) -> None:
    await _destructive(query, args, "delete")


async def _act_pause(query: CallbackQuery, args: List[str]) -> None:
    doc = await _load_post(query, int(args[0]))
    if doc is None:
        return
    if not store.is_daily(doc) or doc.get("status") not in store.PAUSABLE_STATUSES:
        busy = doc.get("status") == store.STATUS_SENDING
        await _answer(query, "The schedule is being sent right now. Try again in a moment." if busy
                      else "Only an active daily schedule can be paused.", alert=True)
        await _edit(query, render_item(doc))
        return
    sid, channel_id = int(doc["schedule_id"]), int(doc["channel_id"])
    ok, _chat, reason = await _check_channel(channel_id, query.from_user.id)
    if not ok:
        await _answer(query, "You can't manage this channel any more.", alert=True)
        await _edit(query, (reason, _retry_markup()))
        return
    updated = await store.pause_schedule(sid, query.from_user.id)
    if updated is None:
        await _answer(query, "Not paused — the schedule is being sent right now. Try again in a moment.",
                      alert=True)
        current = await store.get_schedule(sid)
        if current:
            await _edit(query, render_item(current))
        return
    await _answer(query, "Paused")
    await _edit(query, render_item(updated, notice="⏸ <b>Paused</b> — nothing is posted until you resume it."))
    log.info("schedule #%s: daily schedule paused by %s", sid, query.from_user.id)
    with suppress(Exception):
        await log_schedule_change(channel_id, doc.get("channel_title"), query.from_user, "PAUSED",
                                  schedule_id=sid, when=f"every day at {_daily_label(doc)}",
                                  kind=store.TYPE_DAILY)


async def _act_resume(query: CallbackQuery, args: List[str]) -> None:
    doc = await _load_post(query, int(args[0]))
    if doc is None:
        return
    if not store.is_daily(doc) or doc.get("status") != store.STATUS_PAUSED:
        await _answer(query, "This schedule is not paused.", alert=True)
        await _edit(query, render_item(doc))
        return
    sid, channel_id = int(doc["schedule_id"]), int(doc["channel_id"])
    ok, _chat, reason = await _check_channel(channel_id, query.from_user.id)
    if not ok:
        await _answer(query, "You can't manage this channel any more.", alert=True)
        await _edit(query, (reason, _retry_markup()))
        return
    hour, minute = store.daily_time_of(doc)
    skip_key = doc.get("last_run_key")
    first = st.daily_first_run(store.start_date_of(doc), hour, minute, skip_key=skip_key)
    updated = await store.resume_schedule(sid, first, query.from_user.id, skip_key)
    if updated is None:
        await _answer(query, "Not resumed — the schedule changed meanwhile. Please try again.", alert=True)
        current = await store.get_schedule(sid)
        if current:
            await _edit(query, render_item(current))
        return
    await _answer(query, "Resumed")
    await _edit(query, render_item(updated, notice=f"▶️ <b>Resumed</b> — next post {_when(first)}."))
    wake_worker()
    log.info("schedule #%s: daily schedule resumed by %s; next post %s", sid, query.from_user.id, _when(first))
    with suppress(Exception):
        await log_schedule_change(channel_id, doc.get("channel_title"), query.from_user, "RESUMED",
                                  schedule_id=sid, when=f"every day at {_daily_label(doc)}",
                                  details=f"next post {_when(first)}", kind=store.TYPE_DAILY)


_ACTIONS = {
    "nop": _act_nop, "x": _act_close, "retry": _act_retry, "abort": _act_abort,
    "hm": _act_home, "mg": _act_manage, "ty": _act_type, "ps": _act_pause, "rm": _act_resume,
    "ls": _act_list, "it": _act_item, "pv": _act_preview_post,
    "new": _act_new, "ed": _act_edit, "pm": _act_plain_mode, "pt": _act_plain_draft,
    "cm": _act_compose_again, "nx": _act_next,
    "cal": _act_calendar, "day": _act_day, "hr": _act_hour, "mn": _act_minute,
    "ct": _act_change_time, "cd": _act_change_date, "bk": _act_back, "ok": _act_confirm,
    "rs": _act_reschedule, "ca": _act_cancel_ask, "cy": _act_cancel_yes,
    "da": _act_delete_ask, "dy": _act_delete_yes,
}


# ---------------------------------------------------------------------------
# Delivery worker
# ---------------------------------------------------------------------------

def _post_link(channel_id: int, username: Optional[str], message_id: Optional[int]) -> Optional[str]:
    if not message_id:
        return None
    if username:
        return f"https://t.me/{username}/{int(message_id)}"
    raw = str(int(channel_id))
    return f"https://t.me/c/{raw[4:]}/{int(message_id)}" if raw.startswith("-100") else None


async def _notify(doc: Dict[str, Any], text: str) -> None:
    """Tell the post's creator what happened (best effort)."""
    creator = doc.get("creator_id")
    if not creator:
        return
    with suppress(Exception):
        await app.send_message(int(creator), text, link_preview_options=_NO_PREVIEW)


def _summary(doc: Dict[str, Any], occurrence: Optional[int] = None) -> str:
    when = int(occurrence if occurrence is not None else doc["scheduled_at"])
    lines = [
        f"🆔 <b>Schedule:</b> <code>#{int(doc['schedule_id'])}</code>",
        f"📌 <b>Channel:</b> {_esc(doc.get('channel_title') or doc['channel_id'])}",
    ]
    if store.is_daily(doc):
        lines.append(f"🔁 <b>Repeats:</b> Every day at {_daily_label(doc)}")
    lines += [f"📅 <b>Date:</b> {st.fmt_date(when)}", f"⏰ <b>Time:</b> {st.fmt_time(when)}"]
    return "\n".join(lines)


async def _report_failure(doc: Dict[str, Any], code: str, message: str, uncertain: bool, *,
                          occurrence: Optional[int] = None, next_run: Optional[int] = None) -> None:
    if store.is_daily(doc):
        title = "🔴 <b>" + _sc("Daily Post Failed") + "</b>"
        advice = ("Check the channel — Telegram may have published it. " if uncertain else "")
        advice += ("It is not retried (that could post it twice). The daily schedule stays "
                   f"active — next post: {_when(next_run)}." if next_run else
                   "The daily schedule stays active.")
    else:
        title = "🔴 <b>" + _sc("Scheduled Post Failed") + "</b>"
        advice = ("Check the channel before rescheduling — the post may already be there."
                  if uncertain else "Open /Schedule to edit or reschedule it.")
    await _notify(doc, f"{title}\n\n{_summary(doc, occurrence)}"
                  f"\n\n⚠️ <code>{_esc(code)}</code> — {_esc(message)}\n\n{advice}")
    with suppress(Exception):
        await log_schedule_change(doc["channel_id"], doc.get("channel_title"), None, "FAILED",
                                  schedule_id=int(doc["schedule_id"]),
                                  when=_when(int(occurrence if occurrence is not None else doc["scheduled_at"])),
                                  details=f"{code}: {message}", kind=store.schedule_type(doc))


# Telegram error ids that may come from a post's entities / custom emoji
# (unlike e.g. CHAT_WRITE_FORBIDDEN, which is about the bot's rights).
_EMOJI_ERROR_KEYS = ("ENTIT", "EMOJI", "DOCUMENT", "PREMIUM")


async def _attempt_send(doc: Dict[str, Any]) -> Dict[str, Any]:
    """One delivery attempt of a claimed post.

    Returns {"message_id", "sent", "warnings"} once Telegram confirmed the
    publication, {"flood": seconds} after a FloodWait, or {"code",
    "message", "uncertain"} for a failure (uncertain = Telegram's answer is
    unknown, so it must not be retried automatically).
    """
    sid, channel_id = int(doc["schedule_id"]), int(doc["channel_id"])
    content = doc.get("content") or {}
    try:
        sent, warnings = await asyncio.wait_for(
            send_content(app, channel_id, content, context={"feature": "schedule", "schedule_id": sid}),
            timeout=SEND_TIMEOUT,
        )
    except FloodWait as exc:
        return {"flood": min(max(int(getattr(exc, "value", 0) or 0), 1), 3600)}
    except asyncio.TimeoutError:
        return {"code": "TIMEOUT", "uncertain": True,
                "message": "Telegram did not answer in time, so it is unknown whether the post was published."}
    except ContentError as exc:
        return {"code": "INVALID_CONTENT", "uncertain": False, "message": str(exc)}
    except (InternalServerError, ServiceUnavailable) as exc:
        return {"code": str(getattr(exc, "ID", None) or type(exc).__name__), "uncertain": True,
                "message": f"Telegram server error ({getattr(exc, 'MESSAGE', '') or exc}); the outcome is unknown."}
    except RPCError as exc:
        code = str(getattr(exc, "ID", None) or getattr(exc, "NAME", None) or type(exc).__name__)
        message = str(getattr(exc, "MESSAGE", None) or exc)
        if any(key in code for key in _EMOJI_ERROR_KEYS) and entity_counts(content)[1]:
            message += (" (The post contains Premium custom emoji: in channels Telegram only "
                        "allows them from bots that own a Fragment username — try 📋 Plain "
                        "Text or remove them.)")
        return {"code": code, "message": message, "uncertain": False}
    except Exception as exc:  # network trouble or a bug: the outcome is unknown
        return {"code": type(exc).__name__, "uncertain": True,
                "message": f"{str(exc)[:300] or 'unexpected error'} — the outcome is unknown."}
    wanted, kept = custom_emoji_delivery(content, sent)
    if kept < wanted:
        warnings.append(
            f"Telegram replaced {wanted - kept} of {wanted} custom emoji with standard "
            "emoji: in channels, bots can only post custom emoji if they own a Fragment username."
        )
    message_id = getattr(sent, "id", None)
    return {"sent": sent, "warnings": warnings,
            "message_id": message_id if isinstance(message_id, int) else None}


async def _defer(doc: Dict[str, Any], token: str, wait: int) -> str:
    current = st.now_ts()
    await store.defer(doc, token, retry_at=current + wait, now=current, reason=f"FLOOD_WAIT_{wait}")
    log.warning("schedule #%s: FloodWait %ss — retrying the same occurrence at %s",
                doc.get("schedule_id"), wait, _when(current + wait))
    return store.STATUS_SCHEDULED


async def deliver(doc: Dict[str, Any], token: str, now: int) -> str:
    """Send one claimed post and record the outcome. Returns the new status."""
    if store.is_daily(doc):
        return await _deliver_daily(doc, token, now)
    return await _deliver_once(doc, token, now)


async def _deliver_once(doc: Dict[str, Any], token: str, now: int) -> str:
    sid, channel_id = int(doc["schedule_id"]), int(doc["channel_id"])
    late = int(now) - int(doc["scheduled_at"])
    # Only the FIRST attempt can be "missed": a FloodWait retry is late on
    # purpose and must still go out.
    first_attempt = int(doc.get("attempts") or 0) <= 1
    if first_attempt and late > missed_grace_seconds() and missed_policy() == "skip":
        await store.mark_missed(doc, token, now=now)
        log.warning("schedule #%s: missed (found %s min late) — not sent (SCHEDULE_MISSED_POLICY=skip)",
                    sid, late // 60)
        await _notify(doc, "⌛ <b>" + _sc("Scheduled Post Missed") + "</b>\n\n" + _summary(doc)
                      + f"\n\nThe bot was offline at that time (found {late // 60} min late), "
                      "so — as configured — the post was not sent. Open /Schedule to reschedule it.")
        with suppress(Exception):
            await log_schedule_change(channel_id, doc.get("channel_title"), None, "MISSED",
                                      schedule_id=sid, when=_when(int(doc["scheduled_at"])),
                                      kind=store.TYPE_ONCE)
        return store.STATUS_MISSED
    if int(doc.get("attempts") or 0) > MAX_ATTEMPTS:
        message = "Telegram kept rate-limiting the bot; the post was not sent."
        await store.mark_failed(doc, token, code="TOO_MANY_RETRIES", message=message,
                                now=now, uncertain=False)
        log.warning("schedule #%s failed: TOO_MANY_RETRIES", sid)
        await _report_failure(doc, "TOO_MANY_RETRIES", message, False)
        return store.STATUS_FAILED

    outcome = await _attempt_send(doc)
    if "flood" in outcome:
        return await _defer(doc, token, outcome["flood"])
    if "sent" in outcome:
        message_id, warnings = outcome["message_id"], outcome["warnings"]
        await store.mark_sent(doc, token, message_id=message_id, now=st.now_ts(), warnings=warnings)
        log.info("schedule #%s published to %s (message %s)", sid, channel_id, message_id)
        link = _post_link(channel_id, getattr(getattr(outcome["sent"], "chat", None), "username", None),
                          message_id)
        note = (f"\n\n⏱ Sent {late // 60} min late — the bot was offline at the scheduled time."
                if late >= missed_grace_seconds() else "")
        note += "".join(f"\n⚠️ {_esc(w)}" for w in warnings)
        await _notify(doc, "✅ <b>" + _sc("Scheduled Post Sent") + "</b>\n\n" + _summary(doc)
                      + (f"\n🔗 {link}" if link and isinstance(link, str) else "") + note)
        with suppress(Exception):
            await log_schedule_change(channel_id, doc.get("channel_title"), None, "SENT",
                                      schedule_id=sid, when=_when(int(doc["scheduled_at"])),
                                      details=f"message id {message_id}" if message_id else None,
                                      kind=store.TYPE_ONCE,
                                      protected=is_protected(doc.get("content") or {}))
        return store.STATUS_SENT

    code, message, uncertain = outcome["code"], outcome["message"], outcome["uncertain"]
    await store.mark_failed(doc, token, code=code, message=message, now=st.now_ts(), uncertain=uncertain)
    log.warning("schedule #%s failed: %s: %s (uncertain=%s)", sid, code, message, uncertain)
    await _report_failure(doc, code, message, uncertain)
    return store.STATUS_FAILED


async def _deliver_daily(doc: Dict[str, Any], token: str, now: int) -> str:
    """One occurrence of a daily schedule. Whatever happens, the schedule
    stays active and moves on to its next occurrence (the following IST day);
    an occurrence is never published twice."""
    sid, channel_id = int(doc["schedule_id"]), int(doc["channel_id"])
    occurrence = int(doc["scheduled_at"])
    key = st.run_key(occurrence)

    def following() -> int:
        return store.next_occurrence(doc, max(st.now_ts(), occurrence), skip_key=key)

    if doc.get("last_run_key") == key:
        # Already published for that IST day — never post it twice.
        nxt = following()
        await store.advance_daily(doc, token, next_run=nxt, now=now)
        log.warning("daily schedule #%s: %s was already published — moved to %s", sid, key, _when(nxt))
        return store.STATUS_SCHEDULED

    late = int(now) - occurrence
    attempts = int(doc.get("attempts") or 0)
    if attempts <= 1 and late > missed_grace_seconds():
        # The bot was offline at that time: skip it (no backlog) and go on.
        nxt = following()
        await store.advance_daily(doc, token, next_run=nxt, now=now,
                                  skip={"occurrence": occurrence, "at": int(now), "late_by": late})
        log.warning("daily schedule #%s: missed %s (found %s min late) — skipped; next post %s",
                    sid, _when(occurrence), late // 60, _when(nxt))
        await _notify(doc, "⌛ <b>" + _sc("Daily Post Skipped") + "</b>\n\n" + _summary(doc, occurrence)
                      + f"\n\nThe bot was offline at that time (found {late // 60} min late), so this "
                      f"day's post was skipped. The schedule continues — next post: {_when(nxt)}.")
        with suppress(Exception):
            await log_schedule_change(channel_id, doc.get("channel_title"), None, "SKIPPED",
                                      schedule_id=sid, when=_when(occurrence),
                                      details=f"bot offline; next post {_when(nxt)}", kind=store.TYPE_DAILY)
        return store.STATUS_SCHEDULED
    if attempts > MAX_ATTEMPTS:
        message = "Telegram kept rate-limiting the bot; this day's post was not sent."
        nxt = following()
        await store.advance_daily(doc, token, next_run=nxt, now=now, error={
            "code": "TOO_MANY_RETRIES", "message": message, "at": int(now), "uncertain": False})
        log.warning("daily schedule #%s: %s failed: TOO_MANY_RETRIES; next post %s", sid, key, _when(nxt))
        await _report_failure(doc, "TOO_MANY_RETRIES", message, False, occurrence=occurrence, next_run=nxt)
        return store.STATUS_SCHEDULED

    outcome = await _attempt_send(doc)
    if "flood" in outcome:
        return await _defer(doc, token, outcome["flood"])
    if "sent" in outcome:
        message_id = outcome["message_id"]
        nxt = following()
        await store.mark_daily_sent(doc, token, message_id=message_id, now=st.now_ts(),
                                    next_run=nxt, run_key=key, warnings=outcome["warnings"])
        log.info("daily schedule #%s published %s to %s (message %s); next post %s",
                 sid, key, channel_id, message_id, _when(nxt))
        with suppress(Exception):
            await log_schedule_change(channel_id, doc.get("channel_title"), None, "SENT",
                                      schedule_id=sid, when=_when(occurrence),
                                      details=(f"message id {message_id}; " if message_id else "")
                                      + f"next post {_when(nxt)}", kind=store.TYPE_DAILY,
                                      protected=is_protected(doc.get("content") or {}))
        return store.STATUS_SCHEDULED

    code, message, uncertain = outcome["code"], outcome["message"], outcome["uncertain"]
    nxt = following()
    await store.advance_daily(doc, token, next_run=nxt, now=st.now_ts(), error={
        "code": code, "message": message[:500], "at": st.now_ts(), "uncertain": bool(uncertain)})
    log.warning("daily schedule #%s: %s failed: %s: %s (uncertain=%s); next post %s",
                sid, key, code, message, uncertain, _when(nxt))
    await _report_failure(doc, code, message, uncertain, occurrence=occurrence, next_run=nxt)
    return store.STATUS_SCHEDULED


async def process_due(now: Optional[int] = None) -> int:
    """Claim and deliver the posts due at ``now`` (up to BATCH); returns how many."""
    now = st.now_ts() if now is None else int(now)
    handled = 0
    for _ in range(BATCH):
        token = store.new_claim_token()
        doc = await store.claim_next_due(now, token)
        if doc is None:
            break
        handled += 1
        try:
            await deliver(doc, token, now)
        except Exception as exc:  # never let one post stop the worker
            log.exception("schedule #%s delivery crashed: %s", doc.get("schedule_id"), exc)
    return handled


async def recover_interrupted(now: Optional[int] = None) -> int:
    """Settle posts left in "sending" by a stopped bot (never re-sent)."""
    now = st.now_ts() if now is None else int(now)
    recovered = await store.recover_stale_sending(now, STALE_SENDING_AFTER)
    for doc in recovered:
        error = doc.get("error") or {}
        log.warning("schedule #%s: interrupted while sending — %s", doc.get("schedule_id"),
                    "moved to the next day" if store.is_daily(doc) else "marked failed")
        await _report_failure(doc, error.get("code", "INTERRUPTED"), error.get("message", ""), True,
                              occurrence=error.get("occurrence"),
                              next_run=int(doc["scheduled_at"]) if store.is_daily(doc) else None)
    return len(recovered)


# The worker sleeps until the next post is due; admins' changes wake it up.
_wake_holder: "Tuple[asyncio.AbstractEventLoop, asyncio.Event] | None" = None


def _wake_event() -> asyncio.Event:
    global _wake_holder
    loop = asyncio.get_running_loop()
    if _wake_holder is None or _wake_holder[0] is not loop:
        _wake_holder = (loop, asyncio.Event())
    return _wake_holder[1]


def wake_worker() -> None:
    """A schedule was created or changed: re-check when to wake up."""
    with suppress(RuntimeError):
        _wake_event().set()


def _next_wait(now: int, next_due: Optional[int], handled: int) -> float:
    """Seconds to sleep: until the next due post, at most MAX_IDLE_WAIT."""
    if handled >= BATCH:
        return 0  # more posts are due right now
    wait = float(MAX_IDLE_WAIT)
    if next_due is not None:
        wait = min(wait, max(float(next_due - now), 0.5))
    return wait


async def _worker_loop() -> None:
    with suppress(Exception):
        await store.ensure_schedule_indexes()
    log.info("scheduled-post worker started (schedules are restored from MongoDB)")
    event = _wake_event()
    last_recovery = 0
    while True:
        event.clear()
        try:
            now = st.now_ts()
            if now - last_recovery >= RECOVERY_INTERVAL:
                last_recovery = now
                await recover_interrupted(now)
            handled = await process_due(now)
            wait = _next_wait(st.now_ts(), await store.next_due_at(), handled)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("schedule worker error: %s: %s", type(exc).__name__, exc)
            wait = ERROR_BACKOFF
        if wait > 0:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=wait)


_worker_task: "asyncio.Task | None" = None


async def _start_worker() -> None:
    """Start the one worker of this process (idempotent)."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker_loop())


# Plugins are imported from the running event loop by __main__.py — start
# the persistent worker right away (same bootstrap as autodelete.py).
try:
    asyncio.get_running_loop().create_task(_start_worker())
except RuntimeError:
    pass
