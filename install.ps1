<#
.SYNOPSIS
  muxpost installer for Windows (PowerShell).

.DESCRIPTION
  Installs a `muxpost` command on your PATH, runs the interactive setup, and
  can register an auto-start scheduled task. muxpost shells out to `tmux`,
  which on Windows lives inside WSL — so the bot itself usually runs in WSL
  (use install.sh there). This installer still wires up the command + config.

.EXAMPLE
  ./install.ps1
  ./install.ps1 -Service
  ./install.ps1 -NoSetup
  ./install.ps1 -Uninstall
#>
param(
  [switch]$Service,
  [switch]$NoSetup,
  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$BinDir    = Join-Path $env:LOCALAPPDATA "muxpost\bin"
$Launcher  = Join-Path $BinDir "muxpost.cmd"
$TaskName  = "muxpost"

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

# setup
if (-not $NoSetup) {
  if (Test-Path (Join-Path $ScriptDir "config.json")) {
    Ok "config.json already exists — skipping setup (run: $Python setup.py to change it)"
  } else {
    Write-Host "Running setup..." -ForegroundColor Cyan
    & $Python (Join-Path $ScriptDir "setup.py")
  }
}

# auto-start scheduled task
if ($Service) {
  Write-Host "Registering auto-start task..." -ForegroundColor Cyan
  $action  = "`"$Python`" `"$ScriptDir\muxpost.py`""
  schtasks /Create /TN $TaskName /TR $action /SC ONLOGON /RL LIMITED /F | Out-Null
  schtasks /Run /TN $TaskName | Out-Null
  Ok "scheduled task '$TaskName' created (runs at logon). Remove: schtasks /Delete /TN $TaskName /F"
}

Write-Host ""
Ok "muxpost installed."
Write-Host "Verify : $Python doctor.py"
if ($Service) { Write-Host "Running as a logon task." }
else { Write-Host "Start  : muxpost   (or: $Python muxpost.py)" }
Write-Host "Remove : ./install.ps1 -Uninstall"
