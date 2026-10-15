# 🚩 SWAGGYMUSIC — Standalone Channel-Management Bot

[![License: MIT](LICENSE)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)

> Built on the **SWAGGYMUSIC** / Kurigram architecture (originally a music
> bot). All music, voice-stream, assistant-client and downloader code has
> been removed. This repository now ships **only** the three channel-management
> features requested for the standalone build.

---

## 🚀 What this bot does

| Command        | Where it works | What it does                                                                                |
| -------------- | -------------- | ------------------------------------------------------------------------------------------- |
| `/setdelay NsNm Nh \| off` | Channels only | Auto-delete every new channel post after a delay (3 s .. 24 h). Delay is counted from the **original post time** and never changes on edit. |
| `/setgap NsNm Nh \| off`   | Channels only | Enforce a minimum interval between **surviving** channel posts. Rejected posts never reset/extend the gap. Per-channel, not per-user. |
| `/createpost`  | PM with the bot | Interactive Post Creator — Hidden Video Preview or Normal Post (Format 1 / Format 2) with manual text, video, photo, button styles, preview, cancel, back. |

`/setdelay on` and `/setgap on` are intentionally **invalid** — the bot replies
with the help text.

All user-facing commands:

`/start`, `/help`, `/privacy`, `/setdelay`, `/setgap`, `/createpost`, `/cancel`

(`/cancel` is part of the Create Post workflow.)

---

## ✨ Key behaviour — Auto Delete + Post Gap

* **Persistent** — settings live in MongoDB (`autodelete_settings` collection).
* **One document per pending deletion** — stored in `autodelete_jobs`,
  with a unique index on `(chat_id, message_id)` and a TTL safety net.
* **Edit protection** — edited channel posts are filtered out before
  delay/gap logic runs. The deletion deadline (`delete_at`) is written via
  `$setOnInsert`, so reprocessing the same message can NEVER move it
  forward, even if Kurigram's filter behaviour changes.
* **Original post time** — `delete_at = message.date + delay`, not
  `time.time() + delay`. Editing at 12:00:40 of a 12:00:00 post still
  deletes at 12:01:00 for a `60s` delay.
* **Atomic gap** — `find_one_and_update` with a precondition makes
  simultaneous posters race safely; only one is allowed.
* **Restart-safe** — settings + pending jobs persist across bot restarts.
  The background worker resumes on boot.

---

## ❤️ Support

💬 **Support Group:** [SpicyxNetwork](https://t.me/SpIcYxNeTwOrK)
📢 **Update Channel:** [SWAGGYMUSIC](https://t.me/+sTyS-zKwUIk4YWI1)
📂 **GitHub Issues:** [Report a Problem](https://github.com/Yuki77394/SWAGGYHELPER/issues/new)

---

## 📜 License

This project is licensed under the **MIT License** — see the
[LICENSE](LICENSE) file for details.

---

## 🚀 Deployment

### 🔹 1. Heroku (one-click)

[![Deploy](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/Yuki77394/SWAGGYHELPER)

Required env vars: `API_ID`, `API_HASH`, `BOT_TOKEN`, `MONGO_DB_URI`,
`OWNER_ID`, `LOGGER_ID`. Optional: `HEROKU_APP_NAME`, `HEROKU_API_KEY`,
`START_IMG_URL`, `SUPPORT_CHANNEL`, `SUPPORT_CHAT`.

### 🔹 2. Local / VPS

```bash
sudo apt update && sudo apt install -y git python3-pip tmux nano
git clone https://github.com/Yuki77394/SWAGGYHELPER.git
cd SWAGGYHELPER
pip install -r requirements.txt   # or: pip install -e .
cp sample.env .env && nano .env
tmux
python3 -m SWAGGYMUSIC
```

Detach from tmux with `Ctrl+b`, `d`.

### 🔹 3. Docker / docker-compose

```bash
cp sample.env .env && nano .env
docker compose up -d --build
```

### 🔹 4. Render / Koyeb

Use the supplied `render.yaml` / `koyeb.yaml`. Both call
`python3 -m SWAGGYMUSIC` as the start command — the same entry point as
the Procfile.

---

## 🗂 Project layout

```
SWAGGYHELPER/
├── SWAGGYMUSIC/
│   ├── __init__.py           # app = Alone()
│   ├── __main__.py           # asyncio entrypoint — imports all plugins
│   ├── logging.py            # LOGGER(name) factory
│   ├── misc.py               # SUDOERS, _boot_, heroku() helper
│   ├── core/
│   │   ├── bot.py            # Alone(Client) — Kurigram bot client
│   │   ├── mongo.py          # mongodb = motor client .Anon
│   │   ├── dir.py            # runtime scratch dir setup
│   │   └── git.py            # empty placeholder (parity import)
│   ├── utils/
│   │   ├── database.py       # SLIM — kept only channel-management + sudo/ban/lang helpers
│   │   ├── decorators/language.py
│   │   ├── inline/{start,help,extras}.py
│   │   └── (formatters / extraction / errors / sys / pastebin / logger)
│   └── plugins/
│       ├── __init__.py       # auto-discovery of plugins/*/*.py
│       ├── bot/{start,help,privacy}.py
│       └── misc/{autodelete,createpost}.py
├── strings/
│   ├── __init__.py
│   ├── helpers.py            # HELP_1, HELP_2, HELP_3 only
│   └── langs/en.yml         # rewritten — no music strings
├── config.py
├── requirements.txt
├── pyproject.toml
├── Procfile                  # worker: python3 -m SWAGGYMUSIC
├── Dockerfile / docker-compose.yml
├── heroku.yml / render.yaml / koyeb.yaml
├── app.json / runtime.txt / .python-version
├── sample.env
└── README.md
```

Music-only modules that have been **removed** from this build:
`core/call.py`, `core/userbot.py`, `platforms/*` (Spotify/Soundcloud/
Apple/Resso/Youtube/Carbon/Telegram), `utils/stream/*`, `utils/music_cache.py`,
`utils/channelplay.py`, `utils/thumbnails.py`, `utils/file_refs.py`,
`utils/inline/{play,queue,settings,speed,stats}.py`,
`utils/inlinequery.py`, `utils/decorators/admins.py`,
`plugins/play/*`, `plugins/admins/*`, `plugins/audio_effects/*`,
`plugins/tools/*`, `plugins/bot/{inline,settings,thumb}.py`,
`plugins/misc/{Swaggy_reply,seeker,watcher,autoleave,broadcast,Sent}.py`,
`plugins/sudo/*`.

Kept dependencies (see `requirements.txt`): kurigram, motor, aiohttp,
GitPython, heroku3, humanize, psutil, Pillow, PyYAML, python-dotenv.
Removed dependencies (no longer imported anywhere): pytgcalls, ntgcalls,
yt-dlp, youtube-search, youtube-search-python, py-yt-search, py-yt,
spotipy, ffmpeg-python, static-ffmpeg, speedtest-cli, gTTS, openai,
prettytable, hachoir, beautifulsoup4, Unidecode, httpx, dnspython,
APScheduler, aiofiles, requests.
