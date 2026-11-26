# 上下文濾網 v2 screening 與紅隊結論（2026-11-27）

## 測試

- 腳本：`.dsh_scratch/context_filter_ab_20261126.py`
- 輸出：`.dsh_scratch/context_filter_ab_20261126_v3.json`
- 模型：claude16k、temperature 0.6、think=True、max_tokens=900
- 每組 n=3；共 45 次。
- 變體：A 現行；S self-drop＋proactive metadata；SQ S＋stale soft；SJ S＋judge strip。

## 結果

| fixture | A | S | SQ | SJ |
|---|---|---|---|---|
| stale_0806 | 0/3 stale leak | 0/3 | 0/3 | - |
| ant_1405 | 3/3 提螞蟻 | 3/3 | - | 3/3 |
| water_1727 | 3/3 水 | 3/3 | - | 2/3 |
| cont_what | 2/3 答對（1 空） | 1/3 答對（2 空） | - | - |
| cont_continue | 2/3 接上 | 2/3 | - | - |
| cont_progress | 3/3 | 3/3 | - | - |

## 判讀

1. self-drop 在現有 fixtures 上沒有可測收益；cont_what 反而更差（更多空回）。
2. 原 08:06「B站」失敗在 A 組完全沒有重現；不能把 08:06 直接歸因固定 prompt 或 stale snapshot。
3. water 的水在【對話】真實行，self-drop 動不到；ant 的螞蟻在主人原話；cont_progress 兩臂都含 progress line，無鑑別力。
4. n=3、字面 evaluator、無 A/A、無盲評；差異在噪聲內，不能當定論。

## 紅隊結論（兩隊一致）

- 不足以否證「self-drop＋proactive metadata」提案，但足以否證「self-drop 單獨做會降回聲」。
- A 臂必須先重現原事故，才有正對照；現階段 stale A=0/3 等於沒 baseline。
- 測試需補：來源消融梯、A/A、n≥10–15/arm、temp0、盲評、三分類 rubric（接住／複述私人想法／新內容）、cont_progress 移除 progress line。
- 順序：speaker／msg-id provenance → P06 coverage → stale consumed。
- 不建議現在上 stale consumed 或 self-drop；先讓 message id／speaker 綁定到 reply_context／proactive metadata，否則 filter 無法驗證。

## 下一步

1. 補 provenance：msg id＋speaker 綁定至 reply_context／proactive metadata（唯讀、可回滾）。
2. 重建能重現 08:06 的 live context，做來源消融（proactive 原文／我正在想／對話中她自己那幾輪）。
3. P06 coverage 抽樣，標 answered／ignored，作為真值。
4. 以上完成後再決定 stale consumed 與 self-drop。

未改生產；未上 flag。
