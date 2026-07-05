# Voice Agent — 零安裝的本地語音 AI 助理

> 用作業系統和瀏覽器的**原裝能力**組出完整的語音 AI agent：
> 不裝任何套件、不付任何訂閱、資料留在自己的網路裡。

```
Chrome 網頁（嘴巴+耳朵）                 server.py（大腦，純 Python 標準庫）
├ STT   Web Speech API（Chrome 內建）    ├ Agent 迴圈（OpenAI 相容 tool calling）
├ TTS   speechSynthesis（Windows 語音）  ├ web_search   headless Chrome 抓 Bing
├ VAD   Silero VAD（WASM，語音打斷）  ──▶ ├ web_fetch    讀網頁內文
└ 事件記錄 / 音量條 / 靈敏度調整      ◀── ├ open_app     開程式 / 檔案 / 網址
                                         ├ run_command  PowerShell
                 SSE 串流、逐句朗讀       ├ find_files   遞迴找檔案
                                         └ 技能系統     save / load / delete_skill
                                                │
                                         你的 LLM（任何 OpenAI 相容端點：
                                         vLLM / Ollama / llama.cpp / LM Studio）
```

## 設計理念

1. **最少資源**：終端機器（Windows PC）上全程 CPU、近乎零負擔。STT/TTS/VAD 都借用現成的服務。
2. **零依賴**：`server.py` 只用 Python 標準庫；前端只有一個 HTML 檔（VAD 從 CDN 載入，首次約 2MB 後快取）。
3. **資料自主**：LLM 跑在自己的機器上（例如透過 Tailscale 連到家裡的 GPU 主機），沒有雲端 API 費用。
   唯一的例外是 Chrome 的語音辨識會將音訊送到 Google 服務處理——這是「零安裝」的代價，介意者可換成本機 whisper（見 Roadmap）。

## 功能

- **連續語音對話**：說話 → 停頓自動斷句 → LLM 回覆邊生成邊逐句朗讀 → 講完自動回到聆聽。
- **Agent 工具**：搜尋網路（優先用本機 headless Chrome 抓取，不靠搜尋 API）、讀網頁、開程式、跑 PowerShell、找檔案。
- **自我成長技能**：口頭教它「記住這個做法」，它把步驟存成 `skills/*.md`；之後的對話自動帶入技能索引，遇到相關任務先讀技能照著做。多步驟摸索成功後也會主動問「要不要記成技能？」。
- **語音打斷（barge-in）**：它講話講太長，你直接開口就能打斷（Silero VAD + 音量二次驗證，靈敏度可調）。
- **LLM 可自訂**：網頁上的 ⚙️ 面板即可切換端點 / API key / 模型，儲存並即時測試連線。
- **完整除錯面板**：麥克風音量條、裝置選擇器、事件記錄（辨識、工具呼叫、TTS、打斷判定全程可見）。

## 快速開始

需求：Windows / Linux / macOS、Chrome（或 Chromium）、Python 3.8+、一個 OpenAI 相容的 LLM 端點。

```powershell
python server.py
```

用 Chrome 開 http://localhost:8010 → 點 ⚙️ 填入你的 LLM 位址 → 按「開始對話」並允許麥克風。

注意事項：

- 必須用**真正的 Chrome/Edge** 開（IDE 內嵌瀏覽器拿不到麥克風權限）。
- `localhost` 是安全來源，不需要 HTTPS。
- Chrome 的語音辨識固定使用**系統預設錄音裝置**；用音量條和裝置選單找出有訊號的麥克風後，到系統設定把它設為預設。
- LLM 端建議開啟工具解析（vLLM 範例：`--enable-auto-tool-choice --tool-call-parser hermes`）。
- 若使用 Qwen3 系列，server 會自動要求關閉思考模式並過濾 `<think>` 區塊。

## 部署在遠端主機（例如 GPU 主機 + Tailscale）

server 是跨平台的（Windows / Linux / macOS），工具會自動切換：`run_command` 在
Windows 用 PowerShell、其他系統用 bash；`open_app` 用 start / xdg-open / open；
Chrome 路徑自動偵測（Linux 認得 google-chrome / chromium）。

把 server 跑在和 LLM 同一台主機（例如 NVIDIA DGX Spark），任何裝置——**包括手機的
Chrome**——都能透過瀏覽器連進來語音對話：

```bash
# 在遠端主機上
git clone https://github.com/pondahai/zero-install-voice-agent
cd zero-install-voice-agent
python3 server.py                 # LLM 在同一台時 config 用 http://localhost:8002 即可

# 麥克風需要安全來源（HTTPS），用 Tailscale 內建的 HTTPS 代理最省事：
tailscale serve --bg 8010
```

**必要的一次性開通**：Tailscale 的 Serve/HTTPS 功能在每個 tailnet 預設是關閉的。
第一次執行 `tailscale serve` 會印出一個 `https://login.tailscale.com/f/serve?...` 連結——
用你的 Tailscale 帳號打開它按下啟用（若有提示，連同 MagicDNS 與 HTTPS Certificates 一起開），
回來重跑同一個指令就會拿到正式網址，例如：

```
Available within your tailnet:
https://<主機名>.<你的tailnet>.ts.net/
|-- proxy http://127.0.0.1:8010
```

憑證是自動簽發的正式憑證（Let's Encrypt），瀏覽器不會有任何警告，
且網址只有你 tailnet 裡的裝置連得到。之後從任何在 tailnet 裡的裝置開這個網址即可。

注意兩點：

- **工具跟著 server 走**：`run_command` / `find_files` / `open_app` 操作的是遠端主機，
  不是你手上的裝置。適合「查資料、問問題、累積技能」的用法。
- 直接用 `http://100.x.x.x:8010` 開頁面會因為不是安全來源而**無法使用麥克風**，
  一定要走 HTTPS（或在該裝置上用 SSH 轉發成 localhost）。
- 綁定位址與端口可用環境變數覆寫：`HOST=0.0.0.0 PORT=9000 python3 server.py`
  （搭配 tailscale serve 時維持預設 127.0.0.1 最安全）。

## 教它技能

- 「記住這個做法：以後我問天氣，就直接讀中央氣象署的頁面」→ 存成技能
- 「你會哪些技能？」→ 唸出清單
- 「查天氣的做法改成…」→ 覆寫更新
- 「忘掉查天氣」→ 刪除

技能就是 `skills/` 裡的純文字檔（第一行是描述，之後是步驟），可以直接手動編輯或塞新檔案，下一輪對話即生效。

## 開發歷程

這個專案從一個問題開始：*「VAD/STT/LLM/TTS 的語音 pipeline，能不能純 CPU、只用 Windows 和 Chrome 內建的服務組出來？」* 以下是實際走過的路，含踩過的坑：

1. **可行性驗證** — 確認 Web Speech API（STT）+ speechSynthesis（TTS）+ 自架 vLLM 可以組成完整迴路；用一個 Python 標準庫的 server 同時做靜態檔案和 LLM 代理（順便解 CORS）。
2. **第一個坑：麥克風權限** — IDE 內嵌瀏覽器永遠拿不到麥克風。解法：一律用外部 Chrome 開。
3. **看不見的失敗最難修** — 加上音量條、提示音、事件記錄面板之後，每個環節（收音 → 偵測說話 → 辨識結果 → LLM → TTS）都變得可觀察，之後所有除錯都靠它們。
4. **裝置迷宮** — Web Speech API 只用系統預設麥克風且網頁無法指定。解法：裝置選單 + 音量條幫你找出「活的」麥克風，再去 Windows 設成預設。
5. **Agent 化** — 把對話迴圈從瀏覽器搬進 server（工具必須在 server 端跑），瀏覽器退化成純語音 I/O。vLLM 原生 tool calling 一次就通。
6. **搜尋不靠 API** — DuckDuckGo 會擋 headless 瀏覽器，Bing 不擋；於是用本機 Chrome headless 抓 Bing 結果頁，HTTP 直抓當備援。
7. **自我對話迴圈** — 按停止再開始後，殭屍 LLM 串流的殘句被唸出來、又被麥克風聽進去，agent 開始跟自己聊天。解法：AbortController 真正砍斷串流 + 朗讀守門 + 回聆聽前的緩衝。
8. **技能系統** — 技能 = 純文字檔 + 三個工具（save/load/delete）+ 每輪注入索引。60 行程式碼就讓 agent「越用越順手」，而且重開機不遺失。
9. **語音打斷與誤觸調校** — 最難的一段。Silero VAD 偵測插話容易誤觸，戴耳機也一樣。三個根因：確認計時器永遠比誤觸取消信號先到（競態）、Chrome 自動增益把呼吸聲放大過門檻、單一 VAD 機率門檻不夠。解法：拉長確認時間 + 關閉 AGC + 音量二次驗證（近 0.6 秒內須有四成時間音量過門檻）。

## LLM 能力驗證清單

本專案以 Qwen3.6-35B-A3B（vLLM）開發。開發期間通過的冒煙測試（全部一次通過、零重試）：
基本中文串流對話、標準 tool_calls 輸出、單工具任務（搜尋／開程式／執行指令）、
技能總結與跨對話回憶（load_skill → web_fetch 兩步鏈）。

以下面向尚未系統性驗證——換用其他模型時也建議照這份清單跑一輪，
用語音下指令、看事件記錄面板裡的決策過程即可，不需寫程式：

- [ ] **多跳推理**：「比較 A 和 B 的價格，告訴我哪個划算」（搜尋 → 挑結果 → 讀網頁 → 綜合）
- [ ] **搜尋結果不理想時的行為**：換關鍵字重搜？還是硬掰答案？
- [ ] **知道何時不用工具**：閒聊時會不會多餘地搜尋；該查證的事實會不會憑舊知識亂答
- [ ] **長對話指令維持**：二十輪之後是否仍口語化、簡短、不用 markdown
- [ ] **STT 錯字容忍**：語音辨識的同音錯字（如「查天氣」→「插天氣」）能否意會
- [ ] **工具失敗恢復**：搜尋逾時、指令報錯時能否換路，而非重試到回合上限
- [ ] **模糊指令**：資訊不足時會不會反問，而不是自行腦補
- [ ] **承認查不到**：查一個不存在的東西，看它承認失敗還是編造
- [ ] **主動提議存技能**：多步驟摸索成功後，是否在恰當時機（且不過度）詢問要不要存技能

## Roadmap

- [ ] 真 CDP 常駐 Chrome（重用登入狀態、多步網頁互動、免冷啟動）
- [ ] 完全離線 STT（whisper.cpp 或跑在 GPU 主機上的 faster-whisper）
- [ ] 技能使用統計與自動整理（合併重複、淘汰沒用過的）
- [ ] 第三層自我成長：agent 自己寫新工具（需口頭確認關卡）
- [ ] `run_command` 執行前口頭確認模式

## 安全性說明

`run_command` 讓 LLM 能在你的電腦上執行任意 PowerShell 指令（60 秒逾時、輸出截斷，但**沒有白名單**）。本專案的假設是：LLM 是你自己架的、跑在你信任的網路裡。如果你接的是不受信任的模型或端點，請先拿掉這個工具。

## License

MIT
