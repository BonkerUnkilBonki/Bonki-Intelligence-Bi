"""Defaults and settings keys for Bonki Intel.

Bonki Intel is the "brain" layer: an intelligent Telegram group agent that can
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
    # "1" = answer only when addressed (wake word / @mention / reply to bot)
    "require_mention": "1",
    # "1" = PAUSE everything
    "paused": "0",

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
    # footer appended to every reply (own line). Empty = off.
    "reply_footer": "Bonki Intel",
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
