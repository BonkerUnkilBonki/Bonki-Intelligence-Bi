"""Continuous background learning.

The bot keeps improving while it runs, with no user action:

  * rebuilds the TF-IDF model when files or feedback change
  * retrains the logistic-regression ranker once it has enough examples
  * refreshes the local embedding cache so new files are searchable by meaning
  * re-runs enrichment over remembered files that have no tags yet

It is deliberately gentle: one pass every 20 minutes, each step wrapped so a
failure never stops the loop.
"""
import asyncio
import logging

from . import db, embed, learn, memory, ranker

log = logging.getLogger("learnloop")

INTERVAL = 20 * 60


def _unlearned_files(limit=5):
    """Remembered files that still have no tags - candidates for enrichment."""
    out = []
    for m in db.list_memory(limit=500):
        meta = m.get("meta") or {}
        if meta.get("name") and not meta.get("tags"):
            out.append(meta)
        if len(out) >= limit:
            break
    return out


async def once():
    """One learning pass. Returns a short summary for the log."""
    did = []

    # 1. TF-IDF: invalidate so it is rebuilt from the current file list
    try:
        learn.invalidate(None)
        did.append("tfidf")
    except Exception as e:
        log.info("tfidf refresh failed: %s", e)

    # 2. ranker: retrain if there is enough feedback
    try:
        rows = len(db.list_ranker_rows())
        if rows >= ranker.MIN_ROWS:
            model = ranker.train()
            if model:
                did.append(f"ranker({model.get('n')})")
    except Exception as e:
        log.info("ranker retrain failed: %s", e)

    # 3. embeddings: clear the vector cache so new files get embedded
    try:
        cache = getattr(embed, "_cache", None)
        if isinstance(cache, dict):
            cache.clear()
            did.append("embeddings")
    except Exception as e:
        log.info("embedding refresh failed: %s", e)

    # 4. enrichment: give untagged remembered files their keywords
    try:
        todo = _unlearned_files(3)
        for meta in todo:
            await asyncio.to_thread(memory.enrich, meta.get("chat_id"),
                                    meta.get("message_id"), meta.get("name"),
                                    meta.get("caption") or "")
        if todo:
            did.append(f"enriched({len(todo)})")
    except Exception as e:
        log.info("enrichment pass failed: %s", e)

    return ", ".join(did) or "nothing to do"


async def loop():
    log.info("background learning loop started")
    await asyncio.sleep(120)              # let startup finish first
    while True:
        try:
            summary = await once()
            log.info("learning pass: %s", summary)
            await asyncio.to_thread(db.flush)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("learning loop error: %s", e)
        await asyncio.sleep(INTERVAL)
