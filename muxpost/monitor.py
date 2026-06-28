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

from core.config import BOOT_FILE, PREFIX, SNAPSHOT_FILE, STATE_DIR, USER_ID, dbg, setting
from core.sessions import _session_field, capture_pane, launch_session, list_sessions, session_exists
from muxpost.menus import action_keyboard
from muxpost.panes import _pane_hash, status_text
from muxpost.state import MSG_SESSION, STATE, remember_session, save_state
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


def _boot_time():
    """System boot time (epoch secs) as a string — constant across muxpost
    restarts/upgrades, changes only on an actual reboot."""
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return line.split()[1]
    except OSError:
        pass
    return None


def restore_on_boot():
    """Recreate snapshot sessions, but only ONCE per machine boot. A muxpost
    restart/upgrade re-execs with tmux untouched, so it must not revive sessions
    the user deliberately killed — only a real reboot (new boot time) restores."""
    boot = _boot_time()
    try:
        with open(BOOT_FILE, encoding="utf-8") as fh:
            if boot is not None and fh.read().strip() == boot:
                return 0  # already restored for this boot
    except OSError:
        pass
    n = restore_from_snapshot()
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(BOOT_FILE, "w", encoding="utf-8") as fh:
            fh.write(boot or "")
    except OSError:
        pass
    return n


def baseline_sessions():
    """Treat every live session's current screen as a baseline so a restart
    counts as init — no reports for sessions that were already idle. A report
    only fires once a pane CHANGES after startup and then settles."""
    for session in list_sessions():
        pane = capture_pane(session)
        if pane is None:
            continue
        h = _pane_hash(pane)
        STATE[session] = {"hash": h, "count": setting("idle_ticks"),
                          "reported": True, "last_report": h}
    save_state()


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
            STATE[session] = {"hash": digest, "count": idle_ticks,
                              "reported": True, "last_report": digest}
            dbg(f"{session} baseline {digest[:8]}")
            continue
        if digest == st["hash"]:
            st["count"] += 1
            if st["count"] >= idle_ticks and not st["reported"]:
                st["reported"] = True
                # Suppress if this settled screen is identical to the one we last
                # notified about: a transient flicker (overlay/redraw) that reverts
                # to the same screen re-arms the FSM but isn't new — don't re-notify.
                if digest == st.get("last_report"):
                    dbg(f"{session} settle == last report, suppress ({digest[:8]})")
                    continue
                st["last_report"] = digest
                mid = send(USER_ID, status_text(session, pane, "💤"),
                           reply_markup=action_keyboard(session, pane))
                dbg(f"report {session} idle {st['count']} ticks -> "
                    f"{'sent #' + str(mid) if mid else 'SEND FAILED'} ({digest[:8]})")
                remember_session(session)  # auto-route target: latest reported session
                if mid:
                    MSG_SESSION[mid] = session
            else:
                dbg(f"{session} idle {st['count']}/{idle_ticks} reported={st['reported']} {digest[:8]}")
        else:
            dbg(f"{session} changed {st['hash'][:8]}->{digest[:8]} (count was {st['count']})")
            st["hash"] = digest
            st["count"] = 1
            st["reported"] = False
    # forget sessions that disappeared
    for gone in [s for s in STATE if s not in live_set]:
        STATE.pop(gone, None)
    save_state()
