# 事故記錄：Hindsight 記憶 bank 被誤刪（2026-11-09 21:35）

## 事發
- 21:33–21:35 依主人 m27910「清掉舊詩意回覆」指示，清理 Hindsight 內 11 條詩意記憶。
- 使用 `.dsh_scratch/delete_poetic_memories_20261109.py`：對 `master` 與 `archive` 各呼叫一次
  `DELETE {HINDSIGHT_URL}/v1/default/banks/{bank}/memories` body `{"memory_ids": [...]}`。
- 之後的 recall / health 顯示 master count 0。

## 證據（2026-11-09 22:50–22:52 查核）
- `GET /v1/default/banks`：`text=14`、`master=0`、`archive=0`、`guests=0`（last_document_at 皆 null）。
- 容器內 PostgreSQL（`/home/hindsight/.pg0/installation/18.1.0/bin/psql -U hindsight -d hindsight`）：
  `select bank_id,count(*) from public.memory_units group by bank_id` → 只有 `text 14`。
- 21:34 清理前之詩意記憶備份：`runtime/archives/hindsight-poetic-remnants-20261109-213458.json`（11 條，master 4／archive 7）。
- `runtime/archives/hindsight-db-backup-20261105-225450.sql.gz`（1.6MB，data-only pg_dump，2026-11-05 22:54）仍在。
- 21:33 之前的 health 顯示 master count 100（今日早上多次檢查皆為 100）。

## 判定
- 批量 DELETE endpoint 的行為與假設不符：帶 `memory_ids` 的 DELETE 似乎把整個 bank 清空（master 4 條 id → 整個 bank 歸零；archive 同理）。**此 API 用法不可再使用**。
- 損失範圍：`master`、`archive`、`guests`（原本各自內容）；`text` bank（14 條 10-24 種子）未受影響。
- 本機 `runtime/episodes/`（事件賬本）、`runtime/session_*`、群聊記錄、`thoughts/actions` 均未受影響。

## 可恢復選項
1. **11-05 全庫備份**（`hindsight-db-backup-20261105-225450.sql.gz`）：可還原至 2026-11-05 22:54 狀態；11-05 之後寫入的記憶（含 11-06～11-09）會缺。需先備份當前 DB，再於臨時 DB 還原、只匯入 master/archive 兩 bank 的 `memory_units`（連同 links/entities 視需要），最後驗證 recall。
2. **由本地資料重建**：`runtime/episodes`、`runtime/session_log` 歸檔、群聊記錄、`runtime/self/` 均在；可寫一次 re-ingest 腳本把重要事實重新 retain 回 master。成本較高，但可保留最近幾天的記憶。
3. **接受損失**：不建議；主人明確說過記憶治理是重點。

## 待主人決定
- 選 1（還原 11-05 快照）、選 2（重建）、或 1+2（先還原 11-05，再把 11-05 後的重要對話 re-ingest）。
- 決定前不動 live DB。

## 教訓
- 破壞性 API 先在小 bank（text/guests）驗證語義，再對 master 動手。
- 任何 master/archive 變更前，先做完整 `pg_dump`（非 data-only 需含 schema）並驗證可還原。
- 今天新增的 `docs/記錄-...` 記錄此事故與後續處置。

## 恢復結果（2026-11-09 23:00–23:12，主人 m28329 批准）

- 先做當前 live DB 全量備份：`runtime/archives/hindsight-db-full-20261109-230407.sql.gz`（pg_dump -Fc，198K）。
- 從 11-05 data-only 備份用 Python 解析 `COPY public.memory_units` 段，只抽 `bank_id IN ('master','archive')` 且 `text` 長度 ≤ 800 的列（主人要求避免「一大坨」）：
  - master **95** 條、archive **409** 條（11-05 22:54 狀態）。
- 以 `session_replication_role=replica` 匯入 live（避開 FK 檢查），再補參照完整性：
  - `documents`：master 81、archive 246（原 dump 沒帶此表，用佔位列補回）。
  - `chunks`：master 81、archive 246（同上）。
  - orphan 檢查（units→documents、units→chunks）皆 0。
- 驗證：`GET /v1/default/banks` → master 95、archive 409；`recall`（master）正常命中「主人每天分享學校的生活」等。
- 未還原：`memory_links`（master/archive 原本的關聯圖）、`guests`（原本 0）、11-05 之後（11-06～11-09）的記憶。
- 腳本：`.dsh_scratch/extract_table_rows_20261109.py`（解析 dump）、`.dsh_scratch/restore_master_archive_20261109.sh`、`.dsh_scratch/fix_orphan_docs_20261109.sql`、`.dsh_scratch/fix_orphan_chunks_20261109.sql`。

## 鏡像測試庫（主人 m28329「鏡像一個記憶庫來進行測試」）

- 新 `scripts/mirror_memory_bank.py`：把 master 的 `documents → chunks → memory_units` 複製到 `mirror` bank（新 unit id；`chunk_id` 前綴 `master_`→`mirror_`），只讀 master、只寫 mirror，**不呼叫任何 Hindsight 刪除 API**。
- 用法：
  - `python scripts/mirror_memory_bank.py`（建立或刷新）
  - `python scripts/mirror_memory_bank.py --status`
  - `python scripts/mirror_memory_bank.py --drop`
- 現況：mirror = 95 條（與 master 同步），`recall`（mirror）正常。
- 測試建議：記憶相關測試使用 `HINDSIGHT_BANK_ID=mirror`（或直接對 mirror bank 召回），不再碰 master/archive。

## 事故後補的規矩

1. 任何 Hindsight 破壞性操作（刪除、重整 bank）前：先刷新 `mirror` 或做完整 `pg_dump`，並確認可還原。
2. **禁用**帶 `memory_ids` 的批量 DELETE（已證實會清空整個 bank）。要刪單條：先用 mirror 驗證語義，或直接 psql `DELETE ... WHERE id=...`。
3. 還原後必須驗證三件事：`GET /banks` 計數、`recall` 命中、orphan 檢查（units→documents、units→chunks）。
