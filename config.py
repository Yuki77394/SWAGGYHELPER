#
# Copyright (C) 2021-2022 by SWAGGYMUSIC@Github, < https://github.com/Yuki77394/SWAGGYHELPER >.
#
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.
#
# Standalone channel-management build configuration. Music-only
# variables (STRING_SESSION*, STORAGE_CHANNEL_ID, MUSIC_CACHE_*,
# SPOTIFY_*, YTPROXY_URL, YT_API_KEY, DURATION_LIMIT, etc.) have been
# removed.

import re
from os import getenv

from dotenv import load_dotenv
from pyrogram import filters

RANDOM_THUMBS = [
    "https://files.catbox.moe/jf0yqq.jpg",
    "https://files.catbox.moe/7w0ec2.jpg",
    "https://files.catbox.moe/dfj1l8.jpg",
    "https://files.catbox.moe/e7pbwj.jpg",
    "https://files.catbox.moe/bta4qz.jpg",
    "https://files.catbox.moe/1a1pu2.jpg",
    "https://files.catbox.moe/xvirq4.jpg",
    "https://files.catbox.moe/8dyj3u.jpg",
    "https://files.catbox.moe/x63yfj.jpg",
    "https://files.catbox.moe/3rtw9v.jpg",
    "https://files.catbox.moe/0u6db2.jpg",
]


def get_thumb():
    import random
    return random.choice(RANDOM_THUMBS)


load_dotenv()

# ─── Telegram credentials ────────────────────────────────────────────────────
# Get these values from my.telegram.org/apps
# Validation is deferred to __main__.init() so that importing config.py
# (which happens very early) never raises — but the values are checked
# before the bot starts. See __main__.init() for the validation gate.
try:
    API_ID = int(getenv("API_ID", 0) or 0)
except (TypeError, ValueError):
    API_ID = 0
API_HASH = getenv("API_HASH")

# Bot token from @BotFather
BOT_TOKEN = getenv("BOT_TOKEN")

# ─── MongoDB ─────────────────────────────────────────────────────────────────
# Get your MongoDB URI from cloud.mongodb.com
MONGO_DB_URI = getenv("MONGO_DB_URI", None)

# ─── Owner / log channel / support ──────────────────────────────────────────
# Numeric user id of the bot owner (used for SUDOERS seed + Start "Owner" button)
try:
    OWNER_ID = int(getenv("OWNER_ID", 0) or 0)
except (TypeError, ValueError):
    OWNER_ID = 0

# Chat id of the channel/group used for activity logs.
# Add the bot here and promote it as admin before launching.
try:
    LOGGER_ID = int(getenv("LOGGER_ID", 0) or 0)
except (TypeError, ValueError):
    LOGGER_ID = 0

# Public support links (shown in Start panel + Help menu)
SUPPORT_CHANNEL = getenv("SUPPORT_CHANNEL", "https://t.me/+sTyS-zKwUIk4YWI1")
SUPPORT_CHAT = getenv("SUPPORT_CHAT", "https://t.me/SpIcYxNeTwOrK")

# ─── Start image ─────────────────────────────────────────────────────────────
# Override the random thumbnail with a fixed URL by setting START_IMG_URL.
START_IMG_URL = getenv("START_IMG_URL", get_thumb())
PING_IMG_URL = getenv("PING_IMG_URL", get_thumb())

# ─── Heroku deploy-and-restart automation ────────────────────────────────────
# Optional. If both are set, the bot will be able to git-pull updates and
# restart the Heroku dyno.
HEROKU_APP_NAME = getenv("HEROKU_APP_NAME")
HEROKU_API_KEY = getenv("HEROKU_API_KEY")

# ─── Privacy policy link ────────────────────────────────────────────────────
PRIVACY_LINK = getenv(
    "PRIVACY_LINK",
    "https://telegra.ph/Privacy-Policy-for-SWAGGYMUSIC-08-14",
)

# ─── Debug / safety toggles ─────────────────────────────────────────────────
DEBUG_IGNORE_LOG = True

# ─── In-memory runtime state ────────────────────────────────────────────────
# These are NOT config — they are runtime caches used by some helpers.
BANNED_USERS = filters.user()
adminlist = {}
confirmer = {}


def time_to_seconds(time):
    stringt = str(time)
    return sum(int(x) * 60**i for i, x in enumerate(reversed(stringt.split(":"))))


# ─── Sanity checks ──────────────────────────────────────────────────────────
if SUPPORT_CHANNEL:
    if not re.match("(?:http|https)://", SUPPORT_CHANNEL):
        raise SystemExit(
            "[ERROR] - Your SUPPORT_CHANNEL url is wrong. Please ensure that it starts with https://"
        )

if SUPPORT_CHAT:
    if not re.match("(?:http|https)://", SUPPORT_CHAT):
        raise SystemExit(
            "[ERROR] - Your SUPPORT_CHAT url is wrong. Please ensure that it starts with https://"
        )


# ─── Startup configuration validation ───────────────────────────────────────
#
# Called from SWAGGYMUSIC.__main__.init() BEFORE the MongoDB ping and
# BEFORE app.start(). Raises SystemExit with a clean, credential-free
# message if any required value is missing or invalid.
#
# We DO NOT raise at config.py import time so that:
#   - The module can be imported by tooling (tests, linters) without
#     requiring a full env.
#   - The error message is logged through the application logger
#     (SWAGGYMUSIC.logging.LOGGER) rather than printed to stderr at
#     import time, which is hard to capture on some platforms.
#
# No credential VALUES are ever included in the error messages — only
# the names of the missing/invalid variables.

def validate_config_for_startup() -> None:
    """Validate required configuration values. Raises SystemExit on failure.

    Checks (in order):
      1. API_ID is a positive integer.
      2. API_HASH is a non-empty string.
      3. BOT_TOKEN is a non-empty string with the expected format.
      4. MONGO_DB_URI is a non-empty string starting with mongodb:// or
         mongodb+srv://.
      5. OWNER_ID is a positive integer.

    LOGGER_ID is NOT required (the bot can run without activity logging;
    log_activity() silently no-ops when LOGGER_ID is 0). It is validated
    only if set: must be a non-zero integer.

    Error messages name only the variable, never its value.
    """
    problems = []

    if not API_ID or API_ID <= 0:
        problems.append(
            "API_ID is missing or invalid. Set it to a positive integer "
            "from https://my.telegram.org/apps."
        )

    if not API_HASH or not isinstance(API_HASH, str):
        problems.append(
            "API_HASH is missing. Set it to the API hash string from "
            "https://my.telegram.org/apps."
        )

    if not BOT_TOKEN or not isinstance(BOT_TOKEN, str):
        problems.append(
            "BOT_TOKEN is missing. Get a token from @BotFather and set it "
            "in the environment."
        )
    elif not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", BOT_TOKEN):
        # The Bot API token format is "<bot_id>:<35+ char secret>".
        # We don't log the token itself — only that the FORMAT looks wrong.
        problems.append(
            "BOT_TOKEN does not look like a valid Bot API token "
            "(expected format: '<digits>:<35+ char alphanumeric>'). "
            "Please re-check the token from @BotFather."
        )

    if not MONGO_DB_URI or not isinstance(MONGO_DB_URI, str):
        problems.append(
            "MONGO_DB_URI is missing. Get a connection string from "
            "https://cloud.mongodb.com and set it in the environment."
        )
    elif not re.match(r"^mongodb(\+srv)?://", MONGO_DB_URI):
        # Don't log the URI — it contains credentials.
        problems.append(
            "MONGO_DB_URI does not start with 'mongodb://' or "
            "'mongodb+srv://'. Please check the connection string."
        )

    if not OWNER_ID or OWNER_ID <= 0:
        problems.append(
            "OWNER_ID is missing or invalid. Set it to your numeric "
            "Telegram user ID (a positive integer)."
        )

    # LOGGER_ID is optional but if set, must be non-zero.
    if LOGGER_ID and LOGGER_ID == 0:
        problems.append(
            "LOGGER_ID is set to 0. Either set it to a valid chat ID or "
            "unset it to disable activity logging."
        )

    if problems:
        msg = (
            "Configuration validation failed — the bot cannot start safely.\n\n"
            "Problems:\n  - " + "\n  - ".join(problems) + "\n\n"
            "Fix the environment variables and restart. (Credential values "
            "are intentionally omitted from this message.)"
        )
        raise SystemExit(msg)
