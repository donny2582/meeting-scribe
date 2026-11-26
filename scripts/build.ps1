<#
.SYNOPSIS
  打包 MeetingScribe：PyInstaller（onedir）＋ Inno Setup 安裝檔（相容 PowerShell 5.1 與 7）。
.DESCRIPTION
  1. 準備 ffmpeg.exe（放進 assets\ffmpeg\，隨安裝檔附上）
  2. 產生程式圖示
  3. PyInstaller 打包到 dist\MeetingScribe\
  4. 用打包好的 MeetingScribe.exe 執行自我檢查（不需要金鑰，在本機完成一次處理）
  5. Inno Setup 產生 dist\installer\MeetingScribe_Setup_{版本}.exe
.PARAMETER FfmpegPath
  指定 ffmpeg.exe；不指定時依序找 assets\ffmpeg\、PATH、winget 安裝位置。
.PARAMETER SkipSelfTest
  略過打包後的自我檢查。
.PARAMETER SkipInstaller
  只打包 dist\MeetingScribe\，不產生安裝檔。
.EXAMPLE
  .\scripts\build.ps1
#>
param([string]$FfmpegPath, [switch]$SkipSelfTest, [switch]$SkipInstaller)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { throw '尚未建立 .venv，請先執行 .\scripts\setup.ps1' }

function Step([string]$msg) { Write-Host ''; Write-Host "==> $msg" -ForegroundColor Cyan }

# 版本：pyproject.toml 的 version
$version = (Select-String -Path 'pyproject.toml' -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "MeetingScribe $version"

Step '1/5 準備 ffmpeg.exe'
$target = Join-Path $Root 'assets\ffmpeg\ffmpeg.exe'
if ($FfmpegPath) {
    Copy-Item -LiteralPath $FfmpegPath -Destination $target -Force
} elseif (-not (Test-Path $target)) {
    $found = Get-Command ffmpeg -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
    if (-not $found) {
        $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $found) { throw '找不到 ffmpeg.exe：請執行 winget install Gyan.FFmpeg，或用 -FfmpegPath 指定。' }
    Copy-Item -LiteralPath $found -Destination $target -Force
}
Write-Host ("ffmpeg：{0}（{1:N0} MB）" -f $target, ((Get-Item $target).Length / 1MB))

Step '2/5 產生程式圖示'
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'build') | Out-Null
$ico = Join-Path $Root 'build\meeting_scribe.ico'
$env:QT_QPA_PLATFORM = 'offscreen'
& $venvPy -c "import sys; from pathlib import Path; from PySide6.QtWidgets import QApplication; app = QApplication([]); from meeting_scribe.gui.icon import write_ico; write_ico(Path(sys.argv[1]))" $ico
Remove-Item Env:\QT_QPA_PLATFORM
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $ico)) { throw '產生圖示失敗' }

Step '3/5 PyInstaller 打包'
$dist = Join-Path $Root 'dist'
& $venvPy -m PyInstaller --noconfirm --clean --distpath $dist --workpath (Join-Path $Root 'build\pyinstaller') `
    (Join-Path $Root 'installer\meeting_scribe.spec')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 打包失敗' }
$exe = Join-Path $dist 'MeetingScribe\MeetingScribe.exe'
$size = (Get-ChildItem (Join-Path $dist 'MeetingScribe') -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("完成：{0}（資料夾共 {1:N0} MB）" -f $exe, $size)

if (-not $SkipSelfTest) {
    Step '4/5 自我檢查（打包版，不需要金鑰）'
    $out = Join-Path $Root 'build\selftest'
    if (Test-Path $out) { Remove-Item -Recurse -Force $out }
    $p = Start-Process -FilePath $exe -ArgumentList @('selftest', '--out', "`"$out`"") -Wait -PassThru
    $report = Join-Path $out 'selftest_report.txt'
    if (Test-Path $report) { Get-Content $report -Encoding UTF8 | Write-Host }
    if ($p.ExitCode -ne 0) { throw "自我檢查失敗（結束代碼 $($p.ExitCode)），請看上面的報告。" }
} else {
    Step '4/5 略過自我檢查'
}

if (-not $SkipInstaller) {
    Step '5/5 Inno Setup 產生安裝檔'
    $iscc = @(
        (Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $iscc) { throw '找不到 Inno Setup：請執行 winget install JRSoftware.InnoSetup' }
    & $iscc "/DAppVersion=$version" "/DRoot=$Root" (Join-Path $Root 'installer\meeting_scribe.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup 產生安裝檔失敗' }
    $setup = Get-ChildItem (Join-Path $dist 'installer') -Filter "MeetingScribe_Setup_$version.exe" | Select-Object -First 1
    # 檢查碼：「版本」分頁下載後比對，一致才安裝（發佈 Release 時要一起上傳）
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $setup.FullName).Hash.ToLower()
    $sumFile = "$($setup.FullName).sha256"
    [IO.File]::WriteAllText($sumFile, "$hash  $($setup.Name)`n", (New-Object Text.UTF8Encoding $false))
    Write-Host ("安裝檔：{0}（{1:N0} MB）" -f $setup.FullName, ($setup.Length / 1MB)) -ForegroundColor Green
    Write-Host "檢查碼：$sumFile" -ForegroundColor Green
}