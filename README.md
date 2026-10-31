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

**`/tag` guardrails.** Capped at **100 per command** (change it in Settings → Tag
cap; 100 is the hard ceiling) and limited to one tag command per user per minute.
If you ask for more than the cap, the bot says so instead of silently dropping the
difference. Works
as `/tag @user 5`, `Bonki tag @user 5`, or reply to a user with `/tag 5`.

It also understands natural phrasing: `Bonki <anything> @user 5 times`, or reply
to someone with `… 10 times`. **Only the @mention is sent** — the bot never
repeats the wording of your message, so it can't be used to fire an insult at
someone. Commands like `/scan`, `/find` and `/list` are excluded, so "scan this
group 3 times" stays a scan.

> **Why a big tag count takes time:** Telegram allows roughly **20 messages per
> minute per group**. Bonki paces the sends and, when Telegram returns `429`, it
> obeys the `retry_after` and continues instead of giving up — so `/tag @user 69`
> genuinely delivers 69 messages, in about 3–4 minutes.

### Purge, locks and federations

| Command | Does |
|---|---|
| `/purge` | Reply to the first message → deletes everything up to your command |
| `/purge 20` | Deletes the last 20 messages |
| `/lock media\|sticker\|gif\|link\|forward\|bots\|commands` | Deletes offending messages from non-admins |
| `/unlock <type>` · `/locks` | Remove a lock · list active locks |
| `/newfed <name>` | Create a federation (you become owner) |
| `/joinfed <id>` · `/leavefed` | Attach / detach this group |
| `/fban <user> [reason]` · `/unfban <user>` | Ban across every group in the federation |
| `/fedinfo` · `/fedlist` · `/fedadmins` · `/delfed` | Inspect / manage |
| `/addfedadmin` · `/rmfedadmin` | Owner adds / removes federation admins |
| `/fedbans` | List the federation's bans |

**Purge works in forum topics.** Message ids are unique across the whole chat, so
deleting the id range from the replied-to message to your command only ever
touches messages inside that same topic.

**Federation bans are enforced automatically** — a fedbanned user who posts in any
attached group is banned there and the message removed.

### Multiple chats, auto-attach, and Rose interop

```
/joinfed 1 @chana @chanb -100123   attach several chats in one go
/fedauto on                       every NEW chat joins this fed automatically
/fbanlist csv|json|minicsv        export the ban list
/importfbans                      import one (reply to the file)
```

`/joinfed` always includes the chat you type it in, plus any you name.

**Rose interoperability — three levels, and only two are real.**

1. **Direct, without a user account: impossible.** Not a setting — a platform
   rule. Telegram rejects any bot-to-bot message with `403: Forbidden: bot can't
   send messages to bots`, and Rose publishes no API. So Bonki (a bot) cannot send
   `/newfed` or `/joinfed` to Rose (a bot) by any route. Anyone claiming otherwise
   has not tested it.
2. **File exchange (works):** Rose documents `/fbanlist` and `/importfbans`, and
   Bonki speaks both formats in both directions.
3. **Driving Rose through your account (works, with caveats):** `/rosefed` sends
   the command to `@MissRose_bot` from your connected user account and relays her
   reply — the same thing you'd do by hand.

### `/rosefed` — create and join Rose federations

```
/rosefed create my_fed        → /newfed my_fed
/rosefed join 86718661-…      → /joinfed <id>
/rosefed leave · info · admins · rules
/rosefed banlist csv          → /fbanlist csv
/rosefed raw <any command>    → escape hatch
```

Requirements and caveats, stated plainly:

- **Needs `/userbot`.** Without a connected account there is no way to talk to
  Rose at all; the command says so instead of failing silently.
- **Owner only** — it drives *your* account.
- **20-second cooldown** between commands, because Rose rate-limits aggressively
  and an account that spams her gets muted.
- **Automating another bot may breach Rose's terms.** This is your account
  running your commands, and you carry that risk. Bonki relays her text reply
  verbatim rather than parsing it, so nothing is invented if she changes wording.

### `/fedbridge` — the account-free bridge (both directions)

```
/fedbridge channel @mylogchat   where to publish Bonki's ban list
/fedbridge file                 publish it right now
/fedbridge off                  stop auto-publishing
```

- **Bonki → Rose:** every fedban **re-publishes** the ban list as a Rose-importable
  CSV into your nominated chat. Forward that file to Rose and reply
  `/importfbans` there.
- **Rose → Bonki:** forward any `*fban*.csv` or `.json` here and it imports
  itself — no command needed.

**Why the one manual hop can't be removed:** Telegram rejects bot-to-bot messages
with `403: Forbidden: bot can't send messages to bots`, and Rose has no API. Two
bots simply cannot talk to each other. A file carried by a person is the only
channel that exists. `/fedsync` (push/pull) automates that hop **only** when
`/userbot` is connected — that is the sole way to remove the tap.

### Hands-free import (no account needed)

Export from Rose once with `/fbanlist csv`, then **forward that file into a chat
that's in your federation**. Bonki recognises it and imports the bans
automatically — no command, no user account.

Safety: only the **owner or a fed admin** can trigger it, and the file name must
look like a ban list (`*fban*.csv`, `*ban*.json`, …). A random member dropping a
spreadsheet can't fedban anyone. Re-importing the same file is idempotent — it
updates the reason rather than duplicating the ban.

**File-based exchange (works both ways):

| Direction | How |
|---|---|
| **Rose → Bonki** | In Rose run `/fbanlist csv` (or `json`), then reply to that file here with `/importfbans`. Rose's CSV (`id,firstname,lastname,username,reason`) and NDJSON (`{"user_id":…}`) both import directly |
| **Bonki → Rose** | Run `/fbanlist csv` here, then in Rose reply to that file with `/importfbans` |

So ban lists move between the two in either direction, in Rose's own formats —
real interoperability via export/import, not live sync. Anything claiming live
Rose sync without their API is guessing.

**Honest scope.** This is a practical core of Rose's behaviour, not a
reimplementation of all of it. **Not included:** filters, blacklists, antiflood,
connections, captcha, nightmode, scheduled messages. Those are separate large
modules — tell me which you actually use and I'll add them next.

## Quotes & sticker packs (Stickerkang-style)

| Command | Does |
|---|---|
| `/q` `/quote` `/quotly` | Reply to a message → a rendered quote image (name, text, timestamp) |
| `/kang [emoji]` | Reply to a sticker / photo / image → adds it to **your own** sticker pack, creating the pack on first use |
| `/packs` | Link to your pack and its sticker count |
| `/getsticker` | Reply to a sticker → its raw PNG |
| `/stickerid` | Reply to a sticker → its `file_id`, emoji and set name |
| `/delsticker` | Reply to a sticker **in your pack** → removes it |

`/kang` creates a pack named `b<your_id>_by_<bot_username>` — the name Telegram
requires for a bot-created pack — and resizes any image to 512 px PNG. You can
rename and reorder it later from @Stickers.

**How a quote is rendered — matched to QuotLy.** I read the `LyoSU/quote-bot` v2
source and copied its real parameters rather than guessing:

- base canvas **512 wide**, **ink-trimmed** to the content height (so a one-line
  quote gives a small card, not a square with dead space)
- default background **`#292232`**, the dark purple-grey QuotLy uses
- the sender's name is tinted with **their own accent colour**, so different
  people get different colours, the way Telegram does it
- **no heavy date footer** on the card
- a **reply-context block** above the text when the quoted message is itself a reply
- paragraph breaks preserved exactly

Short and medium quotes go out as a **sticker**; a long one would be unreadable at
512 px, so it goes out as a full-size photo instead.

Two details that matter in practice:

- **A font is bundled** (`app/assets/DejaVuSans.ttf`), so the card looks identical
  on Render, a panel host or your own machine — no dependency on system fonts.
- **Glyphs the font can't draw are dropped.** Emoji and scripts outside the
  font's coverage (CJK, for example) used to render as empty boxes; each glyph's
  bitmap is now compared against the font's `.notdef` bitmap and removed.

**Honest scope.** Static stickers only. **Animated (`.tgs`) and video (`.webm`)
stickers are rejected** with a clear message — converting those needs extra
tooling. The quote card is a clean custom design, not QuotLy's exact layout, and
it has no background/emoji/rating options.

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

## Owner notifications and `/help`

The bot tells the owner what's happening, in DMs:

| When | What the owner gets |
|---|---|
| Bot (re)starts | `🔄 Bonki-Intelligence restarted` with time, groups, files, brain, user-account status. **Throttled to once per 2 minutes**, so a crash-restart loop can't spam the DM |
| Added to a group | `➕ Added to group “X” by Y` |
| Added to a channel | `📢 Added to channel “X” by Y` — and Bonki greets **inside the channel** |
| Removed / kicked | `➖ Removed from “X”` |

`/start` in a DM gives the owner a control summary (files, groups, brain, user
account). **`/groups`** (or `/chats`, `/channels`) lists every chat the bot is in
— split into channels and groups, each with its `@username`, a t.me link, its
file count and whether it's approved.

**DMs always get an answer.** A non-owner who messages the bot receives the
group command list plus the owner's ID; an owner gets the control panel. If a
send fails the bot retries without the threaded reply, so a DM is never
silently swallowed.

### `/help` — two different answers

- **Owner** gets the complete command list: files, moderation, chat settings,
  stickers & quotes, federations, tag.
- **Everyone else** gets the group-command set only (moderation, info, notes,
  search) plus **the real owner ID** and a line asking them to contact the owner
  for the full feature set. Owner-only commands (`/newfed`, `/fban`, `/kang`,
  `/tag`, settings) are not listed.

> The owner ID is shown deliberately, as requested. If you'd rather not publish
> your numeric ID to group members, say so and I'll show nothing or a @username
> instead.

## Remember, silent purge, dump

| Command | Does |
|---|---|
| `/remember` (also `/save`, `/learn`) | Reply to a file → it's indexed **and** Bonki learns about it in the background: keyword tags, a one-line description from the configured model, and its embedding. Silent — no follow-up spam |
| `/knowledge <query>` (also `/memory`) | Search what Bonki has learned about your files |
| `/spurge` (also `/silentpurge`) | Same as `/purge`, but posts **nothing** back — the messages just go |
| `/dump <@chat or -100…> [count]` | **Owner only.** Copies messages from the chat you type it in into the destination chat. Default 100, max 500 per run |

**`/dump` does not use the index.** You type it in the *source* chat and pass the
*destination* as the argument:

```
/dump @paypalagentchat        # copy the last 100 messages from here to there
/dump @paypalagentchat 500    # the last 500
```

Telegram gives bots no way to *list* history, but a bot that is a member can copy a
message when it knows the id — and ids are sequential. So it walks the ids
backwards from your command and copies every one that still exists, skipping
deleted ones. Nothing needs to be indexed first.

`/dump` refuses anyone but the owner and says so, because moving data between
chats is destructive-adjacent: it caps at 200 items per run, reports how many
succeeded, and tells you when the target refused (usually: the bot isn't admin
there).

## Skills and decisions (the "agent" part)

`@mira` is a commercial AI agent (1M+ monthly users) built by The Open Platform.
Its model is GPT/Claude/Qwen behind their infrastructure, and it ships 1,000+ OAuth
integrations, a TON wallet, video generation and confidential-compute "private
mode". **None of that can be cloned** — it is other people's models, credentials
and datacentres. What *can* be built is the agentic behaviour around a model, and
that is what this is.

| Command | Does |
|---|---|
| `/skill every day 09:00 summarise this group` | Runs on schedule, posts the result here |
| `/skill every mon 09:00 send the weekly digest` | Weekly |
| `/skill in 2h remind me to check the upload` | One-shot reminder |
| `/skill at 18:30 stats` | Daily-ish one-off |
| `/skill` · `/skill del N` | List · remove |
| `/decide we ship on Friday` | Record a decision |
| `/decisions` | Recent decisions for this chat |

A skill can run: **summarise**, **stats**, **digest** (recent files), **find
&lt;query&gt;**, or a plain reminder. A background loop checks every 30 seconds and
reschedules recurring skills.

**Honest scope.** Bonki has no OAuth connections, so a skill cannot "pull my
GitHub issues" or "check my inbox" — that needs Mira's integration layer, not
Bonki's. Skills work over this chat's own indexed data plus reminders, and the bot
says so instead of pretending. Likewise there is no image/video generation, no
crypto wallet, and no confidential-compute mode.

## Native channel analytics (a Telemetr.io-style engine)

Bonki computes channel analytics **itself** — no third-party API, no key. Every
number comes from what it has actually indexed plus Telegram's own member count.

| Command | Does |
|---|---|
| `/channelstat [@chat]` (or `/chstat`) | A stats card: members, files indexed, total size, 7-day growth, and files-per-day for the last 14 days — plus top types, top sharers and last activity |
| `/catalog [query]` | Every chat Bonki is in, ranked by how much it has indexed |

A background loop samples `getChatMemberCount` every 6 hours, so **growth over
time is real** rather than a single snapshot.

**What is honestly possible, and what is not:**

| Metric | Status |
|---|---|
| Members / subscribers, and their growth | ✅ real, sampled on a schedule |
| Files, total size, types, top sharers, activity per day | ✅ real, from the index |
| **Views and ER** | ❌ **impossible** — the Bot API never exposes post view counts. Any bot showing "views" without a third-party service is inventing them, so Bonki shows nothing rather than a guess |
| Audience demographics (language, premium share) | ❌ needs a user account |
| A 9-million-channel catalogue | ❌ that is Telemetr.io's own database, not something a bot can have |

## Brains: auto-hook and auto-switch

Bonki speaks to Gemini's API directly and to any **OpenAI-compatible** endpoint,
so every one of these works with no code change:

| Provider | Key | Notes |
|---|---|---|
| Gemini | `GEMINI_API_KEY` | native API |
| OpenAI | `OPENAI_API_KEY` | or any compatible base URL |
| OpenRouter | `OPENROUTER_API_KEY` | free models, e.g. `meta-llama/llama-3.3-70b-instruct:free` |
| Groq | `GROQ_API_KEY` | fast free tier |
| Ollama | `OLLAMA_BASE_URL` | local, no key, fully free and open-source |

**Auto-hook:** any provider whose key is present becomes available at once — no
provider config needed. **Auto-switch:** if the active model fails (quota, outage,
bad key), the next available one answers and the switch is remembered, so an
exhausted free quota degrades instead of taking the bot down.

## Seeing the ML — `/ml`

The ML is not meant to be taken on faith. **`/ml`** prints the live state of every
layer, and **`/ml <query>`** prints the real component scores behind a search:

```
🧠 Machine learning — live status
Brain: gemini (gemini-2.0-flash)
Embeddings (deep learning): ✅ text-embedding-004 · 128 vectors cached
Learned ranker: ⏳ 12/40 examples — tap a result button to feed it
Feedback learned from: 2 (1 ✅ / 1 ❌) · associations 1 · vocab 11
Memory records: 7

🔎 "whatsapp" — 4 file(s) in this chat
• WhatsApp Messenger 2.24.apk
   fuzzy 1.105 · tfidf 0.577 · learned 0.0 · embed 1.0 → 1.307
```

| Column | What it is | Where it comes from |
|---|---|---|
| `fuzzy` | spelling similarity | rapidfuzz, no learning |
| `tfidf` | word importance | TF-IDF built from your file names |
| `learned` | your past choices | feedback buttons you tapped |
| `embed` | **meaning** | pretrained transformer (deep learning) |
| `ranker` | **trained model** | logistic regression on your feedback |
| `→` | final blended score | the number that decides the order |

If a column reads 0.0 it means that layer contributed nothing for that query —
which is information, not a failure. And if the ranker says `⏳ 12/40`, that is the
honest state: it needs 40 examples before it switches on.

## Learning and semantic search — what it actually does

Bonki has two genuine layers, and it is worth being precise about them:

**1. Classical ML (trained here, on your data).** TF-IDF over the real file names,
captions and tags, plus association weights learned from which result you tap. It
starts empty and improves with use.

**2. A learned re-ranker (trained here, on your feedback).** Every time someone
taps the file they actually wanted, the candidate set becomes a labelled example:
feature vector with label 1 for the choice, 0 for the rejections. After 40 rows
Bonki fits a **logistic regression** by gradient descent (numpy) and blends its
probability into ranking, refitting every 10 new rows. Features: base text score,
recency, name length, caption hit, query length, times shared. Until there is
enough data it is a no-op, so a fresh install never ranks worse.

Why logistic regression rather than a neural net: sample size. With tens to
thousands of rows and six features a small linear model generalises; a net would
memorise them. The *deep* part is below.

**3. Deep learning (pretrained, used at inference).** A transformer embedding model
from your configured provider (Gemini `text-embedding-004`, or OpenAI
`text-embedding-3-small`) compares *meaning*, not just spelling. Two-stage
retrieval keeps it cheap: the text ranking picks ~30 candidates, then only those
are embedded and re-scored by cosine similarity. Vectors are cached in memory.

> **What this is not:** Bonki does not **train** a neural network. There is no GPU
> here, no labelled corpus, and training on a few hundred file names would produce
> something worse than the text matching above. It uses a pretrained model and
> never claims otherwise. If no API key is set, this layer simply switches off and
> search falls back to the classical ranking.

### The original learning notes

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

## Scanning without a user account

**A bot cannot read chat history.** That is a Telegram permission boundary, not a
missing feature: the Bot API has no `getChatHistory` method at all, and a bot only
receives messages from the moment it is added. No amount of code changes that.

So there are three ways Bonki can index a group, in order of preference:

| Situation | What works |
|---|---|
| **Public channel** (has an `@username`) | ✅ **Scanned with no account at all** — Bonki reads the channel's public web preview (`t.me/s/<name>`) and indexes the file names it finds there |
| Any group, going forward | ✅ **Live indexing** — with privacy mode off, Bonki indexes every file posted from now on |
| **Any group** (public or private), past messages | ❌ Needs the user account (`/userbot`) — groups have no web preview at all |

**Being an admin does not unlock history.** This is the part that surprises
people: admin rights change *which new messages the bot receives*, not what it can
read backwards. A bot admin gets every message from the moment it is added —
Telegram is explicit that "bot admins and bots with privacy mode disabled will
receive all messages" — but `messages.getHistory` remains user-only. No setting,
permission or workaround changes that.

**Public groups have no web preview either.** Telegram redirects `t.me/s/<group>`
to the group's info card — name, photo, member count, **not one message**. Only
public *channels* have a readable feed. Any tool claiming to show a group's full
chat without an account is not using an official Telegram feature.

Run **`/indexstatus`** in any chat to see exactly what the bot can see there: its
role, whether it receives all messages, whether live indexing is on, and how many
files it has indexed.

**What public-channel scanning can and cannot do:** the preview exposes post text
and photos but **not downloadable documents**, so Bonki indexes the *file name*
(from the document title or the caption) and links to the post. It cannot re-send
that file. It also only sees the recent window the preview exposes.

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

#### No login code arriving — the usual causes

1. **Look inside Telegram, not your SMS app.** Telegram sends the login code as a
   message from the "Telegram" service account. If you are logged in on any device,
   the SMS usually never comes — by design, not a bug.
2. **Use "Send by SMS instead"** on `/userbot`. That asks Telegram for an SMS
   explicitly; it is refused for some numbers, but it is the first thing to try.
3. **Number format:** international, with country code and no spaces —
   `+919876543210`.
4. **Two-step verification:** the code is accepted, then the cloud password is
   required too. Enter it in the second field.
5. **Rate limited:** repeated code requests get locked, sometimes for 24 hours.
   Stop retrying. The error now says exactly how many seconds to wait.
6. **Cloud host (Render / VPS):** Telegram frequently refuses to deliver a
   new-login code to a datacenter IP. This is the most common reason it works on
   your laptop but not on the server.

**The reliable fix for a cloud host:** log in on your own computer and paste the
session string. Run `python userbot_login.py` from the project, complete the login
there (the code arrives normally), and copy the long string it prints into
**Settings → Session string**. The server then never performs a login at all — it
just uses the session.

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
