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

from core.config import NOTIFY_FILE, PIDFILE, ROOT, STATE_DIR
from muxpost.telegram import send


def version():
    r = subprocess.run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def git_pull():
    r = subprocess.run(["git", "-C", ROOT, "pull", "--ff-only"],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _read_pid():
    try:
        with open(PIDFILE, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def running_pid():
    """PID of a live muxpost instance, or None."""
    pid = _read_pid()
    if pid:
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return None
    return None


def write_notify(chat_id, text):
    """Queue a message to be sent once the bot comes back after a restart."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(NOTIFY_FILE, "w", encoding="utf-8") as fh:
            json.dump({"chat_id": chat_id, "text": text}, fh)
    except OSError:
        pass


def _flush_notify():
    try:
        with open(NOTIFY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        os.remove(NOTIFY_FILE)
        send(data["chat_id"], data["text"])
    except (OSError, ValueError, KeyError):
        pass


def restart_inplace():
    """Replace this process with a fresh one (same PID; reloads code + config)."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, ENTRY, "run"])
