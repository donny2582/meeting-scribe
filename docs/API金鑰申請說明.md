# API 金鑰申請說明

MeetingScribe 使用雲端服務做兩件事，**各需要一把金鑰**：

| 用途 | 可選服務（擇一） | 在程式的哪裡設定 |
|---|---|---|
| **語音轉錄**：錄音 → 逐字稿 | Azure AI Speech、OpenAI、Soniox | 「API 金鑰」分頁 → 1. 語音轉錄 |
| **文字整理處理**：臺灣繁體在地化、刪除贅字、整理討論事項／結論／待辦 | Anthropic Claude、OpenAI | 「API 金鑰」分頁 → 2. 文字整理處理 |

> 只要逐字稿 .docx、而且已經有逐字稿（匯入 txt／srt／docx），**不需要任何金鑰**。
> 兩邊都選 OpenAI 時，共用同一把 OpenAI 金鑰。

各服務的網頁介面可能改版，以下按鈕名稱以實際畫面為準。**費用與免費額度請以各服務官網的價目表為準**。

---

## 建議組合

| 組合 | 說明 |
|---|---|
| **Soniox ＋ Anthropic Claude** | Soniox 費用低（約 US$0.10／音訊小時）、單檔可到 300 分鐘；中文輸出由 Claude 立即校潤成臺灣繁體。 |
| **Azure ＋ Anthropic Claude** | Azure 可選亞洲區域、適合已有 Azure 帳號的單位。 |
| **OpenAI ＋ OpenAI** | 只需要申請一個帳號、一把金鑰。 |

哪個語音引擎最準，可以用程式的「引擎評估」分頁拿自己的會議錄音比較後決定。

---

## 1. Azure AI Speech（語音轉錄）

需要：**金鑰**＋**區域**（例如 `eastasia`、`japaneast`）。

1. 到 <https://portal.azure.com> 登入（沒有帳號先註冊 Azure；需要信用卡驗證）。
2. 上方搜尋「**語音服務**」（Speech services）→「建立」。
3. 填寫：
   - **訂用帳戶**、**資源群組**（沒有就按「新建」，名稱自訂，例如 `meetingscribe`）
   - **區域**：選離臺灣近的區域，例如「East Asia」或「Japan East」
   - **名稱**：自訂，例如 `meetingscribe-speech`
   - **定價層**：選標準（S0）。
4. 「檢閱 + 建立」→「建立」，等待部署完成後按「前往資源」。
5. 左側選「**金鑰與端點**」（Keys and Endpoint），複製「**金鑰 1**」，並記下「**位置／區域**」（例如 `eastasia`）。
6. 回到 MeetingScribe：「API 金鑰」分頁 → 1. 語音轉錄選 **Azure** → 貼上金鑰、選擇相同的區域 → 「儲存」→「測試連線」。

> 若單位的 Azure 使用自訂網域，可在同一區塊填「端點」（例如 `https://<資源名稱>.cognitiveservices.azure.com`）取代區域。
> 在 Azure 入口網站的「成本管理」可以設定預算警示。

---

## 2. OpenAI（語音轉錄或文字整理處理）

1. 到 <https://platform.openai.com> 註冊並登入。
2. **先儲值**：右上角設定 →「Billing」→ 加入付款方式並購買點數（Credits）。沒有餘額時 API 會回報額度不足。
   - 建議在「Limits」設定每月用量上限。
3. 到 <https://platform.openai.com/api-keys> →「**Create new secret key**」→ 取一個名稱（例如 `MeetingScribe`）→ 建立。
4. **立即複製金鑰**（以 `sk-` 開頭）；關閉視窗後就無法再看到完整金鑰，遺失只能重新建立。
5. 回到 MeetingScribe：「API 金鑰」分頁 → 在 1. 語音轉錄選 **OpenAI**，或在 2. 文字整理處理選 **OpenAI** → 貼上金鑰 →「儲存」→「測試連線」。

---

## 3. Anthropic Claude（文字整理處理）

1. 到 Claude 開發者平台 <https://platform.claude.com>（原 console.anthropic.com）註冊並登入。
2. **先儲值**：「Settings」→「Billing」→ 加入付款方式並購買點數（Credits）。
   - 可在「Limits」設定每月花費上限。
3. 「Settings」→「**API keys**」→「**Create Key**」→ 取一個名稱（例如 `MeetingScribe`）→ 建立。
4. **立即複製金鑰**（以 `sk-ant-` 開頭），關閉後無法再看到完整金鑰。
5. 回到 MeetingScribe：「API 金鑰」分頁 → 2. 文字整理處理選 **Anthropic Claude** → 貼上金鑰 →「儲存」→「測試連線」。

各工作預設使用的模型（可在同一區塊的下拉選單修改）：

| 工作 | 預設模型 |
|---|---|
| 臺灣在地化 | Opus 5.5 |
| 刪除贅字 | Haiku 5.5 |
| 外語翻譯 | Sonnet 5.5 |
| 整理討論事項、結論、待辦 | Sonnet 5.5（重要會議可改 Opus 5.5） |

---

## 4. Soniox（語音轉錄）

1. 到 <https://console.soniox.com> 註冊並登入。
2. 依畫面加入付款方式或儲值（Billing）。
3. 左側選「**API Keys**」→ 建立新的金鑰 → 複製。
4. 回到 MeetingScribe：「API 金鑰」分頁 → 1. 語音轉錄選 **Soniox** → 貼上金鑰 →「儲存」→「測試連線」。

> Soniox 的中文輸出不一定是臺灣繁體。程式會先用 OpenCC 轉成臺灣繁體，**有設定文字整理處理的金鑰時**再由 AI 校潤成臺灣用語，
> 所以選 Soniox 時建議一併設定 Anthropic 或 OpenAI 金鑰（程式會以橘色「建議設定」提醒）。

---

## 金鑰的保存與安全

- 金鑰存在 **Windows 認證管理員**（控制台 → 認證管理員 → Windows 認證，名稱為 `MeetingScribe`），不會寫進設定檔或日誌；畫面只顯示末 4 碼。
- 不要把金鑰貼到 Email、聊天軟體或文件中；疑似外洩時，到該服務的金鑰頁面**刪除（Revoke）舊金鑰**再建立新的。
- 程式的「API 金鑰」分頁 → **3. 費用**可以設定每月費用上限（本程式估算），達到上限時會停止新的工作；
  實際金額以各服務的帳單為準，建議同時在各服務的網站設定用量上限。
- 正式用於公務前，請先確認單位是否允許把會議錄音上傳到雲端服務。
