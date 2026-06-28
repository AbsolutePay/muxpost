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

from core.config import PREFIX, PROJECT_ROOT, STATE_DIR, USER_ID, setting
from core.sessions import capture_pane, display_name, full_name, launch_session, list_sessions, list_subdirs, sanitize_name, session_cwd, session_exists
from muxpost.control import send_input, send_interrupt, session_from_reply, sessions_by_recency
from muxpost.menus import CONFIRM_KB, action_keyboard, build_dir_keyboard, build_file_keyboard, build_keyboard, kill_confirm_kb, settings_keyboard
from muxpost.panes import _pane_hash, status_text
from muxpost.process import git_pull, restart_inplace, version, write_notify
from muxpost.state import GETFILE_DIR, MSG_SESSION, NEW_DIR_WAIT, PENDING, PENDING_FILE, PENDING_NEW, STATE, last_session, remember_session
from muxpost.telegram import download_file, edit, send, send_document

INCOMING_DIR = os.path.join(STATE_DIR, "incoming")  # downloaded user files land here
INCOMING_TTL_DAYS = 7


def prune_incoming(ttl_days=INCOMING_TTL_DAYS):
    """Delete downloaded files older than ttl_days from the incoming dir.

    Called at startup so received files don't accumulate forever. Returns the
    number removed.
    """
    removed = 0
    try:
        cutoff = time.time() - ttl_days * 86400
        for name in os.listdir(INCOMING_DIR):
            p = os.path.join(INCOMING_DIR, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass  # dir doesn't exist yet — nothing to prune
    return removed


def _message_file(msg):
    """If the message carries a downloadable file, return (file_id, name|None)."""
    if msg.get("document"):
        d = msg["document"]
        return d["file_id"], d.get("file_name")
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"], None        # largest size; name derived
    for key in ("video", "audio", "voice", "animation", "video_note"):
        if msg.get(key):
            return msg[key]["file_id"], msg[key].get("file_name")
    return None


def _relay_file(chat_id, session, path, caption, reply_to=None):
    """Type the caption + downloaded file path into a session as one line."""
    cap = " ".join((caption or "").split())  # single line — newlines submit early
    relayed = f"{cap} [file: {path}]" if cap else f"[file: {path}]"
    ok = send_input(session, relayed)
    disp = html.escape(display_name(session))
    send(chat_id,
         (f"✅ File sent to <b>{disp}</b>:\n<code>{html.escape(path)}</code>"
          if ok else f"⚠️ Failed to send file to <b>{disp}</b>."),
         reply_to=reply_to)


def handle_incoming_file(msg, finfo):
    """Download a file the user sent, then relay its path (+caption) to a session."""
    chat_id = msg["chat"]["id"]
    file_id, fname = finfo
    caption = msg.get("caption") or ""
    path, err = download_file(file_id, INCOMING_DIR, fname)
    if not path:
        send(chat_id, f"⚠️ Couldn't download that file: {html.escape(err)}")
        return
    session = session_from_reply(msg.get("reply_to_message"))
    if session:
        if session not in set(list_sessions()):
            send(chat_id, f"Session <b>{html.escape(display_name(session))}</b> is gone.")
            return
        _relay_file(chat_id, session, path, caption, reply_to=msg["message_id"])
    else:
        PENDING_FILE[chat_id] = {"path": path, "caption": caption}
        show_selection(chat_id, "sf", "Send this file to which session?",
                       reply_to=msg["message_id"])


def rebuild_status(chat_id, message_id, full, note=None, markup=None):
    """Re-capture the pane and rewrite a status/report message in place.

    Keeps the message live: fresh pane, current menu/queue buttons, and an
    always-present Refresh button. `note` (e.g. what action just ran) is shown
    with a timestamp; falling back to a plain refresh stamp.
    """
    pane = capture_pane(full)
    if pane is not None:
        # We just showed the user this exact screen, so treat it as a baseline:
        # scrolling/refreshing changes the pane but shouldn't trigger a fresh
        # idle notification. Re-fires only when the pane changes AFTER this.
        STATE[full] = {"hash": _pane_hash(pane), "count": setting("idle_ticks"),
                       "reported": True}
    stamp = time.strftime("%H:%M:%S")
    tail = f"{note} · {stamp}" if note else f"🔄 Refreshed {stamp}"
    edit(chat_id, message_id,
         text=status_text(full, pane) + f"\n<i>{tail}</i>",
         reply_markup=markup if markup is not None else action_keyboard(full, pane))
    MSG_SESSION[message_id] = full
    remember_session(full)  # auto-route target: latest session you viewed


def do_getfile(chat_id, text, root):
    """Handle /getfile: upload an explicit path, or open the browser at `root`.

    `root` is the project root for a bare /getfile, or a session's folder when
    /getfile is sent as a reply to that session. Relative paths resolve there.
    """
    root = root or PROJECT_ROOT or os.path.expanduser("~")
    parts = text.split(maxsplit=1)
    if len(parts) == 2:  # explicit path -> upload if it exists
        p = os.path.expanduser(parts[1].strip())
        if not os.path.isabs(p):
            p = os.path.join(root, p)
        p = os.path.abspath(p)
        if not os.path.isfile(p):
            send(chat_id, f"No file at <code>{html.escape(p)}</code>.")
            return
        ok, err = send_document(chat_id, p, caption=f"📄 <code>{html.escape(p)}</code>")
        if not ok:
            send(chat_id, f"⚠️ Couldn't send <code>{html.escape(p)}</code>"
                          + (f": {html.escape(err)}" if err else "") + ".")
    else:  # no path -> open the file browser at root
        GETFILE_DIR[chat_id] = root
        send(chat_id, f"📂 <code>{html.escape(root)}</code>\nPick a file to send:",
             reply_markup=build_file_keyboard(root, 0))


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
        remember_session(full)
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
    remember_session(full)


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

    # A) A file/photo attachment -> download it and relay its path to a session.
    finfo = _message_file(msg)
    if finfo:
        handle_incoming_file(msg, finfo)
        return

    # 1) Reply to a report/status/action message -> relay straight to that
    # session, no guessing. The target is recovered even after a restart.
    session = session_from_reply(msg.get("reply_to_message"))
    if session:
        if session not in set(list_sessions()):
            send(chat_id, f"Session <b>{html.escape(display_name(session))}</b> is gone.")
            return
        # Replying with /getfile browses that session's folder, not relays it.
        if text.strip().startswith("/getfile"):
            do_getfile(chat_id, text.strip(), session_cwd(session))
            return
        if not text.strip():
            send(chat_id, "Nothing to send (empty message).")
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
            "• <code>/getfile</code> — browse from the project root and send a file here.\n"
            "• <code>/getfile &lt;path&gt;</code> — send that file directly if it exists.\n"
            "• Reply to a session with <code>/getfile</code> — browse that project's folder.\n"
            "• <code>/setting</code> — configure pane view, notify threshold, etc.\n"
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
            remember_session(full)
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

    if text.startswith("/setting"):
        send(chat_id, "⚙️ <b>Settings</b>\nTap a row to change it.",
             reply_markup=settings_keyboard())
        return

    if text.startswith("/getfile"):
        do_getfile(chat_id, text, PROJECT_ROOT)
        return

    if text.startswith("/"):
        send(chat_id, "Unknown command. Try /help.")
        return

    # 3) Plain text with no reply.
    if text.strip():
        # Auto-route (opt-in): send to the session of the last message muxpost
        # sent, skipping the "which session?" pick. Falls through to the prompt
        # if it's off, nothing's been sent yet, or that session is gone.
        target = last_session()
        if setting("auto_route") and target and target in set(list_sessions()):
            ok = send_input(target, text)
            send(chat_id,
                 (f"✅ Sent to <b>{html.escape(display_name(target))}</b> "
                  "<i>(auto-routed)</i>" if ok else "⚠️ Failed to send (tmux error)."),
                 reply_to=msg["message_id"])
            return
        PENDING[chat_id] = text
        show_selection(chat_id, "sn", "Send this to which session?",
                       reply_to=msg["message_id"])
