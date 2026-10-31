"""Defaults and settings keys for Bonki-Intelligence.

Bonki-Intelligence is the "brain" layer: an intelligent Telegram group agent that can
scan a group, understand a plain-English request, find the file being asked for,
and reply with it - tagging the requester and adding a hashtag.
"""

# What the bot answers to. Comma separated; matched as whole words, case-insensitive.
# The bot's own @username is detected automatically too (via getMe), so a plain
# @mention always works - these are the extra words that wake it without the @.
DEFAULT_WAKE_WORDS = "bonki, bonki_intelligence_bot"

DEFAULT_SETTINGS = {
    # ---- identity ----
    "wake_words": DEFAULT_WAKE_WORDS,
    # only act in these group chat ids (comma separated). Empty = any group the
    # bot is added to.
    "allowed_groups": "",
    # ---- approval (so a stranger can't add the bot and have it index files) ----
    # Telegram user id of the owner. New groups stay inactive until the owner
    # approves. Also settable with the OWNER_ID env var.
    "owner_id": "",
    # "1" = a group the bot is added to must be approved by the owner before it
    # will scan, index or answer there. "0" = approve automatically.
    "require_approval": "1",
    # "1" = answer only when addressed (wake word / @mention / reply to bot)
    "require_mention": "1",
    # "1" = PAUSE everything
    "paused": "0",
    # ---- keep-alive (Render free tier sleeps the service when idle) ----
    # "1" = ping our own public URL on a timer, so Render never sees the service
    # as idle and never spins it down (which takes the bot AND the dashboard down)
    "keepalive": "1",
    "keepalive_minutes": "10",
    # max tags in one /tag command (Telegram allows ~20 messages/min per group,
    # so a big number simply takes proportionally longer)
    "tag_max": "50",
    "keepalive_last": "",      # informational: "<epoch>:<status>" of the last ping

    # ---- the brain ----
    # "gemini" | "openai" | "off"
    "llm_provider": "gemini",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.0-flash",
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_model": "gpt-4o-mini",

    # ---- scanning ----
    # how many recent messages a "scan this group" walks back through
    "scan_depth": "500",
    # "1" = index new files automatically, no manual scan needed
    # (live capture when the bot sees a post + a background scan on a timer)
    "auto_scan": "1",
    # how often the background indexer scans groups for new files, in minutes
    "scan_interval_minutes": "10",
    # file extensions the index keeps (comma separated, empty = all documents)
    "index_extensions": "apk, xapk, apks, zip, apks, 7z, rar, obb, ipa",
    # "1" = also index files as they are posted live (needs group privacy off)
    "index_live": "1",

    # ---- how the bot replies ----
    # "1" = re-post the found file (not just link it)
    "reply_with_file": "1",
    # "1" = @mention the person who asked
    "mention_requester": "1",
    # "1" = add a hashtag label to the reply
    "add_hashtag": "1",
    # "1" = also store the hashtag on the indexed file, so later searches hit it
    "persist_tags": "1",
    # minutes after which the "other close matches" message deletes itself if the
    # user never taps a choice (0 = never delete)
    "feedback_delete_minutes": "10",
    # "1" = when a group is approved, tell the group and post a welcome
    "announce_approval": "1",
    # footer appended to every reply (own line). Empty = off.
    "reply_footer": "Bonki-Intelligence",
    # max size (MB) the bot will re-upload as a file; larger files get a link
    "max_upload_mb": "50",

    # ---- user account (MTProto) - for reading history a bot can't ----
    "tg_api_id": "",
    "tg_api_hash": "",
    "tg_session": "",
    "userbot_last_error": "",

    # ---- webhook ----
    "webhook_secret": "",
    # comma separated Telegram user ids allowed to DM the bot commands
    "allow_user_ids": "",
}

# Settings safe to show on the dashboard (never echo secrets back in full).
SECRET_KEYS = ("gemini_api_key", "openai_api_key", "tg_session", "tg_api_hash", "webhook_secret")
