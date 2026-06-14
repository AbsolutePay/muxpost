<#
.SYNOPSIS
  muxpost installer for Windows (PowerShell).

.DESCRIPTION
  Clones muxpost from GitHub (if not run from a checkout), installs a `muxpost`
  command on your PATH, and can register an auto-start scheduled task. After
  installing, configure with `muxpost init`. muxpost shells out to `tmux`, which
  on Windows lives inside WSL — so the bot itself usually runs in WSL (use
  install.sh there); this still wires up the command and config.

.EXAMPLE
  # one-line install (works once the repo is public):
  irm https://raw.githubusercontent.com/AbsolutePay/muxpost/main/install.ps1 | iex

  ./install.ps1
  ./install.ps1 -Service
  ./install.ps1 -Uninstall
#>
param(
  [switch]$Service,
  [switch]$Uninstall
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

if ($Uninstall) {
  Write-Host "Uninstalling muxpost" -ForegroundColor Cyan
  schtasks /Delete /TN $TaskName /F 2>$null | Out-Null
  if (Test-Path $Launcher) { Remove-Item $Launcher -Force; Ok "removed $Launcher" }
  # remove BinDir from user PATH
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($userPath -and $userPath.Split(";") -contains $BinDir) {
    $newPath = ($userPath.Split(";") | Where-Object { $_ -ne $BinDir }) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Ok "removed $BinDir from PATH"
  }
  Write-Host "Done. (config.json and the project folder were left untouched.)"
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

# config status (configuration is done separately via `muxpost init`)
$HasConfig = Test-Path (Join-Path $ScriptDir "config.json")
if ($HasConfig) { Ok "config.json already present" }

# auto-start scheduled task
if ($Service) {
  if (-not $HasConfig) { Warn "no config yet — run 'muxpost init' before the task can start." }
  Write-Host "Registering auto-start task..." -ForegroundColor Cyan
  $action  = "`"$Python`" `"$ScriptDir\muxpost.py`""
  schtasks /Create /TN $TaskName /TR $action /SC ONLOGON /RL LIMITED /F | Out-Null
  schtasks /Run /TN $TaskName | Out-Null
  Ok "scheduled task '$TaskName' created (runs at logon). Remove: schtasks /Delete /TN $TaskName /F"
}

Write-Host ""
Ok "muxpost installed."
if ($HasConfig) { Write-Host "Next   : muxpost          (run)" }
else {
  Write-Host "Next   : muxpost init     (configure token, user id, project root)"
  Write-Host "Then   : muxpost          (run)"
}
Write-Host "Check  : muxpost doctor"
Write-Host "Remove : powershell $ScriptDir\install.ps1 -Uninstall"
