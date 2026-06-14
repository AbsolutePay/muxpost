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
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
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
hint = f" (current: {cur_token[:8]}…, blank = keep)" if cur_token else ""
token = ask(f"Bot token from @BotFather{hint}", cur_token)

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

# --- write -----------------------------------------------------------------
cfg.update({
    "bot_token": token,
    "user_id": uid,
    "project_root": root,
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
if not token:
    print("\n⚠️  No bot token set — add it before running the bot.")
print("\nNext: python3 doctor.py   then   python3 muxpost.py")
