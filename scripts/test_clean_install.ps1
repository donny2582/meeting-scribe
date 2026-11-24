<#
.SYNOPSIS
  用 Windows 沙箱（全新、沒有 Python 的 Windows）測試安裝檔：自動安裝、執行自我檢查並開啟主視窗。
.DESCRIPTION
  需先執行 .\scripts\build.ps1 產生安裝檔，並在「Windows 功能」啟用「Windows 沙箱」。
  結果寫在 dist\clean_install_result.txt。
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $Root 'dist'
if (-not (Get-ChildItem (Join-Path $dist 'installer\MeetingScribe_Setup_*.exe') -ErrorAction SilentlyContinue)) {
    throw '找不到安裝檔，請先執行 .\scripts\build.ps1'
}
if (-not (Test-Path "$env:windir\System32\WindowsSandbox.exe")) {
    throw '尚未啟用 Windows 沙箱：控制台 → 開啟或關閉 Windows 功能 → 勾選「Windows 沙箱」，重新開機後再執行。'
}
Copy-Item (Join-Path $Root 'installer\sandbox_install_test.ps1') $dist -Force
Remove-Item (Join-Path $dist 'clean_install_result.txt') -ErrorAction SilentlyContinue
$wsb = (Get-Content (Join-Path $Root 'installer\clean_install.wsb.template') -Raw -Encoding UTF8).Replace('__DIST__', $dist)
$wsbPath = Join-Path $dist 'clean_install.wsb'
[IO.File]::WriteAllText($wsbPath, $wsb, (New-Object Text.UTF8Encoding $false))
Start-Process $wsbPath
Write-Host '已開啟 Windows 沙箱；安裝與自我檢查約需 1～2 分鐘，完成後主視窗會在沙箱內開啟。'
Write-Host "結果：$(Join-Path $dist 'clean_install_result.txt')"