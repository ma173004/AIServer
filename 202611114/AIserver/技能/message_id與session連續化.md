# message_id／session 連續化

**這是什麼**：回覆對得上訊息（Message.id）、同頻道／線程的記憶線連續（不是每回合新 uuid），讓「她記得我們剛才聊到哪」不靠猜。

**前置條件**：
- 先加 `X-Message-Id`／`X-Reply-Message-Id` 落 session_log（外鍵）。
- 再把同頻道 session_id 連續化（這才是真正斷點）。
- 與連續思考區的資料分工要先講清楚（已定：不合併、不同層）。

**目前狀態**：只有 `X-Reply-To` 文字片段；無真實 Message.id；session_id 每回合新 uuid。

**解鎖判定**：主人拍板後做兩步；先觀測、再切換。
