# muxpost

*Messaging for your terminal multiplexer.*

A tiny Telegram bot that controls all your `claude-*` tmux sessions from chat.

- **Zero dependencies** — pure Python 3 standard library (`urllib`, `subprocess`).
  Runs on Linux, macOS, and Windows (anywhere Python 3.8+ and `tmux` exist).
- Captures each `claude-*` session's pane every 5s and hashes it.
- When a session's pane is **unchanged for 3 ticks (~15s)** it sends **one** idle
  report — it won't repeat until the pane changes and goes idle again.
- **Reply** to any report or status message to type that text into the session
  (`tmux send-keys` + Enter).
- `/new` lists the folders under your project root as buttons (or lets you create
  a new folder), then creates a `claude-<name>` session there and launches
  `claude`, auto-resuming the latest conversation if that folder has history
  (`claude --continue`). `/new <name> [path]` does it directly.
- `/status` shows paginated session buttons (5 per page); `/status <name>` shows
  one directly.
- `/restart` restarts the bot in place; `/upgrade` does a `git pull` then
  restarts onto the new version and pings you when it's back.
- Send **plain text** (no reply) and the bot asks which session to deliver it to.
- The `claude-` prefix is hidden in the UI for readability.
- Pane contents are shown in an **expandable blockquote**.

## Install

The installer clones muxpost from GitHub, puts a `muxpost` command on your PATH,
and leaves configuration to `muxpost init`. Then:

```
muxpost init      # configure: bot token, your user id, project root
muxpost           # run it   (or: muxpost start  for the background)
```

**Linux / macOS / WSL:**

```bash
curl -fsSL https://raw.githubusercontent.com/AbsolutePay/muxpost/main/install.sh | bash
# add an auto-start service:        ... | bash -s -- --service
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/AbsolutePay/muxpost/main/install.ps1 | iex
```

> The one-liners need the repo to be **public**. While it's private, clone first
> (`gh repo clone AbsolutePay/muxpost && cd muxpost && ./install.sh`) — the
> installer detects it's already a checkout and skips the clone.

Before running `muxpost init`, create a bot with
[@BotFather](https://t.me/BotFather) (copy the token) and get your numeric id
from [@userinfobot](https://t.me/userinfobot).

The installer clones to `~/.local/share/muxpost` (override with `MUXPOST_HOME`)
and adds a launcher to a user bin dir (`~/.local/bin` /
`%LOCALAPPDATA%\muxpost\bin`). Re-running it updates the clone. Remove with
`--service`'s counterpart: `<clone>/install.sh --uninstall` (or `-Uninstall`).

**WSL note:** `--service` auto-detects WSL — with systemd enabled
(`/etc/wsl.conf` `[boot] systemd=true`) it uses a systemd user unit; otherwise it
adds a guarded autostart line to `~/.bashrc` and prints a Windows Task Scheduler
command for logon start. On native Windows the bot needs `tmux`, which lives in
WSL, so run the bot inside WSL (`install.sh` there).

## Manual setup

If you'd rather not use the installer, clone the repo and:

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your numeric user id from [@userinfobot](https://t.me/userinfobot).
3. Configure. Easiest is the interactive init, which also picks your project root:

   ```bash
   python3 setup.py        # or: muxpost init  — prompts for token, user id, project root
   ```

   Or do it by hand (file or env vars):

   ```bash
   cp config.example.json config.json
   # edit config.json: set bot_token, user_id, project_root
   ```

   ```bash
   export TG_BOT_TOKEN="123456:ABC..."
   export TG_USER_ID="123456789"
   export TG_PROJECT_ROOT="/home/you/projects"
   ```

4. Check everything is healthy, then run it:

   ```bash
   python3 doctor.py   # verifies python, tmux, claude, config, Telegram token, sessions
   python3 muxpost.py
   ```

Send `/start` to your bot to confirm it's alive.

## Managing the bot

After installing, the `muxpost` command manages the running bot:

```bash
muxpost            # run in the foreground (same as: muxpost run)
muxpost start      # start in the background (logs to muxpost.log)
muxpost stop       # stop the running bot
muxpost restart    # restart in place (keeps the same PID)
muxpost upgrade    # git pull --ff-only, then restart onto the new version
muxpost status     # show version (git short SHA) and whether it's running
muxpost doctor     # run the preflight check
muxpost setup      # re-run interactive configuration
```

`restart` and `upgrade` work the same from chat: **`/restart`** and **`/upgrade`**.
Both re-exec the process in place via `os.execv`, so they behave identically
whether the bot runs under systemd, launchd, a WSL shell job, or by hand — the
PID is preserved, so your service manager never notices. A restart clears
in-memory state (older report messages become un-repliable; `/status` makes
fresh ones), and after `/upgrade` the bot messages you once it's back on the new
SHA. The CLI signals the running instance via a pidfile at
`~/.cache/muxpost/muxpost.pid`.

## Doctor

`doctor.py` is a preflight check you can run any time. It verifies the Python
version, the `tmux` binary and a running server, the `claude` CLI, your config
(token + user id), Telegram reachability and token validity (`getMe`), the
sessions matching your prefix, and a live pane capture. It exits non-zero if any
**required** check fails (warnings are fine), so it's safe to use in a wrapper
script before launching the bot.

## Config keys

| file key     | env var        | default    | meaning                              |
|--------------|----------------|------------|--------------------------------------|
| `bot_token`  | `TG_BOT_TOKEN` | —          | BotFather token (required)           |
| `user_id`    | `TG_USER_ID`   | —          | allowed Telegram user id (required)  |
| `project_root`| `TG_PROJECT_ROOT`| —      | root folder `/new` creates sessions in|
| `prefix`     | `TG_PREFIX`    | `claude-`  | session name prefix to watch         |
| `interval`   | `TG_INTERVAL`  | `5`        | seconds between capture ticks        |
| `idle_ticks` | `TG_IDLE_TICKS`| `3`        | unchanged ticks before an idle report|
| `page_size`  | `TG_PAGE_SIZE` | `5`        | session buttons per page             |

Only the configured `user_id` can use the bot; everyone else is ignored.

## Notes

- State is in-memory; restarting clears idle tracking and message→session links
  (older report messages become un-repliable, but `/status` regenerates fresh ones).
- On Windows, run it inside the same environment where `tmux` is available
  (e.g. WSL), since it shells out to the `tmux` binary.
