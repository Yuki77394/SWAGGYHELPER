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
API_ID = int(getenv("API_ID", 0))
API_HASH = getenv("API_HASH")

# Bot token from @BotFather
BOT_TOKEN = getenv("BOT_TOKEN")

# ─── MongoDB ─────────────────────────────────────────────────────────────────
# Get your MongoDB URI from cloud.mongodb.com
MONGO_DB_URI = getenv("MONGO_DB_URI", None)

# ─── Owner / log channel / support ──────────────────────────────────────────
# Numeric user id of the bot owner (used for SUDOERS seed + Start "Owner" button)
OWNER_ID = int(getenv("OWNER_ID", 0))

# Chat id of the channel/group used for activity logs.
# Add the bot here and promote it as admin before launching.
LOGGER_ID = int(getenv("LOGGER_ID", 0))

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
