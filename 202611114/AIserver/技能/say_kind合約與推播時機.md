# say_kind 合約與推播時機

**這是什麼**：讓她自己標記想說的話是 share/ask/invite/care/progress/none，progress 不推播；配合通道時機（同 thread 冷卻、連續未回應轉 digest、after_turn 每回合 ≤1）。

**前置條件**：
- shadow 3 天：`say_decisions.jsonl` 累積＋每日抽 20 條人工標註（誤殺率<15%、漏放率<10%）。
- 主人確認「對我有關的」判定邊界。

**目前狀態**：D0/D1 已上（progress 硬排除、ask 放行對主人問句、after_turn 節奏、gates 全記錄）；合約與時機層未上。

**解鎖判定**：shadow 3 天數據＋主人驗收看板。
