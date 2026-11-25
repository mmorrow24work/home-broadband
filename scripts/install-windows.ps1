#Requires -Version 5.1
<#
.SYNOPSIS
  home-broadband installer for Windows 10 / 11.

.DESCRIPTION
  Runs the collector straight out of this checkout (the Windows equivalent of
  install.sh --in-place) and schedules it with Task Scheduler instead of
  systemd timers:

    \broadband-monitor\latency   every minute     ICMP / DNS / HTTP sweep
    \broadband-monitor\speed     hourly           throughput test (engines rotate)
    \broadband-monitor\publish   hourly           aggregate + push to gh-pages
    \broadband-monitor\prune     daily 04:17      retention

  The tasks run as SYSTEM: no console window flashes up every minute, nothing
  stops when you sign out, and no password is stored. State lives under
  %ProgramData%\broadband-monitor (config.yaml, data\, logs\, ssh\).

  Idempotent: re-run it after a `git pull` or to pick up a newly installed engine.

  Prerequisites (both one-liners in an elevated terminal):
    winget install -e --id Python.Python.3.12     # python.org build, not the Store one
    winget install -e --id Git.Git
  curl.exe, ping.exe and the OpenSSH client ship with Windows 10 1803+ / 11.

.PARAMETER InstallOokla
  Download the official Ookla Speedtest CLI into %ProgramData%\broadband-monitor\bin
  and enable the 'ookla' engine. You are accepting Ookla's licence by doing so.

.PARAMETER OoklaUrl
  Where to fetch the Ookla CLI zip from (override when Ookla bumps the version).

.PARAMETER Python
  Path to python.exe if the installer picks the wrong one.

.EXAMPLE
  # From an *elevated* PowerShell in the clone:
  Set-ExecutionPolicy -Scope Process Bypass
  .\scripts\install-windows.ps1 -InstallOokla
#>
[CmdletBinding()]
param(
  [switch]$InstallOokla,
  [string]$OoklaUrl = 'https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-win64.zip',
  [string]$Python
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Say  ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Warn ($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# YAML double-quoted strings treat '\' as an escape, and Python accepts '/' on
# Windows, so every path written into config.yaml uses forward slashes.
function Yaml-Path ($p) { ($p -replace '\\', '/') }

# Windows PowerShell 5.1 turns a native command's redirected stderr into error
# records, which $ErrorActionPreference = 'Stop' then makes fatal - and tools
# like ssh-keyscan write progress to stderr on success. Run those through this.
function Invoke-Quiet ([scriptblock]$block) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $block 2>$null } finally { $ErrorActionPreference = $prev }
}

function Write-Utf8NoBom ($path, $text) {
  # PowerShell 5.1's Set-Content -Encoding UTF8 writes a BOM; keep files clean.
  [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding $false))
}

# ------------------------------------------------------------------ checks
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Die 'Run me from an elevated PowerShell (right-click > Run as administrator).'
}

$AppDir   = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Base     = Join-Path $env:ProgramData 'broadband-monitor'
$DataDir  = Join-Path $Base 'data'
$LogDir   = Join-Path $Base 'logs'
$SshDir   = Join-Path $Base 'ssh'
$BinDir   = Join-Path $Base 'bin'
$Conf     = Join-Path $Base 'config.yaml'
$VenvPy   = Join-Path $AppDir '.venv\Scripts\python.exe'
$TaskPath = '\broadband-monitor\'

Say "Installing from $AppDir"
foreach ($tool in 'git', 'curl.exe', 'ping.exe') {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
    if ($tool -eq 'git') { Die 'git not found. Install it: winget install -e --id Git.Git (then open a new terminal)' }
    Die "$tool not found - it ships with Windows 10 1803+ / 11; is System32 on PATH?"
  }
}

# ssh-keygen / ssh-keyscan: Windows' OpenSSH client, else the copy inside Git.
function Find-SshTool ($name) {
  $cmd = Get-Command "$name.exe" -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $gitRoot = Split-Path (Split-Path (Get-Command git).Source -Parent) -Parent
  $candidate = Join-Path $gitRoot "usr\bin\$name.exe"
  if (Test-Path $candidate) { return $candidate }
  return $null
}
$SshKeygen = Find-SshTool 'ssh-keygen'
$SshKeyscan = Find-SshTool 'ssh-keyscan'
if (-not $SshKeygen) {
  Say 'Adding the Windows OpenSSH client capability'
  Add-WindowsCapability -Online -Name 'OpenSSH.Client~~~~0.0.1.0' | Out-Null
  $SshKeygen = Find-SshTool 'ssh-keygen'
  $SshKeyscan = Find-SshTool 'ssh-keyscan'
  if (-not $SshKeygen) { Die 'ssh-keygen still not found; install the OpenSSH Client optional feature.' }
}

# ------------------------------------------------------------------ python
function Test-Python ($exe) {
  try {
    $out = Invoke-Quiet { & $exe -c 'import sys; print(sys.version_info >= (3, 9)); print(sys.executable)' }
    if ($LASTEXITCODE -eq 0 -and $out -and $out[0] -eq 'True') { return $out[1] }
  } catch { }
  return $null
}

$PyExe = $null
if ($Python) { $PyExe = Test-Python $Python }
if (-not $PyExe -and (Get-Command py -ErrorAction SilentlyContinue)) { $PyExe = Test-Python 'py' }
if (-not $PyExe -and (Get-Command python -ErrorAction SilentlyContinue)) { $PyExe = Test-Python 'python' }
if (-not $PyExe) { Die 'Python 3.9+ not found. Install it: winget install -e --id Python.Python.3.12' }
if ($PyExe -like '*\WindowsApps\*') {
  # Store Python lives in a per-user package the SYSTEM account cannot execute.
  Die "Found the Microsoft Store Python ($PyExe). The scheduled tasks run as SYSTEM and cannot use it. Install the python.org build: winget install -e --id Python.Python.3.12"
}
Say "Python: $PyExe"

# ---------------------------------------------------------------- venv
if (-not (Test-Path $VenvPy)) {
  Say 'Creating the virtualenv'
  & $PyExe -m venv (Join-Path $AppDir '.venv')
  if ($LASTEXITCODE -ne 0) { Die 'venv creation failed' }
}
Say 'Installing Python packages (incl. speedtest-cli and tzdata)'
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r (Join-Path $AppDir 'requirements.txt') speedtest-cli
if ($LASTEXITCODE -ne 0) { Die 'pip install failed' }

# ------------------------------------------------------------- directories
foreach ($d in $Base, $DataDir, (Join-Path $DataDir 'pages'), $LogDir, $SshDir, $BinDir) {
  New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# ------------------------------------------------------------- engines
$Engines = New-Object System.Collections.Generic.List[string]
if (Test-Path (Join-Path $AppDir '.venv\Scripts\speedtest-cli.exe')) { $Engines.Add('speedtest-cli') }
else { Warn 'speedtest-cli did not install; that engine is disabled.' }

function Test-Ookla ($exe) {
  if (-not $exe -or -not (Test-Path $exe)) { return $false }
  try { return ((Invoke-Quiet { & $exe --version } | Select-Object -First 1) -match 'Ookla') } catch { return $false }
}
$OoklaExe = $null
$onPath = Get-Command speedtest.exe -ErrorAction SilentlyContinue
if ($onPath -and (Test-Ookla $onPath.Source)) { $OoklaExe = $onPath.Source }
$local = Join-Path $BinDir 'speedtest.exe'
if (-not $OoklaExe -and (Test-Ookla $local)) { $OoklaExe = $local }
if (-not $OoklaExe -and $InstallOokla) {
  Say "Downloading the Ookla Speedtest CLI from $OoklaUrl"
  $zip = Join-Path $env:TEMP 'ookla-speedtest.zip'
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri $OoklaUrl -OutFile $zip -UseBasicParsing
  Expand-Archive -Path $zip -DestinationPath $BinDir -Force
  Remove-Item $zip -Force
  if (Test-Ookla $local) { $OoklaExe = $local } else { Warn 'Downloaded archive did not contain a working speedtest.exe' }
}
if ($OoklaExe) { $Engines.Add('ookla'); Say "Ookla CLI: $OoklaExe" }
else { Warn "Ookla CLI not installed - re-run with -InstallOokla to enable the 'ookla' engine." }
$Engines.Add('cloudflare')
$EngineList = ($Engines -join ', ')

# ---------------------------------------------------------------- config
$NeedsEdit = $false
if (-not (Test-Path $Conf)) {
  Say "Seeding $Conf"
  $text = [System.IO.File]::ReadAllText((Join-Path $AppDir 'config\config.example.yaml'))
  $text = $text -replace '(?m)^  engines: \[.*\]$', "  engines: [$EngineList]"
  $text = $text -replace '(?m)^  path: ".*broadband\.db"', "  path: `"$(Yaml-Path (Join-Path $DataDir 'broadband.db'))`""
  $text = $text -replace '(?m)^  work_dir: .*$', "  work_dir: `"$(Yaml-Path (Join-Path $DataDir 'pages'))`""
  $NeedsEdit = $true
} else {
  Say "Keeping existing $Conf"
  $text = [System.IO.File]::ReadAllText($Conf)
  if ($text -match '(?m)^  engines: \[(.*)\]$') {
    $missing = $Matches[1].Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $Engines.Contains($_) }
    if ($missing) { Warn "config.yaml lists engine(s) not available here: $($missing -join ', ') (available: $EngineList)" }
  }
}
# Kept in step on every run, as install.sh does for repo_dir.
$text = $text -replace '(?m)^  repo_dir: .*$', "  repo_dir: `"$(Yaml-Path $AppDir)`""
$text = $text -replace '(?m)^  ssh_key: .*$', "  ssh_key: `"$(Yaml-Path (Join-Path $SshDir 'id_ed25519'))`""
if ($OoklaExe) { $text = $text -replace '(?m)^    binary: "speedtest"(\s+#.*)?$', "    binary: `"$(Yaml-Path $OoklaExe)`"`$1" }
Write-Utf8NoBom $Conf $text

# ------------------------------------------------------------- deploy key
$Key = Join-Path $SshDir 'id_ed25519'
$NewKey = $false
if (-not (Test-Path $Key)) {
  Say 'Generating a GitHub deploy key'
  # An empty passphrase is passed differently by Windows PowerShell 5.1 (drops
  # a bare '') and PowerShell 7.3+ (would pass '""' as two literal quotes).
  $emptyPass = if ($PSVersionTable.PSVersion.Major -ge 7) { '' } else { '""' }
  & $SshKeygen -q -t ed25519 -N $emptyPass -C "broadband-monitor@$env:COMPUTERNAME" -f $Key
  if ($LASTEXITCODE -ne 0) { Die 'ssh-keygen failed' }
  $NewKey = $true
}
# Win32 OpenSSH refuses a private key anyone else can read. SIDs, not names,
# so this works on non-English Windows: S-1-5-18 SYSTEM, S-1-5-32-544 Administrators.
& icacls $Key /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null

$KnownHosts = Join-Path $SshDir 'known_hosts'
$GitHost = 'github.com'
if ($text -match '(?m)^  remote: .*git@([^:]+):') { $GitHost = $Matches[1] }
$haveHost = (Test-Path $KnownHosts) -and (Select-String -Path $KnownHosts -SimpleMatch $GitHost -Quiet)
if (-not $haveHost -and $SshKeyscan) {
  Say "Recording the SSH host key for $GitHost"
  $lines = Invoke-Quiet { & $SshKeyscan -T 10 -t rsa,ecdsa,ed25519 $GitHost } |
    Where-Object { $_ -is [string] -and $_ -notmatch '^\s*#' }
  if ($lines) { [System.IO.File]::AppendAllLines($KnownHosts, [string[]]$lines) }
  else { Warn "Could not reach $GitHost; the first publish will fall back to accept-new." }
}

# ------------------------------------------------------------ git safety
# The publisher (as SYSTEM) runs `git pull` in your clone for publish.sync_code;
# git refuses a repo owned by another account unless it is declared safe.
$SafeDir = Yaml-Path $AppDir
$existing = Invoke-Quiet { & git config --system --get-all safe.directory }
if (-not ($existing -contains $SafeDir)) {
  & git config --system --add safe.directory $SafeDir
  Say "Marked $SafeDir as a safe git directory for all accounts"
}

# -------------------------------------------------------------- database
Push-Location $AppDir   # `python -m collector.main` resolves the package from the cwd
try {
  Say 'Initialising the database'
  & $VenvPy -m collector.main --config $Conf init-db
  if ($LASTEXITCODE -ne 0) { Die 'init-db failed - fix config.yaml and re-run' }
  Say 'Verifying the installation'
  & $VenvPy -m collector.main --config $Conf status
} finally { Pop-Location }

# ---------------------------------------------------------- scheduled tasks
Say "Registering scheduled tasks under $TaskPath"
$runAs = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$today = (Get-Date).Date

function Register-Collector ($name, $trigger, $limit) {
  $log = Join-Path $LogDir "$name.log"
  $argLine = "-m collector.main --config `"$Conf`" --log-file `"$log`" $name"
  $action = New-ScheduledTaskAction -Execute $VenvPy -Argument $argLine -WorkingDirectory $AppDir
  $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit $limit
  Register-ScheduledTask -TaskName $name -TaskPath $TaskPath -Action $action -Trigger $trigger `
    -Principal $runAs -Settings $settings -Force | Out-Null
  Write-Host "    $TaskPath$name"
}

# Omitting -RepetitionDuration repeats indefinitely on Windows 10/11.
Register-Collector 'latency' (New-ScheduledTaskTrigger -Once -At $today -RepetitionInterval (New-TimeSpan -Minutes 1)) (New-TimeSpan -Minutes 2)
Register-Collector 'speed'   (New-ScheduledTaskTrigger -Once -At $today.AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1) -RandomDelay (New-TimeSpan -Minutes 5)) (New-TimeSpan -Minutes 10)
# :00/:20/:40, interleaved with the Linux timer's :10/:30/:50 (see systemd/broadband-publish.timer).
Register-Collector 'publish' (New-ScheduledTaskTrigger -Once -At $today -RepetitionInterval (New-TimeSpan -Minutes 20) -RandomDelay (New-TimeSpan -Minutes 1)) (New-TimeSpan -Minutes 15)
Register-Collector 'prune'   (New-ScheduledTaskTrigger -Daily -At $today.AddHours(4).AddMinutes(17)) (New-TimeSpan -Minutes 30)
# Tests requested from the dashboard: one git fetch a minute, half a minute off latency.
# Wi-Fi-only throughput (iperf3 to a wired LAN box) at :45; a no-op until lan.server is set.
Register-Collector 'lan'     (New-ScheduledTaskTrigger -Once -At $today.AddMinutes(45) -RepetitionInterval (New-TimeSpan -Hours 1) -RandomDelay (New-TimeSpan -Minutes 1)) (New-TimeSpan -Minutes 5)
Register-Collector 'control' (New-ScheduledTaskTrigger -Once -At $today.AddSeconds(30) -RepetitionInterval (New-TimeSpan -Minutes 1)) (New-TimeSpan -Minutes 20)

Say 'Done.'
$pub = Get-Content "$Key.pub"

# Ask the collector what is actually left to do, so a re-run does not repeat
# first-run instructions that were finished long ago. If the check itself
# fails, fall back to showing every step.
$todo = @()
Push-Location $AppDir
try {
  $out = Invoke-Quiet { & $VenvPy -m collector.main --config $Conf setup-check }
  if ($LASTEXITCODE -eq 0) {
    $todo = @($out | Where-Object { $_ -match "`t" } | ForEach-Object {
      $step, $detail = $_ -split "`t", 2
      [pscustomobject]@{ Step = $step; Detail = $detail }
    })
  }
} finally { Pop-Location }
if (-not $todo) {
  $todo = @('config', 'key', 'pages', 'smoke') | ForEach-Object { [pscustomobject]@{ Step = $_; Detail = '' } }
}
$steps = $todo | Select-Object -ExpandProperty Step

$ok = $todo | Where-Object Step -eq 'ok'
if ($ok) {
  Write-Host ''
  Write-Host 'Nothing left to set up.' -ForegroundColor Green
  if ($ok.Detail) { Write-Host "Live at $($ok.Detail) - publishing every 20 minutes." }
  Write-Host "Check on it:  Get-ScheduledTask -TaskPath '$TaskPath' | Get-ScheduledTaskInfo"
} else {
  Write-Host "`nNext steps`n----------"
  $n = 0
  $config = @($todo | Where-Object Step -eq 'config' | Where-Object Detail)
  if ($config -or ($steps -contains 'config')) {
    $n++
    Write-Host "$n. Edit the config:   notepad $Conf"
    if ($config) { $config | ForEach-Object { Write-Host "     - $($_.Detail)" } }
    else {
      Write-Host '     - publish.remote, site.domain, latency.targets (your router), site.isp.*'
    }
    Write-Host ''
  }
  if ($steps -contains 'key') {
    $n++
    Write-Host "$n. Add this deploy key to the repo with **write access**"
    Write-Host '   (Settings -> Deploy keys -> Add deploy key -> tick "Allow write access"):'
    Write-Host "`n$pub`n"
    if (-not $NewKey) { Write-Host '   (This key already existed - skip this if it is on GitHub already.)'; Write-Host '' }
  }
  if ($steps -contains 'pages') {
    $n++
    $where = ($todo | Where-Object Step -eq 'pages').Detail
    if ($where) { Write-Host "$n. The data was pushed, but $where does not show this collector yet." }
    else { Write-Host "$n. Turn on GitHub Pages." }
    Write-Host '   In the repo: Settings -> Pages -> Source = "Deploy from a branch",'
    Write-Host '   branch "gh-pages", folder "/ (root)". Then allow up to 10 minutes.'
    Write-Host ''
  }
  if ($steps -contains 'smoke') {
    $n++
    Write-Host "$n. Smoke test (from an elevated PowerShell), then watch it run:"
    Write-Host "     cd $AppDir"
    Write-Host "     .venv\Scripts\python -m collector.main --config $Conf latency --dry-run"
    Write-Host "     .venv\Scripts\python -m collector.main --config $Conf speed --force --engine cloudflare"
    Write-Host "     Start-ScheduledTask -TaskPath '$TaskPath' -TaskName publish"
    Write-Host "     Get-Content $LogDir\publish.log -Tail 30"
    Write-Host "     Get-ScheduledTask -TaskPath '$TaskPath' | Get-ScheduledTaskInfo"
    Write-Host ''
  }
}

if ($NeedsEdit) { Warn 'config.yaml is the stock example - edit it before the first publish.' }
