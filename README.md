# Bonki Intel

An **intelligent Telegram group agent**. Bonki indexes the files people share in a
group, understands a plain-English request, finds the file you mean, and answers —
re-posting the file, **@mentioning whoever asked**, and **adding a hashtag**.

```
Bonki, scan this group and tag apple music apk?
```

Bonki scans the group's history, matches "apple music apk" against the files it
found, and replies with the file itself, tagged `#AppleMusic`, mentioning you.

> Deployed as **@Bonki_Intelligence_bot** — address it by its handle
> (`@Bonki_Intelligence_bot, get the apple music apk`) or the plain wake word
> (`Bonki, …`). Both work; the handle is detected automatically.

Built as a fresh, focused bot inspired by the original Bonki's file-handling
machinery — but where the original mirrored GitHub releases into channels, this
one is a *conversational file brain for groups*.

---

## What it can do

| You say (in the group) | Bonki does |
|---|---|
| `Bonki, scan this group and tag apple music apk` | scans history, finds the APK, re-posts it, mentions you, tags `#AppleMusic` |
| `Bonki, get the latest whatsapp mod apk` | finds the best match and re-posts it |
| `Bonki, list apks` | lists indexed files |
| `Bonki, how many zips` | counts them |
| `Bonki, group stats` | breakdown by file type and top sharers |
| `Bonki, summarise what's been shared` | a short summary of recent files |
| `Bonki, help` | what it can do |

Bonki answers when it's **addressed** — its wake word (`bonki`, and the handle
`bonki_intelligence_bot`, by default), an `@mention` (`@Bonki_Intelligence_bot`),
or a reply to one of its messages. That's configurable.

---

## How it works

```
Telegram ──webhook──▶ handlers.py ──▶ agent.py ──▶ index (db.py)
                        │                 │            ▲
                        │                 │            │
                   live capture       the brain    indexer.py ──▶ userbot.py
                   (Bot API)        (llm.py)      (scan)         (Telethon)
```

- **`handlers.py`** — routes updates, detects when Bonki is addressed, and
  indexes files as they're posted.
- **`indexer.py`** — walks a group's history (via the user account) and records
  every file: name, size, type, date, sender, caption, tags.
- **`agent.py`** — the intelligence: parses the intent, ranks candidate files,
  builds the reply (file + mention + hashtag), and handles list/count/stats/summary.
- **`llm.py`** — the switchable brain: **Gemini** or any **OpenAI-compatible**
  endpoint, or `off` (deterministic keyword matching).
- **`userbot.py`** — a Telethon user account: the only way to read a group's
  history and to pull files larger than a bot can.
- **`telegram.py`** — the Bot API (how Bonki speaks).
- **`db.py`** — durable state: settings, groups, the file index, activity.

### Why a user account?

A Telegram **bot** cannot read a group's past messages (`getChatHistory` isn't
available to bots) and can only download files up to 20 MB. So Bonki uses **your
own account** for scanning and fetching. The account must be a **member** of the
groups you want scanned. Bonki still works without it — it just indexes files as
they're posted live (turn group privacy off for the bot via BotFather
`/setprivacy` → Disable), and can't walk history.

---

## Quick start

### 1. Create the bot
- Message **@BotFather** → `/newbot` → copy the token.
- Add the bot to your group.
- For live indexing, send BotFather `/setprivacy` → pick the bot → **Disable**
  (so it sees all group messages, not just mentions).

### 2. Configure
Copy `.env.example` to `.env` and fill in at least `TELEGRAM_BOT_TOKEN`. Then run:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000/`.

### 3. Connect the user account (to scan history)
- Get `api_id` / `api_hash` from <https://my.telegram.org> → **API development tools**.
- Open **User account** (`/userbot`) in the dashboard and log in — Telegram sends
  the code *inside the app* (the “Telegram” service chat). Or run
  `python userbot_login.py` locally and paste the session string into Settings.

### 4. Set the brain (optional but recommended)
Settings → **Brain** → pick `gemini` or `openai`, paste a key, **Save**, then
**Test**. With no key Bonki still works, using keyword matching.

### 5. Use it
Add the bot to a group and say:

```
Bonki, scan this group and tag apple music apk
```

---

## Deploy on Render

### Before you start
1. **Push the code to GitHub** (a new repo, e.g. `bonki-intel`). Render deploys
   from a Git repo. From inside the `bonki-intel` folder:
   ```bash
   git init && git add . && git commit -m "Bonki Intel"
   git branch -M main
   git remote add origin https://github.com/<you>/bonki-intel.git
   git push -u origin main
   ```
2. **Create the bot** — @BotFather → `/newbot` → copy the token. (This build is
   set up for the handle **@Bonki_Intelligence_bot**; its username is detected
   automatically, so any handle works.)
3. **(Recommended) Make a GitHub token** with the **`gist`** scope — Settings →
   Developer settings → Personal access tokens → Tokens (classic). This is what
   makes your groups and file index survive Render's restarts.
4. **(Optional) Get Gemini key** at <https://aistudio.google.com/app/apikey>.

### Deploy (Blueprint — easiest)
1. On [render.com](https://render.com): **New +** → **Blueprint**.
2. Pick your `bonki-intel` repo → **Apply**. Render reads `render.yaml` and
   creates the web service.
3. When prompted, fill the environment variables:
   | Key | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from @BotFather (**required**) |
   | `GITHUB_TOKEN` | the `gist`-scope token (recommended) |
   | `GEMINI_API_KEY` | your Gemini key (optional) |
   | `TG_API_ID` / `TG_API_HASH` / `TG_SESSION` | only for history scanning |
4. **Create Web Service** and wait for the first deploy. The logs should end with
   `startup: storage=gist, ...` and `telegram mode: long polling`.

### Deploy (manual — if you'd rather not use the blueprint)
**New +** → **Web Service** → pick the repo, then:
- **Runtime:** Python
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Health Check Path:** `/healthz`
- Add env vars: `PYTHON_VERSION=3.12.6` (important — see below),
  `TELEGRAM_BOT_TOKEN`, `GITHUB_TOKEN`, and `USE_POLLING=1`.

### After it's live
- Open `https://<your-app>.onrender.com/` — the dashboard.
- **Groups:** add your group (or just add the bot to it and post once). If you set
  the `TG_*` vars, open **User account** to finish the login, then **Scan**.
- **Settings:** pick the brain (`gemini`/`openai`/`off`), paste a key, **Test**.
- In the group, try: `Bonki, scan this group and tag apple music apk`.

### Keep it awake (Render free tier)
Free services **sleep after ~15 minutes idle**. Bonki is built for that:
- It uses **long polling** by default, and Telegram holds undelivered updates for
  ~24h — so when the service wakes it catches up and nothing is lost.
- To answer in real time, add a free cron at [cron-job.org](https://cron-job.org)
  hitting `https://<your-app>.onrender.com/cron/scan` **every 10 minutes**. That
  wakes the app and refreshes the index. (Render's own Cron Jobs are a paid
  feature; the external one is free.)
- If you'd rather use a webhook, set `USE_POLLING=0` — Render's
  `RENDER_EXTERNAL_URL` is picked up automatically and the webhook is registered
  on boot (a secret is generated for you).

### The three Render gotchas, handled
1. **Ephemeral disk.** Render wipes the filesystem on every deploy/restart, so a
   local state file would lose your groups and index. Set **`GITHUB_TOKEN`** and
   state is stored in a private gist instead — durable across restarts. The
   dashboard shows a warning when storage is local-only.
2. **Sleeping.** Long polling + Telegram's update queue means wake-ups catch up.
3. **Python version.** `render.yaml` pins **`PYTHON_VERSION=3.12.6`** so builds are
   reproducible against a version this was tested on. The whole stack (including
   current Telethon) also runs on 3.13+, so you can change or drop the pin if you
   prefer — it's for predictability, not a hard requirement.

### Docker (any host)
```bash
docker compose up --build
```


---

## Environment variables

| Key | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from @BotFather (**required**) |
| `GITHUB_TOKEN` | `gist` scope → durable state (recommended) |
| `STATE_GIST_ID` / `STATE_REPO` / `STATE_PATH` | pin storage explicitly |
| `WEBHOOK_BASE` | public URL, to auto-register the Telegram webhook |
| `LLM_PROVIDER` | `gemini` \| `openai` \| `off` |
| `GEMINI_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL` | brain credentials |
| `TG_API_ID`, `TG_API_HASH`, `TG_SESSION` | user account for scanning |
| `TELEGRAM_API_BASE` | only for a self-hosted local Bot API server |

Everything except the bot token is also editable in the dashboard.

---

## Honest limits

- **Bots can't read history** — scanning needs the user account, and the account
  must be a member of the group.
- **Bot uploads are capped at 50 MB** (Telegram's cloud limit). Larger files are
  forwarded from the user account instead, or sent as a link.
- **Automating a user account** is a grey area against Telegram's terms, and the
  session string is a full login for your account — treat it like a password.
- The dashboard has **no authentication** — keep the URL private.

---

## Project layout

```
bonki-intel/
  app/
    main.py        FastAPI: webhook, dashboard, cron
    handlers.py    update routing + address detection
    agent.py       the intelligence (intent, matching, reply)
    indexer.py     group file index (history scan + live capture)
    llm.py         switchable brain (Gemini / OpenAI-compatible / off)
    userbot.py     Telethon user account (scan + fetch files)
    telegram.py    Bot API wrapper
    db.py          durable state store
    config.py      defaults
    templates/     dashboard (no auth)
  requirements.txt
  .env.example
  Dockerfile · docker-compose.yml · render.yaml
  userbot_login.py
```
