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

from core.config import MAX_CHARS, setting
from core.sessions import _is_divider, display_name
from muxpost.menus import detect_menu


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
    """Clean the capture, trim to the latest lines, wrap in a blockquote.

    Whether the quote is expandable (collapsed by default) or always shown, and
    how many lines it keeps, are user settings (/setting).
    """
    if pane is None:
        return "<i>(could not capture pane)</i>"
    # Open tag may carry the 'expandable' attribute; the close tag is always
    # plain </blockquote> (the attribute on a close tag is invalid HTML).
    open_tag = "blockquote expandable" if setting("pane_view") == "expandable" else "blockquote"
    lines = clean_pane(pane).split("\n")
    if not lines or not any(l.strip() for l in lines):
        return f"<{open_tag}><i>(empty)</i></blockquote>"
    n = setting("pane_lines")
    if len(lines) > n:
        lines = lines[-n:]
    body = "\n".join(lines)
    if len(body) > MAX_CHARS:
        body = body[-MAX_CHARS:]
    return f"<{open_tag}>{html.escape(body)}</blockquote>"


def status_text(full, pane, header_emoji="🖥"):
    disp = html.escape(display_name(full))
    return (
        f"{header_emoji} <b>{disp}</b>\n"
        f"{render_pane(pane)}\n"
        f"<i>↩️ Reply to this message to send input.</i>"
    )


def _normalize_menu_state(pane):
    """Blank a menu's selection state for idle-hashing.

    Cursor position, checkbox marks, wizard-tab progress, and the selection-
    driven preview box all change as you check/navigate options — but that's
    you fiddling, not new activity, so it shouldn't reset idle tracking and fire
    another notification. Drop those, and blank lines (preview height varies per
    option), leaving the question + option labels as the stable signature.
    """
    out = []
    for line in pane.split("\n"):
        line = re.split(r"[─-╿]", line)[0]               # preview-box / framed art
        line = line.replace("❯", " ")                    # navigation cursor
        line = re.sub(r"\[[ xX✔✗]\]", "[]", line)        # checkbox state
        line = line.replace("☒", "☐").replace("✔", "☐")  # wizard-tab progress
        line = " ".join(line.split())  # collapse spacing (cursor shifts indent)
        if line:
            out.append(line)
    return "\n".join(out)


def _pane_hash(pane):
    # stable across processes (unlike hash()) so persisted state survives restarts.
    # Hash the CLEANED pane — the same content we show in the report — not the raw
    # capture. Otherwise volatile chrome that clean_pane strips (the status footer,
    # the '/clear to save … tokens' hint, dividers) can flicker, flip idle tracking,
    # and fire a second report with a visibly identical screen.
    # While a menu is up, hash with its selection state normalized away instead, so
    # checking/toggling options doesn't reset idle tracking and re-notify.
    text = _normalize_menu_state(pane) if detect_menu(pane) else clean_pane(pane)
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
