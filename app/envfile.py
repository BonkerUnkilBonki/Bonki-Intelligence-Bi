"""Read/write the local .env file, so saved keys survive a restart.

Settings live in the state store, but on a host with an ephemeral disk a redeploy
can wipe it. Writing keys to `.env` as well means they come back from the
environment on the next boot, with no re-entry.
"""
import logging
import os
from pathlib import Path

log = logging.getLogger("envfile")


def _paths():
    here = Path(__file__).resolve().parent.parent
    return [Path.cwd() / ".env", here / ".env"]


def _target():
    for p in _paths():
        if p.is_file():
            return p
    return _paths()[0]


def set_env(key, value):
    """Add or update KEY in the local .env and in this process's environment."""
    try:
        path = _target()
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        out, done = [], False
        for line in lines:
            if line.strip().startswith(key + "=") or line.strip() == key:
                out.append(f"{key}={value}")
                done = True
            else:
                out.append(line)
        if not done:
            out.append(f"{key}={value}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.environ[key] = value
        return True
    except Exception as e:
        log.info("could not write %s to .env: %s", key, e)
        return False


def clear_env(key):
    try:
        path = _target()
        if not path.is_file():
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
        out = [ln for ln in lines
               if not (ln.strip().startswith(key + "=") or ln.strip() == key)]
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.environ.pop(key, None)
        return True
    except Exception as e:
        log.info("could not clear %s from .env: %s", key, e)
        return False


def persist(settings_map):
    """Write several settings to .env at once. Keys are upper-cased."""
    ok = 0
    for k, v in (settings_map or {}).items():
        if set_env(str(k).upper(), str(v)):
            ok += 1
    return ok
