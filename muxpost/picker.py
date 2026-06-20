"""Interactive terminal helpers for the CLI: the `muxpost new` folder picker
and attaching to a session. Split out of cli.py to keep that file focused on
command dispatch and the bot loop.
"""
import os
import sys
import time

from core.config import PROJECT_ROOT
from core.sessions import display_name, full_name, launch_session, list_subdirs, sanitize_name, session_exists


def _exec_attach(full):
    """Replace this process with tmux so it owns the terminal. switch-client
    when we're already inside tmux (attach would refuse to nest)."""
    argv = (["tmux", "switch-client", "-t", full] if os.environ.get("TMUX")
            else ["tmux", "attach-session", "-t", full])
    try:
        os.execvp("tmux", argv)
    except OSError as exc:
        print(f"couldn't run tmux: {exc}", file=sys.stderr)
        sys.exit(1)


def _pick_numbered(options, title):
    if title:
        print(title)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    try:
        raw = input("Pick a number: ").strip()
    except EOFError:
        return None
    return int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(options) else None


def _pick(options, title=""):
    """Arrow-key selector; returns the chosen index or None. Falls back to a
    numbered prompt when stdin/stdout isn't a TTY (or termios is unavailable)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _pick_numbered(options, title)
    try:
        import select as _sel
        import termios
        import tty
    except ImportError:
        return _pick_numbered(options, title)
    if title:
        print(title)
    print("  ↑/↓ move · Enter select · q cancel")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    idx, n = 0, len(options)

    def render(redraw):
        if redraw:
            sys.stdout.write(f"\x1b[{n}A")
        for i, o in enumerate(options):
            if i == idx:
                sys.stdout.write("\r\x1b[2K\x1b[7m❯ " + str(o) + "\x1b[0m\r\n")
            else:
                sys.stdout.write("\r\x1b[2K  " + str(o) + "\r\n")
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        render(False)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                return idx
            if ch in ("q", "\x03"):                 # q / Ctrl-C
                return None
            if ch == "j":
                idx = (idx + 1) % n; render(True)
            elif ch == "k":
                idx = (idx - 1) % n; render(True)
            elif ch == "\x1b":                       # arrow key, or bare Esc
                if _sel.select([fd], [], [], 0.05)[0] and sys.stdin.read(1) == "[":
                    arrow = sys.stdin.read(1)
                    if arrow == "A":
                        idx = (idx - 1) % n; render(True)
                    elif arrow == "B":
                        idx = (idx + 1) % n; render(True)
                else:
                    return None                      # bare Esc cancels
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\r")
        sys.stdout.flush()


def cli_new(name=None):
    if not PROJECT_ROOT:
        print("No project root configured. Run: muxpost init", file=sys.stderr)
        sys.exit(1)
    if name:
        folder = name
    else:
        dirs = list_subdirs(PROJECT_ROOT)
        labels = ["📁 " + d for d in dirs] + ["➕  create a new folder"]
        pick = _pick(labels, f"New claude session — folder under {PROJECT_ROOT}:")
        if pick is None:
            print("cancelled")
            return
        if pick == len(dirs):                        # create-new option
            try:
                folder = input("New folder name: ").strip()
            except EOFError:
                folder = ""
            if not folder:
                print("no name — cancelled")
                return
        else:
            folder = dirs[pick]
    workdir = os.path.join(PROJECT_ROOT, folder)
    if not os.path.isdir(workdir):
        os.makedirs(workdir, exist_ok=True)
        print(f"created {workdir}")
    full = full_name(sanitize_name(folder))
    if session_exists(full):
        print(f"{display_name(full)} already running — attaching…")
    else:
        ok, info = launch_session(full, workdir)
        if not ok:
            print(f"failed to start: {info}", file=sys.stderr)
            sys.exit(1)
        print(f"started {display_name(full)} in {workdir} ({info}) — attaching…")
        time.sleep(0.3)
    _exec_attach(full)
