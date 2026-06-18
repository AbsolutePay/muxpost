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

from core.config import IDLE_TICKS, INTERVAL, PIDFILE, PREFIX, PROJECT_ROOT, RESTART_SIG, RESTORE_SESSIONS, ROOT, SNAPSHOT_FILE, SNAPSHOT_INTERVAL, STATE_DIR, require_config
from core.sessions import display_name, full_name, launch_session, list_subdirs, sanitize_name, session_cwd, session_exists
from muxpost.callbacks import handle_callback
from muxpost.control import sessions_by_recency
from muxpost.handlers import handle_message
from muxpost.monitor import monitor_tick, restore_from_snapshot, snapshot_sessions
from muxpost.process import _flush_notify, _read_pid, git_pull, restart_inplace, running_pid, version
from muxpost.state import load_last_sent, load_offset, load_settings, load_state, save_offset
from muxpost.telegram import api


def dispatch(update):
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    except Exception as exc:  # noqa: BLE001
        print(f"[dispatch] error: {exc}", file=sys.stderr)


BOT_COMMANDS = [
    {"command": "status", "description": "Inspect a session (or pick one)"},
    {"command": "new", "description": "Start a new claude session"},
    {"command": "stop", "description": "Interrupt Claude in a session (or pick one)"},
    {"command": "kill", "description": "Kill a session (or pick one)"},
    {"command": "getfile", "description": "Send a file here (browse, or /getfile <path>)"},
    {"command": "setting", "description": "Configure muxpost (pane view, notify, …)"},
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
    load_settings()  # restore user settings (pane view, notify threshold, …)

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


USAGE = """muxpost — Telegram controller for tmux sessions

usage: muxpost <command>

  run        run the bot in the foreground (default)
  start      start the bot in the background
  stop       stop the running bot
  restart    restart the running bot in place (or start it)
  upgrade    git pull the latest, then restart the running bot
  status     show version and whether the bot is running
  list       list running claude-* sessions (most recent first)
  new [name]     start a claude session (arrow-key folder picker if no name)
  attach <name>  attach to a session's terminal (tmux attach / switch-client)
  restore    recreate snapshot sessions that aren't running (resume claude)
  snapshot   record current claude-* sessions for later restore
  mcp        run as an MCP server over stdio (agent -> user messaging)
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
    logf = open(os.path.join(ROOT, "muxpost.log"), "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, ENTRY, "run"],
        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"started in background (pid {proc.pid}); logs: {ROOT}/muxpost.log")


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
    print(f"muxpost {version()}  ({ROOT})")
    print(f"running: yes (pid {pid})" if pid else "running: no")


def cli_list():
    load_last_sent()  # so ordering reflects what you last worked on
    sessions = sessions_by_recency()
    if not sessions:
        print(f"No {PREFIX}* sessions running.")
        return
    print(f"{len(sessions)} session(s), most recent first:")
    for full in sessions:
        print(f"  {display_name(full):<24} {session_cwd(full) or ''}")
    print("\nattach with:  muxpost attach <name>")


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


def cli_attach(name):
    if not name:
        print("usage: muxpost attach <session-name>  (see: muxpost list)", file=sys.stderr)
        sys.exit(2)
    full = full_name(name)
    if not session_exists(full):
        print(f"no session '{display_name(full)}' — see: muxpost list", file=sys.stderr)
        sys.exit(1)
    _exec_attach(full)


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
    numbered prompt when stdin/stdout isn't a TTY."""
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
    elif cmd in ("list", "ls"):
        cli_list()
    elif cmd == "new":
        cli_new(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "attach":
        cli_attach(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "restore":
        n = restore_from_snapshot()
        print(f"restored {n} session(s)" if n else "nothing to restore "
              "(no snapshot, or all sessions already running)")
    elif cmd == "snapshot":
        snapshot_sessions()
        print(f"snapshot written to {SNAPSHOT_FILE}")
    elif cmd == "mcp":
        from muxpost.mcp import serve
        serve()
    elif cmd in ("init", "setup"):
        subprocess.run([sys.executable, os.path.join(ROOT, "setup.py")])
    elif cmd == "doctor":
        sys.exit(subprocess.run([sys.executable, os.path.join(ROOT, "doctor.py")]).returncode)
    elif cmd in ("help", "-h", "--help"):
        print(USAGE)
    else:
        print(f"unknown command: {cmd}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(1)
