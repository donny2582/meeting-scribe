# 在 Windows 沙箱內執行：靜默安裝 → 確認沒有 Python → 執行自我檢查 → 結果寫回 C:\MeetingScribe_dist\clean_install_result.txt
$ErrorActionPreference = 'Continue'
$dist = 'C:\MeetingScribe_dist'
$result = Join-Path $dist 'clean_install_result.txt'
$lines = @("乾淨環境安裝測試　$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$py = Get-Command python, python3, py -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike '*WindowsApps*' }
$lines += "系統內的 Python：$(if ($py) { ($py.Source -join '、') } else { '沒有（符合乾淨環境）' })"
$setup = Get-ChildItem (Join-Path $dist 'installer\MeetingScribe_Setup_*.exe') | Sort-Object LastWriteTime | Select-Object -Last 1
$p = Start-Process $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/CURRENTUSER', '/NORESTART', '/TASKS=desktopicon' -Wait -PassThru
$lines += "安裝 $($setup.Name)：結束代碼 $($p.ExitCode)"
$exe = Join-Path $env:LOCALAPPDATA 'Programs\MeetingScribe\MeetingScribe.exe'
$lines += "安裝位置：$exe（$(if (Test-Path $exe) { '存在' } else { '不存在' })）"
$out = Join-Path $dist 'clean_selftest'
$t = Start-Process $exe -ArgumentList 'selftest', '--out', $out -Wait -PassThru
$lines += "自我檢查：結束代碼 $($t.ExitCode)"
if (Test-Path (Join-Path $out 'selftest_report.txt')) { $lines += Get-Content (Join-Path $out 'selftest_report.txt') -Encoding UTF8 }
$lines | Set-Content -Path $result -Encoding UTF8
Start-Process $exe  # 開啟主視窗，讓您目視確認