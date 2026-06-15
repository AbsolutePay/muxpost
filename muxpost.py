#!/usr/bin/env python3
"""
muxpost — messaging for your terminal multiplexer.

A zero-dependency Telegram bot that watches every tmux session whose name
starts with a prefix (default "claude-"), reports when a session goes idle,
and relays your Telegram replies straight into that session via tmux send-keys.

Only the Python standard library is used, so it runs on Linux / macOS /
Windows wherever Python 3.8+ and tmux are available.

Configure via environment variables or a config.json next to this file:
    TG_BOT_TOKEN   Telegram bot token (from @BotFather)
    TG_USER_ID     numeric Telegram user id allowed to use the bot
    TG_PREFIX      session prefix to watch         (default "claude-")
    TG_INTERVAL    seconds between capture ticks    (default 5)
    TG_IDLE_TICKS  unchanged ticks before reporting (default 3)
    TG_PAGE_SIZE   sessions per selection page       (default 5)
"""

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

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = {}
_cfg_path = os.path.join(_HERE, "config.json")
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


# Periodically snapshot live sessions; optionally restore them on startup
# (e.g. after a reboot). Snapshot cadence mirrors a 5-minute external snapshot.
RESTORE_SESSIONS = _as_bool(_conf("TG_RESTORE_SESSIONS", "restore_sessions", False))
SNAPSHOT_INTERVAL = float(_conf("TG_SNAPSHOT_INTERVAL", "snapshot_interval", 300))

API = f"https://api.telegram.org/bot{TOKEN}"


def require_config():
    if not TOKEN or not USER_ID:
        print(
            "Missing config. Set TG_BOT_TOKEN and TG_USER_ID via environment "
            "or config.json (run: muxpost setup).",
            file=sys.stderr,
        )
        sys.exit(1)


# Paths for process management (pidfile) and post-restart notifications.
STATE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "muxpost")
PIDFILE = os.path.join(STATE_DIR, "muxpost.pid")
NOTIFY_FILE = os.path.join(STATE_DIR, "notify.json")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LAST_SENT_FILE = os.path.join(STATE_DIR, "last_sent.json")
OFFSET_FILE = os.path.join(STATE_DIR, "offset.json")
SNAPSHOT_FILE = os.path.join(STATE_DIR, "sessions_snapshot.json")
RESTART_SIG = getattr(signal, "SIGHUP", None)

# --------------------------------------------------------------------------
# In-memory state
# --------------------------------------------------------------------------

# session name -> {"hash": str|None, "count": int, "reported": bool}
STATE = {}
# session name -> epoch seconds we last relayed a message to it (sort key for
# the session pickers: most-recently-worked-on first)
LAST_SENT = {}
# message_id -> full session name (replying to it relays input to that session)
MSG_SESSION = {}
# chat_id -> pending text awaiting a "which session?" button choice
PENDING = {}
# chat_id -> True while we wait for the user to type a new folder name
NEW_DIR_WAIT = {}
# chat_id -> {"name", "workdir"} awaiting a create-folder confirmation
PENDING_NEW = {}

# --------------------------------------------------------------------------
# Telegram API helpers
# --------------------------------------------------------------------------


def api(method, _timeout=20, **params):
    """Call a Telegram Bot API method. dict/list params are JSON-encoded."""
    data = {}
    for key, val in params.items():
        if val is None:
            continue
        data[key] = json.dumps(val) if isinstance(val, (dict, list)) else val
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(f"{API}/{method}", data=body)
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()
        except Exception:  # noqa: BLE001
            pass
        print(f"[api] {method} failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return {"ok": False, "error": str(exc), "detail": detail}
    except Exception as exc:  # noqa: BLE001
        print(f"[api] {method} failed: {exc}", file=sys.stderr)
        return {"ok": False, "error": str(exc)}


def send(chat_id, text, reply_markup=None, reply_to=None):
    res = api(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=reply_markup,
        reply_to_message_id=reply_to,
    )
    if res.get("ok"):
        return res["result"]["message_id"]
    return None


def edit(chat_id, message_id, text=None, reply_markup=None):
    if text is not None:
        api(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    else:
        api(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )


def answer(callback_id, text=None):
    api("answerCallbackQuery", callback_query_id=callback_id, text=text)


# --------------------------------------------------------------------------
# tmux helpers
# --------------------------------------------------------------------------


def _tmux(args):
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def list_sessions():
    """Return full names of tmux sessions matching PREFIX, sorted by name."""
    res = _tmux(["list-sessions", "-F", "#{session_name}"])
    if res.returncode != 0:
        return []
    names = [ln for ln in res.stdout.splitlines() if ln.startswith(PREFIX)]
    return sorted(names)


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


def capture_pane(session):
    res = _tmux(["capture-pane", "-p", "-t", session])
    if res.returncode != 0:
        return None
    return res.stdout


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


def session_exists(full):
    return _tmux(["has-session", "-t", full]).returncode == 0


def kill_session(full):
    """Kill the tmux session (ending its Claude process) and forget its state."""
    ok = _tmux(["kill-session", "-t", full]).returncode == 0
    if ok:
        STATE.pop(full, None)
        save_state()
        if LAST_SENT.pop(full, None) is not None:
            save_last_sent()
    return ok


def sanitize_name(s):
    """Make a string safe as a tmux session name / folder name."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", s.strip()).strip("-")
    return cleaned or "session"


def list_subdirs(root):
    """Immediate, non-hidden subdirectories of root (sorted)."""
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    return sorted(
        e for e in entries
        if not e.startswith(".") and os.path.isdir(os.path.join(root, e))
    )


def has_history(workdir):
    """True if Claude Code already has a project history for this directory."""
    enc = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(workdir))
    proj = os.path.join(os.path.expanduser("~"), ".claude", "projects", enc)
    try:
        return os.path.isdir(proj) and any(
            f.endswith(".jsonl") for f in os.listdir(proj)
        )
    except OSError:
        return False


def launch_session(full, workdir):
    """Create a detached session in workdir and start claude (resume if able)."""
    res = _tmux(["new-session", "-d", "-s", full, "-c", workdir])
    if res.returncode != 0:
        return False, res.stderr.strip() or "tmux new-session failed"
    cmd = "claude --continue" if has_history(workdir) else "claude"
    _tmux(["send-keys", "-t", full, cmd, "Enter"])
    return True, cmd


def display_name(full):
    return full[len(PREFIX):] if full.startswith(PREFIX) else full


def full_name(disp):
    return disp if disp.startswith(PREFIX) else PREFIX + disp


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

MAX_LINES = 60
MAX_CHARS = 3500


def _is_divider(line):
    """True for the box-drawing rules that frame Claude's input box."""
    chars = [c for c in line if not c.isspace()]
    return bool(chars) and all(0x2500 <= ord(c) <= 0x257F for c in chars)


def clean_pane(text):
    """Strip the TUI chrome that's common to every claude-* pane.

    Removes the input-box borders, the empty prompt line, the bottom status
    footer, and the '/clear to save … tokens' hint, and collapses blank runs —
    leaving just the conversation. A non-empty prompt (a queued message) is kept.
    """
    out = []
    blanks = 0
    for raw in text.split("\n"):
        line = raw.rstrip()
        st = line.strip()
        if _is_divider(line):
            continue
        if st.startswith("⏵⏵"):                       # "auto mode on … for agents"
            continue
        if "/clear to save" in st and st.endswith("tokens"):
            continue
        if st in ("❯", ">"):                           # empty input prompt
            continue
        if not st:
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def render_pane(pane):
    """Clean the capture, trim to the last lines, wrap in an expandable quote."""
    if pane is None:
        return "<i>(could not capture pane)</i>"
    lines = clean_pane(pane).split("\n")
    if not lines or not any(l.strip() for l in lines):
        return "<blockquote expandable><i>(empty)</i></blockquote>"
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
    body = "\n".join(lines)
    if len(body) > MAX_CHARS:
        body = body[-MAX_CHARS:]
    return f"<blockquote expandable>{html.escape(body)}</blockquote>"


def status_text(full, pane, header_emoji="🖥"):
    disp = html.escape(display_name(full))
    return (
        f"{header_emoji} <b>{disp}</b>\n"
        f"{render_pane(pane)}\n"
        f"<i>↩️ Reply to this message to send input.</i>"
    )


# Ghost/placeholder hints Claude renders inside an empty prompt — not real
# queued messages, so detect_queue must ignore them (compared lowercased).
QUEUE_PLACEHOLDERS = {
    "press up to edit queued messages",
}


def detect_queue(pane):
    """Return a queued/suggested message sitting in the input prompt, or None.

    Claude stages a recommended next message in the prompt (you'd press →/Tab
    then Enter to send it). We take the text after the last '❯' prompt marker;
    an empty prompt means nothing is queued.
    """
    if not pane:
        return None
    lines = pane.split("\n")
    tail = "\n".join(lines[-6:])
    # In a selection menu '❯' marks the highlighted option, not a text prompt.
    if ("Esc to cancel" in tail or "to navigate" in tail
            or "Enter to select" in tail or "Press Enter" in tail):
        return None
    queued = None
    for line in lines:
        st = line.strip()
        if st.startswith("❯"):
            queued = st[1:].strip()
    if queued and queued.lower() in QUEUE_PLACEHOLDERS:
        # Ghost/placeholder hint shown in an empty prompt, not a real message.
        return None
    return queued or None


def detect_menu(pane):
    """Return [(num, label), …] if an interactive selection menu is showing.

    Claude renders a numbered menu ('❯ 1. …', '  2. …') with an
    'Enter to select · ↑/↓ to navigate · Esc to cancel' footer; pressing the
    number selects that option. Description lines (deeper indent) are ignored.

    Only the menu at the very bottom counts. Options are numbered 1..N
    top-to-bottom, so walking up from the footer they descend by exactly 1 —
    we stop the moment that run breaks. This skips dividers and wrapped
    description lines inside the menu (Claude sometimes draws a rule between
    options), while excluding any numbered list printed earlier in the
    conversation: its numbers won't continue the menu's sequence.
    """
    if not pane:
        return None
    lines = pane.split("\n")
    # The footer is the last non-blank line, but Claude can leave several blank
    # lines below it — match on the last few non-blank lines, not a fixed slice.
    tail = "\n".join([l for l in lines if l.strip()][-3:])
    if not ("Esc to cancel" in tail or "to navigate" in tail or "Enter to select" in tail):
        return None
    opts = []
    for line in reversed(lines):
        m = re.match(r"^(?:❯ |  )(\d+)\.\s+(.+)$", line)
        if not m:
            continue
        num = int(m.group(1))
        if opts and num != int(opts[-1][0]) - 1:
            break                    # sequence broke — top of the menu reached
        opts.append((m.group(1), m.group(2).strip()))
    opts.reverse()
    return opts or None


def action_keyboard(full, pane):
    """Context buttons for a report/status: menu options or a queued message,
    plus a Refresh button that re-captures the pane."""
    disp = display_name(full)
    rows = []
    opts = detect_menu(pane)
    if opts:
        for num, label in opts:
            t = f"{num}. {label}"
            rows.append([{"text": t if len(t) <= 45 else t[:44] + "…",
                          "callback_data": f"o|{disp}|{num}"}])
    else:
        q = detect_queue(pane)
        if q:
            label = q if len(q) <= 40 else q[:39] + "…"
            rows.append([{"text": f"▶️ Send queued: {label}",
                          "callback_data": f"q|{disp}"}])
    rows.append([{"text": "🔄 Refresh", "callback_data": f"rf|{disp}"}])
    return {"inline_keyboard": rows}


def rebuild_status(chat_id, message_id, full, note=None):
    """Re-capture the pane and rewrite a status/report message in place.

    Keeps the message live: fresh pane, current menu/queue buttons, and an
    always-present Refresh button. `note` (e.g. what action just ran) is shown
    with a timestamp; falling back to a plain refresh stamp.
    """
    pane = capture_pane(full)
    stamp = time.strftime("%H:%M:%S")
    tail = f"{note} · {stamp}" if note else f"🔄 Refreshed {stamp}"
    edit(chat_id, message_id,
         text=status_text(full, pane) + f"\n<i>{tail}</i>",
         reply_markup=action_keyboard(full, pane))
    MSG_SESSION[message_id] = full


def build_keyboard(tag, sessions, page):
    """Inline keyboard of session buttons (PAGE_SIZE per page) + nav row."""
    pages = max(1, math.ceil(len(sessions) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = sessions[start:start + PAGE_SIZE]
    rows = [
        [{"text": display_name(s), "callback_data": f"{tag}|s|{display_name(s)}"}]
        for s in chunk
    ]
    nav = []
    if page > 0:
        nav.append({"text": "◀️", "callback_data": f"{tag}|p|{page - 1}"})
    if pages > 1:
        nav.append({"text": f"{page + 1}/{pages}", "callback_data": f"{tag}|p|{page}"})
    if page < pages - 1:
        nav.append({"text": "▶️", "callback_data": f"{tag}|p|{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "✖️ Cancel", "callback_data": f"{tag}|c"}])
    return {"inline_keyboard": rows}


def build_dir_keyboard(dirs, page):
    """Folder buttons under PROJECT_ROOT + a 'create new folder' row."""
    pages = max(1, math.ceil(len(dirs) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = dirs[start:start + PAGE_SIZE]
    rows = [[{"text": "📁 " + d, "callback_data": f"nw|s|{d}"}] for d in chunk]
    nav = []
    if page > 0:
        nav.append({"text": "◀️", "callback_data": f"nw|p|{page - 1}"})
    if pages > 1:
        nav.append({"text": f"{page + 1}/{pages}", "callback_data": f"nw|p|{page}"})
    if page < pages - 1:
        nav.append({"text": "▶️", "callback_data": f"nw|p|{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "➕ Create new folder", "callback_data": "nw|c|0"}])
    rows.append([{"text": "✖️ Cancel", "callback_data": "nw|x|0"}])
    return {"inline_keyboard": rows}


CONFIRM_KB = {"inline_keyboard": [[
    {"text": "✅ Create & start", "callback_data": "nw|mk|0"},
    {"text": "✖️ Cancel", "callback_data": "nw|x|0"},
]]}


def kill_confirm_kb(disp):
    """Two-button confirm for killing the session named `disp`."""
    return {"inline_keyboard": [[
        {"text": "💀 Kill it", "callback_data": f"kl|y|{disp}"},
        {"text": "✖️ Cancel", "callback_data": f"kl|x|{disp}"},
    ]]}


def do_new(chat_id, name, workdir, reply_to=None):
    """Create the session (if absent), launch claude, and report it."""
    full = full_name(sanitize_name(name))
    if session_exists(full):
        # Already running — show its current status instead of recreating it.
        pane = capture_pane(full)
        mid = send(
            chat_id,
            f"ℹ️ <b>{html.escape(display_name(full))}</b> is already running.\n"
            + status_text(full, pane),
            reply_markup=action_keyboard(full, pane),
            reply_to=reply_to,
        )
        if mid:
            MSG_SESSION[mid] = full
        return
    ok, info = launch_session(full, workdir)
    if not ok:
        send(chat_id, f"⚠️ Could not start session:\n<code>{html.escape(info)}</code>",
             reply_to=reply_to)
        return
    mid = send(
        chat_id,
        f"🚀 Started <b>{html.escape(display_name(full))}</b>\n"
        f"📂 <code>{html.escape(workdir)}</code>\n"
        f"▶️ <code>{html.escape(info)}</code>\n"
        f"<i>↩️ Reply to this message to send input.</i>",
        reply_to=reply_to,
    )
    if mid:
        MSG_SESSION[mid] = full


# --------------------------------------------------------------------------
# Monitor tick
# --------------------------------------------------------------------------


def _pane_hash(pane):
    # stable across processes (unlike hash()) so persisted state survives restarts
    return hashlib.sha1(pane.encode("utf-8", "replace")).hexdigest()


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


def _session_field(session, fmt):
    r = _tmux(["display-message", "-p", "-t", session, fmt])
    return r.stdout.strip() if r.returncode == 0 else ""


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
        if st is None:
            # First time we see this session (fresh start or newly created):
            # treat whatever is on screen now as a baseline — don't notify it.
            STATE[session] = {"hash": digest, "count": IDLE_TICKS, "reported": True}
            continue
        if digest == st["hash"]:
            st["count"] += 1
            if st["count"] == IDLE_TICKS and not st["reported"]:
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


# --------------------------------------------------------------------------
# Update handlers
# --------------------------------------------------------------------------


def show_selection(chat_id, tag, prompt, reply_to=None):
    sessions = sessions_by_recency()
    if not sessions:
        send(chat_id, "No matching tmux sessions found.")
        return
    send(chat_id, prompt, reply_markup=build_keyboard(tag, sessions, 0),
         reply_to=reply_to)


def handle_message(msg):
    if msg.get("from", {}).get("id") != USER_ID:
        return
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")

    # 0) Waiting for a new-folder name (from the ➕ Create new folder button)?
    if NEW_DIR_WAIT.get(chat_id):
        if text.strip() and not text.startswith("/"):
            NEW_DIR_WAIT.pop(chat_id, None)
            folder = sanitize_name(text)
            workdir = os.path.join(PROJECT_ROOT, folder)
            try:
                os.makedirs(workdir, exist_ok=True)
            except OSError as exc:
                send(chat_id, f"⚠️ Couldn't create folder: {html.escape(str(exc))}")
                return
            do_new(chat_id, folder, workdir)
            return
        NEW_DIR_WAIT.pop(chat_id, None)  # a command cancels the wait; fall through

    # 1) Reply to a tracked report/status message -> relay to that session.
    reply = msg.get("reply_to_message")
    if reply and reply.get("message_id") in MSG_SESSION:
        if not text.strip():
            send(chat_id, "Nothing to send (empty message).")
            return
        session = MSG_SESSION[reply["message_id"]]
        if session not in set(list_sessions()):
            send(chat_id, f"Session <b>{html.escape(display_name(session))}</b> is gone.")
            return
        ok = send_input(session, text)
        send(
            chat_id,
            (f"✅ Sent to <b>{html.escape(display_name(session))}</b>"
             if ok else "⚠️ Failed to send (tmux error)."),
            reply_to=msg["message_id"],
        )
        return

    # 2) Commands.
    if text.startswith("/start") or text.startswith("/help"):
        send(
            chat_id,
            "<b>muxpost</b>\n"
            "• I report when a <code>" + html.escape(PREFIX) + "*</code> session goes idle.\n"
            "• Reply to any report/status to type into that session.\n"
            "• <code>/new</code> — pick a folder (or create one) and start a session.\n"
            "• <code>/new &lt;name&gt; [path]</code> — start one directly.\n"
            "• <code>/status</code> — pick a session to inspect.\n"
            "• <code>/status &lt;name&gt;</code> — inspect one directly.\n"
            "• <code>/stop</code> — interrupt Claude (pick one), or "
            "<code>/stop &lt;name&gt;</code> directly.\n"
            "• <code>/kill</code> — pick a session to kill (asks to confirm).\n"
            "• <code>/kill &lt;name&gt;</code> — kill one directly (asks to confirm).\n"
            "• <code>/restart</code> — restart me. <code>/upgrade</code> — update + restart.\n"
            "• Send plain text — I'll ask which session to send it to.",
        )
        return

    if text.startswith("/restart"):
        send(chat_id, f"♻️ Restarting on <code>{html.escape(version())}</code>…")
        write_notify(chat_id, "✅ muxpost is back up.")
        restart_inplace()
        return  # unreached (process is replaced)

    if text.startswith("/upgrade"):
        send(chat_id, "⬆️ Pulling latest…")
        ok, out = git_pull()
        body = f"<blockquote expandable>{html.escape(out[-1500:])}</blockquote>" if out else ""
        if not ok:
            send(chat_id, f"⚠️ Upgrade failed.\n{body}")
            return
        if "up to date" in out.lower() or "up-to-date" in out.lower():
            send(chat_id, f"✅ Already up to date on <code>{html.escape(version())}</code>.\n{body}")
            return
        send(chat_id, f"✅ Updated → <code>{html.escape(version())}</code>. Restarting…\n{body}")
        write_notify(chat_id, f"✅ muxpost back up on <code>{html.escape(version())}</code>.")
        restart_inplace()
        return  # unreached

    if text.startswith("/new"):
        if not PROJECT_ROOT:
            send(chat_id, "No project root configured. Run "
                          "<code>python3 setup.py</code> first.")
            return
        parts = text.split(maxsplit=2)
        if len(parts) == 1:
            dirs = list_subdirs(PROJECT_ROOT)
            send(chat_id,
                 f"📂 <b>{html.escape(PROJECT_ROOT)}</b>\n"
                 "Pick a folder for the new session, or create one:",
                 reply_markup=build_dir_keyboard(dirs, 0))
            return
        name = parts[1]
        if len(parts) == 3:
            workdir = os.path.abspath(os.path.expanduser(parts[2]))
        else:
            workdir = os.path.join(PROJECT_ROOT, name)
        if os.path.isdir(workdir):
            do_new(chat_id, name, workdir, reply_to=msg["message_id"])
        else:
            PENDING_NEW[chat_id] = {"name": name, "workdir": workdir}
            send(chat_id,
                 f"📁 <code>{html.escape(workdir)}</code> doesn't exist.\n"
                 f"Create it and start <b>{html.escape(sanitize_name(name))}</b>?",
                 reply_markup=CONFIRM_KB)
        return

    if text.startswith("/status"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            disp = parts[1].strip()
            full = full_name(disp)
            if full not in set(list_sessions()):
                send(chat_id, f"No session named <b>{html.escape(disp)}</b>.")
                return
            pane = capture_pane(full)
            mid = send(chat_id, status_text(full, pane),
                       reply_markup=action_keyboard(full, pane))
            if mid:
                MSG_SESSION[mid] = full
        else:
            show_selection(chat_id, "st", "Select a session to inspect:")
        return

    if text.startswith("/kill"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            disp = parts[1].strip()
            full = full_name(disp)
            if full not in set(list_sessions()):
                send(chat_id, f"No session named <b>{html.escape(disp)}</b>.")
                return
            send(chat_id,
                 f"⚠️ Kill <b>{html.escape(display_name(full))}</b>? "
                 "This ends the tmux session and its Claude process.",
                 reply_markup=kill_confirm_kb(display_name(full)))
        else:
            show_selection(chat_id, "kl", "Select a session to kill:")
        return

    if text.startswith("/stop"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            disp = parts[1].strip()
            full = full_name(disp)
            if full not in set(list_sessions()):
                send(chat_id, f"No session named <b>{html.escape(disp)}</b>.")
                return
            ok = send_interrupt(full)
            send(chat_id,
                 f"⏹ Interrupted <b>{html.escape(display_name(full))}</b>." if ok
                 else f"⚠️ Failed to interrupt <b>{html.escape(display_name(full))}</b>.")
        else:
            show_selection(chat_id, "sp", "Select a session to interrupt:")
        return

    if text.startswith("/"):
        send(chat_id, "Unknown command. Try /help.")
        return

    # 3) Plain text with no reply -> ask which session to send it to.
    if text.strip():
        PENDING[chat_id] = text
        show_selection(chat_id, "sn", "Send this to which session?",
                       reply_to=msg["message_id"])


def handle_callback(cq):
    if cq.get("from", {}).get("id") != USER_ID:
        answer(cq["id"], "Not authorized")
        return
    data = cq.get("data", "")
    msg = cq.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    parts = data.split("|", 2)
    if len(parts) < 2:
        answer(cq["id"])
        return
    tag, kind = parts[0], parts[1]
    val = parts[2] if len(parts) > 2 else ""

    # Refresh: re-capture the pane and rebuild this status/report message.
    if tag == "rf":
        full = full_name(kind)
        if not session_exists(full):
            answer(cq["id"], "Session is gone")
            edit(chat_id, message_id, reply_markup=None)
            return
        rebuild_status(chat_id, message_id, full)
        answer(cq["id"], "Refreshed")
        return

    # Send a queued/suggested message Claude staged in the prompt.
    if tag == "q":
        full = full_name(kind)
        if not session_exists(full):
            answer(cq["id"], "Session is gone")
            edit(chat_id, message_id, reply_markup=None)
            return
        ok = send_queued(full)
        answer(cq["id"], "Sent ✓" if ok else "Failed")
        rebuild_status(chat_id, message_id, full,
                       note="▶️ Sent queued message" if ok
                            else "⚠️ Failed to send queued message")
        return

    # Pick an option from a Claude selection menu (press the number).
    if tag == "o":
        full = full_name(kind)
        if not session_exists(full):
            answer(cq["id"], "Session is gone")
            edit(chat_id, message_id, reply_markup=None)
            return
        ok = _tmux(["send-keys", "-t", full, val]).returncode == 0
        if ok:
            mark_sent(full)
        answer(cq["id"], f"Selected {val} ✓" if ok else "Failed")
        rebuild_status(chat_id, message_id, full,
                       note=f"✅ Selected option {val}" if ok
                            else f"⚠️ Failed to select option {val}")
        return

    # New-session / folder picker flow.
    if tag == "nw":
        if kind == "p":
            try:
                page = int(val)
            except ValueError:
                page = 0
            edit(chat_id, message_id,
                 reply_markup=build_dir_keyboard(list_subdirs(PROJECT_ROOT), page))
            answer(cq["id"])
            return
        if kind == "c":  # "create new folder" -> wait for a typed name
            NEW_DIR_WAIT[chat_id] = True
            PENDING_NEW.pop(chat_id, None)
            edit(chat_id, message_id,
                 text="✏️ Send a name for the new folder, created under "
                      f"<code>{html.escape(PROJECT_ROOT)}</code>.",
                 reply_markup={"inline_keyboard": [[
                     {"text": "✖️ Cancel", "callback_data": "nw|x|0"}]]})
            answer(cq["id"])
            return
        if kind == "x":  # cancel a pending create-confirm / name entry
            PENDING_NEW.pop(chat_id, None)
            NEW_DIR_WAIT.pop(chat_id, None)
            edit(chat_id, message_id, text="✖️ Cancelled.")
            answer(cq["id"])
            return
        if kind == "mk":  # confirm: create the folder, then launch
            req = PENDING_NEW.pop(chat_id, None)
            if not req:
                edit(chat_id, message_id, text="⌛ That request expired. Try /new again.")
                answer(cq["id"])
                return
            try:
                os.makedirs(req["workdir"], exist_ok=True)
            except OSError as exc:
                edit(chat_id, message_id,
                     text=f"⚠️ Couldn't create folder: {html.escape(str(exc))}")
                answer(cq["id"])
                return
            edit(chat_id, message_id,
                 text=f"📁 Created <code>{html.escape(req['workdir'])}</code>")
            do_new(chat_id, req["name"], req["workdir"])
            answer(cq["id"])
            return
        if kind == "s":  # an existing folder was picked
            workdir = os.path.join(PROJECT_ROOT, val)
            if not os.path.isdir(workdir):
                edit(chat_id, message_id, text=f"📁 <b>{html.escape(val)}</b> is gone.")
                answer(cq["id"])
                return
            edit(chat_id, message_id, text=f"📂 <code>{html.escape(workdir)}</code>")
            do_new(chat_id, val, workdir)
            answer(cq["id"])
            return
        answer(cq["id"])
        return

    # Kill flow: pick -> confirm -> kill (pagination falls through to generic).
    if tag == "kl" and kind in ("s", "y", "x"):
        full = full_name(val)
        disp = html.escape(display_name(full))
        if kind == "x":
            edit(chat_id, message_id, text=f"✖️ Cancelled. <b>{disp}</b> is untouched.")
            answer(cq["id"])
            return
        if kind == "s":  # session picked -> ask for confirmation
            if full not in set(list_sessions()):
                edit(chat_id, message_id, text=f"Session <b>{disp}</b> is gone.")
                answer(cq["id"])
                return
            edit(chat_id, message_id,
                 text=f"⚠️ Kill <b>{disp}</b>? "
                      "This ends the tmux session and its Claude process.",
                 reply_markup=kill_confirm_kb(display_name(full)))
            answer(cq["id"])
            return
        if kind == "y":  # confirmed -> kill
            if not session_exists(full):
                edit(chat_id, message_id, text=f"<b>{disp}</b> is already gone.")
                answer(cq["id"])
                return
            ok = kill_session(full)
            edit(chat_id, message_id,
                 text=f"💀 Killed <b>{disp}</b>." if ok
                      else f"⚠️ Failed to kill <b>{disp}</b>.")
            answer(cq["id"], "Killed" if ok else "Failed")
            return

    # Cancel a picker (status / send / kill / stop).
    if kind == "c":
        PENDING.pop(chat_id, None)
        edit(chat_id, message_id, text="✖️ Cancelled.")
        answer(cq["id"])
        return

    # Pagination.
    if kind == "p":
        try:
            page = int(val)
        except ValueError:
            page = 0
        edit(chat_id, message_id, reply_markup=build_keyboard(tag, sessions_by_recency(), page))
        answer(cq["id"])
        return

    # Session chosen.
    if kind == "s":
        full = full_name(val)
        if full not in set(list_sessions()):
            edit(chat_id, message_id, text=f"Session <b>{html.escape(val)}</b> is gone.")
            answer(cq["id"])
            return

        if tag == "st":
            pane = capture_pane(full)
            edit(chat_id, message_id, text=status_text(full, pane),
                 reply_markup=action_keyboard(full, pane))
            MSG_SESSION[message_id] = full
            answer(cq["id"], "Reply to that message to type into it.")
            return

        if tag == "sn":
            pending = PENDING.pop(chat_id, None)
            if pending is None:
                edit(chat_id, message_id, text="⌛ That request expired. Send the message again.")
                answer(cq["id"])
                return
            ok = send_input(full, pending)
            disp = html.escape(display_name(full))
            preview = html.escape(pending if len(pending) <= 80 else pending[:77] + "…")
            edit(
                chat_id,
                message_id,
                text=(f"✅ Sent to <b>{disp}</b>:\n<code>{preview}</code>"
                      if ok else f"⚠️ Failed to send to <b>{disp}</b>."),
            )
            answer(cq["id"])
            return

        if tag == "sp":
            ok = send_interrupt(full)
            disp = html.escape(display_name(full))
            edit(chat_id, message_id,
                 text=f"⏹ Interrupted <b>{disp}</b>." if ok
                      else f"⚠️ Failed to interrupt <b>{disp}</b>.")
            answer(cq["id"], "Interrupted" if ok else "Failed")
            return

    answer(cq["id"])


def dispatch(update):
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    except Exception as exc:  # noqa: BLE001
        print(f"[dispatch] error: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# Main loop (long-poll getUpdates + scheduled monitor ticks)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Process management / restart / upgrade
# --------------------------------------------------------------------------


def version():
    r = subprocess.run(["git", "-C", _HERE, "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def git_pull():
    r = subprocess.run(["git", "-C", _HERE, "pull", "--ff-only"],
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
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), "run"])


# Commands shown in Telegram's command menu / autocomplete (via setMyCommands).
BOT_COMMANDS = [
    {"command": "status", "description": "Inspect a session (or pick one)"},
    {"command": "new", "description": "Start a new claude session"},
    {"command": "stop", "description": "Interrupt Claude in a session (or pick one)"},
    {"command": "kill", "description": "Kill a session (or pick one)"},
    {"command": "restart", "description": "Restart muxpost"},
    {"command": "upgrade", "description": "Update muxpost, then restart"},
    {"command": "help", "description": "Show what muxpost can do"},
]


def register_commands():
    res = api("setMyCommands", commands=BOT_COMMANDS)
    if res.get("ok"):
        print(f"registered {len(BOT_COMMANDS)} bot commands")
    else:
        print(f"[warn] setMyCommands failed: {res}", file=sys.stderr)


def main():
    require_config()
    me = api("getMe")
    if not me.get("ok"):
        print("Could not reach Telegram / bad token.", file=sys.stderr)
        sys.exit(1)
    print(f"Started as @{me['result'].get('username')} on {version()}. "
          f"Watching '{PREFIX}*' every {INTERVAL}s, "
          f"reporting after {IDLE_TICKS} idle ticks.")
    register_commands()
    load_state()  # restore report flags so a restart doesn't re-notify
    load_last_sent()  # restore picker ordering (last message sent per session)

    # record our PID and install an in-place restart on SIGHUP
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PIDFILE, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    if RESTART_SIG is not None:
        signal.signal(RESTART_SIG, lambda *_: restart_inplace())

    def _cleanup(*_):
        try:
            if _read_pid() == os.getpid():
                os.remove(PIDFILE)
        except OSError:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _cleanup)

    _flush_notify()  # tell the user we're back, if a restart queued it

    if RESTORE_SESSIONS:
        n = restore_from_snapshot()
        if n:
            print(f"restored {n} session(s) from snapshot")

    offset = load_offset()  # resume past already-handled updates (survives re-exec)
    last_tick = 0.0
    last_snapshot = 0.0
    while True:
        now = time.monotonic()
        if now - last_tick >= INTERVAL:
            last_tick = now
            try:
                monitor_tick()
            except Exception as exc:  # noqa: BLE001
                print(f"[monitor] error: {exc}", file=sys.stderr)
        if now - last_snapshot >= SNAPSHOT_INTERVAL:
            last_snapshot = now
            try:
                snapshot_sessions()
            except Exception as exc:  # noqa: BLE001
                print(f"[snapshot] error: {exc}", file=sys.stderr)

        # Long-poll only until the next tick is due (keeps ticks on time).
        wait = max(1, int(INTERVAL - (time.monotonic() - last_tick)))
        res = api(
            "getUpdates",
            _timeout=wait + 10,
            offset=offset,
            timeout=wait,
            allowed_updates=["message", "callback_query"],
        )
        if not res.get("ok"):
            time.sleep(1)
            continue
        for update in res["result"]:
            offset = update["update_id"] + 1
            save_offset(offset)  # persist BEFORE dispatch: a /restart re-execs
            dispatch(update)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

USAGE = """muxpost — Telegram controller for tmux sessions

usage: muxpost <command>

  run        run the bot in the foreground (default)
  start      start the bot in the background
  stop       stop the running bot
  restart    restart the running bot in place (or start it)
  upgrade    git pull the latest, then restart the running bot
  status     show version and whether the bot is running
  restore    recreate snapshot sessions that aren't running (resume claude)
  snapshot   record current claude-* sessions for later restore
  init       configure muxpost (token, user id, project root)
  doctor     run the preflight health check
  help       show this message
"""


def cli_start():
    if running_pid():
        print(f"already running (pid {running_pid()})")
        return
    require_config()
    os.makedirs(STATE_DIR, exist_ok=True)
    logf = open(os.path.join(_HERE, "muxpost.log"), "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "run"],
        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"started in background (pid {proc.pid}); logs: {_HERE}/muxpost.log")


def cli_stop():
    pid = running_pid()
    if not pid:
        print("not running")
        return
    os.kill(pid, signal.SIGTERM)
    print(f"stopped (pid {pid})")


def cli_restart():
    pid = running_pid()
    if pid and RESTART_SIG is not None:
        os.kill(pid, RESTART_SIG)
        print(f"restart signal sent (pid {pid})")
    elif pid:
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
        cli_start()
    else:
        print("not running — starting")
        cli_start()


def cli_upgrade():
    print("pulling latest…")
    ok, out = git_pull()
    print(out or ("(no output)" if ok else "pull failed"))
    if not ok:
        sys.exit(1)
    pid = running_pid()
    if pid and RESTART_SIG is not None:
        os.kill(pid, RESTART_SIG)
        print(f"reloaded running bot (pid {pid}) → {version()}")
    elif pid:
        cli_restart()
        print(f"→ {version()}")
    else:
        print(f"now at {version()} (bot not running; start with: muxpost start)")


def cli_status():
    pid = running_pid()
    print(f"muxpost {version()}  ({_HERE})")
    print(f"running: yes (pid {pid})" if pid else "running: no")


def cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        main()
    elif cmd == "start":
        cli_start()
    elif cmd == "stop":
        cli_stop()
    elif cmd == "restart":
        cli_restart()
    elif cmd == "upgrade":
        cli_upgrade()
    elif cmd == "status":
        cli_status()
    elif cmd == "restore":
        n = restore_from_snapshot()
        print(f"restored {n} session(s)" if n else "nothing to restore "
              "(no snapshot, or all sessions already running)")
    elif cmd == "snapshot":
        snapshot_sessions()
        print(f"snapshot written to {SNAPSHOT_FILE}")
    elif cmd in ("init", "setup"):
        subprocess.run([sys.executable, os.path.join(_HERE, "setup.py")])
    elif cmd == "doctor":
        sys.exit(subprocess.run([sys.executable, os.path.join(_HERE, "doctor.py")]).returncode)
    elif cmd in ("help", "-h", "--help"):
        print(USAGE)
    else:
        print(f"unknown command: {cmd}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    try:
        cli()
    except KeyboardInterrupt:
        print("\nbye")
