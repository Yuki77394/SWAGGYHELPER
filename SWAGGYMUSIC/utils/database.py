#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/KURIGRAMSWAG > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/Yuki77394/KURIGRAMSWAG/blob/master/LICENSE >
# All rights reserved.

"""
SWAGGYMUSIC database helpers — standalone channel-management build.

Music-only helpers (assistant, autoplay, music-on/off, stream queue,
thumbnail, playmode, music-cache, etc.) have been removed.

What remains is the shared infrastructure used by the Start UI, Help menu,
sudo system, banned-user filter, and the autodelete/setgap collections.
"""

from typing import Dict, List, Union

from SWAGGYMUSIC.core.mongo import mongodb

# ---------------------------------------------------------------------------
# Collections kept by the channel-management build
# ---------------------------------------------------------------------------

authdb = mongodb.adminauth          # reserved for /auth feature parity (unused now)
authuserdb = mongodb.authuser       # reserved (unused now)
blacklist_chatdb = mongodb.blacklistChat
blockeddb = mongodb.blockedusers
chatsdb = mongodb.chats
gbansdb = mongodb.gban
langdb = mongodb.language
onoffdb = mongodb.onoffper
sudoersdb = mongodb.sudoers
usersdb = mongodb.tgusersdb

# ---------------------------------------------------------------------------
# Channel-management collections — the core of this build
# ---------------------------------------------------------------------------

settings_db = mongodb.autodelete_settings
jobs_db = mongodb.autodelete_jobs


# ---------------------------------------------------------------------------
# Index bootstrap — idempotent, called on every boot
# ---------------------------------------------------------------------------

async def ensure_channel_indexes() -> None:
    """Create the indexes required by autodelete_settings / autodelete_jobs.

    Idempotent — safe to call on every restart.

    - settings: one document per channel (unique chat_id)
    - jobs:     one document per (chat_id, message_id) so re-processing
                an existing message can never insert a duplicate deletion job
                (this is the foundation of the $setOnInsert protection that
                keeps the original ``delete_at`` immutable across edits)
    - jobs TTL: hard safety-net garbage collector so even a permanently
                stuck job cannot remain in Mongo forever
    """
    from contextlib import suppress

    with suppress(Exception):
        await settings_db.create_index("chat_id", unique=True)

    with suppress(Exception):
        await jobs_db.create_index(
            [("chat_id", 1), ("message_id", 1)], unique=True
        )

    with suppress(Exception):
        await jobs_db.create_index([("delete_at", 1)])

    with suppress(Exception):
        await jobs_db.create_index("expires_at", expireAfterSeconds=0)


# ---------------------------------------------------------------------------
# Language helper (used by Start/Help/inline callbacks)
# ---------------------------------------------------------------------------

async def get_lang(chat_id: int) -> str:
    lang = await langdb.find_one({"chat_id": chat_id})
    if not lang:
        return "en"
    return lang["lang"]


async def set_lang(chat_id: int, lang: str) -> None:
    await langdb.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "lang": lang}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Maintenance flag (used by language decorator)
# ---------------------------------------------------------------------------

async def is_maintenance() -> bool:
    flag = await onoffdb.find_one({"on_off": 1})
    return bool(flag["mode"]) if flag else True


async def maintenance_on() -> None:
    await onoffdb.update_one(
        {"on_off": 1}, {"$set": {"mode": False}}, upsert=True
    )


async def maintenance_off() -> None:
    await onoffdb.update_one(
        {"on_off": 1}, {"$set": {"mode": True}}, upsert=True
    )


# ---------------------------------------------------------------------------
# on/off helpers (used by Start / privacy / sudo plugins)
# ---------------------------------------------------------------------------

async def is_on_off(on_off: int) -> bool:
    flag = await onoffdb.find_one({"on_off": on_off})
    return bool(flag["mode"]) if flag else False


async def add_on(on_off: int) -> None:
    await onoffdb.update_one(
        {"on_off": on_off}, {"$set": {"mode": True}}, upsert=True
    )


async def add_off(on_off: int) -> None:
    await onoffdb.update_one(
        {"on_off": on_off}, {"$set": {"mode": False}}, upsert=True
    )


# ---------------------------------------------------------------------------
# Served chats / users (kept for Start handler)
# ---------------------------------------------------------------------------

async def is_served_user(user_id: int) -> bool:
    return bool(await usersdb.find_one({"user_id": user_id}))


async def get_served_users() -> list:
    return [user["user_id"] for user in await usersdb.find({}).to_list(None)]


async def add_served_user(user_id: int) -> None:
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id}},
        upsert=True,
    )


async def get_served_chats() -> list:
    return [chat["chat_id"] for chat in await chatsdb.find({}).to_list(None)]


async def is_served_chat(chat_id: int) -> bool:
    return bool(await chatsdb.find_one({"chat_id": chat_id}))


async def add_served_chat(chat_id: int) -> None:
    await chatsdb.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Blacklisted chats (kept for Start handler exit path)
# ---------------------------------------------------------------------------

async def blacklisted_chats() -> list:
    return [chat["chat_id"] for chat in await blacklist_chatdb.find({}).to_list(None)]


async def blacklist_chat(chat_id: int) -> bool:
    if await blacklist_chatdb.find_one({"chat_id": chat_id}):
        return False
    await blacklist_chatdb.insert_one({"chat_id": chat_id})
    return True


async def whitelist_chat(chat_id: int) -> bool:
    if not await blacklist_chatdb.find_one({"chat_id": chat_id}):
        return False
    await blacklist_chatdb.delete_one({"chat_id": chat_id})
    return True


# ---------------------------------------------------------------------------
# Sudoers / ban helpers (kept for sudo plugin + start banner)
# ---------------------------------------------------------------------------

async def get_sudoers() -> list:
    sudoers_doc = await sudoersdb.find_one({"sudo": "sudo"})
    return sudoers_doc["sudoers"] if sudoers_doc else []


async def add_sudo(user_id: int) -> bool:
    sudoers = await get_sudoers()
    if user_id in sudoers:
        return False
    sudoers.append(user_id)
    await sudoersdb.update_one(
        {"sudo": "sudo"},
        {"$set": {"sudoers": sudoers}},
        upsert=True,
    )
    return True


async def remove_sudo(user_id: int) -> bool:
    sudoers = await get_sudoers()
    if user_id not in sudoers:
        return False
    sudoers.remove(user_id)
    await sudoersdb.update_one(
        {"sudo": "sudo"},
        {"$set": {"sudoers": sudoers}},
        upsert=True,
    )
    return True


async def get_gbanned() -> list:
    return [user["user_id"] for user in await gbansdb.find({}).to_list(None)]


async def is_gbanned_user(user_id: int) -> bool:
    return bool(await gbansdb.find_one({"user_id": user_id}))


async def add_gban_user(user_id: int) -> None:
    await gbansdb.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id}},
        upsert=True,
    )


async def remove_gban_user(user_id: int) -> None:
    await gbansdb.delete_one({"user_id": user_id})


async def get_banned_users() -> list:
    return [user["user_id"] for user in await blockeddb.find({}).to_list(None)]


async def get_banned_count() -> int:
    return await blockeddb.count_documents({})


async def is_banned_user(user_id: int) -> bool:
    return bool(await blockeddb.find_one({"user_id": user_id}))


async def add_banned_user(user_id: int) -> None:
    await blockeddb.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id}},
        upsert=True,
    )


async def remove_banned_user(user_id: int) -> None:
    await blockeddb.delete_one({"user_id": user_id})


# ---------------------------------------------------------------------------
# Auth users — kept for parity, unused by channel-management build
# ---------------------------------------------------------------------------

async def _get_authusers(chat_id: int) -> Dict[str, int]:
    return await authuserdb.find_one({"chat_id": chat_id}) or {}


async def get_authuser_names(chat_id: int) -> List[str]:
    return [user["name"] for user in (await _get_authusers(chat_id)).get("users", [])]


async def get_authuser(chat_id: int, name: str) -> Union[bool, dict]:
    for user in (await _get_authusers(chat_id)).get("users", []):
        if user["name"] == name:
            return user
    return False


async def save_authuser(chat_id: int, name: str, note: dict) -> None:
    doc = await _get_authusers(chat_id)
    users = doc.get("users", [])
    users.append({"name": name, **note})
    await authuserdb.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "users": users}},
        upsert=True,
    )


async def delete_authuser(chat_id: int, name: str) -> bool:
    doc = await _get_authusers(chat_id)
    users = doc.get("users", [])
    new_users = [u for u in users if u["name"] != name]
    if len(new_users) == len(users):
        return False
    await authuserdb.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "users": new_users}},
        upsert=True,
    )
    return True
