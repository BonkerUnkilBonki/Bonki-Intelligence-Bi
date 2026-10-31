"""Entry point for panel hosts (Pterodactyl, bot-hosting, Falcon, etc.).

**This is the file to select in the panel's "file to run" picker.**

Panels run something like `python main.py`, and they hand you a port through an
environment variable. This file starts the Bonki web server on that port and
serves the dashboard; the Telegram bot itself runs inside it (long polling), so
the bot answers whether or not anyone opens the dashboard.

Run it yourself the same way:

    python main.py

Environment:
    SERVER_PORT / PORT   port to bind (panels set one of these; default 8000)
    HOST                 bind address (default 0.0.0.0)
"""
import os
import sys

try:
    import uvicorn
except ImportError:                                    # pragma: no cover
    print("uvicorn is not installed. Run:  pip install -r requirements.txt",
          file=sys.stderr)
    raise


def _port():
    """Panels use different names for the port - accept all the common ones."""
    for key in ("SERVER_PORT", "PORT", "SERVER_PORT_WEB", "WEB_PORT"):
        val = (os.environ.get(key) or "").strip()
        if val.isdigit():
            return int(val)
    return 8000


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = _port()
    print(f"Bonki-Intelligence starting on http://{host}:{port}", flush=True)
    uvicorn.run("app.main:app", host=host, port=port, workers=1, log_level="info")
