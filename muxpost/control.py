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

from core.config import PREFIX
from core.sessions import _tmux, full_name, list_sessions, session_exists
from muxpost.state import LAST_SENT, MSG_SESSION, STATE, mark_sent, save_last_sent, save_state


def sessions_by_recency():
    """Matching sessions ordered by when you last sent them a message.

    The project you most recently worked on (replied to / sent a queued message
    / picked a menu option) floats to the top. Sessions you've never messaged
    sort below those you have, ordered among themselves by tmux's
    session_activity so the list still stays meaningful for fresh sessions.
    """
    res = _tmux(["list-sessions", "-F", "#{session_activity}\t#{session_name}"])
    if res.returncode != 0:
        return list_sessions()
    rows = []
    for ln in res.stdout.splitlines():
        act, _, name = ln.partition("\t")
        if not name.startswith(PREFIX):
            continue
        try:
            act = int(act)
        except ValueError:
            act = 0
        rows.append((LAST_SENT.get(name, 0), act, name))
    # last-sent first, then tmux activity, then name — all most-recent-first.
    rows.sort(key=lambda r: (-r[0], -r[1], r[2]))
    return [name for _, _, name in rows]


def send_input(session, text):
    """Type text literally into the session, then press Enter."""
    text = text.rstrip("\n")
    r1 = _tmux(["send-keys", "-t", session, "-l", "--", text])
    if r1.returncode != 0:
        return False
    r2 = _tmux(["send-keys", "-t", session, "Enter"])
    if r2.returncode == 0:
        mark_sent(session)
    return r2.returncode == 0


def send_queued(session):
    """Accept + submit a queued suggestion: Right (accept) then Enter (send)."""
    _tmux(["send-keys", "-t", session, "Right"])
    ok = _tmux(["send-keys", "-t", session, "Enter"]).returncode == 0
    if ok:
        mark_sent(session)
    return ok


def send_interrupt(session):
    """Interrupt Claude's current run — the TUI's 'esc to interrupt'.

    Sends Escape twice: the first stops an in-progress run, the second clears a
    half-typed prompt / dismisses any leftover menu.
    """
    ok = _tmux(["send-keys", "-t", session, "Escape"]).returncode == 0
    ok = _tmux(["send-keys", "-t", session, "Escape"]).returncode == 0 and ok
    return ok


def kill_session(full):
    """Kill the tmux session (ending its Claude process) and forget its state."""
    ok = _tmux(["kill-session", "-t", full]).returncode == 0
    if ok:
        STATE.pop(full, None)
        save_state()
        if LAST_SENT.pop(full, None) is not None:
            save_last_sent()
    return ok


def session_from_reply(reply):
    """Which live session a replied-to bot message is about, or None.

    Fast path: the in-memory message->session map. Fallback (after a restart
    clears that map, or for confirmation messages we never tracked): recover
    the display name from the message's bold header — every status / report /
    action message renders the session name in bold — and accept it only if
    that session is currently live.
    """
    if not reply:
        return None
    tracked = MSG_SESSION.get(reply.get("message_id"))
    if tracked:
        return tracked
    text = reply.get("text") or ""
    units = text.encode("utf-16-le")  # Telegram entity offsets are UTF-16 units
    for ent in reply.get("entities") or []:
        if ent.get("type") == "bold":
            lo, hi = ent["offset"] * 2, (ent["offset"] + ent["length"]) * 2
            disp = units[lo:hi].decode("utf-16-le", "ignore").strip()
            full = full_name(disp)
            return full if session_exists(full) else None
    return None
