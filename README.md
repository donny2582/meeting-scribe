# 會議紀錄產生器（MeetingScribe）

把 1–3 小時的會議錄音自動轉成**會議紀錄初稿 .docx**、**逐字稿 .docx** 與**心智圖**，再由紀錄人員人工校對後陳核。
完整需求見 [SPEC.md](SPEC.md)，開發守則見 [CLAUDE.md](CLAUDE.md)。

## 開發進度

| 階段 | 內容 | 狀態 |
|---|---|---|
| 0 | 引擎評估（STT 介面、Azure、OpenAI、評估指令碼） | ✅ 程式完成，**待使用者以真實錄音評估** |
| 1 | 核心管線（CLI） | 尚未開始 |
| 2 | 摘要 | 尚未開始 |
| 3 | 輸出渲染 | 尚未開始 |
| 4 | GUI | 尚未開始 |
| 5 | 打包 | 尚未開始 |

## 開發環境安裝

需求：Windows 10／11、Python 3.12（或 3.11）、ffmpeg。

```powershell
.\scripts\setup.ps1
```

- 找不到 Python 3.12／3.11 時會改用預設的 `python` 並顯示警告；可執行 `py install 3.12` 安裝 3.12。
- ffmpeg：執行 `winget install Gyan.FFmpeg`，或把 `ffmpeg.exe` 放到 `assets\ffmpeg\`（不納入 git）。

執行測試：

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=meeting_scribe
```

## 第 0 階段：STT 引擎評估

### 1. 設定金鑰與區域（設定視窗）

在檔案總管中**按兩下專案根目錄的 `MeetingScribe設定.cmd`**，開啟設定視窗：

- **API 金鑰**：貼上 Azure Speech、OpenAI（及之後摘要用的 Anthropic）金鑰，按「儲存」。金鑰存在 Windows 認證管理員，畫面只顯示末 4 碼；留空表示不變更；「測試連線」可確認金鑰是否有效；「清除」會刪除已儲存的金鑰。
- **Azure 區域**：選擇 Azure Speech 資源所在區域（例：`eastasia`），或填寫自訂端點。
- **OpenAI 模型名稱**：預設 `gpt-4o-transcribe-diarize`，可改。
- **API 單價**（美元／音訊小時）：請依官方價目表填寫，用來估算費用；未填時報告顯示「未設定單價」。

區域、模型、單價存在 `%APPDATA%\MeetingScribe\config.toml`。（也可以用命令列 `set-key`、`show-keys` 設定金鑰。）

### 2. 準備標準答案

先用引擎產生草稿，再人工校正（比從頭打字快）：

```powershell
.\scripts\benchmark_stt.ps1 -Audio "D:\錄音\測試10分鐘.m4a" -MakeReference -LangAudio "D:\錄音\日韓3分鐘.m4a"
```

用 Excel 開啟 `benchmark_out\reference_draft.csv`，逐列校正 `text`（文字）、`speaker`（講者，任意名稱）、`lang`（zh／en／ja／ko），另存為 `reference.csv`（CSV UTF-8）。語言測試錄音的 `reference_lang_draft.csv` 也照樣處理。

### 3. 執行評估

```powershell
.\scripts\benchmark_stt.ps1 -Audio "D:\錄音\測試10分鐘.m4a" -Reference .\benchmark_out\reference.csv -LangAudio "D:\錄音\日韓3分鐘.m4a" -LangReference .\benchmark_out\reference_lang.csv
```

報告存在 `benchmark_out\benchmark_report.md`，包含 SPEC 5.3 的全部指標（CER、講者錯誤比例、詞庫命中率、語言判定正確率、中文誤判率、處理時間、費用）。
轉錄結果會存在 `benchmark_out\_work\`；修改標準答案後加 `-Reuse` 只重算指標，不會重新呼叫 API。

### 引擎實作重點

- **Azure AI Speech 快速轉錄**：一次上傳整段錄音（不超過設定的長度與大小上限時不切段），送出候選語言、語者辨識與詞彙清單（phrase list）。若 API 回報不支援詞彙清單，可在設定中把 `send_phrase_list` 設為 `false`。
- **OpenAI 具語者辨識的轉錄**：單檔大小有上限，一律切成每段 20 分鐘、重疊 30 秒的 mp3，再以重疊區域把各段講者串接起來；對應不到的講者標「（疑似）」（SPEC 5.2）。
- 兩者都在網路錯誤或 429／5xx 時以指數退避重試 3 次；金鑰無效、額度不足會顯示明確原因。
- 日誌位於 `%LOCALAPPDATA%\MeetingScribe\logs\`，不記錄逐字稿內容。

## 專案結構

依 SPEC 第 11 章建立；尚未實作的模組只有說明文字，會在對應的階段補上。
第 0 階段另外新增 `src/meeting_scribe/benchmark.py`（評估指標與報告）。

## 注意事項

- 錄音檔、逐字稿、`benchmark_out\` 都已列入 `.gitignore`，不會進入版控。
- 正式用於公務前，請先確認單位是否允許錄音上傳雲端（SPEC 13）。
