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


def capture_pane(session):
    res = _tmux(["capture-pane", "-p", "-t", session])
    if res.returncode != 0:
        return None
    return res.stdout


def session_exists(full):
    return _tmux(["has-session", "-t", full]).returncode == 0


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


def _is_divider(line):
    """True for the box-drawing rules that frame Claude's input box."""
    chars = [c for c in line if not c.isspace()]
    return bool(chars) and all(0x2500 <= ord(c) <= 0x257F for c in chars)


def list_dir_entries(path):
    """(dirs, files) in `path`, each sorted; dirs first. Empty on error."""
    try:
        names = os.listdir(path)
    except OSError:
        return [], []
    dirs, files = [], []
    for n in names:
        full = os.path.join(path, n)
        (dirs if os.path.isdir(full) else files).append(n)
    return sorted(dirs), sorted(files)


def session_cwd(full):
    """The working directory of a session's active pane (its project folder)."""
    p = _session_field(full, "#{pane_current_path}")
    return p if p and os.path.isdir(p) else None


def _session_field(session, fmt):
    r = _tmux(["display-message", "-p", "-t", session, fmt])
    return r.stdout.strip() if r.returncode == 0 else ""
