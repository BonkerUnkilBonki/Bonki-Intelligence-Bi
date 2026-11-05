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

**`/tag` — no cap.** Whatever number you say is the number sent
(`/tag @user 500`). Only a one-per-minute cooldown per user remains, and the
per-group Telegram limit (~20 messages/minute) still paces the sending, so a large
number takes proportionally longer.

**Wording that triggers a run.** The sentence must contain a tag word
(`tag`/`mention`/`ping`) plus a number, or an `@username` plus a number:

| Message (replying to someone) | Result |
|---|---|
| `Bonki tag him 69 times` | tags 69 times |
| `Bonki mention @saptak 5x` | tags 5 times |
| `Bonki fuck him 69 times` | **does not tag** — says hello |
| `Bonki tag him 69 times` (no reply, no @) | asks who to tag |
| `Bonki find netflix 5 times` | searches, does not tag |

That first-vs-third distinction matters: any sentence ending in "N times" used to
start a run, so an ordinary message could fire 69 mentions, hit Telegram's flood
limit and leave the bot silent for minutes.

**Rate limits are handled, not lost.** Telegram allows ~20 messages/minute per
group. A 429 used to raise an exception and the reply simply vanished — the bot
looked dead for the whole restriction window while it was running fine. Sends now
wait out `retry_after` and retry, and a large tag run pauses while the bot is
restricted instead of burning attempts on guaranteed failures. Anything over 200
tags warns you up front how long it will take and that other replies will stall.

**Stopping a run:** while tagging is in progress the **owner** can say `stop` or
`bonki stop` (or `/stop` with no argument) and it halts within a second, replying
`⏹ Stopped after N of M tags`. Members cannot stop it. This works because the tag
loop runs as a background task — previously the bot was blocked inside the command
and could not even receive the stop message.

Works
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

### Bonk aliases (from UnkilBonker)

```
/bonk  = /ban      /fbonk = /fban      /bonki = /warn
```

### Ported from BonkBot

Bonki absorbed the features from the earlier BonkBot project that it did **not**
already have. Everything below is new here; nothing was duplicated.

| Command | Does |
|---|---|
| `/filter hi Hello` · `/stop hi` · `/stopall` · `/filters` | Auto-replies on a trigger word. Supports `prefix:` and `exact:` modifiers, or reply to a message to use it as the reply |
| `/afk eating` | Auto-replies when someone pings or replies to you, and clears itself when you return |
| `/tagall hello` · `/utagall` | Mentions **everyone** — including silent members — when `/userbot` is connected. Without it, admins + members seen speaking |
| `/disable purge` · `/enable purge` · `/disabled` | Per-chat command toggles |
| `/gban` · `/ungban` · `/gbanlist` | **Owner only** — ban across every chat Bonki is in, enforced on sight |
| `/export` · `/import` | Move a chat's settings, notes and rules to another group as JSON |
| `/promote` · `/demote` · `/settitle` · `/setdesc` · `/ping` | Admin conveniences |

**How `/tagall` reaches everyone.** The Bot API has **no member-list method** —
`getChatMembers` does not exist — so a bot alone can only reach admins (via
`getChatAdministrators`) and people it has watched speak. `/tagall` therefore uses
the best source available and tells you which one it used:

| Source | Who gets mentioned |
|---|---|
| `/userbot` connected | **every member**, including those who never spoke (up to 1000) |
| no user account | admins + members seen speaking |

Bots are excluded, long lists are split across messages to stay under Telegram's
4096-character limit, and there's a 60-second cooldown so it can't be used to spam.

**Already present, so not ported:** moderation (ban/kick/mute/warn/purge/pin),
locks, notes, rules, welcome, federations, captcha, blocklist, antiflood,
stickers/kang, quotes, tag, scheduled skills, analytics, `/ask`, dump/mirror and
owner approvals.

**Not ported (deliberately):** `memefi` (image meme generator), `rewriter`
(AI-rewrites channel posts), anti-raid thresholds, captcha modes, and the
`connect`/`disconnect` chat-linking commands. Say which you want and I'll add them
— I left them out to keep this change reviewable rather than dumping 60 commands
in at once.

### `/config` — per-chat module switches (inside Telegram)

When two bots sit in the same group they duplicate each other: two bans, two
welcomes, two captchas. `/config` (admins only) opens an inline panel and lets you
switch Bonki's modules off for that chat alone.

```
⚙️ Bonki modules for this chat
✅ Moderation      ✅ Welcome / goodbye
✅ CAPTCHA         ✅ Antiflood
✅ Blocklist       ✅ Locks
✅ Filters         ✅ Notes / rules
✅ File search     ✅ Tagging
✅ Federation      ✅ Chat replies
✅ Dump / mirror   ✅ Scheduled skills
[🔄 Turn all ON] [⛔ Turn all OFF] [🩺 Check permissions]
```

Tap any module to toggle it. Turning **Moderation** off stops `/ban`, `/kick`,
`/mute`, `/warn`, `/purge` and `/pin` in that chat — while search, notes and
everything else keep working. Nothing else in the bot is affected, and no other
chat is touched.

Every automatic behaviour respects these switches too: welcome, CAPTCHA,
antiflood, blocklist, locks, filters, federation enforcement, live indexing,
mirroring and chat replies.

### `/permissions` — what the bot is missing

Reports the bot's role in the chat and every admin right it lacks, with what each
one enables:

```
🩺 Admin ✅ but these rights are missing:
• can_restrict_members — mute/kick (mute, warn escalation, antiflood, CAPTCHA)
• can_change_info — set title/description
```

**A bot cannot grant itself admin rights** — Telegram requires a human admin to
promote it. What Bonki does instead: it says exactly what's missing, and when it is
added to a channel it posts a setup note telling you to promote it and run
`/permissions`.

### Anti-spam: CAPTCHA, blocklist, antiflood

The three protections every serious group bot ships — all local, no external
service.

| Command | Does |
|---|---|
| `/captcha on\|off` | New members are **muted on join** and must tap a verify button. Unverified members are kicked after 5 minutes |
| `/addblocklist <word>` · `/rmblocklist <word>` · `/blocklist` | Case-insensitive word filters. Admins are exempt |
| `/blocklist mode mute 60` | Delete only (default), or also mute for N minutes |
| `/setflood 5 10` · `/setflood off` · `/flood` | Mute after **5 messages in 10 seconds** |

**CAPTCHA is the one that matters.** Every other protection reacts to messages
that have already been posted; CAPTCHA acts at the join, so a bot never gets to
post at all. It needs *restrict members* and *ban users* rights — without them the
mute silently fails and Bonki logs it.

The button is tied to the joiner's own account: anyone else tapping it is told
"That button isn't for you", so it can't be used to verify someone else in.

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

### Full two-way sync with Rose (needs `/userbot`)

With a user account connected, Bonki and a Rose federation share **one live ban
list** in both directions:

**Sync is ON by default — there is nothing to switch on.** As soon as a user
account is connected, the loop runs every 5 minutes with no user action:

- queued Bonki bans are pushed to Rose
- Rose's ban list is pulled every 10 minutes and imported
- a `/fban` pushes **immediately**, not on the next cycle

```
/fedsync                       status: on/off, queued, last pull
/fedsync push                  force a push now
/fedsync pull                  force a pull now
/fedsync sub <rose_fed_id>     subscribe this fed to ANOTHER Rose fed
/fedsync unsub <rose_fed_id>   unsubscribe
/fedsync off                   stop (set "0"; on is the default)
```

What happens automatically:

| Direction | Mechanism |
|---|---|
| **Bonki → Rose** | A `/fban` here is queued and pushed **immediately** to Rose as `/fban <id> <reason>` from your account |
| **Rose → Bonki** | Bonki asks Rose for `/fbanlist json`, downloads the file she replies with, and imports it — on the 15-minute loop or on `/fedsync pull` |
| **Enforcement** | An imported Rose ban is applied **the moment that user speaks** in any chat in your federation: banned and the message deleted |

**Crossing between Rose federations** is `/fedsync sub <id>`, which drives Rose's
own `/subfed`. Note what that does: it makes Rose enforce *their* bans, it does not
merge the two ban lists.

**The limit that remains:** all of this runs through your user account, because
Telegram forbids bot-to-bot messages (`403`). If the account disconnects, syncing
stops — `/fedsync` shows `user account: ❌ needed` when that happens.

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
/dump @paypalagentchat live   # mirror every NEW message, continuously
/dump off                     # stop the live mirror
/dump                         # show whether a mirror is running
```

**Live mode** keeps copying as messages arrive, so the destination becomes a real
mirror of the source. Backfill the past with a one-off dump, then switch to live:

```
/dump @paypalagentchat 500    # history
/dump @paypalagentchat live   # and everything after
```

**Messages are forwarded, not re-authored.** The destination shows
*"Forwarded from …"* with the original sender, instead of every message appearing
to come from Bonki. If a message can't be forwarded (channels with restricted
saving), Bonki falls back to copying that one message so the mirror doesn't stall.

The bot must be able to read the source and post in the destination. A message it
cannot send either way is logged and skipped.

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

## ML and deep learning — on by default, no API needed

Bonki's learning layers work **out of the box with no key and no download**:

| Layer | Runs without a key? | What it is |
|---|---|---|
| TF-IDF ranking | ✅ | classical ML over your file names and captions |
| **Local embeddings** | ✅ | hashed word + **character 3-gram** vectors, IDF-weighted, with **LSA (truncated SVD)** once the corpus passes ~25 files |
| **Intent net** | ✅ | a **numpy MLP** (512→48→9) with real backprop, trained at startup on ~90 labelled phrasings — reads *what you're asking* when no LLM key is set |
| **Category net** | ✅ | a second numpy MLP (512→40→10) that sorts file names into app / game / mod / ebook / movie / music / archive / document / image — powers `list games`, `how many ebooks` |
| **Duplicate finder** | ✅ | cosine similarity over the local vectors — `/dupes` groups the same app at different versions |
| **Implicit learning** | ✅ | if nobody rejects a result within 3 minutes, it counts as a weak positive, so the ranker learns from normal use, not just taps |
| Learned ranker | ✅ | logistic regression — starts from **hand-set prior weights** so it is on from the first search, then replaces them with real training after **12 feedback taps** |
| Pretrained transformer | ⬆️ optional | if you add `GEMINI_API_KEY` / `OPENAI_API_KEY`, embeddings switch to a real transformer; everything else stays the same |

**What the local embeddings actually are, plainly:** unsupervised learning on your
own corpus, in numpy. Character 3-grams mean it still matches `whatsap` to
`WhatsApp`, `spotfy` to `Spotify`, and glued names like `whatsappmod`. LSA then
compresses those vectors so words that appear together end up near each other.

**The intent net is a real neural network** — forward pass, backprop, SGD and
cross-entropy, all hand-written in numpy, no PyTorch or TensorFlow. It is trained
from scratch on your machine at first use (under a second, ~40k parameters) and
classifies phrasing that keyword matching misses:

```
"can you pull the spotify one"  -> find     1.00
"what did i miss"               -> summarise 0.99
"who shares the most"           -> stats    1.00
"hello there"                   -> unknown  1.00
```

Its train accuracy is 100% on those templates, which also tells you the honest
limit: it generalises to *similar phrasings*, not to arbitrary language, and it
cannot write a sentence. Anything generative still needs an LLM or the built-in
templates.

**What they are not:** a neural network. There are no pretrained transformer
weights here, because shipping one means either a ~100 MB download or an API call.
If you want true transformer semantics, add a key and it upgrades itself — the
local path stays as the fallback.

## Answering questions from the group (`/ask`)

`/ask` — or just asking a question in chat — answers from **this group's own stored
facts**: files, sizes, dates, top sharers, decisions and learned notes.

```
/ask who shares the most   -> Most active sharers: Alice (3)
/ask how many files        -> 3 file(s) indexed, 102 MB in total
/ask latest file           -> The newest file is WhatsApp 2.24.apk, shared 31 Oct
/ask biggest file          -> The biggest file is GTA San Andreas.apk (85.8 MB)
/ask what did we decide    -> • we ship on Friday — 31 Oct 2026
```

**Two modes, and it tells you which one answered:**

- **With an LLM key** — retrieval-augmented generation. The retrieved facts go into
  the prompt and the model writes the answer. This is the good path.
- **Without a key** — pattern-matched answers computed directly from the stored
  data, labelled *"answered from what I have stored — no model, no guessing"*.

**About "cloning Mira's database": that isn't possible and Bonki doesn't try.** It
is 1M+ users' private data held by a company; there is no public export, and
replicating it would be a privacy violation. What Mira actually does *inside your
group* is remember that group — which is exactly what `/ask`, `/decide`,
`/knowledge` and the memory store do here, from your data only.

## Talking to it like a person

Ordinary chat no longer gets answered with a menu about indexing:

```
Bonki hello         -> Hey 👋 Ask me for a file any time
Bonki how are you   -> Running fine — indexing and ready. What do you need?
Bonki thanks        -> Any time 👍
Bonki who are you   -> I'm Bonki — a bot that indexes files here. Not a person.
Bonki <anything rude> -> I'll pass on that one. If you want something from the
                         index, say Bonki find <name>
```

Two rules make that work:

- **Only real file shapes become a search.** Previously any two content words did,
  so "how are you" and worse were treated as file lookups and answered with the
  "nothing indexed" wall of text. Now it needs an extension, a search verb, or a
  confident classifier verdict.
- **Short messages get a human reply first**, before the intent dispatch, so a
  greeting never lands on the help menu.

**Honest limit:** this is a set of rules, not a language model. It handles
greetings, thanks, identity and rudeness well, and gives a short honest line for
anything else. For real conversation, connect an LLM — Bonki then prefers it and
falls back to these rules offline.

## Replies that fail are queued, not lost

When Telegram rate-limits the bot, a send raises 429 and the reply used to be
thrown away — so the bot looked **completely dead** even though the search had
worked and the answer existed.

Now a refused reply goes to an **outbox** that retries every 45 seconds for up to
30 minutes, and the owner gets one DM saying so (a DM is a different chat, so a
group restriction does not block it):

```
⚠️ Telegram is rate-limiting me
• 3 repl(y/ies) queued, not lost
• I will send them as soon as the limit lifts
• cause: usually a very large /tag run

I'm still running — this is a delivery limit, not a crash.
```

Verified: with every send refused, the reply was queued (`queued: 1`); once the
restriction lifted, `drain_once` delivered it and the queue emptied.

`/diag` also reports the current restriction and queue size.

## Running without admin rights

**In a group, most of what matters needs no admin at all.** Bonki checks once and
runs in read-only mode instead of firing API calls that are certain to fail:

| Works without admin | Needs admin |
|---|---|
| search and share files | ban · kick · mute · warn |
| indexing new files* | purge · pin |
| notes, filters, `/tag`, `/diag` | locks · CAPTCHA · antiflood · blocklist |
| federation *lookups* | applying bans |

\* only if privacy mode is off — see below.

`/setup` explains the rest, and `/diag` shows `mode: 🔓 read-only (not admin)`.
Moderation commands now answer *"needs me to be an admin here"* instead of failing
silently.

### The privacy setting that actually matters

With **privacy mode ON** (Telegram's default), a bot in a group only receives
commands and replies — **it never sees the files people post**, so it cannot index
them. Fix it in [@BotFather](https://t.me/BotFather):

```
/setprivacy → choose your bot → Disable
then remove and re-add the bot to the group
```

This is the single most common reason a file bot "finds nothing".

### Channels are different

A bot **must** be an admin with *Post messages* to send anything in a channel.
That is a Telegram rule, not a Bonki limitation — no bot can post in a channel as
a plain member. `/setup` says so plainly when run inside a channel.

### `/leech reset` — start completely over

Deletes **everything** the leech side stores, behind a confirmation button:

```
🧨 Reset the leech bot?

This deletes everything the leech side has stored:
• every watched repo and route
• the record of which releases were already posted
• cached GitHub data and rate-limit state
• all leech settings, and it switches chats back to Find mode

Messages already posted in your channel are not deleted.

[🧨 Yes, reset everything]  [Cancel]
```

Clears: `leech_routes`, `leech_state`, `leech_repo`, `gitly_repo`,
`gitly_channel`, `gitly_on`, `gitly_interval`, `gitly_prerelease`, `gitly_seen`,
`gitly_etags`, `gitly_last_check`, `gitly_last_error`, `gitly_backoff_until` — plus
every chat's mode back to **Find**.

Owner only. Verified: the confirm appears before anything is deleted, Cancel leaves
everything intact, confirming clears all of it and switches the mode back, and a
non-owner is refused.

### Why a removed repo could keep posting (fixed)

There used to be **two independent posting paths**: the route list, and an older
single-channel setting (`gitly_channel` + `gitly_repo`). Removing a route cleared
the first but left the second running, so a repo you had removed kept publishing.

Now there is **one mechanism — routes**. The background loop reads only the route
list; if that list is empty it does nothing at all, whatever else is saved.
`/gitly channel` creates a route too, so it can be removed the same way.

Verified: with a route, releases post; after removing it, the loop runs and posts
**nothing** even though the old channel/repo settings are still present.

### Removing a repo

```
/leech                     see what is being watched
/leech remove              remove everything in THIS chat
/leech remove owner/name   remove that repo everywhere
/leech remove all          wipe every route
```

A repo link works here too (`/leech remove https://github.com/owner/name`). The
reply tells you what is left, and when nothing remains it says the watcher has
nothing to do and points at `/mode find` if you want the chat to be a find bot
again.

Verified: removing by chat clears only that chat's routes, removing by name clears
that repo everywhere, and the confirmation reports the remaining count.

### No API, no rate limit — it reads the public pages

GitHub publishes releases as ordinary web pages, so Bonki no longer calls the API
at all for releases:

| Reads | From |
|---|---|
| release list | `/OWNER/REPO/releases.atom` |
| files of a release | `/OWNER/REPO/releases/expanded_assets/TAG` |
| the file itself | `/OWNER/REPO/releases/download/TAG/FILE` |
| recent commits | `/OWNER/REPO/commits.atom` |

That is the same information you see when you open the repo in a browser — **no
token, no API quota, no rate limit**. The API is now only a fallback, and only when
a token is configured.

Verified against a real repo: **all three assets found** — a 102 MB `.tar.gz`, a
107 MB `.zip` and a 2.09 MB `.apk` — with correct sizes, and all three delivered
(two via the user account, one via the bot).

Two parsing traps fixed along the way: asset sizes were being read out of the
**sha256 hash** (`…12502974be6af…` looked like "12502974 bytes"), and the release
list needs the Atom feed rather than the HTML page.

### Files over 50 MB — the user account lifts the limit

**50 MB is a Bot API limit, not a Telegram limit.** A bot can upload 50 MB; a
**user account (MTProto) can upload 2 GB**. Since Bonki has your account connected,
a release asset over 50 MB is now uploaded **through your account** instead of being
reduced to a download link.

| Asset size | Who uploads it |
|---|---|
| ≤ 50 MB | the bot (normal post, shows "Bonki") |
| > 50 MB | your user account (shows **your** name) |
| > 50 MB, no account connected | a notice with the size and a download link |

Verified: a 54 MB APK went through the user account to the right topic while a 3 MB
zip went through the bot in the same release; with no account connected the bot
posted the size and link instead.

**Two trade-offs to know:**
- The big file is sent by **your account**, so it appears with your name, not the
  bot's. There is no way around that — the 50 MB ceiling belongs to the bot API.
- Deleting one later needs the account too, which Bonki handles automatically.

### All files, and edits that reach Telegram

Three real bugs lived here:

1. **Only the first `.apk` was sent.** A release with `Gitly.apk`, `gitly-linux.gz`
   and `gitly-win.zip` produced one upload — or none at all if there was no `.apk`.
   Now **every asset** is uploaded: apk, zip, gz, tar, anything.
2. **A release published before its files were attached was marked "seen"** and
   skipped forever. Now an asset-less release is **not** marked done — it waits and
   retries every poll until the files appear.
3. **Edits never reached Telegram.** Renaming or replacing a file, or changing the
   changelog, did nothing.

**Telegram cannot rename a file inside a message** — there is no API for it — so
Bonki does the closest correct thing automatically:

| What you changed on GitHub | What happens in Telegram |
|---|---|
| Added files to an existing release | the new files are uploaded |
| Renamed / replaced an asset | **old posts deleted**, the new set uploaded |
| Edited only the changelog | **captions edited in place** — files kept |

Each release keeps its own record (asset fingerprint + message ids), so a rename is
detected as a change rather than ignored, and an unchanged release costs nothing.

Verified: empty release → `waiting`; files added → **all three uploaded**; no change
→ nothing; rename → old messages deleted and all three re-uploaded; changelog edit →
captions updated with no re-upload.

**One limitation to know:** a bot can delete its own messages freely for 48 hours;
older ones need the *Delete messages* admin right. Bonki has that as an admin, but if
you ever remove it, an old post may remain beside the new one.

### Pasting a repo link works

`/leech repo` accepts anything you paste — a browser link, a clone URL, or a bare
name:

| You send | Used as |
|---|---|
| `https://github.com/SwitchToMonk/Gitly.git` | `SwitchToMonk/Gitly` |
| `github.com/SwitchToMonk/Gitly/releases` | `SwitchToMonk/Gitly` |
| `git@github.com:SwitchToMonk/Gitly.git` | `SwitchToMonk/Gitly` |
| `SwitchToMonk/Gitly.git` | `SwitchToMonk/Gitly` |

Using the URL verbatim was why a public repo answered **"not found"** — GitHub was
asked for a repo literally named `https://github.com/…`.

If a name still 404s, Bonki asks GitHub for that owner's repos and suggests the
closest match:

```
⚠️ SwitchToMonk/Gity — not found

Did you mean: SwitchToMonk/Gitly
```

### Switching an existing channel to Leech

A channel added before this existed has no mode set, so it defaults to Find. Three
commands, all sent **in the channel**:

```
/mode leech
/leech repo SwitchToMonk/Gitly
```
then open the topic you want and send:
```
/leech here release
```

That's it — it's live. Anything new is pushed automatically.

**Note:** in a channel a post has no author, so the admin check used to fail and
every command answered "Admins only". Channel posts are now trusted, because only
an admin can post in a channel at all.

## Two bots in one — Find and Leech

The same bot runs as either mode, **per chat**:

| Mode | What it does |
|---|---|
| **🔎 Bonki Find** | everything it already did — index, search, share, moderate |
| **📦 Bonki Leech** | watch a GitHub repo and dump every new release into a channel **topic** |

**When the bot is added to a chat it asks which one you want**, with buttons:

```
🤖 How should I work here?
🔎 Bonki Find      📦 Bonki Leech
```

Change it any time with `/mode` (in a group) or by DMing the bot. A chat in Leech
mode stops answering file searches — the two roles don't bleed into each other.

### Leech: repo → topic

```
/leech repo SwitchToMonk/Gitly     what to watch
/leech here                        run this INSIDE the topic you want
/leech                             status: routes and watcher state
/leech remove <repo>               drop a route
/leech off                         pause
```

A repo produces more than releases, so **each route picks which activity it
wants** and where it goes:

| Type | What it posts |
|---|---|
| `release` | the APK + full changelog, tag, 🟢 Stable / 🟠 Pre-Release |
| `commit` | `🔨 New commit abc1234 — message, by author, link` |
| `issue` | `🟢 Issue #7 · open — title, body, link` (PRs included) |

```
/leech here release     ← run INSIDE the releases topic
/leech here commit      ← run INSIDE the commits topic
/leech here issue       ← run INSIDE the issues topic
```

Each route stores its own repo, event type and `thread_id`, so one repo can feed
three topics with three different kinds of update:

```
Routes
• 🚀 Releases · SwitchToMonk/Gitly → Gitly Support · topic 42
• 🔨 Commits  · SwitchToMonk/Gitly → Gitly Support · topic 43
• 🐞 Issues   · SwitchToMonk/Gitly → Gitly Support · topic 44
```

Verified: all three routes posted to their own topic — the APK to 42, the commit to
43, the issue to 44 — and each item posts **once per route**.

### Leech mode is exclusive — everything else is dead

In a Leech chat the bot is a release feed and nothing else. A hard allowlist gates
every command:

**Allowed:** `/leech` · `/mode` · `/diag` · `/setup`

**Dead:** everything else — `/ban`, `/kick`, `/mute`, `/warn`, `/purge`, `/tag`,
`/lock`, `/welcome`, `/captcha`, `/filters`, `/notes`, `/stats`, `/find`, `/web`,
`/config`, `/keys`, `/help`, and every other Rose command. Plain messages and
`Bonki find …` are ignored outright.

Also stopped in a Leech chat: indexing, searching, sharing, moderation, welcome,
CAPTCHA, antiflood, blocklist, locks, filters and chat replies.

Verified in **both a channel and a group**: 15 different non-leech commands produced
no response at all, `/leech` still answered, plain messages were ignored, and
`/mode find` switched the chat back to a working Find bot immediately.

## `/releases` — every GitHub release as a menu you can tap

A paged list of **every release in the watched repo**, with its APK size. Tap one
and Bonki downloads it and sends the file to you in the DM:

```
📦 SwitchToMonk/Gilty — 15 release(s)
Tap one and I'll send its APK here.
Gitly 1.1 (v1.1) · 6.0 MB
Gitly 1.2 (v1.2) · 7.0 MB
…
[1/3]  [Next ➡️]
```

```
/releases        (aliases: /apks, /oldfiles)
```

- Shows the **release type** on every line: `🟢 Stable` or `🟠 Pre-Release`.
- Lists **all** releases, newest first, 6 per page.
- Each release has **two buttons**:

```
Gitly 1.1 (v1.1) · 6.0 MB
[📥 send to me]  [📢 to channel]
```

  **📥 send to me** delivers the APK to your DM. **📢 to channel** posts it to the
  channel set with `/gitly channel` — so you can re-publish an old release without
  touching GitHub.
- If no channel is set, the channel button says so instead of failing.
- Releases with no APK show `· no APK`; tapping says so.
- Anything over **50 MB** is not downloaded — you get the size and a direct
  download link, because Telegram's bot upload ceiling is 50 MB.
- The list is cached for 5 minutes, so paging doesn't re-hit GitHub.

Verified: 15 releases paged correctly, tapping `v1.3` delivered `gitly-1.3.apk`,
and a 200 MB asset was refused with the link instead of a failed upload.

## `/files` — browse and pull any indexed file from a DM

A paged menu of everything indexed, as buttons. Tap one and Bonki copies it to you:

```
📂 Files — 20 file(s)
1. WhatsApp 2.24.apk
2. Netflix 8.1.apk
…
[1/3]  [Next ➡️]
```

- **Paged**, 8 per screen, with Prev/Next.
- **Searchable**: `/files netflix` filters the list.
- **In a group** it lists that group's files; **in a DM** it lists everything you
  are allowed to see.
- Tapping copies the real message, so the file arrives in the DM without a
  "Forwarded from" header.

**Privacy is enforced, not implied.** A file is only listed *and* sent if the
person asking is a member of the chat it came from (or is the owner). Membership is
checked with the Bot API at the moment of the tap — not cached — so leaving a group
revokes access immediately.

Verified: the owner saw all 6 files; a member of one group saw only that group's 3
and the other group's files were hidden; a stranger's tap was refused with
*"You're not in that group, so I can't send it."*

## `/gitly` — GitHub releases → Telegram (ported from Gitly-Leech-Bot)

Every time you publish a release on your repo, Bonki posts the **changelog** to a
channel and attaches the release **`.apk`** as a document.

Default repo is **SwitchToMonk/Gilty**; change it any time.

```
/gitly                          status: repo, channel, on/off, last check
/gitly channel @BonkyFinds      where to post (or a numeric id)
/gitly repo owner/name          what to watch
/gitly on  ·  /gitly off
/gitly check                    look for releases right now
/gitly prerelease on|off
/gitly token ghp_…              raises the GitHub limit 60 → 5000/hour
```

### "chat not found" on a channel that clearly exists

`@username` is not always the channel's real username. The link shown in Channel
Info (`t.me/GitlySupport`) is often a **custom invite link**, so sending to
`@GitlySupport` returns `chat not found` even though the bot is an admin there.

Bonki no longer gives up on that. When the configured target won't resolve it
**falls back to the numeric ids of channels it is already in**, sends the post, and
**corrects the stored setting**:

```
1) configured (broken): @GitlySupport
2) 'to channel' -> sent to: -1001234567890
   auto-corrected to: -1001234567890
```

The error message now names the real cause too — "that address doesn't exist for
me" instead of blaming permissions.

### One post, not two

The release used to arrive as a text message (header + changelog) **and then** the
APK with its own caption. Now it is a **single post**: the file, with the whole
release as its caption.

```
🚀 Gitly
Tag: v6.19 · 🟠 Pre-Release

- Updated settings, including links, a Support tab…
- Improved image handling with folder galleries…
- Fixed the owner/repository ".git" crash.
…

https://github.com/SwitchToMonk/Gitly/releases/tag/v6.19
```

**The constraint:** Telegram caps a caption at **1024 characters**, and real
changelogs are longer. So a long one is trimmed in the **middle** — the header and
the link always survive, and a `…` marks the cut. Verified: a 39-line changelog
became a 969-character caption with header, notes and link intact, and **zero
separate text messages**.

A release with no APK is still a plain text post, because there is no file to
attach it to.

### Release captions include the changelog

Tapping 📥 or 📢 now sends the APK with the full release notes attached:

```
Gitly (v3.94) · Stable

• Fixed login
• Faster builds

https://github.com/SwitchToMonk/Gitly/releases/tag/v3.94
```

Previously the caption was just the tag and a link. Capped at Telegram's 1024-
character caption limit.

### "No releases found" when releases exist

A bug in the ETag caching: `304 Not Modified` means *"nothing changed"*, not
*"there are none"*. The watcher polls every minute and keeps the ETag fresh, so
opening `/releases` right after a poll got a `304`, the code turned that into an
empty list, and the menu said **"No releases found"** while releases were sitting
right there.

Fixed: a `304` now keeps whatever is already cached, and a cold cache with a stale
ETag refetches without it. Verified — first load shows the release, a following
`304` still shows it, and a cold cache recovers too.

### `/gitly debug` — what GitHub actually answered

If the release list ever looks wrong, this prints the raw facts instead of a guess:

```
🔍 GitHub probe

repo: SwitchToMonk/Gitly
url: https://api.github.com/repos/SwitchToMonk/Gitly/releases?per_page=5
HTTP: 200
releases returned: 5
• v6.19  draft=False pre=True assets=1
• v6     draft=False pre=True assets=1
• v5.60  draft=False pre=True assets=1
• v4.90  draft=False pre=True assets=1
• v3.94  draft=False pre=False assets=1
token in use: no
```

`/releases` also **self-heals**: if the first read comes back empty it clears the
cache and ETag and asks GitHub again, and only if that is still empty does it show
the probe output.

### Auto-push

**Setting the channel turns auto-push on.** No separate `/gitly on` needed — the
moment a channel is chosen, Bonki checks GitHub immediately and then keeps watching:

```
📢 Releases will be posted to Gitly (-1001234567890)
✅ Auto-push is on — checking GitHub now…
📦 Posted 1 release(s)
```

From then on, publishing a release is the only action required: it appears in the
channel within about a minute. Verified end-to-end — a new release was posted with
its APK attached, with **no command run at all**.

`/gitly off` pauses it; `/gitly on` resumes.

### Setting the channel without guessing

`@username` often fails with `chat not found` — the invite link shown in the
channel (`t.me/SomethingSupport`) is frequently a **custom invite link**, not the
channel's real username.

So `/gitly channel` with **no argument** lists the channels Bonki is already in,
with buttons. Tap one and it stores that channel's **numeric id**, which always
resolves:

```
📢 Which channel should I post releases to?
[Gitly]
```

Setting a name is still validated up front: if Telegram can't resolve it you get the
reason and the picker, instead of a silent failure at post time.

**How fast:** `/gitly on` checks **immediately** and tells you what it posted.
After that it polls every **60 seconds** (`/gitly interval 60`, minimum 30s) — and
because unchanged polls return `304` they cost no GitHub quota at all.

If GitHub ever does push back, Bonki **backs off for 15 minutes** instead of
hammering it, so a 1-minute poll can never turn into a ban. A manual
`/gitly check` still forces a fetch.

**Pre-releases are posted too** — on by default. Turn them off with
`/gitly prerelease off`. Verified: a `v2.0-beta` pre-release was announced with its
APK attached.

**First run does not spam the channel.** If you have a backlog of releases, only
the **newest** is announced; the older ones are marked as seen. Verified with six
existing releases: one posted, five skipped.

Anything new after that is posted **once** — release IDs are remembered across
restarts, so a redeploy never re-posts.

If a post fails, the reason is stored and shown in `/gitly` (`chat not found`,
`not enough rights`, and so on) instead of vanishing into the log.

Verified with three releases: the one with a 12 MB APK posted the changelog and
attached the file; the 120 MB one posted the changelog and a download link instead
of a broken upload; the one with no APK posted the changelog only; and a second run
posted **nothing** (dedupe).

**Two limits that are Telegram's, not the code's:**
- A bot can only upload **50 MB**. A larger APK is announced with a download link —
  no bot can attach it.
- The channel must have the bot as an **admin with Post messages**.

**No token is needed for a public repo.** GitHub allows 60 requests/hour to
anonymous callers, and Bonki polls only 6 times an hour. It also sends
`If-None-Match`, and a `304 Not Modified` reply **does not count against the
limit at all** — so steady polling is effectively free.

`/gitly token` is optional, and only useful in two cases: the host's IP is shared
(Render and similar) and something else has already used the hourly quota, or the
repo is private.

## Adding the bot to a new chat — it just works

**Approval is OFF by default.** The bot works in whatever chat it is added to, with
no tapping. Existing installs are migrated once on startup, so a deployment that
previously had the gate on is switched off automatically.

The gate is still available if you want it (Settings → Require approval). When on,
`/pending` lists waiting chats and `/approve <chat_id>` verifies them.

**Remove and re-add** now resets the chat's flags (`enabled`, `approval_notified`).
Before, a removal left `enabled=0` behind, so the bot came back but stayed silent —
and because it was still marked approved, it never asked again. Verified: add →
works, remove, re-add → works.

## Channels (public and private)

Two bugs made a **private channel** look dead while a public one worked:

1. **Channels were excluded from command and reply handling.** The dispatch only
   ran for `group` and `supergroup`, so in a channel the bot indexed files and
   never answered anything — `/diag` included. Channels are now included.
2. **A permission failure was silent.** A bot can only post in a channel as an
   **admin with "Post messages"**; without it Telegram returns `403` and the send
   simply failed. Bonki now DMs the owner once an hour per chat:

```
🚫 I can't post in a chat
• chat: -1002222222222
• reason: I'm not an admin there

In a channel I must be an admin with 'Post messages' — a private
channel especially, since there is no username to invite me by.
Fix: Channel → Administrators → Add → give me Post messages.
```

Verified: `Bonki find poweramp` and `/diag` both answer in a channel, and a
simulated 403 produced exactly one owner DM (repeats suppressed for an hour).

## `/diag` — "why isn't it working in this chat?"

Run it in the chat that seems dead. One screen, no guessing:

```
🩺 Bonki diagnostics for this chat

• chat: -1001234567890 (supergroup)
• approved: ❌ NO — owner must approve
  ⛔ This is why I stay silent.
• paused: ✅ no
• admin: ✅ (missing: can_restrict_members)
• modules off: none
• files indexed here: 2702
• AI: gemini / gemini-2.0-flash
• user account: ✅ on
• wake words: bonki
```

It works **even in an unapproved chat** when the owner asks — that is precisely
when it is needed. Members cannot run it.

## "Find" doing nothing (fixed)

A trained intent model could label **"find music"** as *chat*, so the bot answered
conversationally instead of returning the file — indistinguishable from "search is
broken". Two fixes:

- **Explicit search words now outrank the classifier.** `find`, `search`, `get`,
  `send`, `give`, `download`, `show`, `share`, `list`, `scan` force the search
  path regardless of what the model guesses. An extension in the text already did.
- **The wake word is stripped from the query**, so `Bonki find music` searches for
  `music` instead of `Bonki music`.

Verified with a deliberately hostile classifier returning `chat` at 0.95 for every
input: `find music`, `search netflix apk` and `get me spotify` all reached the
search path, while `hello how are you` and `who shares the most` stayed
conversational.

## Fewer wrong files: confidence gating

The single biggest cause of "it sent the wrong file" was not bad ranking — it was
**certainty**. The bot shared whatever came first even when three files were
equally plausible.

Now, before sharing, it checks two things:

- is the top score high enough (`≥ 0.95`)?
- is the runner-up at least **18% behind**, relatively?

If either fails — and the query is not a full name match — it **shows the
candidates with links and buttons instead of guessing**:

```
🔎 Not sure which one you mean by "whatsapp":
1. WhatsApp Messenger 2.24.apk — 48 MB
2. WhatsApp Business 2.23.apk — 51 MB
3. WhatsApp Mod Gold.apk — 44 MB
[1] [2] [3] [❌ None of these]
```

Verified: `whatsapp` (three matches) asks; `whatsapp messenger 2.24` shares;
`netflix` with one match shares. A wrong file you have to delete is worse than one
extra tap.

Also fixed: the LLM "pick" step could previously overrule a correct top match. It
now refuses to overrule an exact name match or a clear leader.

## Continuous background learning

A loop runs every 20 minutes while the bot is up and does, with no user action:

- rebuilds the TF-IDF model from the current file list
- retrains the logistic-regression ranker once it has enough feedback
- clears the embedding cache so newly added files become searchable by meaning
- enriches remembered files that still have no tags

## Adding AI keys from inside Telegram

```
/freeai                       where free keys actually come from (with links)
/setkey groq gsk_xxx          test it, store it, start using it
/setkey groq k1,k2,k3         add several - used in order
/keys                         what's configured (masked) and which is active
/delkey groq                  remove them
```

**Every key is tested before it is stored**, so a typo or an expired key is
rejected immediately instead of failing mid-conversation.

**Key pools and rotation.** Several keys per provider are supported. When one hits
its free quota (HTTP 429) or is rejected, the next is tried automatically — verified:
key #1 returned 429, key #2 answered, and the bot carried on. That is the closest
thing to "auto keys" that exists.

**What Bonki will not do: find or generate keys.** A key is issued by a provider to
an account holder. Scraping publicly posted keys off the internet would mean using
someone else's credentials — a terms-of-service breach, usually revoked within
hours, and frequently a honeypot. `/freeai` lists the legitimate free sources
instead.

## Auto-add and auto-save keys

**Just paste the key.** In a DM with the bot, sending a bare key is enough — Bonki
recognises the provider from its prefix, tests it, saves it, and writes it to
`.env`:

```
you:  gsk_abcdefghijklmnop…
bot:  ✅ Saved a groq key (gsk_…3456). Pool now holds 1.
      Also written to .env, so it survives a restart.
```

Recognised prefixes: `gsk_` (Groq), `AIza…` (Gemini), `sk-or-v1-` (OpenRouter),
`sk-…` (OpenAI), `csk-` (Cerebras), `hf_` (Hugging Face), 64-char hex (Together).

Safety rules that apply:

- **Owner only.** A member pasting a key is ignored — it never reaches the store.
- **Tested before saving.** A wrong or expired key is rejected with the provider's
  own reason instead of being stored and failing later.
- **Written to `.env` as well as the state store**, so an ephemeral disk or a
  redeploy does not lose it. If the host is read-only, it says so rather than
  pretending.

**What "auto" does not include:** discovering or generating keys. A key is issued
by a provider to an account holder — see `/freeai` for where the free ones come
from.

## Free and open-source AI — Ollama, auto-connected

**Open-weight models running on your own machine**: no account, no key, no cost,
and nothing leaves your hardware. That is what "free and open source AI" actually
delivers, and Bonki now finds it by itself.

```
/ollama            status: server, model, what's installed
/ollama detect     search for a local server and switch to it
/ollama setup      3-step install instructions
/ollama url http://IP:11434    point at a remote one
```

At startup, **if no AI provider is configured, Bonki probes for Ollama
automatically** (`localhost`, `127.0.0.1`, `host.docker.internal`, `ollama`) and
switches the brain to it if it answers. Nothing to set up.

**Open models that run well on modest hardware:**

| Model | Size | Good for |
|---|---|---|
| `tinyllama` | 640 MB | weak machines, quick replies |
| `llama3.2` | 2 GB | best all-rounder at this size |
| `qwen2.5` | 4.7 GB | multilingual, strong reasoning |
| `gemma2` | 5 GB | fast and capable |

**Bot on a server, Ollama on your PC?** Start Ollama with `OLLAMA_HOST=0.0.0.0` and
run `/ollama url http://YOUR-IP:11434`. Use a tunnel (Tailscale, Cloudflare Tunnel)
rather than exposing the port to the internet.

**Honest limits:** Ollama needs roughly 8 GB RAM for the small models and more for
bigger ones, so it will not run on a 256 MB free host. It also writes prose far
worse than Gemini or Groq — a 2 GB model is a fallback, not a replacement. The bot
prefers an API key when one exists and uses Ollama when none does.

## Eighteen providers, auto-switch, and owner notifications

**Local open-source servers (no key, no cost, nothing leaves your machine):**
`ollama` · `lmstudio` · `vllm` · `llamacpp` · `localai`

**Free/cheap hosts of open-weight models:**
`gemini` · `groq` · `openrouter` · `cerebras` · `mistral` · `github` ·
`together` · `sambanova` · `hyperbolic` · `fireworks` · `deepinfra` ·
`huggingface` · `openai`

**Auto-switch.** Every call walks the providers in order, then each key in that
provider's pool. A failure marks that key cooling (15 min for a quota error, 6
hours for an auth error) and the next one answers — so a dead key or an exhausted
free tier never takes the bot down.

**The owner gets a DM whenever the model changes:**

```
🟢 AI model started
• groq / llama-3.1-8b-instant
• key pool: 2 key(s)

🔀 Switched AI model
• now: gemini / gemini-2.0-flash
• was: groq
• reason: the previous model failed or hit its quota
```

Only one message per event — a switch on the first call sends the switch notice
alone, and the same provider is not re-announced within 10 minutes, so a flapping
endpoint cannot spam you. Verified: 1 DM on switch, 1 on clean start, 0 on repeats.

## More free AI providers (auto-hook)

Nine providers are now understood, all auto-detected from whichever key exists:

`gemini` · `openai` · `openrouter` · `groq` · `together` · `cerebras` ·
`deepinfra` · `huggingface` · `ollama`

Any OpenAI-compatible endpoint works by setting `*_BASE_URL` and `*_MODEL`. If the
active one fails or hits quota, the next is used automatically and remembered.

## `/web` — internet search

`/web <query>` searches the web with no API key (DuckDuckGo HTML, then the lite
endpoint). **It depends on outbound HTTPS being allowed**: many hosts block it, and
when they do the bot says so plainly rather than failing silently.

## Why a search can return the wrong file (and how it is fixed)

Ranking blends four signals: fuzzy spelling, TF-IDF, learned associations and
embeddings. That blending is useful for vague queries, but it had a real flaw — a
**learned favourite or an embedding preference could outrank the file whose name
you actually typed**. One wrong confirmation, or one uncorrected result, was enough
to make the bot keep returning that file.

Two fixes:

1. **Exact matches now score far higher.** A query contained in the file name gets
   `+0.60`; all words present in the name gets `+0.35`. That is more than any
   reranker can add.
2. **Exact matches are pinned after every rerank.** If the query matches a file
   name, that file is forced to the top regardless of learned weights, embeddings
   or the ranker.

Verified against a deliberately hostile index — files whose *captions* mention
"netflix" while the real `Netflix 8.1.apk` sits beside them, plus **eight wrong
learned confirmations** pointing at Spotify. Netflix still won, and a 22-query
regression suite passed with zero mismatches.

If a search still looks wrong, run **`/ml <query>`** — it prints the real
component scores for the top candidates, so you can see which signal is
misbehaving instead of guessing.

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

#### api_id / api_hash / phone are saved

All three persist now (phone was never stored before, so every retry started from
scratch). The `/userbot` page shows exactly what the server holds:

```
Saved on this server: api_id ✅ 12345678 · api_hash ✅ saved · phone ✅ +91…
```

`api_hash` is shown as "saved" rather than printed, because it is a secret — but it
is stored, and leaving the field blank keeps the existing value instead of wiping
it.

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

## Surviving a restart, redeploy or server renewal

Two separate things decide whether the bot is alive after a renewal: the **process
must come back**, and the **bot must be reachable by Telegram**.

### 1. The process starts itself

| Host | What makes it restart automatically |
|---|---|
| **VPS / dedicated** | `deploy/bonki.service` — a systemd unit with `Restart=always` and `StartLimitIntervalSec=0`. Install once: `sudo cp deploy/bonki.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now bonki-intel`. `enable` is what starts it after a reboot. |
| **Docker** | `docker-compose.yml` already has `restart: unless-stopped` — `docker compose up -d` and it comes back after a daemon restart or machine reboot. |
| **Render** | Auto-deploy on push; a paid instance never sleeps. On free it sleeps after ~15 min idle — the keep-alive loop plus an external cron hitting `/healthz` wakes it. |
| **Panel hosts** | Set the startup file to `main.py` and enable the panel's auto-restart if it has one. |

### 2. Telegram can still reach it (the real "dead bot" cause)

The webhook is registered at startup. **If the host's public URL changes on a
renewal — a new instance, a new subdomain — Telegram keeps delivering to the OLD
URL.** The process is running perfectly and the bot looks completely dead. Nothing
in the logs screams about it.

A **watchdog** now runs every 3 minutes and fixes this with no user action:

- checks the token still works (`getMe`)
- asks Telegram what webhook it currently has (`getWebhookInfo`)
- if the URL is wrong, empty, or Telegram reports `last_error_message`, it
  **re-registers the webhook** against the current public URL

Verified: a stale URL is repaired, a `502` error report is repaired, a healthy
webhook is left alone, and in polling mode it makes no webhook calls at all.

### 3. You can see it happening

- **Diagnostics** shows `boot #N`, uptime, last downtime and RAM. A climbing boot
  count means a restart loop rather than a silent failure.
- The **restart DM** now says `boot #N · was down 3h` so you know it went away and
  came back without opening anything.

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
