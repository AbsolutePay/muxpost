#!/usr/bin/env python3
"""
doctor.py — preflight check for muxpost.

Verifies that everything the bot relies on is present and healthy:
  - Python version
  - tmux binary (external) and a running server
  - claude CLI (external, optional — the thing your sessions usually run)
  - bot configuration (token + user id, from env or config.json)
  - Telegram reachability + token validity (getMe)
  - matching tmux sessions and a live pane capture

Exit code is 0 when there are no failures (warnings are allowed), 1 otherwise.
Run:  python3 doctor.py
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_GREEN, _YELLOW, _RED, _RST = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    _GREEN = _YELLOW = _RED = _RST = ""

results = []


def check(status, title, detail=""):
    color = {PASS: _GREEN, WARN: _YELLOW, FAIL: _RED}[status]
    mark = {PASS: "✓", WARN: "!", FAIL: "✗"}[status]
    line = f"{color}[{mark}] {title}{_RST}"
    if detail:
        line += f"\n      {detail}"
    print(line)
    results.append(status)


# --------------------------------------------------------------------------
# Config loading (does NOT exit on missing values, unlike muxpost.py)
# --------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG = {}
_cfg_path = os.path.join(_HERE, "config.json")
if os.path.exists(_cfg_path):
    try:
        with open(_cfg_path, "r", encoding="utf-8") as fh:
            _CFG = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        check(FAIL, "config.json present but unreadable", str(exc))


def conf(env, key, default=None):
    val = os.environ.get(env)
    if val:
        return val
    if _CFG.get(key) not in (None, ""):
        return _CFG[key]
    return default


TOKEN = conf("TG_BOT_TOKEN", "bot_token")
USER_ID = conf("TG_USER_ID", "user_id")
PREFIX = conf("TG_PREFIX", "prefix", "claude-")
PROJECT_ROOT = conf("TG_PROJECT_ROOT", "project_root")


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

print("muxpost doctor\n" + "-" * 33)

# Python
if sys.version_info >= (3, 8):
    check(PASS, f"Python {sys.version.split()[0]}")
else:
    check(FAIL, f"Python {sys.version.split()[0]} — need 3.8+")

# tmux binary
tmux_path = shutil.which("tmux")
if tmux_path:
    try:
        ver = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        ver = f"version check failed: {exc}"
    check(PASS, f"tmux found ({ver})", tmux_path)

    # tmux server / sessions
    res = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        check(WARN, "tmux server not running (no sessions yet)",
              res.stderr.strip() or "start a session, then re-run")
        sessions = []
    else:
        sessions = res.stdout.split()
        matching = [s for s in sessions if s.startswith(PREFIX)]
        if matching:
            shown = ", ".join(s[len(PREFIX):] for s in sorted(matching)[:8])
            more = "" if len(matching) <= 8 else f" (+{len(matching) - 8} more)"
            check(PASS, f"{len(matching)} session(s) matching '{PREFIX}*'", shown + more)
        else:
            check(WARN, f"no sessions matching '{PREFIX}*'",
                  f"{len(sessions)} total session(s); check the prefix")
else:
    check(FAIL, "tmux not found on PATH", "install tmux (or run inside WSL on Windows)")
    sessions = []

# Pane capture test
matching = [s for s in sessions if s.startswith(PREFIX)] if sessions else []
if matching:
    res = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", matching[0]],
        capture_output=True, text=True,
    )
    if res.returncode == 0:
        check(PASS, "pane capture works",
              f"{len(res.stdout.splitlines())} lines from '{matching[0][len(PREFIX):]}'")
    else:
        check(FAIL, "pane capture failed", res.stderr.strip())

# claude CLI (external, optional)
claude_path = shutil.which("claude")
if claude_path:
    check(PASS, "claude CLI found", claude_path)
else:
    check(WARN, "claude CLI not on PATH",
          "optional — only needed if these sessions run Claude Code")

# Config presence
if TOKEN:
    check(PASS, "bot token configured")
else:
    check(FAIL, "bot token missing", "set TG_BOT_TOKEN or config.json bot_token")

if USER_ID and str(USER_ID).isdigit() and int(USER_ID) != 0:
    check(PASS, f"authorized user id set ({USER_ID})")
else:
    check(FAIL, "user id missing/invalid", "set TG_USER_ID or config.json user_id")

# Project root (used by /new)
if PROJECT_ROOT:
    root = os.path.abspath(os.path.expanduser(str(PROJECT_ROOT)))
    if os.path.isdir(root):
        subs = [e for e in os.listdir(root)
                if not e.startswith(".") and os.path.isdir(os.path.join(root, e))]
        check(PASS, f"project root set ({root})", f"{len(subs)} folder(s) available to /new")
    else:
        check(FAIL, "project root does not exist", root)
else:
    check(WARN, "project root not set", "run setup.py — /new needs it to create sessions")

# Telegram reachability + token validity
if TOKEN:
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/getMe"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
        if data.get("ok"):
            check(PASS, "Telegram reachable, token valid",
                  f"@{data['result'].get('username')}")
        else:
            check(FAIL, "Telegram rejected the token", str(data))
    except urllib.error.HTTPError as exc:
        check(FAIL, "Telegram token invalid", f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        check(WARN, "could not reach Telegram", str(exc))

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

print("-" * 33)
fails = results.count(FAIL)
warns = results.count(WARN)
if fails:
    print(f"{_RED}{fails} failure(s){_RST}, {warns} warning(s) — fix failures before running muxpost.py")
    sys.exit(1)
elif warns:
    print(f"{_YELLOW}All required checks passed{_RST} with {warns} warning(s).")
    sys.exit(0)
else:
    print(f"{_GREEN}All checks passed — you're good to go: python3 muxpost.py{_RST}")
    sys.exit(0)
