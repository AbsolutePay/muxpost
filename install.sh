#!/usr/bin/env bash
#
# muxpost installer — Linux, macOS, and WSL.
#
# One-line install (works once the repo is public):
#   curl -fsSL https://raw.githubusercontent.com/AbsolutePay/muxpost/main/install.sh | bash
#   # pass flags after --, e.g.  ... | bash -s -- --service
#
# From a checkout:
#   ./install.sh              install the `muxpost` command
#   ./install.sh --service    also register an auto-start service
#   ./install.sh --uninstall  remove the command and any service
#
# After installing, configure with:  muxpost init
# Needs git + python3 (and tmux at runtime).

set -eu

# --- locate ourselves ------------------------------------------------------
SOURCE="${BASH_SOURCE[0]:-$0}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" >/dev/null 2>&1 && pwd)"

# --- pretty printing -------------------------------------------------------
if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1m'; N=$'\033[0m'
else G=""; Y=""; R=""; B=""; N=""; fi
info() { printf "%s\n" "$1"; }
ok()   { printf "%s✓%s %s\n" "$G" "$N" "$1"; }
warn() { printf "%s!%s %s\n" "$Y" "$N" "$1"; }
die()  { printf "%s✗ %s%s\n" "$R" "$1" "$N" >&2; exit 1; }

is_wsl() { grep -qi microsoft /proc/version 2>/dev/null || [ -n "${WSL_DISTRO_NAME:-}" ]; }
has_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; }

MARK_BEGIN="# >>> muxpost autostart >>>"
MARK_END="# <<< muxpost autostart <<<"

remove_profile_block() {
  local PROFILE
  for PROFILE in "$HOME/.bashrc" "$HOME/.profile"; do
    [ -f "$PROFILE" ] || continue
    if grep -qF "$MARK_BEGIN" "$PROFILE" 2>/dev/null; then
      awk -v b="$MARK_BEGIN" -v e="$MARK_END" \
        '$0==b{skip=1} skip&&$0==e{skip=0;next} !skip{print}' \
        "$PROFILE" > "$PROFILE.muxtmp" && mv "$PROFILE.muxtmp" "$PROFILE"
    fi
  done
}

# --- flags -----------------------------------------------------------------
DO_SERVICE=0; DO_UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --service)   DO_SERVICE=1 ;;
    --uninstall) DO_UNINSTALL=1 ;;
    -h|--help)
      sed -n '3,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $arg (try --help)" ;;
  esac
done

# --- find python -----------------------------------------------------------
PYTHON=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' 2>/dev/null; then
      PYTHON="$(command -v "$cand")"; break
    fi
  fi
done

OS="$(uname -s)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/muxpost"

# --- service paths ---------------------------------------------------------
SYSTEMD_UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/muxpost.service"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/com.muxpost.agent.plist"

uninstall() {
  info "${B}Uninstalling muxpost${N}"
  if [ "$OS" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now muxpost.service >/dev/null 2>&1 || true
    [ -f "$SYSTEMD_UNIT" ] && rm -f "$SYSTEMD_UNIT" && ok "removed systemd service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  if [ "$OS" = "Darwin" ]; then
    launchctl unload "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
    [ -f "$LAUNCHD_PLIST" ] && rm -f "$LAUNCHD_PLIST" && ok "removed launchd agent"
  fi
  remove_profile_block && ok "removed any shell-profile autostart"
  [ -f "$LAUNCHER" ] && rm -f "$LAUNCHER" && ok "removed $LAUNCHER"
  info "Done. (config.json and the project folder were left untouched.)"
  exit 0
}

[ "$DO_UNINSTALL" = "1" ] && uninstall

# --- obtain the project (clone from GitHub when not run from a checkout) ----
REPO_URL="${MUXPOST_REPO:-https://github.com/AbsolutePay/muxpost.git}"
INSTALL_DIR="${MUXPOST_HOME:-$HOME/.local/share/muxpost}"
if [ ! -f "$SCRIPT_DIR/muxpost.py" ]; then
  command -v git >/dev/null 2>&1 || die "git is required to install muxpost."
  if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating muxpost in $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only -q || warn "update failed; using existing copy"
  else
    info "Cloning $REPO_URL into $INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 -q "$REPO_URL" "$INSTALL_DIR" \
      || die "clone failed — is the repo public, or have you run: gh auth setup-git ?"
  fi
  SCRIPT_DIR="$INSTALL_DIR"
fi

# --- install ---------------------------------------------------------------
info "${B}Installing muxpost${N} from $SCRIPT_DIR"
[ -n "$PYTHON" ] || die "Python 3.8+ not found. Install python3 and re-run."
ok "python: $PYTHON ($("$PYTHON" -V 2>&1))"

if command -v tmux >/dev/null 2>&1; then
  ok "tmux: $(command -v tmux) ($(tmux -V 2>/dev/null))"
else
  warn "tmux not found — muxpost needs it at runtime (on Windows use WSL)."
fi

# launcher on PATH
mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$PYTHON" "$SCRIPT_DIR/muxpost.py" "\$@"
EOF
chmod +x "$LAUNCHER"
ok "installed command: $LAUNCHER"

case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) warn "$BIN_DIR is not on your PATH. Add this to your shell profile:"
     printf '      export PATH="%s:$PATH"\n' "$BIN_DIR" ;;
esac

# config status (configuration is done separately via `muxpost init`)
HAS_CONFIG=0
[ -f "$SCRIPT_DIR/config.json" ] && HAS_CONFIG=1
[ "$HAS_CONFIG" = "1" ] && ok "config.json already present"

# optional auto-start service
install_service_profile() {
  # For WSL (no systemd) and other init-less setups: launch from shell profile,
  # guarded so it only starts once even across many shells.
  PROFILE="$HOME/.bashrc"; [ -f "$PROFILE" ] || PROFILE="$HOME/.profile"
  remove_profile_block
  cat >> "$PROFILE" <<EOF
$MARK_BEGIN
# start muxpost in the background once per session (e.g. WSL without systemd)
if command -v pgrep >/dev/null 2>&1 && ! pgrep -f "$SCRIPT_DIR/muxpost.py" >/dev/null 2>&1; then
  nohup "$PYTHON" "$SCRIPT_DIR/muxpost.py" >> "$SCRIPT_DIR/muxpost.log" 2>&1 &
  disown 2>/dev/null || true
fi
$MARK_END
EOF
  ok "autostart added to $PROFILE (starts on next shell). Logs: $SCRIPT_DIR/muxpost.log"
  warn "open a new shell to start it now, or run: muxpost &"
  if is_wsl; then
    info "      Tip: to start muxpost without opening a WSL shell, add a Windows"
    info "      Task Scheduler task at logon running:"
    info "        wsl.exe -d ${WSL_DISTRO_NAME:-<distro>} -e $PYTHON $SCRIPT_DIR/muxpost.py"
  fi
}

install_service_linux() {
  if ! has_systemd; then
    if is_wsl; then
      warn "WSL without systemd detected — using shell-profile autostart instead."
    else
      warn "systemd not running — using shell-profile autostart instead."
    fi
    install_service_profile
    return
  fi
  mkdir -p "$(dirname "$SYSTEMD_UNIT")"
  cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=muxpost — telegram tmux controller
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON $SCRIPT_DIR/muxpost.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now muxpost.service
  ok "service enabled (systemd user). Logs: journalctl --user -u muxpost -f"
  warn "for it to run while logged out: sudo loginctl enable-linger $USER"
}

install_service_macos() {
  mkdir -p "$(dirname "$LAUNCHD_PLIST")"
  cat > "$LAUNCHD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.muxpost.agent</string>
  <key>ProgramArguments</key>
  <array><string>$PYTHON</string><string>$SCRIPT_DIR/muxpost.py</string></array>
  <key>WorkingDirectory</key><string>$SCRIPT_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$SCRIPT_DIR/muxpost.log</string>
  <key>StandardErrorPath</key><string>$SCRIPT_DIR/muxpost.log</string>
</dict>
</plist>
EOF
  launchctl unload "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
  launchctl load "$LAUNCHD_PLIST"
  ok "service loaded (launchd). Logs: $SCRIPT_DIR/muxpost.log"
}

if [ "$DO_SERVICE" = "1" ]; then
  [ "$HAS_CONFIG" = "1" ] || warn "no config yet — run 'muxpost init' before the service can start."
  info "${B}Registering auto-start service…${N}"
  case "$OS" in
    Linux)  install_service_linux ;;
    Darwin) install_service_macos ;;
    *) warn "unknown OS '$OS'; skipping service." ;;
  esac
fi

info ""
ok "${B}muxpost installed.${N}"
if [ "$HAS_CONFIG" = "1" ]; then
  info "Next   : ${B}muxpost${N} to run   (or ${B}muxpost start${N} for background)"
else
  info "Next   : ${B}muxpost init${N}    to configure (token, user id, project root)"
  info "Then   : ${B}muxpost${N}         to run   (or ${B}muxpost start${N})"
fi
info "Check  : muxpost doctor"
info "Remove : $SCRIPT_DIR/install.sh --uninstall"
