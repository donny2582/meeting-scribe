"""臺灣繁體中文在地化（LLM）——第 1 階段實作。

規則（使用者指定）：STT 引擎設定 ``llm_localize_zh_tw = true``（目前為 Soniox）時，
第 1 階段後處理必須**最先**執行本步驟，再做詞庫校正、翻譯、贅字刪除：

1. 輸入：``SttResult.needs_zh_tw_localization`` 為 True 的結果（已先經 OpenCC s2twp 轉成繁體）。
2. LLM 逐片段校潤成臺灣繁體中文與臺灣在地用語：修正 OpenCC 轉錯的字（例：一對多的繁簡字）、
   套用 ``assets/term_map_tw.csv`` 用語對照表與詞庫正確詞；只處理中文片段，英日韓原文不動。
3. 只改字形與用語，不得增刪內容或改寫語意；片段數量與順序必須不變，否則退回 OpenCC 結果並寫入日誌。
4. 原始引擎文字保存在 stt_raw.json，不受影響。
"""
