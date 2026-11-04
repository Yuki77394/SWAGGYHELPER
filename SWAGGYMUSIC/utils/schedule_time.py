"""Indian Standard Time helpers for the /Schedule scheduled-post manager.

Every date and time the admin sees or types is Indian Standard Time
(Asia/Kolkata, UTC+05:30). Everything stored is a UTC epoch timestamp
(int seconds, the same convention as autodelete's ``delete_at``), so the
server's local timezone is never consulted.

IST has been a fixed UTC+05:30 offset with no daylight-saving time since
1945, so it is modelled as ``datetime.timezone(timedelta(hours=5,
minutes=30))``. That is exact for every date the scheduler accepts and,
unlike ``zoneinfo``, needs no tz database on the host (slim Docker images
often ship without one).

Display formats (one source of truth for the whole UI):
    date  ``DD-MM-YYYY``        e.g. 05-11-2026
    time  ``hh:mm AM/PM IST``   e.g. 10:30 PM IST

Daily schedules repeat at the same IST wall-clock time every day
(``next_daily_run``); ``run_key`` names the IST day of an occurrence so a
schedule publishes at most once per day.
"""

import calendar
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30), "IST")
TIMEZONE_NAME = "Asia/Kolkata"

# A new or rescheduled post must be at least this far in the future.
MIN_LEAD_SECONDS = 60
# ...and at most this far (the calendar shows the same window).
MAX_AHEAD_DAYS = 366


def now_ts() -> int:
    """Wall-clock UTC epoch seconds (patchable in tests)."""
    return int(time.time())


def to_ist(ts: int) -> datetime:
    """A UTC epoch timestamp as an aware IST datetime."""
    return datetime.fromtimestamp(int(ts), tz=IST)


def ist_today(now: Optional[int] = None) -> date:
    return to_ist(now_ts() if now is None else now).date()


def ist_timestamp(day: date, hour: int, minute: int) -> int:
    """The UTC epoch timestamp of an IST wall-clock date and time."""
    local = datetime(day.year, day.month, day.day, int(hour), int(minute), tzinfo=IST)
    return int(local.timestamp())


# ---------------------------------------------------------------------------
# Daily schedules
# ---------------------------------------------------------------------------

def run_key(ts: int) -> str:
    """The IST calendar day an occurrence belongs to, e.g. ``2026-11-05``.

    A daily schedule publishes at most once per key, so a time change or a
    retry can never post twice on the same IST day.
    """
    return to_ist(ts).date().isoformat()


def next_daily_run(hour: int, minute: int, after: int, skip_key: Optional[str] = None) -> int:
    """The first ``hour:minute`` IST strictly after ``after`` (epoch seconds),
    never on the IST day ``skip_key`` (the day already published).

    IST has no daylight-saving time, so every day has exactly one such
    minute: walking forward day by day handles midnight and month/year ends.
    """
    day = to_ist(after).date()
    ts = ist_timestamp(day, hour, minute)
    while ts <= after or (skip_key is not None and run_key(ts) == skip_key):
        day += timedelta(days=1)
        ts = ist_timestamp(day, hour, minute)
    return ts


def daily_first_run(start: date, hour: int, minute: int, now: Optional[int] = None,
                    skip_key: Optional[str] = None) -> int:
    """The first post of a daily schedule that starts on ``start``.

    Normally ``start`` at ``hour:minute`` IST. If that moment has already
    passed (or is less than MIN_LEAD_SECONDS away), the first post is the
    next day the time can still run — the start date itself is never
    changed, and the UI tells the admin when the first post really goes out.
    """
    current = now_ts() if now is None else int(now)
    planned = ist_timestamp(start, hour, minute)
    return next_daily_run(hour, minute, max(current + MIN_LEAD_SECONDS - 1, planned - 1), skip_key)


def first_daily_run(hour: int, minute: int, now: Optional[int] = None,
                    skip_key: Optional[str] = None) -> int:
    """``daily_first_run`` for a schedule starting today."""
    return daily_first_run(ist_today(now), hour, minute, now, skip_key)


# Formatting is done by hand, not with strftime's %p / %b: those follow the
# process locale, and the UI must read the same on every host.

def _h12(hour: int) -> Tuple[int, str]:
    hour = int(hour)
    return (hour % 12 or 12), ("AM" if hour < 12 else "PM")


def fmt_day(day: date) -> str:
    return f"{day.day:02d}-{day.month:02d}-{day.year:04d}"


def fmt_date(ts: int) -> str:
    return fmt_day(to_ist(ts).date())


def fmt_hhmm(hour: int, minute: int) -> str:
    h12, suffix = _h12(hour)
    return f"{h12:02d}:{int(minute):02d} {suffix}"


def fmt_time(ts: int) -> str:
    local = to_ist(ts)
    return fmt_hhmm(local.hour, local.minute) + " IST"


def fmt_datetime(ts: int) -> str:
    """``DD-MM-YYYY hh:mm AM/PM IST`` — e.g. 06-11-2026 10:30 AM IST."""
    return f"{fmt_date(ts)} {fmt_time(ts)}"


def ist_hm(ts: int) -> Tuple[int, int]:
    """(hour, minute) of a timestamp in IST."""
    local = to_ist(ts)
    return local.hour, local.minute


def fmt_hour(hour: int) -> str:
    """``0`` -> ``12 AM``, ``13`` -> ``01 PM``."""
    h12, suffix = _h12(hour)
    return f"{h12:02d} {suffix}"


# ---------------------------------------------------------------------------
# Typed input
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})\s*$")
_TIME_12_RE = re.compile(r"^\s*(\d{1,2})(?:\s*[:.]\s*(\d{2}))?\s*([AaPp])\.?\s*[Mm]\.?\s*$")
_TIME_24_RE = re.compile(r"^\s*(\d{1,2})\s*[:.]\s*(\d{2})\s*$")


def parse_date(text: str) -> Optional[date]:
    """``DD-MM-YYYY`` (``/`` or ``.`` also accepted) -> date, else None.

    Day first, always — ``05-11-2026`` is 5 November. Impossible dates
    (31-02-2026) return None.
    """
    match = _DATE_RE.match(text or "")
    if not match:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_time(text: str) -> Optional[Tuple[int, int]]:
    """``hh:mm AM/PM`` (or ``h AM``, or 24-hour ``HH:MM``) -> (hour, minute).

    ``12:00 AM`` is midnight and ``12:30 PM`` is half past noon, as on any
    12-hour clock. Returns None for anything else.
    """
    raw = text or ""
    match = _TIME_12_RE.match(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        pm = match.group(3).lower() == "p"
        hour = hour % 12 + (12 if pm else 0)
        return hour, minute
    match = _TIME_24_RE.match(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        return hour, minute
    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def last_allowed_day(now: Optional[int] = None) -> date:
    return ist_today(now) + timedelta(days=MAX_AHEAD_DAYS)


def date_error(day: date, now: Optional[int] = None) -> Optional[str]:
    """Why ``day`` cannot be picked (None if it can)."""
    today = ist_today(now)
    if day < today:
        return "That date is in the past."
    if day > last_allowed_day(now):
        return f"Pick a date within the next {MAX_AHEAD_DAYS} days."
    return None


def time_error(ts: int, now: Optional[int] = None) -> Optional[str]:
    """Why the timestamp cannot be scheduled (None if it can)."""
    current = now_ts() if now is None else now
    if ts <= current:
        return "That time is in the past."
    if ts < current + MIN_LEAD_SECONDS:
        return "Pick a time at least 1 minute from now."
    if to_ist(ts).date() > last_allowed_day(current):
        return f"Pick a time within the next {MAX_AHEAD_DAYS} days."
    return None


def hour_is_open(day: date, hour: int, now: Optional[int] = None) -> bool:
    """True if some minute of that IST hour can still be scheduled."""
    return time_error(ist_timestamp(day, hour, 59), now) is None


def minute_is_open(day: date, hour: int, minute: int, now: Optional[int] = None) -> bool:
    return time_error(ist_timestamp(day, hour, minute), now) is None


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

WEEKDAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def month_weeks(year: int, month: int) -> List[List[int]]:
    """Monday-first weeks of a month; 0 marks a day of another month."""
    return calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)


def shift_month(year: int, month: int, delta: int) -> Tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_label(year: int, month: int) -> str:
    return f"{_MONTHS[month - 1]} {year}"
