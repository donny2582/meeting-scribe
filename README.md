# 會議紀錄產生器（MeetingScribe）

把 1–3 小時的會議錄音自動轉成**會議紀錄初稿 .docx**（公文格式）、**逐字稿 .docx** 與**心智圖**（PNG／XMind），
再由紀錄人員人工校對後陳核。Windows 桌面程式，**不需要安裝 Python**。

## 下載安裝

**[⬇ 下載安裝檔 MeetingScribe_Setup_0.5.4.exe](https://github.com/donny2582/meeting-scribe/releases/download/v0.5.4/MeetingScribe_Setup_0.5.4.exe)**
（約 122 MB，Windows 10／11 64 位元）　｜　[所有版本（Releases）](https://github.com/donny2582/meeting-scribe/releases)

1. 雙擊安裝檔。預設只裝給目前使用者，**不需要系統管理員權限**。
   出現「Windows 已保護您的電腦」時，按「其他資訊」→「仍要執行」（安裝檔沒有數位簽章）。
2. 從開始功能表或桌面開啟「**MeetingScribe 會議紀錄**」。
3. 到「**API 金鑰**」分頁設定金鑰（見下一節）。

> 0.5.4 為預覽版：已在開發電腦驗證安裝、執行與解除安裝，尚未在全新 Windows 上驗證。

## 需要的 API 金鑰

| 用途 | 選一個服務 | 申請網站 |
|---|---|---|
| 語音轉錄（錄音 → 逐字稿） | **Azure AI Speech** | <https://portal.azure.com>（建立「語音服務」資源 → 金鑰與端點） |
| | **OpenAI** | <https://platform.openai.com/api-keys> |
| | **Soniox** | <https://console.soniox.com> |
| 文字整理處理（在地化、刪贅字、整理會議紀錄） | **Anthropic Claude** | <https://platform.claude.com>（Settings → API keys） |
| | **OpenAI** | <https://platform.openai.com/api-keys> |

逐步申請教學（含儲值、設定用量上限、金鑰安全）：**[API 金鑰申請說明](docs/API金鑰申請說明.md)**

已有逐字稿檔、只要產出逐字稿 .docx 時不需要金鑰。

## 文件

| 文件 | 內容 |
|---|---|
| [程式使用說明書](docs/程式使用說明書.md) | 安裝、轉錄、修正逐字稿、產出會議紀錄與心智圖、費用、中斷續跑、常見問題 |
| [API 金鑰申請說明](docs/API金鑰申請說明.md) | Azure、OpenAI、Anthropic Claude、Soniox 的申請步驟 |
| [打包與部署](docs/打包與部署.md) | PyInstaller＋Inno Setup 打包、自我檢查、乾淨 Windows 驗證、靜默安裝、發佈到 GitHub Releases |
| [程式架構說明](docs/程式架構說明.md) | 處理流程、模組說明、設定與資料位置、擴充方式 |
| [CHANGELOG.md](CHANGELOG.md) | 各版本的更新紀錄 |
| [SPEC.md](SPEC.md) | 需求規格書 |
| [CLAUDE.md](CLAUDE.md) | 開發守則 |

## 功能概要

- **轉錄**：Azure／OpenAI／Soniox 雲端語音辨識，自動分辨講者；可只轉錄錄音中的一段（拉桿＋試聽），時間碼對應原始錄音。
- **臺灣繁體**：中文輸出轉成臺灣繁體與臺灣用語（OpenCC＋AI 校潤）。
- **產出文件**：修正逐字稿、講者命名與合併、填會議資訊；也可匯入既有的 txt／srt／docx 逐字稿。
- **AI 校正**：產出前由 AI 找出語音辨識錯字（例：獨架→毒駕）並推測講者身分（例：講者1→主席），使用者確認後才套用。
- **會議紀錄 .docx**：依三種會議類型（主題式、問答式、議程逐項式）整理討論事項、結論、待辦；每條結論以 Word 註解標出依據的錄音時間，不確定的內容黃色標示「待查證」。
- **逐字稿 .docx**（可刪除贅字，只刪不改）、**心智圖 PNG／XMind**。
- **詞庫**、**引擎評估**、**每月費用上限與用量**、**中斷後從中斷處繼續**（已付費的部分不重複計費）。
- **錯誤安全網**：程式出錯時顯示中文說明並記錄細節，可一鍵產生「問題回報檔」（不含錄音、逐字稿與金鑰）。

## 開發進度

| 階段 | 內容 | 狀態 |
|---|---|---|
| 0 | 引擎評估 | ✅ 程式完成，待使用者以真實錄音評估並選定預設引擎 |
| 1 | 核心管線（CLI） | 🔧 轉錄、續跑、在地化已完成；外語翻譯、講者不確定標記尚未實作 |
| 2 | 摘要 | 🔧 程式完成，已用真實會議試跑 |
| 3 | 輸出渲染 | 🔧 程式完成，已用 Word 開啟驗證；XMind 待以 XMind 軟體確認 |
| 4 | GUI | 🔧 整合式主視窗已完成（轉錄、產出文件、引擎評估、詞庫、API 金鑰） |
| 5 | 打包 | 🔧 安裝檔完成並發佈 v0.5.4（預覽版）；乾淨 Windows 驗證待執行 |
| 6 | 使用回饋調整 | 尚未開始 |

## 開發環境

需求：Windows 10／11、Python 3.12（或 3.11）、ffmpeg（`winget install Gyan.FFmpeg`）。

```powershell
.\scripts\setup.ps1
```

```powershell
.\scripts\run_dev.ps1
```

```powershell
.\.venv\Scripts\python.exe -m pytest
```

```powershell
.\scripts\build.ps1
```

依序為：建立 .venv 並安裝相依套件、以開發模式開啟 GUI、執行全部測試、打包安裝檔（詳見[打包與部署](docs/打包與部署.md)）。

### 引擎評估（命令列）

圖形介面請用「引擎評估」分頁。命令列：不加 `-Reference` 是快速比較（不需標準答案）；要計算正確率時，先產生標準答案草稿，用 Excel 校正後另存為 `reference.csv` 再評估。

```powershell
.\scripts\benchmark_stt.ps1 -Audio "D:\錄音\測試10分鐘.m4a"
```

```powershell
.\scripts\benchmark_stt.ps1 -Audio "D:\錄音\測試10分鐘.m4a" -MakeReference
```

```powershell
.\scripts\benchmark_stt.ps1 -Audio "D:\錄音\測試10分鐘.m4a" -Reference .\benchmark_out\reference.csv
```

報告存在 `benchmark_out\benchmark_report.md`，逐段對照表在 `engines_side_by_side.csv`；有標準答案時包含 SPEC 5.3 的全部指標（CER、講者錯誤比例、詞庫命中率、語言判定正確率、處理時間、費用）；
加 `-Reuse` 只重算指標，不重新呼叫 API。

## 注意事項

- 錄音檔、逐字稿、API 金鑰、`benchmark_out\`、`dist\`、`build\` 都不會進入版控。
- 正式用於公務前，請先確認單位是否允許把會議錄音上傳到雲端服務（SPEC 13）。
- 安裝檔內附 ffmpeg（GPL 授權，來源 <https://www.gyan.dev/ffmpeg/builds/>）。
