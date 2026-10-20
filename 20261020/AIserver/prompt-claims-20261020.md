# 映月 · 提示詞問題清單（669 行）逐條驗證報告

- 日期：2026-10-20
- 驗證人：bug-triage（映月項目 bug 處理專員）
- 待驗清單：`/data/ai/import/提示詞問題.txt`（669 行，AI 總結的 P0-1..P0-20 / P1-1..P1-37 / P2-1..P2-15 + 死代碼 + prompt 目錄方案）
- 機器可讀結果：`/data/ai/logs/bugs/prompt-claims.json`（81 條：清單 72 條 + 新發現 9 條）
- 原則：清單只是假設，全部用「源碼 + 函數級絕對邊界測試 + 真實 11435 端到端 + 歷史日誌」驗證；本次**未修改任何 prompt / 主鏈代碼 / 服務**，修法僅為建議。

## 0. 一頁摘要（給主人）

**先修這三個（真問題）：**
1. **P0-10｜research_agent 工具越權**：prompt 說「只能上網」，但執行器不檢查 agent 白名單，模型輸出 `<call>bash ...</call>` / `read_file` 會真的執行（已實測繞過成功）。修法：`execute_tool_calls` 加 `allowed` 參數，不改流程。
2. **X-2｜誠實閘門有覆蓋漏洞（新發現，P0）**：`honesty._CLAIM` 只認部分簡體句式。「我已經幫你把提醒設置好了」「我幫你訂了明天的機票」→ 判定為「沒有動作聲明」→ **直接放行，0 次校驗**。繁體主人常用句式正好漏網。
3. **P2-8｜honesty 校驗失敗默認放行（fail-open）**：模型輸出非 JSON / 超時 → 返回「检查异常，默认放行」；已實測。修法改 fail-safe + log。

**不要再花時間修的（清單判斷錯了）：**
- P0-1 / P0-2 / P0-3 / P0-4 / P0-6：「輸入含 `{}` 導致 `.format()` / f-string KeyError」**不成立**——Python 的 format/f-string 不會重新解析被代入的值；5 條全部 REFUTED，實測 22 項全 PASS。日誌也從未出現相關錯誤。
- P0-18：「_CLAIM 誤殺未來時承諾」是**刻意設計**（honesty.py:27 註釋），實測改寫行為合理。
- P2-5 / P2-6：「split(':') 截斷冒號後內容」不成立，代碼是 `split(':', 1)`。

**統計**：81 條 → CONFIRMED 56、PARTIAL 12、REFUTED 8、NEEDS-RUNTIME-TEST 5；嚴重度 P0 2、P1 15、P2 56、N/A 8。

## 1. 方法與證據來源

| 類型 | 內容 | 位置 |
| --- | --- | --- |
| 函數級絕對邊界測試 | 22 個 case（格式注入/parser/schema/fusion/CLAIM/模板/JSON fallback） | `/data/ai/.dsh_scratch/bug/boundary_tests.py`、`boundary_results.json` |
| 誠實閘門專項 | _CLAIM 覆蓋、fail-open、enforce 直調 | `/data/ai/.dsh_scratch/bug/verify2.py`、`verify2_results.json` |
| 運行時批次 1（真模型） | hallucination/judge/persona/vision/summarize/whitelist | `/data/ai/.dsh_scratch/bug/runtime1.py`、`runtime1_results.json` |
| 運行時批次 2 | judge 證據、persona 深夜、enforce、工具 E2E、白名單繞過 | `/data/ai/.dsh_scratch/bug/runtime2.py`、`runtime2_results.json` |
| 運行時批次 3 | q3 引用保留、parse fallback、research E2E | `/data/ai/.dsh_scratch/bug/runtime3.py`、`runtime3_results.json` |
| 運行時批次 4 | read_file 大代碼 E2E（P0-20） | `/data/ai/.dsh_scratch/bug/runtime4.py`、`runtime4_results.json` |
| 端到端（真實入口 11435） | 含 `{}`/JSON/代碼塊/敵意輸入、研究、任務、read_file | 各 runtime 腳本 C/P1/U1/X1/W1 段 |
| 歷史日誌 | `agents/logs/middleware/middleware-2026-10-*.log`、`trace/` | memory_guard_rewrite、need_search_hint、fake_cite_stripped、persona_tone 等 |
| 死代碼 | 全庫 grep + 調用鏈核對 | 見 §7 |

重要：所有端到端測試都經 `/v1/chat/completions`（:11435）真實管道，未重啟/未改動任何服務。

## 2. 重大方法論更正（為什麼 5 條 P0 是假警報）

清單作者把「用戶輸入含 `{}`」與「模板轉義」混為一談。實際 Python 行為：

```python
"{a}".format(a="{b}")   # -> "{b}"  ，代入值不會被二次解析
f"{x}" if False else None  # f-string 同理：表達式結果原樣插入
"{code}".format()       # -> KeyError: 'code'，只有模板自身未轉義的 { 才會炸
```

- `_check_hallucination`（chat_agent.py:89-105）、`_judge_with_tools`（honesty.py:105）、`_synth`（vision.py:150+）的模板佔位符都正確、JSON 示例也都用 `{{...}}` 轉義；
- `astra_execute_step` / `task_agent` / `learning_agent` / `writing_agent` 用 f-string，表達式值含多少大括號都安全；
- `chat_owner_v5.md` 掃描後只有 `{now_text}` `{emotion}` 兩個佔位符，`tpl.format` 正常（T02/T20）；
- 全量日誌 grep `chat_prompt_template_error|synth_failed|检查异常|默认放行` → **0 命中**。

真正的外部模板風險防禦建議：外部模板（md 文件）保持 `{{ }}` 轉義紀律；代入值不需要任何轉義。這類「防禦性重寫」可以不做。

## 3. P0 問題逐條結論

| ID | 結論 | 嚴重度 | 一句話 |
| --- | --- | --- | --- |
| P0-1 | REFUTED | N/A | format 代入值含 {} 不炸（T01/T03） |
| P0-2 | REFUTED | N/A | 同上（T04） |
| P0-3 | REFUTED | N/A | f-string 不安全前提錯誤（T05） |
| P0-4 | REFUTED | N/A | 模板只有 2 個佔位符（T02/T20） |
| P0-5 | PARTIAL | P2 | 注入不成立；但 executor 把 agent 異常包成「（xxx 执行报错：e）」當正常結果 |
| P0-6 | REFUTED | N/A | 注入不成立；vision 真問題是貪婪 JSON 提取（P2-2） |
| P0-7 | PARTIAL | P2 | replace 順序 bug 屬實，但 STICKER_MOOD_LLM 默認關閉、觸發需字面 {reply} |
| P0-8 | CONFIRMED | P2 | dsr1 示例全 `<call>`；死代碼，勿重啟 |
| P0-9 | NEEDS-RUNTIME-TEST | P1 | _rewrite 用 q3_polish 做「否認動作」，單樣本可用，語義穩定性待 20+ 樣本 |
| **P0-10** | **CONFIRMED** | **P0** | **research 可調 bash/read_file，執行器無白名單（已實測）** |
| P0-11 | CONFIRMED | P1 | write_file.path required=True vs 示例無 path vs 執行器默認 notes/ |
| P0-12 | CONFIRMED | P2 | learning spawn 示例字面 X 會被照抄 |
| P0-13 | PARTIAL | P2 | persona.txt「從不軟化」vs persona.py 深夜保留，屬刻意設計但文檔矛盾 |
| P0-14 | CONFIRMED | P1 | confidence<0.5「追问」與 memory_guard 改寫衝突（日誌有實例） |
| P0-15 | PARTIAL | P2 | chat 模板已有誠實條款；缺「代碼塊完整保留」 |
| P0-16 | CONFIRMED | P2 | learning/writing 無完整性/長度約束，8192 截斷無標記 |
| P0-17 | NEEDS-RUNTIME-TEST | P2 | 超步數後 q3 可能編造，需構造耗盡查詢實測 |
| P0-18 | REFUTED | N/A | 未來時攔截是設計（honesty.py:27），實測改寫合理 |
| P0-19 | PARTIAL | P2 | persona.txt 無誠實條款，但 q3 另有 11 條鐵律兜底 |
| P0-20 | CONFIRMED | P1 | 代碼被摘要成 100 字、read_file 靜默丟原文（E2E 已重現） |

## 4. P1 問題結論（37 條）

| ID | 結論 | 嚴重度 | 一句話 |
| --- | --- | --- | --- |
| P1-1 | CONFIRMED | P2 | dsr1 示例全 <call>（同 P0-8，死代碼） |
| P1-2 | CONFIRMED | P2 | learning spawn X（同 P0-12） |
| P1-3 | CONFIRMED | P1 | SHORT_TOOL_PROMPT 無 path（同 P0-11） |
| P1-4 | CONFIRMED | P2 | q2a 訂機票示例不完整；q2a 死代碼 |
| P1-5 | PARTIAL | P2 | 鐵律 11 未列 [[CITEn]]；單樣本實測 token 被保留 |
| P1-6 | NEEDS-RUNTIME-TEST | P2 | ASTRA「讀檔並總結」scope 邊界模糊 |
| P1-7 | CONFIRMED | P2 | writing_agent 收 history 但完全不用（續寫小說斷檔） |
| P1-8 | CONFIRMED | P2 | decompose ~3500 字全量重傳，無 cache |
| P1-9 | CONFIRMED | P2 | q2a ~2000 字（死代碼） |
| P1-10 | CONFIRMED | P2 | q2b_verify ~1800 字（死代碼） |
| P1-11 | CONFIRMED | P2 | astra plan ~2200 字 |
| P1-12 | CONFIRMED | P2 | research ~1500 字 |
| P1-13 | CONFIRMED | P2 | q3 每次讀 persona.txt+拼 11 鐵律 |
| P1-14 | CONFIRMED | P2 | chat 模板外置後又拼「業務鐵律/引用規則」 |
| P1-15 | CONFIRMED | P2 | judge 證據截斷 900 字 |
| P1-16 | CONFIRMED | P2 | persona.txt 5 行 vs chat_owner_v5 33 行 |
| P1-17 | CONFIRMED | P2 | chat_system_v2/v3/v4 無引用（死資產） |
| P1-18 | CONFIRMED | P2 | prompts.py 舊人格死代碼 |
| P1-19 | CONFIRMED | P2 | q3 內聯人格是第三套 |
| P1-20 | CONFIRMED | P2 | learning「傲嬌但專業」且「不要回我做不到」與 chat「做不到就直說」矛盾 |
| P1-21 | CONFIRMED | P2 | writing 第一人稱、無映月人格 |
| P1-22 | CONFIRMED | P2 | research 無人格、<reply> 早退直接返回 |
| P1-23 | CONFIRMED | P2 | task 經 Q3，有人格（正常） |
| P1-24 | CONFIRMED | P2 | work 返回 __TASK_CREATED__，無人格 |
| P1-25 | CONFIRMED | P2 | chat 模板降級版與 v5 細節不同 |
| P1-26 | PARTIAL | P2 | need_search 實際 2 處（q2a 已死），非三處 |
| P1-27 | CONFIRMED | P2 | _check_hallucination + honesty 雙重守門 |
| P1-28 | CONFIRMED | P2 | 引用保護三段；E2E 出現重複引用標記 |
| P1-29 | CONFIRMED | P2 | 人格注入三層互相抵消 |
| P1-30 | PARTIAL | P2 | CHAT 判斷語義隱式但等效，未見實際 bug |
| **P1-31** | **CONFIRMED** | **P1** | **fusion and/sequence 永遠不命中（T13：兩者都落 fused）** |
| P1-32 | CONFIRMED | P2 | skills.note_for_names 每次重建映射（量小） |
| P1-33 | NEEDS-RUNTIME-TEST | P1 | 同 P0-9 |
| P1-34 | CONFIRMED | P2 | spawn 語義=插隊首，非「暫停回注」；research 的 spawn 不可達 |
| P1-35 | CONFIRMED | P2 | q2b retry 無消費方（q2b 死代碼） |
| P1-36 | CONFIRMED | P1 | 同 P0-14 |
| P1-37 | PARTIAL | P2 | 同 P1-5 + 引用拼裝衝突 |

## 5. P2 問題結論（15 條）

| ID | 結論 | 嚴重度 | 一句話 |
| --- | --- | --- | --- |
| **P2-1** | **CONFIRMED** | **P1** | **decompose JSON 失敗靜默 fallback 成 chat，無 log/重試（T16 + E2E）** |
| P2-2 | CONFIRMED | P2 | vision 貪婪 `\{.*\}` 提取，雜訊即 fallback |
| P2-3 | PARTIAL | P2 | 目標函數搞錯（q2b 有 re.sub 去 fence）；dsr1/decompose/q2a 才用朴素 strip |
| P2-4 | CONFIRMED | P2 | q3 缺標籤時原文返回/空回覆，無告警 |
| P2-5 | REFUTED | N/A | 實際 split(':',1)，不截斷（T19） |
| P2-6 | REFUTED | N/A | 同 P2-5 |
| P2-7 | PARTIAL | P2 | parser 只認 <call>；learning spawn 可用，research 不可達（X-1） |
| **P2-8** | **CONFIRMED** | **P1** | **honesty JSON 解析失敗默認放行（實測）** |
| P2-9 | PARTIAL | P2 | 多行/冒號通過；極端嵌套未測 |
| P2-10 | CONFIRMED | P2 | prompts.py 死代碼 |
| P2-11 | CONFIRMED | P2 | q2a/q2b 死代碼（僅測試引用） |
| P2-12 | CONFIRMED | P2 | dsr1_classify 死代碼 |
| P2-13 | CONFIRMED | P2 | SOFT_ENDINGS/_ending_for 死代碼 |
| P2-14 | CONFIRMED | P2 | format_tool_plan 死代碼 |
| P2-15 | CONFIRMED | P2 | SIMPLE_TOOLS 死代碼（僅 import） |

## 6. 新發現（不在原清單，均已驗證）

| ID | 嚴重度 | 問題 | 證據 |
| --- | --- | --- | --- |
| **X-2** | **P0** | honesty._CLAIM 繁中/把字句漏檢：「我已經幫你把提醒設置好了」→ has_claim=False → enforce 直接 pass（0 次 LLM） | verify2 A/B；honesty.py:28-45 |
| X-1 | P1 | research_agent 的 `<spawn>` 回流不可達：含 `<reply>` 提前 return、無 reply 走 Q3 丟標籤 | research_agent.py:87-92,96-99；T17 |
| X-3 | P1 | 管道異常直接把 `[管道错误] {e}` 原文回給主人 | dshmid.py:570/606 |
| X-5 | P1（NRT） | judge 對「有證據但語義不支持」的假聲明可能放行（單樣本） | runtime2 J1 |
| X-6 | P1 | decompose prompt 自相矛盾：241 行「agent 目前只有 chat 和 task」vs 247 行列 5 種 agent | diaoyongai.py:241,247 |
| X-8 | P1 | read_file 代碼摘要 + 回覆聲稱「已成功讀取」通過 honesty | runtime4 C；與 P0-20 同根 |
| X-4 | P2 | 最終引用重複「[1][2] [1][2]」+ 雙重「**參考來源**」 | runtime3 X1 |
| X-7 | P2 | approval 審批死路：create_plan 只被測試調用，主人回「確認」不會執行 | pending_manager.py:57；safety.py:164-199 |
| X-9 | P2 | STICKER_MOOD_LLM 默認 0 → _llm_mood 默認不可達 | stickers.py USE_LLM |

## 7. REFUTED 清單（避免白修）

| ID | 原假設 | 實測結論 |
| --- | --- | --- |
| P0-1 | 用戶輸入含 {} → _check_hallucination KeyError | format 代入值不解析；T03 正常返回 |
| P0-2 | 證據/回覆含 {} → judge 失效 | T04 正常判斷（False 攔截） |
| P0-3 | plan_md 含 {} → astra f-string 崩潰 | T05 正常返回 |
| P0-4 | chat_owner_v5.md 含未轉義 {} → 降級 | 模板只有 2 個佔位符，T02 成功 |
| P0-6 | OCR/問題含 {} → vision fallback | T09 正常；真脆弱點是模型回覆非 JSON（P2-2） |
| P0-18 | _CLAIM 誤殺未來承諾 | 設計如此（honesty.py:27），L1 改寫合理 |
| P2-5 | _parse_task_block split(':') 截斷 | 實際 split(':',1)，T19 完整保留 |
| P2-6 | _parse_step_block 同上 | 同一實現，REFUTED |

部分失效（不要按原描述修）：P0-5（f-string 部分）、P1-5/P1-37（[[CITEn]] 實測被保留）、P2-3（函數指錯）、P1-26（三處→兩處）、P1-30（CHAT 等效）。

## 8. NEEDS-RUNTIME-TEST（建議 Lead 安排）

| ID | 要測什麼 | 建議方法 | 預期成本 |
| --- | --- | --- | --- |
| P0-9 / P1-33 | _rewrite 用 q3_polish 改寫假聲明的語義穩定性 | 20 條不同 claim（繁簡/把字句/未來時）× enforce，統計「仍保留已完成聲明」比例 | ~20 次 q3 調用（可夜間跑） |
| P0-17 | research 超步數是否編造 | 構造冷門實體查詢（如「2026 年某小眾獎項得主」）逼 max_steps 耗盡，檢查回覆是否如實說未找到 | 3-5 次 research |
| P1-6 | ASTRA「讀檔並總結」scope 判斷 | work 大任務：「讀 persona.txt 並總結要點」→ 看 plan 是否拆出總結 step | 1-2 次 astra |
| X-5 | judge 對「有證據但不相關」的誤放率 | 20 組（真證據/無關證據/無證據）×（真/假 claim） | ~20 次 3B |
| X-6 | decompose 路由是否被 241 行壓抑 | 10 條需 research/writing/learning 的問題，統計 agent 分佈 | 10 次 decompose |

## 9. 死代碼確認（grep 全庫 + 調用鏈）

| 對象 | 定義位置 | 引用情況 | 結論 |
| --- | --- | --- | --- |
| dsr1_classify | diaoyongai.py:135 | 僅 agents/test | 死代碼，可刪 |
| qwen_q2a_decide | diaoyongai.py:311 | 僅測試 | 死代碼 |
| qwen_q2b_draft | diaoyongai.py:414 | 僅測試 | 死代碼 |
| qwen_q2b_verify | diaoyongai.py:469 | 僅測試 | 死代碼 |
| prompts.py PROMPT_STEP1/2/SUMMARY | agent_mod/prompts.py:5,35,59 | 全庫無 import | 死代碼 |
| persona.SOFT_ENDINGS / _ending_for | persona.py:32-41,85-87 | 無調用 | 死代碼（polish 用硬編碼 line139） |
| formatter.format_tool_plan | formatter.py:9 | __init__ 導出 + diaoyongai import，無調用 | 死代碼 |
| SIMPLE_TOOLS | schema.py:76 | diaoyongai:12 import 未使用 | 死代碼 |
| task_runner.verify_step | task_runner.py:76 | 無調用 | 死代碼（清單外證實） |
| pending_manager.create_plan | pending_manager.py:57 | 僅 __main__ 測試 | 死代碼 → 審批無出口（X-7） |
| safety.get_approval / set_approval_status | safety.py:189,198 | 無生產調用 | 死代碼 |
| chat_system_v2/v3/v4.md | prompts/persona/ | 無 .py 引用 | 死資產，可歸檔 |

註：死代碼清理屬 P2，建議一次 PR 集中刪除並同步改 agents/test。

## 10. 修法優先級建議（供 Lead/主人拍板）

**立即（P0，安全/誠實）**
1. `tool_runner.execute_tool_calls` 加 `allowed` 白名單：research 只給 `{web_search, web_fetch}`，越權回「被拒絕」；不改 agent 流程。（P0-10）
2. `honesty._CLAIM` 補全繁中/把字句/「幫你+V+了」句式，並先把 20+ 正反例寫進 tests 再改正則。（X-2）
3. `honesty._judge_with_tools` / `_check_hallucination` 解析失敗改 fail-safe（先走規則判斷），加 log。（P2-8）

**本週（P1）**
4. decompose JSON 失敗：log + 補救重試一次（「只輸出 JSON」），仍失敗才 fallback 並標記。（P2-1）
5. `collector.choose_fusion` 加 `and→fused / sequence→separate` 映射。（P1-31）
6. write_file 的 schema 描述與示例統一（建議 path 可選 + prompt 明示默認路徑）。（P0-11）
7. memory_guard 只改寫「記憶充足卻還問」的任務，不碰指代不明追問。（P0-14）
8. read_file 對 code/配置類不摘要（或摘要附原文 fence + 截斷標記）。（P0-20/X-8）
9. 管道 except 改固定兜底句，traceback 只進日誌。（X-3）
10. research_agent 返回時保留 `<spawn>` 或刪除 prompt 中不可達承諾。（X-1）

**Backlog（P2）**
- 死代碼集中刪除；prompt 外置 `/data/ai/prompts/` + lru_cache（清單方案大方向正確，但不急）；
- 人格統一（persona.txt 為底色，q3 鐵律只留輸出契約）；引用保護/拼裝收斂；P0-7 替換順序；P1-7 writing 接 history。

**明確不建議做的**
- 不因 P0-1..P0-4/P0-6 做「防注入」重寫（前提錯誤，徒增複雜度）。
- 不要為「像 Agent」重構頂層循環、global_replan 閉環、spawn 回注——與陪伴型助理「工作流穩定 + 局部 Agent」的設計意圖一致，屬新功能而非 bug。
- 不動 prompt 文案本身（本次只出建議；prompt 凍結由 Lead/主人決定解凍）。

## 11. 附錄

- 機器可讀：`/data/ai/logs/bugs/prompt-claims.json`（81 條，含 evidence/fix_suggestion）
- 測試腳本：`/data/ai/.dsh_scratch/bug/`（boundary_tests.py、runtime1-4.py、verify2.py、results json、test_bugreport.sh）
- 日誌證據：`/data/ai/agents/logs/middleware/middleware-2026-10-19.log`（memory_guard_rewrite、need_search_hint、fake_cite_stripped、persona_tone、honesty_block）；`middleware-2026-10-16/17.log`（歷史管道錯誤原文）
- bug 上報入口：`/data/ai/docs/bugs/inbox/README.md`；CLI：`/data/ai/scripts/bug_report.py`；資料：`/data/ai/runtime/bugs.jsonl`
- 本次未改動任何 prompt / 主鏈代碼 / 服務進程；所有修改僅限 docs/logs/scripts/.dsh_scratch。
