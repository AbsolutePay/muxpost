#!/usr/bin/env bash
#
# muxpost installer — Linux, macOS, and WSL.
#
# One-line install (works once the repo is public):
#   curl -fsSL https://raw.githubusercontent.com/AbsolutePay/muxpost/main/install.sh | bash
#
# It clones muxpost, installs the `muxpost` command, then runs `muxpost init`
# (configuration), which also asks whether to enable autostart.
#
# From a checkout:
#   ./install.sh              install the command, then run init
#   ./install.sh --no-init    install the command only (configure later)
#   ./install.sh --uninstall  remove the command and any autostart
#
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

# --- paths -----------------------------------------------------------------
OS="$(uname -s)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/muxpost"
SYSTEMD_UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/muxpost.service"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/com.muxpost.agent.plist"

# --- autostart service (defined early; called by --service-only/off + init) -
install_service_profile() {
  # WSL (no systemd) and other init-less setups: launch from the shell profile,
  # guarded so it only starts once even across many shells.
  local PROFILE; PROFILE="$HOME/.bashrc"; [ -f "$PROFILE" ] || PROFILE="$HOME/.profile"
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
  ok "autostart added to $PROFILE (starts each new shell). Logs: $SCRIPT_DIR/muxpost.log"
  # start now too, if configured and not already running
  if [ -f "$SCRIPT_DIR/config.json" ] && ! pgrep -f "$SCRIPT_DIR/muxpost.py" >/dev/null 2>&1; then
    nohup "$PYTHON" "$SCRIPT_DIR/muxpost.py" >> "$SCRIPT_DIR/muxpost.log" 2>&1 &
    ok "started now (pid $!)"
  fi
  if is_wsl; then
    info "      Tip: to start without opening a WSL shell, add a Windows Task"
    info "      Scheduler task at logon running:"
    info "        wsl.exe -d ${WSL_DISTRO_NAME:-<distro>} -e $PYTHON $SCRIPT_DIR/muxpost.py"
  fi
}

install_service_linux() {
  if ! has_systemd; then
    is_wsl && warn "WSL without systemd — using shell-profile autostart." \
           || warn "systemd not running — using shell-profile autostart."
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
  ok "autostart enabled (systemd user). Logs: journalctl --user -u muxpost -f"
  if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
    ok "linger is on — muxpost keeps running while you're logged out."
  else
    warn "muxpost will pause when you log out. To keep it running 24/7, run once:"
    info "        sudo loginctl enable-linger $USER"
  fi
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
  ok "autostart loaded (launchd). Logs: $SCRIPT_DIR/muxpost.log"
}

install_service() {
  case "$OS" in
    Linux)  install_service_linux ;;
    Darwin) install_service_macos ;;
    *) warn "unknown OS '$OS'; skipping autostart." ;;
  esac
}

remove_service() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now muxpost.service >/dev/null 2>&1 || true
    [ -f "$SYSTEMD_UNIT" ] && rm -f "$SYSTEMD_UNIT" && ok "removed systemd service"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  if [ "$OS" = "Darwin" ]; then
    launchctl unload "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
    [ -f "$LAUNCHD_PLIST" ] && rm -f "$LAUNCHD_PLIST" && ok "removed launchd agent"
  fi
  if grep -qsF "$MARK_BEGIN" "$HOME/.bashrc" "$HOME/.profile" 2>/dev/null; then
    remove_profile_block && ok "removed shell-profile autostart"
  fi
}

# --- find python -----------------------------------------------------------
PYTHON=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 \
     && "$cand" -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' 2>/dev/null; then
    PYTHON="$(command -v "$cand")"; break
  fi
done

# --- flags -----------------------------------------------------------------
DO_UNINSTALL=0; DO_INIT=1; SERVICE_ACTION=""
for arg in "$@"; do
  case "$arg" in
    --uninstall)    DO_UNINSTALL=1 ;;
    --no-init)      DO_INIT=0 ;;
    --service-only) SERVICE_ACTION="only" ;;   # used by `muxpost init`
    --service-off)  SERVICE_ACTION="off" ;;    # used by `muxpost init`
    -h|--help)      sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $arg (try --help)" ;;
  esac
done

# --- uninstall -------------------------------------------------------------
if [ "$DO_UNINSTALL" = "1" ]; then
  info "${B}Uninstalling muxpost${N}"
  remove_service
  [ -f "$LAUNCHER" ] && rm -f "$LAUNCHER" && ok "removed $LAUNCHER"
  info "Done. (config.json and the project folder were left untouched.)"
  exit 0
fi

# --- service-only / service-off (invoked by init) --------------------------
if [ -n "$SERVICE_ACTION" ]; then
  [ -n "$PYTHON" ] || die "Python 3.8+ not found."
  case "$SERVICE_ACTION" in
    only) install_service ;;
    off)  remove_service ;;
  esac
  exit 0
fi

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

mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$PYTHON" "$SCRIPT_DIR/muxpost.py" "\$@"
EOF
chmod +x "$LAUNCHER"
ok "installed command: $LAUNCHER"

case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) warn "$BIN_DIR is not on your PATH. Add to your shell profile:"
     printf '      export PATH="%s:$PATH"\n' "$BIN_DIR" ;;
esac

# --- run init (configuration + autostart question) -------------------------
if [ "$DO_INIT" = "1" ]; then
  if [ -r /dev/tty ]; then
    info ""
    info "${B}Configuring muxpost…${N}"
    "$PYTHON" "$SCRIPT_DIR/muxpost.py" init </dev/tty || warn "init didn't finish — run: muxpost init"
  else
    warn "no terminal available for interactive setup."
  fi
fi

info ""
ok "${B}muxpost installed.${N}"
info "Configure : ${B}muxpost init${N}     (token, user id, project root, autostart)"
info "Run       : ${B}muxpost${N}          (or muxpost start)"
info "Check     : muxpost doctor"
info "Remove    : $SCRIPT_DIR/install.sh --uninstall"
