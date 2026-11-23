# 訊號定義

分析 session 時要找的訊號。每個訊號都要附上證據：session ID 與訊息序號（`[U#12]` 的 12）。

## correction：使用者糾正

使用者否定或修正 agent 剛做的事，例如「不對」「不是這個」「我說的是…」「改回來」「不要…」「重來」，或改用更具體的方式重述同一需求。

- 判斷：糾正的原因能寫成一條未來可遵守的規則，例如「查詢資料前先確認是哪個伺服器與資料庫」。只是需求本身改變的，不算糾正。
- 去處：相關 skill；跨任務通用的偏好放 `AGENTS.md`。

## repeated-context：重複補充資訊

使用者在不同 session 一再手動提供相同資訊，例如路徑、連線、環境、命名慣例或業務規則，而這些資訊 agent 原本可以自己查到，或應該記錄在文件裡。

- 判斷：同一筆資訊至少出現在 2 個 session。
- 去處：`AGENTS.md`、skill 的 references，或專案文件。

## tool-friction：工具卡點

工具失敗、超時、權限拒絕、編碼亂碼、shell 語法錯（例如在 pwsh 中用 bash 語法），或同一工具連續失敗後重試。`meta.json` 的 `tool_error_rate`、`retry_after_error`、`top_errors` 是起點，要回到對話紀錄確認原因。

- 判斷：找出可避免的根因，例如搜尋範圍太大、少帶參數、選錯工具。屬於外部服務暫時故障的，列為 Rejected。
- 去處：能用設定、權限或 script 擋下的放 Backlog；需要判斷的寫進相關 skill 的步驟。

## missed-skill：skill 漏觸發

任務明顯符合某個已安裝 skill 的描述，agent 卻沒載入；或使用者必須用 `@skill` 手動指定（`meta.json` 的 `skills_mentioned_by_user`）；或 agent 載入了不相關的 skill。

- 判斷：讀該 skill 的 `description`（路徑在 `meta.json` 的 `skills_used`；未列出的 skill 依名稱在 skill 來源中尋找），說明哪個字或哪種情境沒被描述涵蓋。
- 去處：調整該 skill 的 `description`。

## rule-drift：違反既有規則

agent 做了 `AGENTS.md` 或某個 skill 已明文禁止或要求的事，例如沒用 `question` 工具確認、沒依規則建分支、直接呼叫不該由 primary 呼叫的工具。

- 判斷：先找到被違反的那一條規則並引用位置。規則寫得含糊時，改寫措辭。規則寫得清楚卻仍被違反時，代表沒有機制強制執行，放 Backlog，註明可用的強制手段，例如權限設定、hook 或 script 檢查。

## subagent：委派問題

子 session（subagent）撞到步數上限而被迫停止、交辦範圍過大、回報不完整卻被 primary 當成完成，或同一件事被重複委派。對話紀錄中子 session 標題帶「（疑似撞到步數上限）」的是起點；`meta.json` 的 `totals.agents` 列出各類 agent 的子 session 數、工具呼叫、失敗與疑似撞到上限的次數，`totals.step_limit_suspected` 是總數。

- 判斷：同一類 agent 反覆撞到上限時，找出原因，例如一次交辦太多題、範圍沒切片、缺少可直接使用的前置資訊（連線、路徑）而浪費步數。要回到子 session 段落確認它把步數花在哪裡。
- 去處：調整委派方式寫進 `AGENTS.md` 或相關 skill（例如每次只交辦一個主要假設）；需要調整步數上限或 agent 權限的放 Backlog。

## wasted-work：白工

重複讀同一批檔案、subagent 重做 primary 已完成的查詢、使用者中斷（`Step interrupted`、`Tool execution interrupted`）後重做、方案被否決後大幅返工，或因 context 壓縮（COMPACT）而遺失進度。

- 判斷：說明浪費的成本（多少步或哪段流程），以及一個能避免浪費的具體做法。

## works-well：順利的流程

一次到位、使用者直接接受的流程。只記錄可複製的做法，作為保留現有規則的依據，不產生修改提案。

## 不收錄

- 偶發事件：跨 session 模式指只出現在單一 session 的；單次模式指本次對話只發生一次、且使用者未要求記下的。
- 會很快失效的細節：SHA、版本號、筆數、暫時性的錯誤訊息。
- 現有規則已寫清楚、而且這次也確實被遵守的內容。
- 客戶個資或業務資料本身；證據只寫摘要與位置。
