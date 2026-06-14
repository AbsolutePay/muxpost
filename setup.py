#!/usr/bin/env python3
"""
setup.py — interactive init for muxpost.

Prompts for the bot token, your Telegram user id, and the root project
directory new sessions are created under, then writes config.json.
Re-run any time; it keeps existing values unless you change them.

    python3 setup.py
"""

import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def bot_username(token):
    """Return the bot's @username via getMe, or None if the token is bad."""
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
        if data.get("ok"):
            return data["result"].get("username")
    except Exception:  # noqa: BLE001
        return None
    return None
CFG_PATH = os.path.join(HERE, "config.json")

cfg = {}
if os.path.exists(CFG_PATH):
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not read existing config.json: {exc}")


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        print("\nNo interactive input — run `muxpost init` in a terminal.", file=sys.stderr)
        sys.exit(1)
    return val or default


print("muxpost setup")
print("-------------\n")

# --- bot token -------------------------------------------------------------
cur_token = cfg.get("bot_token", "")
hint = " (configured — leave blank to keep)" if cur_token else ""
token = ask(f"Bot token from @BotFather{hint}", cur_token)
if token:
    uname = bot_username(token)
    if uname:
        print(f"  ✓ verified — bot is @{uname}")
    else:
        print("  ⚠️  couldn't verify this token (bad token or no network)")

# --- user id ---------------------------------------------------------------
cur_uid = str(cfg.get("user_id", "") or "")
while True:
    uid = ask(f"Your numeric Telegram user id (from @userinfobot)", cur_uid)
    if uid.isdigit() and int(uid) != 0:
        uid = int(uid)
        break
    print("  please enter a numeric id (e.g. 123456789)")

# --- project root ----------------------------------------------------------
home = os.path.expanduser("~")
candidates = []


def add(path):
    if not path:
        return
    p = os.path.abspath(os.path.expanduser(path))
    if p not in candidates:
        candidates.append(p)


add(cfg.get("project_root"))
add(os.path.join(home, "projects"))
add(os.getcwd())
add(os.path.dirname(os.getcwd()))
add(home)

print("\nWhere do your projects live? New sessions launch in a folder under this root.")
for i, c in enumerate(candidates, 1):
    mark = "exists" if os.path.isdir(c) else "missing"
    print(f"  {i}) {c}  ({mark})")
choice = ask("\nPick a number, or type another path", "1")

if choice.isdigit() and 1 <= int(choice) <= len(candidates):
    root = candidates[int(choice) - 1]
else:
    root = os.path.abspath(os.path.expanduser(choice))

if not os.path.isdir(root):
    if ask(f"{root} doesn't exist — create it? (y/N)", "n").lower().startswith("y"):
        try:
            os.makedirs(root, exist_ok=True)
            print(f"  created {root}")
        except OSError as exc:
            print(f"  could not create it: {exc}")
    else:
        print("  (saving anyway; create it before running the bot)")

# --- autostart -------------------------------------------------------------
cur_auto = bool(cfg.get("autostart", False))
default_auto = "y" if cur_auto else "n"
auto_ans = ask("Start muxpost automatically (background + on boot)? (y/N)", default_auto)
autostart = auto_ans.lower().startswith("y")

# --- restore sessions ------------------------------------------------------
cur_restore = bool(cfg.get("restore_sessions", False))
default_restore = "y" if cur_restore else "n"
restore_ans = ask("Bring tmux sessions back automatically on startup "
                  "(after a reboot)? (y/N)", default_restore)
restore_sessions = restore_ans.lower().startswith("y")

# --- write -----------------------------------------------------------------
cfg.update({
    "bot_token": token,
    "user_id": uid,
    "project_root": root,
    "autostart": autostart,
    "restore_sessions": restore_sessions,
})
cfg.setdefault("prefix", "claude-")
cfg.setdefault("interval", 5)
cfg.setdefault("idle_ticks", 3)
cfg.setdefault("page_size", 5)

with open(CFG_PATH, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")

print(f"\nWrote {CFG_PATH}")
print(f"  prefix       {cfg['prefix']}")
print(f"  project_root {cfg['project_root']}")
print(f"  autostart    {autostart}")
print(f"  restore      {restore_sessions}")
if not token:
    print("\n⚠️  No bot token set — add it before running the bot.")

# --- apply autostart via the platform installer ----------------------------
if os.name == "nt":
    installer = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", os.path.join(HERE, "install.ps1")]
    flag = "-ServiceOnly" if autostart else "-ServiceOff"
else:
    installer = ["bash", os.path.join(HERE, "install.sh")]
    flag = "--service-only" if autostart else "--service-off"
try:
    print("\n" + ("Enabling autostart…" if autostart else "Disabling autostart…"))
    subprocess.run(installer + [flag], check=False)
except Exception as exc:  # noqa: BLE001
    print(f"(autostart step skipped: {exc})")

print("\nNext: muxpost doctor   then   muxpost")
