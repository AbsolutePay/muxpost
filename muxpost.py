#!/usr/bin/env python3
"""
muxpost — messaging for your terminal multiplexer.

A zero-dependency Telegram bot that watches every tmux session whose name
starts with a prefix (default "claude-"), reports when a session goes idle,
and relays your Telegram replies straight into that session via tmux send-keys.

Only the Python standard library is used, so it runs on Linux / macOS /
Windows wherever Python 3.8+ and tmux are available.

Configure via environment variables or a config.json next to this file:
    TG_BOT_TOKEN   Telegram bot token (from @BotFather)
    TG_USER_ID     numeric Telegram user id allowed to use the bot
    TG_PREFIX      session prefix to watch         (default "claude-")
    TG_INTERVAL    seconds between capture ticks    (default 5)
    TG_IDLE_TICKS  unchanged ticks before reporting (default 3)
    TG_PAGE_SIZE   sessions per selection page       (default 5)
"""
from muxpost.cli import cli

if __name__ == "__main__":
    cli()
