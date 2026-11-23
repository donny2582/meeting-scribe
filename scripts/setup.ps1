<#
.SYNOPSIS
  建立 .venv 並安裝相依套件（相容 PowerShell 5.1 與 7）。
.PARAMETER Dev
  一併安裝測試與打包工具（requirements-dev.txt）。預設開啟。
#>
param([switch]$NoDev)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Python {
    # PowerShell 5.1 在 Stop 模式下會把原生程式的 stderr 視為錯誤，這裡暫時改為 Continue
    $ErrorActionPreference = 'Continue'
    foreach ($ver in @('3.12', '3.11')) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py "-$ver" -c "import sys" 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { return , @('py', "-$ver") }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        Write-Warning '找不到 Python 3.12 或 3.11，改用預設的 python（規格建議 3.11／3.12，可執行 py install 3.12 安裝）。'
        return , @('python')
    }
    throw '找不到 Python，請先安裝 Python 3.12。'
}

if (-not (Test-Path '.venv')) {
    $py = @(Find-Python)
    Write-Host "建立虛擬環境：$($py -join ' ')"
    $pyArgs = @($py | Select-Object -Skip 1) + @('-m', 'venv', '.venv')
    & $py[0] @pyArgs
    if ($LASTEXITCODE -ne 0) { throw '建立 .venv 失敗' }
}

$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
& $venvPy -m pip install --upgrade pip
$req = if ($NoDev) { 'requirements.txt' } else { 'requirements-dev.txt' }
& $venvPy -m pip install -r $req
if ($LASTEXITCODE -ne 0) { throw "安裝 $req 失敗" }
& $venvPy -m pip install -e . --no-deps
if ($LASTEXITCODE -ne 0) { throw '安裝本專案失敗' }

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and -not (Test-Path 'assets\ffmpeg\ffmpeg.exe')) {
    Write-Warning '找不到 ffmpeg：請執行 winget install Gyan.FFmpeg，或把 ffmpeg.exe 放到 assets\ffmpeg\。'
}
Write-Host '完成。啟用虛擬環境：.\.venv\Scripts\Activate.ps1'
