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

from core.config import PROJECT_ROOT, SETTINGS_SPEC, USER_ID
from core.sessions import _tmux, capture_pane, display_name, full_name, list_dir_entries, list_sessions, list_subdirs, session_exists
from muxpost.control import kill_session, send_input, send_interrupt, send_queued, sessions_by_recency
from muxpost.handlers import _relay_file, do_new, rebuild_status
from muxpost.menus import KEY_LABEL, action_keyboard, build_dir_keyboard, build_file_keyboard, build_keyboard, detect_menu, keys_keyboard, kill_confirm_kb, settings_keyboard
from muxpost.panes import status_text
from muxpost.state import GETFILE_DIR, MSG_SESSION, NEW_DIR_WAIT, PENDING, PENDING_FILE, PENDING_NEW, cycle_setting, mark_sent
from muxpost.telegram import answer, edit, send, send_document


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

    # Settings: cycle a setting in place, or close the menu.
    if tag == "set":
        if kind == "close":
            edit(chat_id, message_id, text="⚙️ Settings closed.", reply_markup=None)
            answer(cq["id"])
            return
        if kind in SETTINGS_SPEC:
            newval = cycle_setting(kind)
            edit(chat_id, message_id, reply_markup=settings_keyboard())
            answer(cq["id"], f"{SETTINGS_SPEC[kind]['label']}: {newval}")
            return
        answer(cq["id"])
        return

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

    # Key-pad: open it (hides the option buttons), send a key, or close it.
    if tag == "k":
        full = full_name(kind)
        if not session_exists(full):
            answer(cq["id"], "Session is gone")
            edit(chat_id, message_id, reply_markup=None)
            return
        if val == "close":
            rebuild_status(chat_id, message_id, full, note="⌨️ Closed keys")
            answer(cq["id"])
            return
        if val == "pad":  # open the pad / refresh while on it
            rebuild_status(chat_id, message_id, full, note="⌨️ Keys",
                           markup=keys_keyboard(kind))
            answer(cq["id"])
            return
        ok = _tmux(["send-keys", "-t", full, val]).returncode == 0
        if ok:
            mark_sent(full)
        label = KEY_LABEL.get(val, val)
        answer(cq["id"], f"Sent {label}" if ok else "Failed")
        rebuild_status(chat_id, message_id, full,
                       note=f"⌨️ {label}" if ok else f"⚠️ Failed: {label}",
                       markup=keys_keyboard(kind))
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

    # Drive a Claude selection menu: a number selects (single-select) or toggles
    # a checkbox (multi-select). "Submit" commits a multi-select by moving right
    # to the Submit tab then pressing Enter. rebuild_status re-captures, so a
    # toggled ☐→☑ shows immediately.
    if tag == "o":
        full = full_name(kind)
        if not session_exists(full):
            answer(cq["id"], "Session is gone")
            edit(chat_id, message_id, reply_markup=None)
            return
        if val == "Submit":          # checkbox menu: go to Submit tab, then Enter
            _tmux(["send-keys", "-t", full, "Right"])
            ok = _tmux(["send-keys", "-t", full, "Enter"]).returncode == 0
        elif val == "Enter":         # enriched wizard: Enter commits the question
            ok = _tmux(["send-keys", "-t", full, "Enter"]).returncode == 0
        elif val == "chat":          # unnumbered "Chat about this": it's the
            # bottom-most item and Down clamps there, so over-step then Enter.
            n = len(detect_menu(capture_pane(full)) or [])
            _tmux(["send-keys", "-t", full] + ["Down"] * (n + 3))
            ok = _tmux(["send-keys", "-t", full, "Enter"]).returncode == 0
        else:                        # a number: select / toggle
            ok = _tmux(["send-keys", "-t", full, val]).returncode == 0
        if ok:
            mark_sent(full)
        if val in ("Submit", "Enter"):
            answer(cq["id"], "Submitted ✓" if ok else "Failed")
            note = "✅ Submitted" if ok else "⚠️ Failed to submit"
        elif val == "chat":
            answer(cq["id"], "Chat about this ✓" if ok else "Failed")
            note = "💬 Chat about this" if ok else "⚠️ Failed"
        else:
            answer(cq["id"], f"Sent {val} ✓" if ok else "Failed")
            note = f"✅ Sent option {val}" if ok else f"⚠️ Failed to send {val}"
        rebuild_status(chat_id, message_id, full, note=note)
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

    # /getfile browser: descend dirs, go to parent, paginate, upload a file.
    if tag == "gf":
        if kind == "x":
            GETFILE_DIR.pop(chat_id, None)
            edit(chat_id, message_id, text="✖️ Cancelled.")
            answer(cq["id"])
            return
        cur = GETFILE_DIR.get(chat_id)
        if not cur:
            edit(chat_id, message_id, text="⌛ That browse expired. Run /getfile again.")
            answer(cq["id"])
            return
        if kind == "noop":
            answer(cq["id"])
            return
        if kind == "p":
            page = int(val) if val.isdigit() else 0
            edit(chat_id, message_id, reply_markup=build_file_keyboard(cur, page))
            answer(cq["id"])
            return
        if kind == "up":
            cur = GETFILE_DIR[chat_id] = os.path.dirname(cur.rstrip("/")) or "/"
            edit(chat_id, message_id,
                 text=f"📂 <code>{html.escape(cur)}</code>\nPick a file to send:",
                 reply_markup=build_file_keyboard(cur, 0))
            answer(cq["id"])
            return
        # d (descend) / f (upload): resolve the index against a fresh listing.
        dirs, files = list_dir_entries(cur)
        entries = [("d", d) for d in dirs] + [("f", f) for f in files]
        idx = int(val) if val.isdigit() else -1
        if not (0 <= idx < len(entries)) or entries[idx][0] != kind:
            edit(chat_id, message_id, reply_markup=build_file_keyboard(cur, 0))
            answer(cq["id"], "Listing changed — refreshed")
            return
        target = os.path.join(cur, entries[idx][1])
        if kind == "d":
            cur = GETFILE_DIR[chat_id] = target
            edit(chat_id, message_id,
                 text=f"📂 <code>{html.escape(cur)}</code>\nPick a file to send:",
                 reply_markup=build_file_keyboard(cur, 0))
            answer(cq["id"])
            return
        # kind == "f": upload it
        answer(cq["id"], "Uploading…")
        ok, err = send_document(chat_id, target,
                                caption=f"📄 <code>{html.escape(target)}</code>")
        if not ok:
            send(chat_id, f"⚠️ Couldn't send <code>{html.escape(target)}</code>"
                          + (f": {html.escape(err)}" if err else "") + ".")
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

    # Cancel a picker (status / send / kill / stop / file).
    if kind == "c":
        PENDING.pop(chat_id, None)
        PENDING_FILE.pop(chat_id, None)
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

        if tag == "sf":
            pending = PENDING_FILE.pop(chat_id, None)
            if pending is None:
                edit(chat_id, message_id, text="⌛ That file expired. Send it again.")
                answer(cq["id"])
                return
            edit(chat_id, message_id, text=f"📎 Sending file to <b>{html.escape(display_name(full))}</b>…")
            _relay_file(chat_id, full, pending["path"], pending["caption"])
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
