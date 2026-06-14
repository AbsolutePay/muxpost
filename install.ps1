<#
.SYNOPSIS
  muxpost installer for Windows (PowerShell).

.DESCRIPTION
  Clones muxpost from GitHub (if not run from a checkout), installs a `muxpost`
  command on your PATH, then runs `muxpost init` (configuration), which also asks
  whether to enable autostart. muxpost shells out to `tmux`, which on Windows
  lives inside WSL — so the bot itself usually runs in WSL (use install.sh
  there); this still wires up the command and config.

.EXAMPLE
  # one-line install (works once the repo is public):
  irm https://raw.githubusercontent.com/AbsolutePay/muxpost/main/install.ps1 | iex

  ./install.ps1            # install the command, then run init
  ./install.ps1 -NoInit    # install the command only
  ./install.ps1 -Uninstall # remove the command and any autostart
#>
param(
  [switch]$NoInit,
  [switch]$Uninstall,
  [switch]$ServiceOnly,   # used by `muxpost init`
  [switch]$ServiceOff     # used by `muxpost init`
)

$ErrorActionPreference = "Stop"
$RepoUrl    = if ($env:MUXPOST_REPO) { $env:MUXPOST_REPO } else { "https://github.com/AbsolutePay/muxpost.git" }
$InstallDir = if ($env:MUXPOST_HOME) { $env:MUXPOST_HOME } else { Join-Path $env:LOCALAPPDATA "muxpost\src" }
$ScriptDir  = if ($PSCommandPath) { Split-Path -Parent $PSCommandPath } else { $null }
$BinDir     = Join-Path $env:LOCALAPPDATA "muxpost\bin"
$Launcher   = Join-Path $BinDir "muxpost.cmd"
$TaskName   = "muxpost"

function Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

function Find-Python {
  foreach ($c in @("python", "python3", "py")) {
    $p = Get-Command $c -ErrorAction SilentlyContinue
    if ($p) {
      try {
        $ok = & $p.Source -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return $p.Source }
      } catch {}
    }
  }
  return $null
}

function Install-Service($Python, $ProjectDir) {
  $action = "`"$Python`" `"$ProjectDir\muxpost.py`""
  schtasks /Create /TN $TaskName /TR $action /SC ONLOGON /RL LIMITED /F | Out-Null
  schtasks /Run /TN $TaskName | Out-Null
  Ok "autostart task '$TaskName' created (runs at logon)."
}

function Remove-Service {
  schtasks /Delete /TN $TaskName /F 2>$null | Out-Null
  Ok "autostart task removed (if it existed)."
}

if ($Uninstall) {
  Write-Host "Uninstalling muxpost" -ForegroundColor Cyan
  Remove-Service
  if (Test-Path $Launcher) { Remove-Item $Launcher -Force; Ok "removed $Launcher" }
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($userPath -and $userPath.Split(";") -contains $BinDir) {
    $newPath = ($userPath.Split(";") | Where-Object { $_ -ne $BinDir }) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Ok "removed $BinDir from PATH"
  }
  Write-Host "Done. (config.json and the project folder were left untouched.)"
  exit 0
}

# service-only / service-off (invoked by `muxpost init`)
if ($ServiceOnly -or $ServiceOff) {
  $Python = Find-Python
  if (-not $Python) { Die "Python 3.8+ not found." }
  if (-not ($ScriptDir -and (Test-Path (Join-Path $ScriptDir "muxpost.py")))) { $ScriptDir = $InstallDir }
  if ($ServiceOnly) { Install-Service $Python $ScriptDir } else { Remove-Service }
  exit 0
}

# obtain the project (clone from GitHub when not run from a checkout)
if (-not ($ScriptDir -and (Test-Path (Join-Path $ScriptDir "muxpost.py")))) {
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git is required to install muxpost." }
  if (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Host "Updating muxpost in $InstallDir" -ForegroundColor Cyan
    git -C $InstallDir pull --ff-only -q
  } else {
    Write-Host "Cloning $RepoUrl into $InstallDir" -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir) | Out-Null
    git clone --depth 1 -q $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) { Die "clone failed — is the repo public, or have you run: gh auth setup-git ?" }
  }
  $ScriptDir = $InstallDir
}

Write-Host "Installing muxpost from $ScriptDir" -ForegroundColor Cyan

$Python = Find-Python
if (-not $Python) { Die "Python 3.8+ not found. Install it from python.org and re-run." }
Ok "python: $Python ($(& $Python -V 2>&1))"

if (Get-Command tmux -ErrorAction SilentlyContinue) {
  Ok "tmux found on PATH"
} else {
  Warn "tmux not found — muxpost needs it at runtime. On Windows, run the bot inside WSL (use install.sh there)."
}

# launcher
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
@"
@echo off
"$Python" "$ScriptDir\muxpost.py" %*
"@ | Set-Content -Path $Launcher -Encoding ASCII
Ok "installed command: $Launcher"

# add to user PATH if missing
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if ($userPath.Split(";") -notcontains $BinDir) {
  [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $BinDir), "User")
  Warn "Added $BinDir to your PATH. Open a new terminal for `muxpost` to resolve."
}

# run init (configuration + autostart question)
if (-not $NoInit) {
  Write-Host ""
  Write-Host "Configuring muxpost..." -ForegroundColor Cyan
  & $Python (Join-Path $ScriptDir "muxpost.py") init
}

Write-Host ""
Ok "muxpost installed."
Write-Host "Configure : muxpost init     (token, user id, project root, autostart)"
Write-Host "Run       : muxpost          (or muxpost start)"
Write-Host "Check     : muxpost doctor"
Write-Host "Remove    : powershell $ScriptDir\install.ps1 -Uninstall"
