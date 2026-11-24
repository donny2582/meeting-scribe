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
  2. 結果帶有 `SttResult.needs_zh_tw_localization = True`；第 1 階段後處理必須**最先**由 LLM 做
     「臺灣繁體中文與在地化用語」校潤，再進行詞庫校正、翻譯、贅字刪除（細節見 `postprocess/localize.py`）。
  3. 是否啟用由設定 `[stt.soniox] convert_zh_tw`、`llm_localize_zh_tw` 控制（預設皆開啟）。

## 目前進度
- 第 0 階段（引擎評估）：程式已完成，待使用者以真實錄音執行評估並選定預設引擎。
- 使用者要求提前建立整合式主視窗（分頁：轉錄、引擎評估、詞庫、API 金鑰、服務與費用），
  介面風格參考 github.com/donny2582/live-subtitle 的設定視窗。第 4 階段在此基礎上加入新工作、進度、審閱分頁。
