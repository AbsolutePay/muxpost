import hashlib
import html
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "muxpost.py")


_CFG = {}


_cfg_path = os.path.join(ROOT, "config.json")


if os.path.exists(_cfg_path):
    try:
        with open(_cfg_path, "r", encoding="utf-8") as fh:
            _CFG = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not read config.json: {exc}", file=sys.stderr)


def _conf(env, key, default=None):
    val = os.environ.get(env)
    if val is not None and val != "":
        return val
    if key in _CFG and _CFG[key] not in (None, ""):
        return _CFG[key]
    return default


TOKEN = _conf("TG_BOT_TOKEN", "bot_token")


USER_ID = int(_conf("TG_USER_ID", "user_id", 0) or 0)


PREFIX = _conf("TG_PREFIX", "prefix", "claude-")


INTERVAL = float(_conf("TG_INTERVAL", "interval", 5))


IDLE_TICKS = int(_conf("TG_IDLE_TICKS", "idle_ticks", 3))


PAGE_SIZE = int(_conf("TG_PAGE_SIZE", "page_size", 5))


PROJECT_ROOT = _conf("TG_PROJECT_ROOT", "project_root", "")


if PROJECT_ROOT:
    PROJECT_ROOT = os.path.abspath(os.path.expanduser(PROJECT_ROOT))


def _as_bool(v):
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "y", "on")


RESTORE_SESSIONS = _as_bool(_conf("TG_RESTORE_SESSIONS", "restore_sessions", False))


DEBUG = _as_bool(_conf("TG_DEBUG", "debug", False))


def dbg(msg):
    """Trace line to stderr (the journal) when debug is on; no-op otherwise."""
    if DEBUG:
        print(f"[dbg {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


SNAPSHOT_INTERVAL = float(_conf("TG_SNAPSHOT_INTERVAL", "snapshot_interval", 300))


API = f"https://api.telegram.org/bot{TOKEN}"


SETTINGS_SPEC = {
    "pane_view":  {"label": "📜 Pane view",      "values": ["expandable", "compact"]},
    "pane_lines": {"label": "📏 Pane lines",      "values": [15, 30, 60]},
    "idle_ticks": {"label": "🔔 Notify after ticks", "values": [2, 3, 4, 5]},
}


SETTINGS_DEFAULTS = {"pane_view": "expandable", "pane_lines": 60, "idle_ticks": IDLE_TICKS}


SETTINGS = {}  # overrides loaded from SETTINGS_FILE


def setting(key):
    """Current value of a setting: user override, else the default."""
    val = SETTINGS.get(key)
    return val if val is not None else SETTINGS_DEFAULTS[key]


def require_config():
    if not TOKEN or not USER_ID:
        print(
            "Missing config. Set TG_BOT_TOKEN and TG_USER_ID via environment "
            "or config.json (run: muxpost setup).",
            file=sys.stderr,
        )
        sys.exit(1)


STATE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "muxpost")


PIDFILE = os.path.join(STATE_DIR, "muxpost.pid")


NOTIFY_FILE = os.path.join(STATE_DIR, "notify.json")


STATE_FILE = os.path.join(STATE_DIR, "state.json")


LAST_SENT_FILE = os.path.join(STATE_DIR, "last_sent.json")


OFFSET_FILE = os.path.join(STATE_DIR, "offset.json")


SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")


SNAPSHOT_FILE = os.path.join(STATE_DIR, "sessions_snapshot.json")


# Records the boot time we last auto-restored for, so a muxpost restart/upgrade
# (same boot) skips restore and only an actual reboot revives sessions.
BOOT_FILE = os.path.join(STATE_DIR, "restored_boot")


RESTART_SIG = getattr(signal, "SIGHUP", None)


DOC_MAX_BYTES = 50 * 1024 * 1024  # Telegram bot upload limit


MAX_CHARS = 3500  # hard cap on pane text length (Telegram message budget)
