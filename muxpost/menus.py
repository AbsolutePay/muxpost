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

from core.config import PAGE_SIZE, SETTINGS_SPEC, setting
from core.sessions import _is_divider, display_name, list_dir_entries


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
    """Return [{num, label, check}, …] if a selection menu is showing, else None.

    Claude renders a numbered menu ('❯ 1. …', '  2. …') with an
    'Enter to select · ↑/↓ to navigate · Esc to cancel' footer. `check` is
    None for a single-select option, or True/False for a multi-select checkbox
    ('1. [✔] …' / '2. [ ] …') — multi-select toggles per keypress and commits
    on Enter. Description lines (deeper indent) are ignored.

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
        # Leading marker is '❯ ' (highlighted) or a little whitespace; enriched
        # menus indent non-highlighted options by a single space.
        m = re.match(r"^\s{0,3}(?:❯\s+)?(\d+)\.\s+(.+)$", line)
        if not m:
            continue
        num = int(m.group(1))
        if opts and num != opts[-1]["num"] - 1:
            break                    # sequence broke — top of the menu reached
        # Drop any preview-box art that shares the option's line (┌ │ └ …).
        label = re.split(r"[─-╿]", m.group(2))[0].strip()
        check = None
        cb = re.match(r"^\[(.)\]\s*(.*)$", label)  # '[✔] …' / '[ ] …' checkbox
        if cb:
            check = cb.group(1).strip() != ""      # any non-blank mark = checked
            label = cb.group(2).strip()
        opts.append({"num": num, "label": label, "check": check})
    opts.reverse()
    return opts or None


def needs_explicit_submit(pane):
    """True for Claude's form/wizard select, where a number only *highlights*
    and you press Enter to commit — as opposed to a plain select that submits
    on the number press.

    These render a ballot-box step header ('☐ Next step', or a multi-tab
    '←  ☒ Onboarding  ☐ Root  ✔ Submit  →') and offer 'add notes' / 'switch
    questions' affordances in the footer. A plain Yes/No select shows none of
    that. We look at the bottom region only so a checklist earlier in the
    conversation can't trigger it.
    """
    if not pane:
        return False
    lines = pane.split("\n")
    footer = "\n".join([l for l in lines if l.strip()][-3:])
    if "to add notes" in footer or "to switch questions" in footer:
        return True
    # Fallback: a ballot-box step header ('☐ Next step', '←  ☒ A  ☐ B  →') sits
    # right under the menu's divider rule — a conversation checklist never does.
    prev = ""
    for ln in lines:
        if ("☐" in ln or "☒" in ln) and _is_divider(prev):
            return True
        if ln.strip():
            prev = ln
    return False


def action_keyboard(full, pane):
    """Context buttons for a report/status: menu options or a queued message,
    plus a Refresh button that re-captures the pane."""
    disp = display_name(full)
    rows = []
    opts = detect_menu(pane)
    if opts:
        checkbox = any(o["check"] is not None for o in opts)
        for o in opts:
            if o["check"] is None:
                t = f"{o['num']}. {o['label']}"
            else:
                t = f"{'☑' if o['check'] else '☐'} {o['num']}. {o['label']}"
            rows.append([{"text": t if len(t) <= 45 else t[:44] + "…",
                          "callback_data": f"o|{disp}|{o['num']}"}])
        # "Chat about this" is sometimes a numbered option, sometimes an
        # unnumbered escape at the very bottom. Add a button for the unnumbered
        # case (it can't be reached by a number key).
        labels = {o["label"].lower() for o in opts}
        if "chat about this" not in labels and re.search(
                r"(?mi)^[ \t]*(?:❯[ \t]*)?chat about this[ \t]*$", pane):
            rows.append([{"text": "💬 Chat about this", "callback_data": f"o|{disp}|chat"}])
        if checkbox:
            # Checkbox multi-select: a number toggles, Enter toggles the
            # highlighted box. Commit by moving right to the Submit tab + Enter.
            rows.append([{"text": "✅ Submit", "callback_data": f"o|{disp}|Submit"}])
        elif needs_explicit_submit(pane):
            # Form/wizard select: a number only highlights the current question;
            # Enter commits it (and advances to the next, if any).
            rows.append([{"text": "⏎ Submit", "callback_data": f"o|{disp}|Enter"}])
    else:
        q = detect_queue(pane)
        if q:
            label = q if len(q) <= 40 else q[:39] + "…"
            rows.append([{"text": f"▶️ Send queued: {label}",
                          "callback_data": f"q|{disp}"}])
    rows.append([{"text": "⌨️ Keys", "callback_data": f"k|{disp}|pad"},
                 {"text": "🔄 Refresh", "callback_data": f"rf|{disp}"}])
    return {"inline_keyboard": rows}


# Common Claude Code keys, sent verbatim to the pane via `tmux send-keys`.
# Names are tmux key names (BTab = Shift-Tab, C-c = Ctrl-C). Esc interrupts,
# Shift-Tab cycles permission modes, PageUp/Down scroll the transcript.
KEYS = [
    ("←", "Left"), ("↑", "Up"), ("↓", "Down"), ("→", "Right"),
    ("PgUp", "PageUp"), ("PgDn", "PageDown"), ("Esc", "Escape"), ("⏎ Enter", "Enter"),
    ("Tab", "Tab"), ("⇧Tab", "BTab"), ("Ctrl-C", "C-c"),
]
KEY_LABEL = {key: lbl for lbl, key in KEYS}


def keys_keyboard(disp):
    """Key-pad for a status message: each button sends that key to the session
    and refreshes. Replaces the option buttons while open; Close brings them
    back, Refresh re-captures and stays on the pad."""
    rows, row = [], []
    for lbl, key in KEYS:
        row.append({"text": lbl, "callback_data": f"k|{disp}|{key}"})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "✖️ Close", "callback_data": f"k|{disp}|close"},
                 {"text": "🔄 Refresh", "callback_data": f"k|{disp}|pad"}])
    return {"inline_keyboard": rows}


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


def build_file_keyboard(path, page):
    """File browser for /getfile: dirs to descend, files to upload, parent + nav.

    Entries are addressed by their index in the combined (dirs + files) list so
    long/odd names never blow the 64-byte callback_data limit.
    """
    dirs, files = list_dir_entries(path)
    entries = [("d", d) for d in dirs] + [("f", f) for f in files]
    pages = max(1, math.ceil(len(entries) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    rows = []
    for i in range(start, min(start + PAGE_SIZE, len(entries))):
        kind, name = entries[i]
        icon = "📁" if kind == "d" else "📄"
        label = f"{icon} {name}"
        rows.append([{"text": label if len(label) <= 45 else label[:44] + "…",
                      "callback_data": f"gf|{kind}|{i}"}])
    if not entries:
        rows.append([{"text": "(empty folder)", "callback_data": "gf|noop"}])
    nav = []
    if page > 0:
        nav.append({"text": "◀️", "callback_data": f"gf|p|{page - 1}"})
    if pages > 1:
        nav.append({"text": f"{page + 1}/{pages}", "callback_data": f"gf|p|{page}"})
    if page < pages - 1:
        nav.append({"text": "▶️", "callback_data": f"gf|p|{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "⬆️ Parent", "callback_data": "gf|up"},
                 {"text": "✖️ Cancel", "callback_data": "gf|x"}])
    return {"inline_keyboard": rows}


def kill_confirm_kb(disp):
    """Two-button confirm for killing the session named `disp`."""
    return {"inline_keyboard": [[
        {"text": "💀 Kill it", "callback_data": f"kl|y|{disp}"},
        {"text": "✖️ Cancel", "callback_data": f"kl|x|{disp}"},
    ]]}


def settings_keyboard():
    """One button per setting (shows current value, taps to cycle) + Close."""
    rows = [[{"text": f"{spec['label']}: {setting(key)}", "callback_data": f"set|{key}"}]
            for key, spec in SETTINGS_SPEC.items()]
    rows.append([{"text": "✖️ Close", "callback_data": "set|close"}])
    return {"inline_keyboard": rows}
