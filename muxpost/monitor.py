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

from core.config import PREFIX, SNAPSHOT_FILE, STATE_DIR, USER_ID, setting
from core.sessions import _session_field, capture_pane, launch_session, list_sessions, session_exists
from muxpost.menus import action_keyboard
from muxpost.panes import _pane_hash, status_text
from muxpost.state import MSG_SESSION, STATE, save_state
from muxpost.telegram import send


def snapshot_sessions():
    """Record live claude-* sessions (name + cwd + command) for later restore."""
    sessions = []
    for full in list_sessions():
        sessions.append({
            "name": full,
            "path": _session_field(full, "#{pane_current_path}"),
            "command": _session_field(full, "#{pane_current_command}"),
        })
    if not sessions:
        return  # don't overwrite a good snapshot with an empty one
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = SNAPSHOT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"prefix": PREFIX, "count": len(sessions), "sessions": sessions}, fh)
        os.replace(tmp, SNAPSHOT_FILE)
    except OSError:
        pass


def restore_from_snapshot():
    """Recreate snapshot sessions that aren't currently running; resume claude."""
    try:
        with open(SNAPSHOT_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    home = os.path.expanduser("~")
    restored = 0
    for s in data.get("sessions", []):
        name = s.get("name")
        if not name or session_exists(name):
            continue
        path = s.get("path") or home
        workdir = path if os.path.isdir(path) else home
        ok, info = launch_session(name, workdir)
        if ok:
            restored += 1
            print(f"restored {name} in {workdir} ({info})")
    return restored


def monitor_tick():
    live = list_sessions()
    live_set = set(live)
    for session in live:
        pane = capture_pane(session)
        if pane is None:
            continue
        digest = _pane_hash(pane)
        st = STATE.get(session)
        idle_ticks = setting("idle_ticks")
        if st is None:
            # First time we see this session (fresh start or newly created):
            # treat whatever is on screen now as a baseline — don't notify it.
            STATE[session] = {"hash": digest, "count": idle_ticks, "reported": True}
            continue
        if digest == st["hash"]:
            st["count"] += 1
            if st["count"] >= idle_ticks and not st["reported"]:
                st["reported"] = True
                mid = send(USER_ID, status_text(session, pane, "💤"),
                           reply_markup=action_keyboard(session, pane))
                if mid:
                    MSG_SESSION[mid] = session
        else:
            st["hash"] = digest
            st["count"] = 1
            st["reported"] = False
    # forget sessions that disappeared
    for gone in [s for s in STATE if s not in live_set]:
        STATE.pop(gone, None)
    save_state()
