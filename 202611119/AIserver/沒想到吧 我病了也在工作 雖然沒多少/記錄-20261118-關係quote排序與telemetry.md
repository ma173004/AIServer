# 記錄-20261118：關係 quote 排序、per-quote dedupe、telemetry（m45222）

## 一、主人指示
「修吧 記得給紅隊反駁一下 看看是否有不可行狀況 / 然後我等我看過數據再簽字」——修三件（R1 容器排序、per-quote 24h dedupe、回覆/quote 重疊 telemetry），先讓兩隊紅隊反駁不可行狀況，master2 切換等主人看過數據再簽字。

## 二、兩隊紅隊反駁摘要（原文另存紅隊筆記）
- 意圖紅隊（redteam-int-2）：
  1. 硬性 priority＋top_k 8 會蓋過候選自身相關度；意圖組只能 soft boost，且改法應以「預期答案型別」離線評估，不要字面組。實測容器句本來就在 top5，「不來」詞太泛（「失去興趣」）。
  2. per-quote dedupe 會造成同日重問像失憶；需區分重問（normalize 相似度 ≥0.8 或「還記得／又問／再說一次」→bypass 並 log reask）；dedupe 條件理想是「被注入且她真的用過」。
  3. chat_agent thread-local 會漏 MIMO／非 native／probe；建議注入事件在 memory.py 記 cycle 資訊，reply overlap 用 episodes 離線 join。
  4. 順序應為 ③ telemetry 先跑基線 → ② dedupe → ① 排序；三者不要同日上。
  5. seen.json 用 read-modify-write 會丟寫入／壞檔，改 append-only jsonl；加 env `RELATION_QUOTE_DEDUPE=0` 給 eval；top_k 只影響關係題。
- 系統紅隊（redteam-sys-2）：
  1. top_k 8 救唔到排序；容器句本就在 top5。建議「固定 canonical query 再 recall」比意圖組更可解釋；「對你來說」太泛，要意圖詞＋第一人稱＋候選存在。
  2. dedupe 只 dedupe quote 標記，不要 dedupe 內容；同 id 仍可作一般 recall 出現；key 要含 bank（`bank:id`）；加 lock／atomic。
  3. telemetry 四個洞：每輪開頭清 thread-local、一輪可能多條 quote、回覆定稿前會被 echo regen／retry／strip_leaks 重寫（要算最終文字）、MIMO 要標 merged_batch、probe 分母要標；8-gram Jaccard 對短中文不靈敏，加 4-gram containment／LCS。
  4. 「每日 shadow cap 2 只 log」不足以回滾判斷；要有 echo 分布、重問率、主人指背台詞次數；V2 只在 scratch 時生產 chat 不會有 overlap，要 replay harness 真生成。
- 兩隊共識：方向可做、無硬性不可行；順序 ③→②→①，排序需 replay 證據。

## 三、已實作（全部只在 V2；生產 `MEMORY_RECALL_V2` 未設）
`agents/memory.py`：
- 排序：`_prefer_relation_quote` 加 rank2 組（env `MEMORY_RELATION_QUOTE_RANK2` 預設 1，只在 V2＋關係題生效）：意圖詞＋第一人稱（我／我們）才啟用；加「對你來說／我對你／意味著什麼→容器／存放／錨點／意義」「不來／離開→陪／身邊／一直在」「怎麼看我／你覺得我→容器／意義／自主」；relation recall `top_k` 5→8（rank2 開時）。
- dedupe：`relation_quote_seen.jsonl` append-only（env `RELATION_QUOTE_SEEN_PATH`），key=`bank:id`，TTL 24h；只擋 quote 標記，不影響一般 recall；同 session 同句或「還記得／又問／再說一次」→bypass（reason `reask`／`same_session_reask`）；env `MEMORY_RELATION_QUOTE_DEDUPE=0` 可整體關閉（eval 用）。
- telemetry：inject 事件寫 `runtime/shadow/relation_quote.jsonl`（ts/session/query/quote_id/quote_text/bank/top_k/candidates/dedupe/daily_metric）；`note_quote_selected` 記本輪 quote 清單；`note_final_reply` 對最終交付文字算 8-gram Jaccard、4-gram containment、LCS，寫 `type=reply_overlap`；每輪 `begin_turn()` 清空。
`agents/agent_mod/chat_agent.py`：`_process_impl` 開頭 `begin_turn()`；回覆最終 `_final_reply` 後呼叫 `note_final_reply`（只 log，例外不影響回覆）。
`scripts/relation_quote_stats.py`：只讀統計（inject 數、dedupe 原因、每日分佈、reply 匹配／echo rate／平均 overlap／max LCS；沒有 live overlap 時依 session[:6] join trace 06_chat_agent 離線補算）；probe 分開計。
新 `tests/test_relation_quote.py` 15 checks；`scripts/run_all_tests.sh` 加 `relation_quote` 群組。

## 四、驗收數據（scratch `master2_scratch`，V2 env）
- 種子標籤修正：之前只有 4 條帶 `scope:relationship`，R4 承諾（7b69f03b）帶 `scope:promise` 導致「不來」問題檢索不到陪伴承諾 → 改成所有非 owner_fact 種子同時帶 `scope:relationship,scope:<kind>`；重建後 9 條關係種子全可召回。
- 兩隊獨立 held-out（9 正／6 中／4 負）：
  * rank2=0（舊）：pos 7/9（「你怎麼看我」召回月色、「不來」召回邊界句）；
  * rank2=1（新）：pos **8/9**，唯一「不來」實際注入 R4「一直在身邊」但 expectation 字串是「陪」，屬 expectation 匹配差異而非召回錯；中性 0、負向 0、avg 83–86ms。
- 60 題 dev：**19–20/20、中性 0、負向 0、avg 76–80ms**（「你看我」那題與 held-out 同類，排序後召回容器／價值句）。
- telemetry smoke：今日 shadow 4 筆 inject（dedupe：new 1／reask 3）、reply_matched 0（皆為直接 memory 呼叫、非 chat 回合，符合預期）；stats 腳本輸出正常。
- 回滾 env：`MEMORY_RECALL_V2=0`（回 legacy）、`MEMORY_RELATION_QUOTE_RANK2=0`（只關排序）、`MEMORY_RELATION_QUOTE_DEDUPE=0`（只關 dedupe）。

## 五、殘留
- 「不來」的 expectation 差異（陪 vs 身邊）不改種子原文，報告中標註。
- 尚未做：50 回合關係自然度盲評、A/A 噪聲、真實 chat 回合的 reply_overlap 分佈（需要 V2 開在一個測試 session 或 replay harness）；per-quote dedupe 的「echo 後才 dedupe」條件尚未實現（目前以重問 bypass 近似）。
- master2 切換仍等主人看數據後簽字；master 寫入仍凍結、archive 召回關。

## 六、telemetry smoke 與回歸
- smoke（temp shadow，V2 開）：`note_quote_selected` → inject 事件（quote_id=m1）；`note_final_reply` 近乎同句回覆 → jaccard8 0.125／contain4 0.304／LCS 10；無關回覆 → 0／0／1。符合「短中文看 contain4／LCS，Jaccard 只作輔助」。
- `/shadow` 頁已登記 `relation_quote.jsonl`（scripts/shadow_page.py KNOWN＋runtime/shadow/README.md），shadow_page 15 checks 0 failed。
- 全量回歸 `/tmp/full_reg_20261118e.log`：**163 組 0 FAIL**。
