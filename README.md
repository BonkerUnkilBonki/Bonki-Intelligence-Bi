# Bonki-Intelligence

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
| `Bonki, find apple music apk` | finds the best match and re-posts it |
| `Bonki, find Antigravity-bypass.apk` | finds it by name — **no file type needed** |
| `Bonki, find antigravty` | **fuzzy** — typos and partial names still match |
| *(any new file posted)* | **indexed automatically** — no manual scan |
| `Bonki, list apks` | lists indexed files |
| `Bonki, how many zips` | counts them |
| `Bonki, group stats` | breakdown by file type and top sharers |
| `Bonki, summarise what's been shared` | a short summary of recent files |
| `Bonki, help` | what it can do |

Bonki answers when it's **addressed** — its wake word (`bonki`, and the handle
`bonki_intelligence_bot`, by default), an `@mention` (`@Bonki_Intelligence_bot`),
or a reply to one of its messages. That's configurable.

**Auto-indexing.** You never have to scan by hand. Files are indexed the moment
they're posted (live capture), and a background pass re-scans each group on a
timer for anything missed — including files posted while the service was asleep.
Toggle it under Settings → **Auto-index new files**; set the interval there too.

**Sharing the file, not a link.** When Bonki finds a file it shares the *actual
file*, tried in this order: a server-side copy by the bot (full size, no upload,
appears as Bonki), an upload by the bot (files up to the size limit), then a
server-side forward via your user account (any size up to 2 GB). A link is sent
only if all of those fail.

## Group management (Rose-style)

Commands work as ordinary slash commands in any **approved** group — **no wake
word needed**, so it behaves like a normal group-management bot:

| Area | Commands |
|---|---|
| Moderation (admins) | `/ban` `/unban` `/kick` `/mute [30m]` `/unmute` `/warn` `/warns` `/resetwarns` `/purge` `/del` `/pin` `/unpin` `/report` |
| Notes | `/save name text` `/get name` `/clear name` `/notes` |
| Rules | `/setrules text` `/rules` |
| Welcome | `/setwelcome text ({name})` `/welcome on\|off` `/setgoodbye` `/goodbye` |
| Info | `/id` `/info` `/admins` `/help` |
| Special | `/tag @user 5` — tags them 5 times |

Moderation needs the **bot to be an admin** with the relevant rights (ban/mute/pin),
and the person issuing it to be a **group admin** (or you, the owner). At 3
warnings the member is muted for an hour and warnings reset.

**`/tag` guardrails.** Capped at **20 tags per command** and rate-limited to one
tag command per user per minute, so it can't be turned into a spam/harassment tool.
Works as `/tag @user 5` or `Bonki tag @user 5`.

**Honest scope.** This is a practical core of Rose's behaviour, not a
reimplementation of all of it. **Not included:** filters, locks (media/link
locks), blacklists, antiflood, federations, connections, captcha, scheduled
messages. Those are separate large modules — tell me which you actually use and
I'll add them next.

## Verification — the bot only works in *your* groups

Anyone can add a bot to a group, so **nothing happens in a new group until you
approve it**. This is on by default.

**Approve from Telegram (no dashboard needed):**

1. Set your **Owner ID** in Settings (get it from `@userinfobot`) and send the bot
   `/start` in DM once — that's what lets it message you.
2. When the bot is added to a group it posts a **✅ Approve this group** button
   *in the group*, and DMs you the same buttons.
3. Tap **✅ Approve** — done. (Tap **🚫 Reject** and the bot leaves the group.)

Until then the bot won't index, scan, or answer there, and it says so if someone
asks. Only your tap counts — anyone else tapping gets *"Only the bot owner can
approve this group."*

**Any time, from the chat:** DM the bot `/pending` for a list of unverified groups,
each with its own Approve / Reject buttons. Or `/approve <chat_id>` / `/reject <chat_id>`.

**From the dashboard:** unverified groups appear in a *Waiting for your approval*
card on the home page with the same Approve / Reject buttons.

**Channel links.** Every group on the home page is a clickable link to the chat
itself — `https://t.me/<username>` for public ones, `https://t.me/c/<id>` for
private ones (opens for members). So you can jump straight to the channel the bot
was added to.

**Approval & request history.** The home page keeps a log of every event: the bot
being added to a channel (with who added it), each approval request, each
yours-approve/reject, and every scan requested — each row linked to its channel,
newest first. It's durable, so it survives restarts (keep `GITHUB_TOKEN` set).

Groups you had before this feature stay approved, and a group you add yourself in
the dashboard is approved on the spot. Turn the whole gate off with
**Settings → Require owner approval** if you don't want it.

## Learning — what it actually does

Bonki learns from real usage. No pretrained weights, no GPU, nothing invented.
Two things are genuinely learned, both from data that arrives in your group:

1. **A TF-IDF vector model** over the files actually in each group — their real
   names, captions and tags. It is rebuilt as files are indexed, so retrieval
   improves as the corpus grows. This is a real vector-space retrieval model.
2. **Association weights from feedback.** Under each result the bot shows
   buttons — *"1. KernelSU v3.3.0.apk"*, *"2. KernelSU_Next…"*, *"None of these"*.
   When someone taps the file they actually wanted, that query's words are linked
   to that file (and the others are marked wrong). Next time those words pull the
   right file up — even if the words don't appear in its name at all.

The close-matches message **deletes itself after 10 minutes** if nobody taps a
choice, so the group stays tidy; if someone does tap, it stays. Set the timeout
under Settings → *Auto-delete the “close matches” message after* (`0` = never).

Both feed a re-ranker: `score = fuzzy + 0.35·TF-IDF + 0.6·learned`. The candidate
set is the *union* of what each signal likes, so a file the fuzzy matcher never
saw can still surface because real users taught it. The **Learning** page shows
what it has learned per group — examples, confirmed-correct rate, and the
strongest learned word→file links — with **Rebuild model** and **Clear feedback**.

**Honest limits.** This is classical retrieval ML (vector-space model plus a
learned re-ranker) — not a neural network, and not "on-device deep learning".
There is no training on data you haven't given it, and no model weights shipped:
the model *is* your group's files plus the feedback your users leave. It needs no
GPU and runs inside the same small service. The more real files and real
confirmations it sees, the better it ranks.

**How matching works.** A query is split into *distinctive* words
(`antigravity`) and *type* words (`apk`). Type words are only a hint — they never
pick a file on their own, so "apple music apk" won't fall back to an unrelated
APK. Distinctive words match fuzzily (typos, prefixes, partial names), and if
nothing genuinely fits, Bonki says so instead of guessing. It uses `rapidfuzz`
when available and falls back to a built-in matcher otherwise.

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
   git init && git add . && git commit -m "Bonki-Intelligence"
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

### Deploy as a Web Service (recommended)
1. On [render.com](https://render.com): **New +** → **Web Service**, then connect
   your `bonki-intel` repo.
2. Fill in these settings:
   | Field | Value |
   |---|---|
   | Language / Runtime | **Python** |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
   | Instance Type | **Free** |
   | Health Check Path | `/healthz` |

   `$PORT` is assigned by Render (you'll see `Uvicorn running on …:10000` in the
   logs) — the start command already binds to it, and HEAD probes on `/healthz`
   now answer 200 instead of 405.
3. Under **Environment**, add:
   | Key | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from @BotFather (**required**) |
   | `USE_POLLING` | `1` |
   | `GITHUB_TOKEN` | the `gist`-scope token (recommended) |
   | `GEMINI_API_KEY` | your Gemini key (optional) |
   | `TG_API_ID` / `TG_API_HASH` | only if you'll scan history (see below) |
4. **Create Web Service** and wait for the deploy. The logs should end with
   `startup: storage=…` and `telegram mode: long polling`.

The Python version is pinned by the **`.python-version`** file in the repo root
(`3.12.6`), so you don't need to set `PYTHON_VERSION` — a fresh Render service
otherwise defaults to Python 3.14. Change the number in that file to move
versions.

### After it's live
- Open `https://<your-app>.onrender.com/` — the dashboard.
- **Telegram login (for scanning).** Open **User account** (`/userbot`) and log in
  your own account, entirely in the browser — no computer needed:
  1. `api_id` + `api_hash` from <https://my.telegram.org> → **API development tools**
  2. your phone number (with country code) → **Send code**
  3. the code Telegram sends — it arrives **inside the app**, in the “Telegram”
     service chat, not by SMS → **Log in** (plus your 2FA password if you have one).

  Then add the group and hit **Scan**. (Skip this and you still get live indexing.)
  Keep `GITHUB_TOKEN` set, or the session is lost on restart and you'll log in again.
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

### Staying online ("the bot and the dashboard keep going offline")

On Render's **free** tier a service is spun down after **~15 minutes with no
traffic**. When it sleeps, the web dashboard is unreachable *and* the Telegram
bot stops answering — both, together, because they are the same service. This is
normal free-tier behaviour, not a crash.

**Bonki now keeps itself awake.** With **Settings → Keep-alive** on (the
default), it pings its own public URL every 10 minutes. Render counts that as
traffic, so the service never idles and stays online. Check
**/diagnostics → Keep-alive** to see the last ping and its HTTP status.

Two caveats, stated plainly:

- The self-ping only works **while the service is already awake**. If it has
  genuinely gone to sleep, something outside has to wake it. Open the app URL in
  a browser, or (better) add a free external cron at
  [cron-job.org](https://cron-job.org) hitting `https://<your-app>.onrender.com/healthz`
  every 10 minutes. Once it's up, the self-ping keeps it up.
- Keeping one service awake ~24/7 uses ~730 of Render's 750 free
  instance-hours per month — it fits, but only for a single always-on service.

On a paid always-on plan (or any host that doesn't sleep) you can turn keep-alive
**off**; it isn't needed there.

### Panel hosts (Pterodactyl, bot-hosting, Falcon, etc.)

These panels ask you to pick **the file your server should run**. Choose:

```
main.py        (in the project root - NOT app/main.py)
```

and set the start command to:

```
python main.py
```

(or `sh start.sh` if the panel wants a shell command). `main.py` reads the port
the panel gives you — `SERVER_PORT`, `PORT`, `SERVER_PORT_WEB` or `WEB_PORT` — so
there's nothing to configure by hand.

Then set these environment variables in the panel:

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from @BotFather (**required**) |
| `USE_POLLING` | `1` |
| `GITHUB_TOKEN` | optional — only if the panel's disk is wiped between restarts |
| `TG_API_ID` / `TG_API_HASH` / `TG_SESSION` | optional — history scanning only |

> **Do not put `TG_SESSION` on a shared free panel.** A Telethon session string
> is a full login to your personal Telegram account. Run bot-token only there —
> everything except history scanning still works.

### Troubleshooting: `409 Conflict: terminated by other getUpdates request`

Telegram allows **one** `getUpdates` poll per bot token. This error means a
**second copy of the bot is running** with the same token. It is not a code bug —
the bot now backs off for 15s and shows the conflict on `/diagnostics`, but you
need to stop the duplicate. In order of likelihood:

1. **A second Render service.** If you created the bot twice (e.g. once from the
   Blueprint *and* again as a Web Service), both poll the same token. Delete or
   suspend the extra one: Render dashboard → the other service → **Suspend**.
2. **A run on your own machine** — a local `uvicorn` you left running.
3. **A deploy still overlapping.** Render starts the new instance before stopping
   the old, so a few 409s right after a deploy are normal and clear on their own.
4. **More than one worker.** Keep the start command as `uvicorn app.main:app
   --host 0.0.0.0 --port $PORT` — no `--workers`.

Only one instance should ever run. (If you'd rather not think about it, set
`USE_POLLING=0` and the bot uses a webhook instead — Render's URL is picked up
automatically — but you must then make sure the webhook points at the one live
service.)

### The three Render gotchas, handled
1. **Ephemeral disk.** Render wipes the filesystem on every deploy/restart, so a
   local state file would lose your groups and index. Set **`GITHUB_TOKEN`** and
   state is stored in a private gist instead — durable across restarts. The
   dashboard shows a warning when storage is local-only.
2. **Sleeping.** Long polling + Telegram's update queue means wake-ups catch up.
3. **Python version.** The repo pins **`3.12.6`** via a `.python-version` file
   (Render's own default for a new service is Python 3.14, and 3.12 is the version
   this was tested on). Edit that one-line file to change it — the whole stack also
   runs on 3.13+.

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
    learn.py       the learning layer: TF-IDF model + feedback associations
    userbot.py     Telethon user account (scan + fetch files)
    telegram.py    Bot API wrapper
    db.py          durable state store
    config.py      defaults
    templates/     dashboard (no auth, incl. Learning page)
  requirements.txt
  .env.example
  .python-version      pins Python 3.12.6 for Render
  Dockerfile · docker-compose.yml
  userbot_login.py
```
