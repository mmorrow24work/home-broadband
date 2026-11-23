#Requires -Version 5.1
<#
.SYNOPSIS
  Remove home-broadband's scheduled tasks. Keeps the database unless -Purge.

.DESCRIPTION
  Your checkout is never deleted - install-windows.ps1 runs from it in place.
  Remove .venv yourself if you want the disk space back.

.EXAMPLE
  .\scripts\uninstall-windows.ps1          # tasks only
  .\scripts\uninstall-windows.ps1 -Purge   # also %ProgramData%\broadband-monitor
#>
[CmdletBinding()]
param([switch]$Purge)

$ErrorActionPreference = 'Stop'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host '[x] Run me from an elevated PowerShell.' -ForegroundColor Red; exit 1
}

$TaskPath = '\broadband-monitor\'
$Base = Join-Path $env:ProgramData 'broadband-monitor'
$AppDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue | ForEach-Object {
  Unregister-ScheduledTask -TaskName $_.TaskName -TaskPath $TaskPath -Confirm:$false
  Write-Host "Removed task $TaskPath$($_.TaskName)"
}
# Remove the now-empty task folder.
try {
  $svc = New-Object -ComObject Schedule.Service
  $svc.Connect()
  $svc.GetFolder('\').DeleteFolder('broadband-monitor', 0)
} catch { }

$ErrorActionPreference = 'Continue'   # git may warn on stderr; never fatal here
& git config --system --unset-all safe.directory ('^' + [regex]::Escape(($AppDir -replace '\\', '/')) + '$') 2>$null

Write-Host "Left your checkout at $AppDir alone."
if ($Purge) {
  Remove-Item -Recurse -Force $Base -ErrorAction SilentlyContinue
  Write-Host "Removed $Base, including the measurement database and deploy key."
} else {
  Write-Host "Kept $Base (config, database, deploy key, logs). Re-run with -Purge to delete it."
}
