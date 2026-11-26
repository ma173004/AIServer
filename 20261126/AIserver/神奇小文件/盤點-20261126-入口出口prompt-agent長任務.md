# 盤點：入口／出口／prompt／agent／長任務（2026-11-26）

> 目的：先看清楚現在到底有什麼在跑，再決定砍什麼。本文只盤點，不改代碼、不改 prompt。
> 證據：`ps` 進程、最近 24h trace、`prompts/` 檔案、`runtime/` 目錄、crontab。

## A. 入口（東西從哪裡進來）

| 名稱 | 入口／觸發 | 輸出給誰 | 現在真的跑？ | 建議 | 備註 |
|---|---|---|---|---|---|
| dshmid 主線 | Web／DSH `:11435`，主人對話 | 主人（SSE） | 是，常駐 | **留** | 主聊天線；P06 ledger 也在這 |
| Discord bot | Discord DM／群／通知 | 主人、朋友、群 | 是，常駐 | **留** | 第二入口，最後要收束到同一回合狀態 |
| 客人線 | dshmid `X-Audience=guest` | 朋友 | 是 | **留** | 已有 guest system/user 分層、朋友頁 |
| relay 朋友→主人 | `agents/relay.py` | 主人 | 代碼在，尚無真實轉達 | **留，先觀察** | `relay.stats().total=0` |
| task_runner | 背景任務佇列 | 主人／任務 | 是，常駐 | **留** | 第二線；`tasks/active` 目前 0、archived 189 |
| scheduler／reminders | `runtime/schedules.json`、提醒到期 | 主人 | 是 | **留** | 舊固定排程有 disabled，要另行清理 |
| collectors | phone／eclass／hko／mtr／sysmon／mind | 資訊線 | 是 | **留** | 信息線入口；採集器失敗已記 faults |
| thought_loop | 主人回合後／idle | 主人（say）／紙條 | 是 | **留，但收** | 側迴路，最容易長出主動訊息 |
| mind_loop | 每 180s tick | 自己的 thoughts | 是 | **查／合** | 產出多、決策出口少；可併入 thought_loop 或降頻 |
| session_watcher | 會話空閒／歸檔 | archive／摘要 | 是 | **留** | 摘要與歸檔 |
| log_viewer | `:8890` 頁面 | 人（主人／我） | 是 | **留** | 只讀 |
| CLI `bin/ai-*` | 手動／她呼叫 | 檔案、系統 | 是 | **留** | 工具面，不是管線 |

## B. 出口（東西從哪裡出去）

| 名稱 | 出口 | 現在真的跑？ | 建議 | 備註 |
|---|---|---|---|---|
| dshmid 最終回覆 | SSE／Discord reply | 是 | **留** | 主出口 |
| discord_bot send | Discord 訊息 | 是 | **留** | 回覆、通知、主動訊息共用 |
| outbox.emit | proactive／say | 是 | **留，收束** | 目前有自己一套發送 |
| followup.generate | 追加話 | 是 | **合** | 與 outbox／say 功能重疊 |
| thought_loop say | idle／after_turn | 是 | **留，但走 outbox** | 不應自己直接送 |
| discord_notify.push | 通知 | 是 | **留，收束** | 與 outbox 重疊 |
| task_runner 通知 | 任務完成／失敗 | 是 | **留，收束** | 與 notify 重疊 |
| relay owner→friend | 轉達朋友 | 尚無真實 | **留** | 需 owner 明示 |

**結論：出口至少五條，沒有單一出口 guard。** 下一步的 foundation 是把這五條都接到同一個 `output_guard`（先 shadow，不改內容）。

## C. Prompt

### 真正在用的（候選保留）
| 檔案 | 用途 | 建議 |
|---|---|---|
| `prompts/persona/chat_owner_v9.md` | 主人 chat 人格（第一人稱） | **留** |
| `prompts/persona/chat_guest.md` | 客人 chat 人格 | **留** |
| `prompts/overlays/owner_private.md` | 主人私聊場景 | **留** |
| `prompts/overlays/owner_task.md` | 主人任務場景 | **留** |
| `prompts/overlays/guest.md` | 客人場景 | **留** |
| `prompts/overlays/proactive.md` | 主動訊息場景 | **查**（是否還有讀者） |
| `prompts/overlays/public_silent.md` | 公開沉默場景 | **查**（群聊未開） |
| `prompts/agents/research_agent.md` | research agent | **留，限流** |
| `prompts/agents/writing_agent.md` | writing agent | **凍結** |
| `prompts/agents/learning_agent.md` | learning agent | **凍結** |
| `prompts/agents/generic_agent.md` | generic agent | **凍結** |
| `prompts/README.md` | 規則 | 留 |

- `prompts/persona/archive/` 有 16 個歷史檔（v5–v13、eval、fewshot）——不是 runtime，**移到 `docs/archive/` 或壓縮歸檔**，不要跟現行 prompt 混在一起。
- 現行 prompt 檔共 11 個（persona 2＋overlay 5＋agents 4）；目標先減到 8 個以內。
- 動態 context 區塊（不是檔案）是另一張表：時間、主人此刻、我此刻、judge、工作臺、歷史摘要、事實卡、記憶召回、提醒、對話、對話線、outbox、inbound… 這部分先只做 provenance，不急著砍。

## D. Agent

| Agent | 用途 | 最近 24h 生產 trace | 建議 | 備註 |
|---|---|---|---|---|
| chat_agent | 主聊天 | `executor_chat` 70 | **留** | 主線 |
| task_agent | 任務／工具 | `executor_task` 26 | **留** | 第二線 |
| research_agent | 查資料／研究 | `executor_research` 2 | **留，限流** | 偶爾用，不要常駐 |
| writing_agent | 寫作 | 未見 | **凍結** | prompt 留著即可 |
| code_agent | 寫程式 | 未見 | **凍結** | 有安全風險 |
| learning_agent | 學習 | 未見 | **凍結** | 與自主學習混淆 |
| generic_agent | 通用 | 未見 | **凍結** | 與 chat/task 重疊 |
| work_agent | 工作 | 未見 | **凍結** | 與 task 重疊 |
| executor.py（調度） | 分派 agent、融合／q3 | 是 | **留** | 不要一次改寫 |
| thought_loop | idle／after_turn | 是 | **留，收** | 自循環主要來源 |
| dialogue_thread | 對話線／反思 | 是 | **留** | 已被 chat 讀 |
| mind_loop | 背景想法 | 是 | **查／合** | 產出多、出口少 |
| self_questions | 問題清單 | 是 | **留但限流** | 300 open 是存量問題 |
| consolidate_worker | 夜間記憶固化 shadow | cron 03:40 | **留，觀察** | 尚未寫 master |

- 9 個 `agent_mod` 檔案裡，日常真正跑的是 chat／task；research 偶爾；其餘 5 個（writing／code／learning／generic／work）最近 24h 未見生產 trace。**功能廣度目前是負債。**

## E. 長任務／背景循環

| 名稱 | 觸發 | 現在狀態 | 建議 |
|---|---|---|---|
| task_runner | 常駐 | 跑 | **留**（第二線） |
| task_manager | 任務建立／歸檔 | active 0、archived 189 | **留**，但先清舊任務 |
| B5 reflux 回流書 | 自動／通知 | 1156 條：1147 auto、1 approved、8 rejected | **停 auto，只留主人 approved 路徑**；沒有真正的 self-rollback |
| reminer/scheduler | cron／到期 | 跑 | **留**，清理 disabled 舊排程 |
| collectors | 常駐 | 跑 | **留** |
| thought_loop idle | 30 分鐘 | 跑 | **留，限制主動訊息** |
| mind_loop | 180 秒 | 跑 | **查／合** |
| weekly_profile | 週日 21:00 | 跑 | **查**（是否還需要） |
| experience_daily | 每日 23:30 | 跑 | **留**（但先確認輸出） |
| turn_timing | 每 2 分鐘 | 跑 | **留**（可觀測） |
| watch_delivery | 每 10 分鐘 | 跑 | **留** |
| sleep settle | 每 15 分鐘 | 跑 | **留** |
| shadow_openloops | 每 30 分鐘 | 跑 | **查**（shadow 無出口） |
| exp_medfix_watch | 每 10 分鐘 | 跑 | **留到實驗結束** |
| consolidate | 每日 03:40 | 跑 | **留**（shadow） |

- 背景 cron 目前 8 條；建議先保留 5 條（turn_timing、watch_delivery、sleep settle、consolidate、experience_daily），其餘標「查」。
- **沒有真正的 agent 自我回滾／自循環優化**：回流書多數 auto，代碼改動沒有自動 rollback。這不是現在要補的功能；先把出口和訊息生命週期做好。

## F. 建議的減法順序（先不改代碼）

1. **Foundation 先做**：一個 turn ledger（P06 已有）＋一個 output guard（shadow 先）。
2. **Agent 減法**：凍結 writing／code／learning／generic／work；只留 chat＋task，research 限流。
3. **Prompt 減法**：archive 移走；active 11 → 8；overlay 的 proactive／public_silent 先查讀者。
4. **長任務減法**：reflux auto 停；mind_loop 先降頻或併入 thought_loop；每週/每日 cron 只留 5 條。
5. **最後才談新功能**：不在 foundation 完成前加新側迴路、新 agent、新 prompt。

## G. 待主人決定

- 上面「凍結」與「查」的項目，是否直接照表執行？  
- 「停 reflux auto、留 approved」是否現在做？  
- long task 的 189 個 archived 要不要先清？  
- `mind_loop` 要降頻、合併，還是先關？
