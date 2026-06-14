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
- Send **plain text** (no reply) and the bot asks which session to deliver it to.
- The `claude-` prefix is hidden in the UI for readability.
- Pane contents are shown in an **expandable blockquote**.

## Install

One command sets up a `muxpost` command on your PATH and runs the interactive
setup. First create a bot with [@BotFather](https://t.me/BotFather) (copy the
token) and get your numeric id from [@userinfobot](https://t.me/userinfobot).

**Linux / macOS:**

```bash
./install.sh              # install command + run setup
./install.sh --service    # also auto-start (systemd user unit / launchd agent)
./install.sh --uninstall  # remove command + service
```

**WSL:** run `install.sh` inside your WSL distro. `--service` auto-detects WSL:
if systemd is enabled (`/etc/wsl.conf` `[boot] systemd=true`) it uses a systemd
user unit; otherwise it adds a guarded autostart line to your `~/.bashrc` that
launches muxpost in the background once per shell (logs to `muxpost.log`). To
start it without opening a shell, the installer also prints a Windows Task
Scheduler command you can add at logon.

**Windows (PowerShell):** muxpost shells out to `tmux`, which lives in WSL — so
the bot itself usually runs inside WSL (use `install.sh` there). On native
Windows the installer still wires up the command and config:

```powershell
./install.ps1             # install command + run setup
./install.ps1 -Service    # auto-start as a logon scheduled task
./install.ps1 -Uninstall
```

The installer only adds a small launcher to a user bin dir
(`~/.local/bin` / `%LOCALAPPDATA%\muxpost\bin`); the code stays in this folder.

## Manual setup

If you'd rather not use the installer:

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your numeric user id from [@userinfobot](https://t.me/userinfobot).
3. Configure. Easiest is the interactive init, which also picks your project root:

   ```bash
   python3 setup.py        # prompts for token, user id, and project root
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
