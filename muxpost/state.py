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

from core.config import LAST_SENT_FILE, OFFSET_FILE, SETTINGS, SETTINGS_FILE, SETTINGS_SPEC, STATE_DIR, STATE_FILE, setting


STATE = {}


LAST_SENT = {}


MSG_SESSION = {}


PENDING = {}


NEW_DIR_WAIT = {}


PENDING_NEW = {}


GETFILE_DIR = {}


def load_state():
    """Restore per-session report state so a restart doesn't re-notify."""
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            STATE.update(data)
    except (OSError, ValueError):
        pass


def save_state():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(STATE, fh)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def load_settings():
    """Restore user settings overrides from disk (validated against the spec)."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        for key, spec in SETTINGS_SPEC.items():
            if key in data and data[key] in spec["values"]:
                SETTINGS[key] = data[key]


def save_settings():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(SETTINGS, fh)
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        pass


def cycle_setting(key):
    """Advance a setting to its next value (wrapping) and persist it."""
    values = SETTINGS_SPEC[key]["values"]
    try:
        nxt = values[(values.index(setting(key)) + 1) % len(values)]
    except ValueError:
        nxt = values[0]
    SETTINGS[key] = nxt
    save_settings()
    return nxt


def load_offset():
    """Restore the Telegram getUpdates offset, or None if unset.

    Persisting this is what stops a /restart or /upgrade from looping: the
    command's update must be acknowledged to Telegram (by fetching with a
    higher offset) or it's redelivered forever. We re-exec before the next
    fetch, so the offset has to survive the re-exec on disk.
    """
    try:
        with open(OFFSET_FILE, encoding="utf-8") as fh:
            val = json.load(fh)
        return int(val)
    except (OSError, ValueError, TypeError):
        return None


def save_offset(offset):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = OFFSET_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(offset, fh)
        os.replace(tmp, OFFSET_FILE)
    except OSError:
        pass


def load_last_sent():
    """Restore the per-session 'last message sent' timestamps."""
    try:
        with open(LAST_SENT_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            LAST_SENT.update({k: int(v) for k, v in data.items()})
    except (OSError, ValueError, TypeError):
        pass


def save_last_sent():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LAST_SENT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(LAST_SENT, fh)
        os.replace(tmp, LAST_SENT_FILE)
    except OSError:
        pass


def mark_sent(session):
    """Record that we just relayed a message to `session` and persist it."""
    LAST_SENT[session] = int(time.time())
    save_last_sent()
