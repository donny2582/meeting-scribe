# MeetingScribe 開發守則

- 需求以 `SPEC.md` 為準；有衝突或不清楚的地方先提問，不要自行假設。
- 依 `SPEC.md` 第 12 章的階段開發，一次只做一個階段，完成並通過驗收後才進入下一階段。
- 目標平台是 Windows；路徑一律使用 `pathlib`；指令碼使用 PowerShell（同時相容 5.1 與 7）。
- 程式碼要有 Type Hint 和 Docstring，並維持模組化；核心邏輯可用 CLI 獨立執行，GUI 只呼叫核心。
- 介面文字與輸出內容使用臺灣繁體中文、臺灣用語。
- 不要把 API 金鑰、錄音檔、逐字稿加入 git。
- 模型名稱、API 單價不可以寫死在程式碼中（放在 `assets/default_config.toml` 或使用者設定檔）。
- 日誌不得記錄逐字稿內容，只記錄階段、時間和錯誤訊息。
- 每個階段完成時：執行全部測試（`pytest`）、更新 README、建立 git commit。

## 使用者追加規則
- **Soniox 引擎的中文處理**：Soniox 的中文只有 `zh`，輸出不保證是臺灣繁體。
  1. 轉錄後中文片段立即以 OpenCC s2twp 轉成臺灣繁體（日文、韓文片段不轉，避免日文漢字被改錯）。
  2. **有文字整理處理服務（Anthropic 或 OpenAI，設定 `[llm] provider`）的金鑰時，轉錄完成當下立即**由 LLM 做「臺灣繁體中文與在地化用語」校潤
     （`tasks.localize_if_possible` → `postprocess/localize.py`），之後的詞庫校正、翻譯、贅字刪除都以此為準。
     沒有金鑰或 LLM 失敗時保留 OpenCC 結果，`needs_zh_tw_localization` 維持 True，第 1 階段後處理開始前要再補做。
  3. 是否啟用由設定 `[stt.soniox] convert_zh_tw`、`llm_localize_zh_tw` 控制（預設皆開啟）；
     各工作的模型與推理強度在 `[llm.<服務>.models]`、`[llm.anthropic.effort]`／`[llm.openai.reasoning_effort]`；
     批次大小 `[llm] localize_batch_chars`。
- **文字整理處理依工作選模型**（使用者指定的建議）：刪除贅字＝Haiku 5.5（OpenAI：gpt-6-luna）、
  外語翻譯＝Sonnet 5.5、整理討論事項／結論／待辦＝Sonnet 5.5（重要會議可選 Opus 5.5）；臺灣在地化＝Opus 5.5。
  呼叫 LLM 時一律指定 task（localize／filler／translate／summary）。模型選項與建議在 `assets/model_catalog.toml`。
- **模型名稱一律用下拉選單＋右側「＋」新增**；「↻ 從服務更新模型清單」用官方「列出模型」API（不要抓網頁）。
  新增的存 `[models.custom]`、查到的存 `[models.fetched]`。
- **API 金鑰分頁只有兩個區塊**：「語音轉錄」（選服務，只顯示該服務的金鑰與區域／模型，與「轉錄」分頁的引擎同步）
  與「文字整理處理」（Anthropic／OpenAI）。不要再拆回每個服務一個區塊。

## 目前進度
- 第 0 階段（引擎評估）：程式已完成，待使用者以真實錄音執行評估並選定預設引擎。
- 使用者要求提前建立整合式主視窗（分頁：轉錄、引擎評估、詞庫、API 金鑰）。
  使用者不要重複的設定頁：引擎、語言、輸出資料夾只在「轉錄」分頁選並記住上次選擇；
  Azure 區域與模型名稱放在「API 金鑰」分頁對應服務的區塊；單價只在設定檔。
  介面風格參考 github.com/donny2582/live-subtitle 的設定視窗。第 4 階段在此基礎上加入新工作、進度、審閱分頁。
- 使用者要求新增「產出文件」分頁（轉錄完成自動跳轉；也可匯入既有逐字稿 txt／srt／docx／json）：
  修正逐字稿、會議資訊表頭（SPEC 4.1）、產出項目；文件存成工作資料夾的 document.json。
  已提前實作逐字稿 .docx（render/docx_transcript.py、render/roc_date.py）；會議紀錄與心智圖仍待第 2、3 階段，
  完成後接到本分頁的「產出文件」按鈕。
- 測試一律透過 tests/conftest.py 隔離 APPDATA 與金鑰，不得讀寫使用者真正的設定檔。
- **與使用者一律用繁體中文溝通。**
- **語音範圍**（「轉錄」分頁，gui/audio_range.py，QtMultimedia 試聽）：只轉錄 clip＝（起點, 終點）；
  前處理用 ffmpeg -ss/-t 擷取，轉錄後所有片段時間加上起點（tasks.shift_to_clip），SttResult 記錄 clip_start／clip_end。
  暫存依範圍區分。之後任何顯示或輸出時間碼的功能都必須對應原始錄音時間，不可從 0 開始。
- **費用上限與使用量**（API 金鑰分頁「3. 費用」）：每次雲端呼叫都要記到 `usage.py`（STT 用 record_stt、
  LLM 用 record_llm，指定 task）；開始新工作前檢查 `[budget] monthly_limit_usd`，達上限就停止。
  模型 token 單價在 `assets/model_catalog.toml`（price_in／price_out）。新增 LLM 呼叫時務必記錄用量。
- **中斷續跑（使用者明確要求：餘額不足等中斷時不可全部遺失或從頭開始）**：`checkpoint.py` 的暫存
  （`%LOCALAPPDATA%\MeetingScribe\cache\{錄音指紋}\{引擎}_{設定指紋}\`）。已付費的結果一完成就存檔：
  轉錄結果（stt_result.json）、分段（chunks/）、Soniox 已送出的工作（soniox_job_*.json，伺服器完成就取回不重送）、
  在地化每批（localize.json）。中斷時保留已完成部分並在訊息說明「再按開始轉錄會從中斷處繼續、不重複計費」。
  **第 2 階段之後新增的 AI 步驟（刪贅字、翻譯、摘要）也必須照這個模式逐批存檔、可續跑。**
