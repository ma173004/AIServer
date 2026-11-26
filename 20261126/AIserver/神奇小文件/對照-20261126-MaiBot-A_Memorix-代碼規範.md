# 對照：MaiBot / A_Memorix 與映月（2026-11-26）

> 資料來源：`C:\Users\ma173\Desktop\New folder\解析报告\00-总览与横向对照.md`、`02-MaiBot-解析.md`；
> 以及 `C:\Users\ma173\Desktop\New folder\MaiBot-1.4.8` 的 `pyproject.toml`、`AGENTS.md`、`src/A_memorix` 源碼。
> 本文只做對照與可借鏡清單，不改生產代碼。

## 0. 一句話

映月是「伴侶功能廣度領先、工程完成度落後」；MaiBot 是「通用人格聊天智能體、工程基礎更成熟」。
不是同一條產品線，不能直接用「誰更完整」比；但它的**記憶資料模型、檢索分層、工程規範**有幾樣值得拆出來用。

## 1. A_Memorix 到底有什麼

它不是「一個大圖」而已，是三層疊起來：

1. **段落層**：`paragraphs`（原文、時間、來源、knowledge_type、metadata）；檢索的基本單位。
2. **關係/實體層**：`entities`、`relations`、`paragraph_entities`、`paragraph_relations`；支持一對一、一對多、多跳。
3. **事實/畫像層**：`fact_claims`、`fact_evidence`、`fact_transitions`、`person_identities`、`person_identity_aliases`；支持 conflict、supersede、retract、valid_from/to、authority。
4. **Episode 層**：`episodes`、`episode_paragraphs`；把一次對話/一段經歷聚成可重建單元。
5. **檢索層**：向量 + BM25 + 圖關係 + PPR（Personalized PageRank）+ grounding/reliability + 分數校準；mode 有 `search/time/hybrid/episode/aggregate`。
6. **維護層**：schema v31、`schema_migrations`、自動遷移、備份、外鍵修復、刪除/恢復、導入/調優管理。

關鍵設計不是「圖很大」，而是：
- 每個 claim 都有 **evidence + authority + status + 時間**；
- 單值槽位出現不同值 → `conflicted`，不按時間靜默覆蓋；
- 檢索結果有 **grounding/reliability**，不是只給相似度；
- schema 有版本、遷移、備份，不是 ad-hoc jsonl；
- 人物身份用 `person_identities` + alias，不靠自然語言稱呼猜。

## 2. 我們現在有什麼、缺什麼

| 維度 | 映月現況 | A_Memorix | 差距性質 |
|---|---|---|---|
| 記憶單元 | Hindsight `memory_units` / documents / chunks；master 已凍結 | 段落 + 實體 + 關係 + episode + claim | 我們只有文本級，沒有 typed relation |
| 事實帳本 | fact_cards（6 條 confirmed）+ profile.json；無 evidence/status/conflict | fact_claims + fact_evidence + transitions | 缺「同一事實多來源、衝突、時間、權限」 |
| 人物身份 | people.json + owner_guard_label；手動 | person_identities + aliases + 平台 ID | 我們手動、易混名 |
| 時間 | 每條記憶有 ts；召回無 time filter | 查詢 mode=time/hybrid 必須 time_start/end | 缺「某段時間內」的可控召回 |
| 檢索 | Hindsight recall top_k=12；V2 可回滾；無 BM25/圖 | 向量+BM25+圖+PPR 融合 | 我們單路，遇到實體關係題吃虧 |
| 衝突 | 無；舊事實蓋新事實 | conflicted/superseded/retracted + evidence | 這是幻覺/反覆改口的根 |
| 可解釋 | evidence ledger 只記注入區塊 | grounding/reliability + evidence id | 我們有 ledger，但沒有 claim 級證據 |
| 持久化 | runtime jsonl + 少量 SQLite（Hindsight） | SQLite + migration + backup | 我們無 schema/version/rotation |
| 刪除 | Hindsight 批量或整庫 | delete preview/execute/restore/purge | 我們刪除仍危險 |

## 3. 對我們最有用的三件（不是整套搬）

### 3.1 事實 claim 三表（最高價值）

最小三表即可：
- `fact_claim(id, scope, key, value, status, authority, valid_from/to, first/last_seen, confidence)`
- `fact_evidence(claim_id, source_type, source_id, quote, observed_at, stance)`
- `fact_transition(old_id, new_id, type, reason, evidence_id, created_at)`

用途直接打在最近的 bug：
- 「7 個欄位」→ 沒有 claim/evidence，不能說；
- 「47 分鐘」→ 沒有來源，不能說；
- 17:30「你沒想好下一步」→ 不是主人 claim，不能算 owner-stated；
- 主人職業「軟體工程師 vs AI 架構師」→ conflict，不靜默覆蓋；
- 舊事實過期 → status=superseded，不是靠 `_same_project` 合併。

寫入權限：只收 owner-stated / tool-verified / 有 source_id 的 memory；**her own inference 只能進 inference 區，不能升格 fact**。這與我們 11-25 的 provenance 方向一致。

### 3.2 人物身份 + alias

`people.json` 目前是手動 label；A_Memorix 的做法是：
- `person_id` 由 platform+user_id 決定；
- alias 另表，帶 group_id、first/last_seen；
- 顯示名與 canonical name 分開。

我們可以只做 `person_identity` + `person_alias` 兩張小表，接到現有 `relations.py`；群聊 speaker resolution 和人稱問題會少一層猜。

### 3.3 時間感知召回

A_Memorix 的 `mode=time` 要求 time_start/end，不靜默按未命中；我們目前召回沒有時間條件，所以「昨天」「上週」「小時候」只能靠模型猜。
最小做法：Hindsight recall 前先解析時間範圍（owner 原話 + HINT），召回結果帶 `observed_at`；沒有時間命中就說不知道，不編。

## 4. 不建議搬的

- **整套 SQLite + 圖 + PPR**：29.6 萬行、五個 store、四路融合；我們 14B 單機，維護不起。
- **LLM 自動抽 fact/relation**：它的 ingest 會用模型抽，我們已被 3B 抽取器污染過一次；先用 deterministic + owner-confirmed。
- **A_Memorix 的 migration 全量**：可以借「版本欄位＋遷移腳本」的形式，不需要 55 代。
- **prompt 資產化整套**：owner 自己也說了 231 個 prompt 是堆砌；我們只借 `strict placeholder + meta/version`，不借三語 231 份。
- **A_Memorix WebUI/插件/MCP**：與我們單實例不符。

## 5. 代碼規範對照

MaiBot 的做法（可借）：
- `pyproject.toml`：Python >=3.12、依賴分組、`[tool.ruff]`（E/F/B，line 120）、`[tool.pytest.ini_options]`；
- `.pre-commit-config.yaml`；
- `AGENTS.md`：import 分組、型別註解、禁止 fallback/防禦性兜底、事件循環規範、session ID 規範、prompt 三語同步；
- A_Memorix：dataclass/frozen config、Enum、`__post_init__` 驗證、mixins 分層、schema 版本、runtime self-check、backup、明確 raise（不靜默）；
- 測試：pytest + fixtures 目錄，不是單一 shell script。

我們可以低成本借的：
1. 加 `pyproject.toml`（只放 ruff/pytest 設定，不做 packaging）+ pre-commit；先只對 changed files 跑。
2. 所有持久化 jsonl 加 `schema_version` 欄位＋一個 `migrations.py`（目前 turn ledger 已有 `ledger_v`，可推廣）。
3. 將 `dshmid.py`（2423 行）、`thought_loop.py`（2736 行）、`chat_agent.py`（2098 行）分三步拆：
   - 先抽純函數/常數（ledger、prompt sections、person formatting）；
   - 再抽 state class（不要一次大重構）；
   - 最後才動 pipeline。
4. state/config 用 dataclass + Enum，不用裸 dict + `getattr` fallback。
5. 測試 fixture 一律 tmp path（我們已做一部分）；新增測試用 pytest 風格或至少統一 `check()`。
6. 錯誤不要 `except: pass`；已有 `faults.py`，統一走它。
7. 加 runtime self-check（像 A_Memorix `runtime_self_check.py`）：檢查 ledger 可寫、prompt budget、Hindsight 可達、env 開關一致。

## 6. 人格與 T6（回答 owner 的兩點）

### 人格
owner 說的「人格可通用、按環境加載」是對的。我們已經是「一個 universal persona + overlays」，方向沒錯。
紅隊說的「人格不完整」不是指內容，而是**運行時承載**：沒有人稱/說話人帳本、沒有 provenance，所以人格再漂亮也會被 context 污染。要先修承載，不是再寫人格。

### T6
T6 不是沒用；它是「內部狀態 → 行為傾向」的 substrate。問題是**開環**：
- 沒有 event appraisal（事件對目標/關係的意義）；
- 沒有 evidence（為什麼是這個情緒）；
- 沒有 episode（情緒的起點、衰減、結束）。

可補的不是再加一層 llama，而是：
1. 保留五通道 + tier；
2. 加 deterministic appraisal（goal relevance / agency / certainty / control / norm）；
3. 加 emotion episode（cause、evidence_id、decay、resolved）；
4. LLM 只做離線標註/評估，不進實時控制；
5. 用行為結果校準（主人回應、打斷、沉默），不是靠她自述。

這比「最先進」更重要：可觀測、可回滾、可證偽。

## 7. 建議的下一步（等 owner 選）

- A. context provenance + 高危塊過濾（直接打今天五個失敗，零模型呼叫）。
- B. P06 收件匣 + 快照（根治 queued lose / merged coverage）。
- C. 先做 memory claim 三表 spike（只寫 owner-stated/verified，shadow 讀，不接生成）。
- D. 只做代碼規範低成本項（pyproject/ruff/schema_version/self-check），不改架構。

我建議今晚先 A；C 另開 spike；D 可並行但不動 pipeline。

## 8. 待補

- owner 正在找的源碼文件（三份解析之外）。
- A_Memorix `MODIFICATION_POLICY.md` 的歸屬約束，若真要抄代碼需先讀。
- MaiBot prompt 引擎的 strict placeholder 實作，若要借需看 `src/prompt`。
