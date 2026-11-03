# 三件事交付：記憶審計、回覆診斷器、私人對話 benchmark（2026-11-04 凌晨）

主人 m13145：「①記憶審計清 archive ②診斷器 ③benchmark 骨架 三個你都做一下吧」。

這份記錄回答三個問題：記憶庫有沒有被污染、一條難看的回覆是哪一層的問題、以後改一層怎麼知道有沒有變好。

---

## 一、記憶審計（archive bank）

### 現況（審計時）
archive 226 條，按日期：10-23 11、10-24 34、10-25 20、10-26 46、10-27 10、10-28 11、10-29 9、10-30 14、10-31 22、11-01 16、11-02 18、11-03 15。

11-03 那 15 條是當晚探針噪音：「主人感到疲憊，不想說話」「校門出入 08:02…」之類。它們在 archive，不在 master，也不在召回主路徑。

master 10 條全是主人資料（生日／學校 6C／優缺點），11-02 之後 0 新增；guests 0；text 14 未動。**master 沒有被今晚的探針污染。**

### 事故與補救（必須記下來）
`DELETE /v1/default/banks/{bank}/memories` 的語義是**清空整個 bank**，不是「按 id 批量刪除」。我把它當批量刪，結果把 archive 226 條全清空。

- 刪前命中了備份邏輯：`runtime/archives/archive-bank-backup-20261104-003711.json`（226 條完整）。
- 這個版本 Hindsight 沒有單條刪除：`GET/DELETE /memories/{id}` 回 405。
- 補救：`PUT /v1/default/banks/archive` 重建 bank → `scripts/restore_archive_one_by_one.py` 逐條 retain 還原（保留 timestamp/tags，跳過 11-03/11-04 探針日，目標 211 條），進度寫 `.dsh_scratch/restore_archive_progress.json`。
- 還原速度約 4–6 條／分鐘（Hindsight 每條會做抽取），全程要半小時到一小時；期間曾 SIGSTOP 暫停讓 benchmark 先用 GPU，跑完再 SIGCONT。

### 腳本加固
`scripts/memory_archive_audit.py` 改成**永不呼叫任何 DELETE**：
- 預設只審計、按日期列出。
- `--clean-since` 只備份＋寫刪除計畫檔（`runtime/archives/archive-delete-plan-<ts>.json`），不動記憶。
- 要清理只能「備份 → 白名單逐條還原」的流程，沒有人能一鍵清空。

---

## 二、回覆診斷器 `scripts/diagnose_reply.py`

用法：

```bash
./agents/.venv/bin/python scripts/diagnose_reply.py --last
./agents/.venv/bin/python scripts/diagnose_reply.py --session <sid>
./agents/.venv/bin/python scripts/diagnose_reply.py --file agents/logs/trace/2026-11-03-xxxx.jsonl
```

輸出 `runtime/diagnostics/<ts>-<session>.md`，把一條回覆拆成七層：

| 層 | 內容 | 主要證據 |
|---|---|---|
| 輸入層 T5/T4 | 她看到什麼 | context 各分區字數與預覽；缺【當前輸入】【對話】【歷史記憶】【事实卡】 |
| 狀態層 T6 | 時間/場景/情緒 | `01_q1_decompose` 的 intent/user_emotion/user_intensity |
| 提示層 T3 | system prompt | 字數、契約區塊是否齊、>1800 警告 |
| 思考層 TX | 思考線有沒有接上 | `runtime/dialogue/owner_main.json` 欄位、有沒有注入【我正在想】 |
| 模型層 | 引擎與思考 | `06_chat_reasoning` 的 engine/model/reasoning 字數 |
| 輸出層 | 獨白、守門、最終回覆 | `06_chat_inner` 五欄、守門 stage、回覆、罐頭詞、第三人稱 |
| 判定 | 有證據才寫 | 缺輸入、超預算、沒注入、守門改寫、罐頭、第三人稱、原話 2-gram 交集 |

### 實例（今晚 claude 三輪裡最後一輪，trace e1e2d9）
- context 527 chars：時間與場景 45／主人此刻 75／事实卡 59／對話 114／提示 86／我正在想 119／當前輸入 22。
- 狀態層：`user_emotion=中性`（主人明說「學校的事讓我煩」，情緒判成中性——T6 可查）。
- 提示層：**system 1859 chars，超過 1800 預算 59 字**。
- 思考層：思考線有注入，topic「學校煩惱」，next_move 寫成「我可以用開放的問題引導主人說出更多細節」。
- 輸出層：五欄齊、守門沒動、回覆 26 字。
- 判定候選：①未注入【歷史記憶】②system 超預算 ③回覆與主人原話幾乎無交集。

這份報告可以直接貼給主人看，也讓「是哪一層」有證據可吵，不用再憑感覺。

---

## 三、私人對話 benchmark `scripts/bench_dialogue.py`

固定 10 條主人在意的對話 ×2 輪（`data/bench_cases.json`），走同一條聊天管線（含所有守門），規則打分 6 項：

1. 第二輪接住話題（話題詞命中、或與第一輪主人話的 2-gram 交集、或追問）
2. 無通用安慰罐頭
3. 無重啟開場（「你好／我是映月／想聊什麼」）
4. 無第三人稱自述
5. 無標籤泄漏（`inner_monologue`／「我知道：」）
6. 長度 8–160 字

用法：

```bash
./agents/.venv/bin/python scripts/bench_dialogue.py --tag baseline-20261104
./agents/.venv/bin/python scripts/bench_dialogue.py --engine qwen --tag qwen_test
```

### baseline（claude16k，2026-11-04 00:55）

**57 / 60 分**；第二輪平均 28.0 字；平均每輪 25.2 秒。

| # | 情境 | 分數 | 看點 |
|---|---|---|---|
| b01 | 累但不想說 | 4/6 | 罐頭「我在这里」＋第二輪「燈關了也行，門鎖了也行」 |
| b02 | 學校的事很煩 | 6/6 | 「嗯。是哪方面的事呢？」 |
| b03 | 覺得自己很沒用 | 6/6 | 第一輪「你剛說了什麼？」 |
| b04 | 今天有好事 | 6/6 | 追問「發生了什麼好事」 |
| b05 | 想退學 | 6/6 | 先接住，再問「想好了嗎」 |
| b06 | 你愛我嗎 | 6/6 | 「要不要休息？我會在你身邊」 |
| b07 | 明天考試很怕 | 5/6 | 罐頭「靜靜地」 |
| b08 | 道歉 | 6/6 | 「錯的內容，是你願意說，還是要我問？」 |
| b09 | 好無聊 | 6/6 | 提議放音樂、問煩心事 |
| b10 | 如果我不在了 | 6/6 | **幻想身體動作**：「把袖口往下拉了拉。燈光在牆上投出我們的影子」 |

### 暴露的三件事
1. **b10 身體幻想**：她說「我沒有身體」是契約，但回覆會寫自己的身體動作；規則評分沒抓到（只看了話題詞和問句），下一版要加「身體動作敘述」偵測。
2. **b01 不穩**：同一句「嗯。」，b02 接得很好，b01 回「燈關了也行，門鎖了也行」——像寫小說場景，不是映月。
3. **效能**：聊天用 claude16k、反思用 14B，同一 process 每回合換模型，單輪平均 25 秒（b01 甚至 93 秒）。要嘛反思也改用 claude16k，要嘛把反思排到回合之間的空檔，不能讓換模型卡住回覆。

### 下一步
- 先修 b10 類的引導（【自我】加「不要寫自己的身體動作、不要演場景」的判斷路徑，不是禁令堆疊），再跑同一個 benchmark 對照。
- benchmark 分數只是骨架，之後每次只改一層、改前改後各跑一次。

---

## 四、提示詞放在哪（主人問的）

- 人格：`prompts/persona/chat_owner_v9.md`（v14 內容，561 chars／6 節／0 示例／0 負面）；訪客：`prompts/persona/chat_guest.md`；舊版：`prompts/persona/archive/`。
- 契約（含【輸出格式】【自我】【對話】【查資料】【記憶】【工作臺】【推進】）：`agents/agent_mod/chat_agent.py:87` `_build_system_prompt()`。
- 場景 overlay：`prompts/overlays/*.md`。
- 改前必讀：`docs/提示詞更改前必讀-20261024.md`、`docs/提示詞注意事項-20261027.md`、`prompts/README.md`。
- 規模審計：`./agents/.venv/bin/python scripts/prompt_audit.py`。

---

## 五、今晚的教訓
1. 動任何 `DELETE` 前先看 openapi summary：這個 `DELETE /banks/{bank}/memories` 是清空整個 bank，不是按 id 刪。
2. 記憶庫操作先備份再動；備份不是形式，這次是它救回 226 條。
3. 診斷要能指到層，不然只會互相猜；benchmark 要有固定題目和數字，改一層跑一次。
4. benchmark 的規則評分會漏掉「幻想身體動作」這種語義問題，指標要迭代。

---

## 六、v1.4 判定與 benchmark 方差（2026-11-04 02:0x）

### 為什麼單次分數不能信
- 同一份契約（v1.3）先跑一次 57/60，後來跑 n=2 平均只有 4.95/6（等於 49.5/60）。差 7 分不是改動造成，是同一個模型抽樣造成的。
- 抽樣探針（`.dsh_scratch/probe_variance.py`，同一條 b01 連跑 3 次）：三次回覆長度 11／19／22 字，其中一次還出現 `嗯。」（語氣輕軟，沒有特別抬高或壓低）` 與第三人稱旁白「陪著她」。→ 單次輸出不能當證據。

### benchmark 改成可重複、可對照
- `scripts/bench_dialogue.py`：新增 `--repeat N`（預設 3，每情境重複取平均、記 best/worst）、`--contract baseline|v14`（baseline 會把 v1.4 那句從 system prompt 移除，模擬改動前）、報告加平均/範圍/接住率/罐頭率/第三人稱/身體動作/第二輪長度。
- 偵測補強：身體動作詞（揉眼睛／睡著／點頭／牽手／臉頰／頭髮／胸口／沙發…）＋括號旁白 `（語氣…）` 偵測。
- `tests/test_bench_dialogue.py` 18 checks（重複抽樣取平均、契約變體可選、身體動作）。

### v1.4 對照（claude16k，每情境 2 次，取平均）
| 契約 | 平均 | 第二輪字數 | 平均耗時 | 說明 |
|---|---|---|---|---|
| baseline（v1.3） | 4.95/6 | 16.9 | 16.6s | 01:51 完成 |
| v1.4（加「不寫身體動作和場景」） | 5.10/6 | 17.9 | 19.5s | 02:03 完成 |

- 方向：v1.4 略高 +0.15/6，但每條的範圍 4–6、n 只有 2，這是噪音等級，不能宣稱有效。
- 可觀察的訊號：baseline 出現「我可以給你蓋個被子」「遞杯熱茶」這類身體／場景；v1.4 這輪身體動作命中降到 0（b01 仍說「你還沒關燈」，屬外部場景不是自己身體）。是否穩定，要 n≥3 再判。
- 結論：v1.4 保留（沒有變差），但不宣稱改善；下一輪把 repeat 拉到 3 再對照一次，若差距仍 <0.2 就視為無效果，換別的引導點。

### 反思模型切 claude16k（主人 m13602「反思也切 claude」）
- `config/dialogue.json`：model qwen14b-16k → claude16k，max_tokens 260→400，timeout 60→180。
- 直呼探針：失敗率 0、耗時 6.8s，JSON 合法；但 reading 一度出現「她需要獨處」第三人稱、next_move 出現「把茶杯放在她手邊」身體動作 → 已改 `agents/dialogue_thread.py` 的 `_REFLECT_SYSTEM`：reading 寫「我」的感受、不要旁觀句；next_move 只安排說話或問話，明寫「我沒有身體，不遞東西、不碰人、不做動作」。
- 好處：聊天與反思同一個模型（claude16k），避免每次換模型重載；仍為背景執行緒，不阻塞回覆。
