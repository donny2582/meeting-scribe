<#
.SYNOPSIS
  第 0 階段 STT 引擎評估（SPEC 5.3）。
.DESCRIPTION
  1. 先用 -MakeReference 由引擎結果產生標準答案草稿，人工校正後存檔。
  2. 再以 -Reference 執行評估，產出 benchmark_out\benchmark_report.md。
.EXAMPLE
  .\scripts\benchmark_stt.ps1 -Audio D:\錄音\測試10分鐘.m4a -MakeReference
.EXAMPLE
  .\scripts\benchmark_stt.ps1 -Audio D:\錄音\測試10分鐘.m4a -Reference .\benchmark_out\reference.csv `
      -LangAudio D:\錄音\日韓3分鐘.m4a -LangReference .\benchmark_out\reference_lang.csv
#>
param(
    [Parameter(Mandatory = $true)][string]$Audio,
    [string]$Reference,
    [string]$LangAudio,
    [string]$LangReference,
    [string[]]$Engines = @('azure', 'openai'),
    [string[]]$Languages = @('zh', 'en'),
    [string]$OutDir = 'benchmark_out',
    [switch]$MakeReference,
    [string]$ReferenceEngine = 'azure',
    [switch]$Reuse
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { throw '尚未建立 .venv，請先執行 .\scripts\setup.ps1' }

if ($MakeReference) {
    $out = Join-Path $OutDir 'reference_draft.csv'
    & $venvPy -m meeting_scribe make-reference --audio $Audio --engine $ReferenceEngine --languages $Languages --out $out
    if ($LangAudio) {
        $outLang = Join-Path $OutDir 'reference_lang_draft.csv'
        & $venvPy -m meeting_scribe make-reference --audio $LangAudio --engine $ReferenceEngine --languages zh en ja ko --out $outLang
    }
    exit $LASTEXITCODE
}

if (-not $Reference) { throw '請提供 -Reference（標準答案 CSV），或先用 -MakeReference 產生草稿。' }

$argList = @('-m', 'meeting_scribe', 'benchmark', '--audio', $Audio, '--reference', $Reference,
    '--engines') + $Engines + @('--languages') + $Languages + @('--out-dir', $OutDir)
if ($LangAudio) { $argList += @('--lang-audio', $LangAudio, '--lang-reference', $LangReference) }
if ($Reuse) { $argList += '--reuse' }
& $venvPy @argList
exit $LASTEXITCODE
