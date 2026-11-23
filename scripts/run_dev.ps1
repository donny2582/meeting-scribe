<#
.SYNOPSIS
  以開發模式啟動 MeetingScribe（GUI 於第 4 階段完成；目前參數會轉給 CLI）。
.EXAMPLE
  .\scripts\run_dev.ps1 --help
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { throw '尚未建立 .venv，請先執行 .\scripts\setup.ps1' }
& $venvPy -m meeting_scribe @args
exit $LASTEXITCODE
