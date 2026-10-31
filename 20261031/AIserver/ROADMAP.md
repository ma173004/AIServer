# 映月路线图 v2（2026-10-18 重画）

> 背景：原《阶段.txt》是「工作流管道」时代的路线图。工作流已经改成 agent 架构，
> 原阶段 0-3（硬件/模型/中间层/工具/提示词）大部分已落地，阶段 4 之后的「功能」不变。
> 本图把「agent 骨架」和「功能/skills」拆成两条线，避免混在一起互相堵。

## 两条线

### A 线：记忆 / 上下文 / 质量（已见底，进入维护与优化）
| 里程碑 | 状态 | 证据 |
|---|---|---|
| M0 安全网：git 基线、RUNBOOK、测试脚手架 | ✅ | commit 26cd38e / 7b96929 / 55ac507；RUNBOOK/docs |
| M1 记忆闭环 v0：写/去重/召回合入库、提问不入库、立即记忆 | ✅ | memory 10/10；E2E 颜色、乐队均召回；master 干净事实 |
| M2 上下文：归档保留摘要、空闲 30 分钟归档可配、续聊 | ✅ | continuation 10/10；IDLE 缩放实测通过 |
| M3 记忆规范/观测 | ✅ 基础版 | /health memory stats、policies/memory_schema.json、memory-report.md |
| 3B 车道分工（深度记忆管理 / 子任务分担 / 备用） | ✅ 压测完成 | `docs/bench3b.md`；alias `yingyue-mem3b/sub3b/guard3b` 已建 |
| 长输入压缩（>6000 字压缩进管道，不再硬截断） | ✅ | 9127 字 E2E 30.1s 两问全对；14B 压缩 ≤3000 字 |
| Hindsight 抽取模型（14B → 3B 评估） | ✅ 结论：保留 14B | 3B 慢 4 倍且产出「颜色尚未确定」错误事实；详见 `docs/bench3b.md` |
| 截圖 4 句功能回歸（時間幻覺/假完成/代碼丟失/代碼在哪） | ✅ | 時間注入 + 禁假完成規則 + Q3/executor 代碼塊保護；回歸通過 |
| 记忆治理（重复/过期/坏事实清理） | 🚧 待做 | recall 端已去重，库内仍会累积 |

### B 线：agent 骨架（用户设计主导，代码我出；按阶段推进，不跳级）
| 阶段 | 内容 | 状态 |
|---|---|---|
| B1 总管+拆解器 | qwen_decompose（intent/情绪/subtasks/fusion/confidence）+ executor 队列 | ✅ 已有 |
| B2 七 Agent | ✅ 7/7（2026-10-25 深夜）：chat/task/research/writing/learning/work 已接；code agent 驗證可用（claude16k+model_swap，14 項回歸）；新增 generic 通用兜底（未知 agent 落 generic，11 項回歸） | ✅ |
| B3 收集器+融合 | ✅ 基础版（2026-10-18）：`agents/collector.py` 收集 item（agent/slot/status/chars/elapsed）；三策略 single/separate/fused/main_first；代码块保护；`/health` 无需改，`log_event("collection")` 可观测。**依賴調度 depends_on v0（2026-10-26 晚）**：`agents/task_deps.py` 正規化/失敗傳播/依賴提示；拆解器規則 14 產 `depends_on`；executor 依賴未完成則跳過（status=dep_failed）、完成則結果注入子任務；live 兩步任務驗證 sub[1].depends_on=[0] 且回覆用上前置結果（trace 2026-10-26-ee98db） | ✅ |
| B4 完成通知/中途确认 | ✅ 真流式（2026-10-18 晚）：dshmid 改真 SSE —— T+0 立即确认（0.0s）、拆解里程碑（「拆成 N 步」）、执行超过 8s 的步进里程碑、最终完整融合。非流式路径保留（测试用） | ✅ |
| B5 回流机制 | ✅ 基础版：`agents/reflux.py` 五类回流书（local_repair / sibling_notify / global_replan / reactive_exploration / cross_session_resume）统一 schema + 阈值分流 + `runtime/reflux/reflux.jsonl` 落盘；`<spawn>` 落 sibling_notify、重试耗尽落 local_repair、空结果按置信度分 global_replan/reactive、阶段 0 扫未完成任务落 cross_session（去重）。**自动执行重规划 v0（2026-10-26 深夜）**：失败/依赖失败后 `agents/replan.py` 用 14B 生成 ≤2 个新子任务（防重複/防迴圈/預算守門），executor 插入队首重跑并把失败项从融合输出摘除（不再漏工具报错原文）；**審批閉環 v1（2026-10-28）**：pending 書每會話提醒一次（`unseen_pending_books`）、Discord `!審批` / `!批准 <id>` / `!駁回 <id>`、核准後下輪注入「【已核准恢復】」並消費、駁回不執行並消費；15 項測試＋live trace 證據；清掉 7 本舊 E2E 殘留 pending | ✅ |
| B6 动态创建 | 五類動態創建 v0（info_gap/capability_gap/dependency/opportunity/conflict）＋2026-10-27 v1：chat 自主組合子任務/工具、結果回饋整合（一次閉環） | ✅ v1 |
| B7 预算/Checkpoint/打断恢复 | ✅ 最小版：`agents/budget.py`（调用次数/子任务数双预算，prompt 报剩余）+ `agents/checkpoint.py`（每步落盘任务图/位置/预算/结果，正常完成清空；超时中断后下一轮注入「上次未完成」提示）。**分層/幂等 v0（2026-10-26 深夜）**：全量快照按 session 分檔（`runtime/checkpoints/sessions/<sid>.json`，修多會話互蓋）＋append-only 增量事件 `events.jsonl`；工具冪等（write_file 600s / set_reminder 300s 同參數去重，重試不重複寫檔/設提醒）；**中斷恢復 live 驗收（2026-10-28）**：`checkpoint.save`→下一輪 05 注入「【上次未完成】」→`turn_done` 清除（middleware `checkpoint_resume_hint`/`checkpoint_cleared`）；`/health` 顯示 sessions/events | ✅ |
| B8 技能注册表 | ✅ 基础版：`agents/skills.py` L0 全量注入拆解器、L1 匹配后注入执行 prompt、L2（script/工具依赖）按需加载；`registry/skills/` 5 个样例技能（检索/代码/总结/提醒/翻译）；executor/dshmid 接入；/health 暴露技能清单 | ✅ |
| B9 后台任务回流 | ✅ 完成通知＋進度主動推送 v0（2026-10-26 深夜）：`agents/notify_center.py` 統一投遞——完成通知走 Discord 主動 DM，投遞成功後回清 `notify_pending`（主人下一輪不再重複告知；失敗才留 pending 由 `_inject_notify` 補報）；階段進度 `emit_progress` 節流後主動 DM（第 1/最後一步、每 3 步、同任務 ≥60s）；bot 投遞迴圈改走 `notify_center.ack`；18 項測試＋live 驗證 2 則 DM、pending 清空 | ✅ |
| B10 工作臺連續思考 | ✅ v0（2026-10-31 深夜）：`agents/self_memory.py` 工作臺自動回寫 API（`append_workbench_result/append_workbench_next`，去重/上限 8/空板不注入）；`agents/task_runner.py` 任務完成/失敗自動寫 `## 最近結果`；`chat_agent` 契約新增【推進】（已回答話題不重複問）；`tests/test_workbench_loop.py` 13 checks；live trace 05 注入【我的工作臺】+【推進】（context 472 字）。下一步：前景任務回寫與「正在進行」自動化 | ✅ v0 |

## 3B 车道（GPU 固定给 14B，3B 全在 CPU；压测数据见 `docs/bench3b.md`）
- **lane1 深度记忆**：`yingyue-mem3b`（q5_k_m，8192 ctx，4 线程，temp0.2）→ 会话摘要（默认）、Hindsight consolidation、记忆治理。
- **lane2 子任务分担**：`yingyue-sub3b`（Q4_K_M，4096 ctx，4 线程，temp0.1）→ 简单子任务、格式化、轻判断（B 线接入）。
- **lane3 备用**：`yingyue-guard3b`（q5_k_m，4096 ctx，temp0）→ 反幻觉守门替换候选；主动信息阶段再上。
- 结论：**长提示活（抽取/长输入压缩）必须 14B GPU**（prompt eval 快 25–45 倍）；3B 适合短提示的判断/守门/摘要/巩固。
- 目标：14B 不因抽取/巩固被抢占；CPU 12 核吃满但留 4 核给系统与 14B 前置。

## 功能阶段（原 阶段 5-12，B 线落地后按序推进）
5. 信息采集（简版）：时间 ✅ / 地点 🚧 / 心情 🚧 → 基础状态卡
6. 状态工程（简易）：用户状态、Agent 状态、输出调节
7. 主动消息：触发判断 / 调度 / 模板 / 语气一致
   - 🚧 前置（2026-10-28，shadow）：`agents/event_frame.py` 采集事件统一信封（`runtime/events/external_events.jsonl`，dedupe/cooldown）＋`agents/proactive_trigger.py` 五条触发判断（下雨/放学未到家/自身状态分享/任务完成/提醒到期），只写 `runtime/proactive/shadow_proposals.jsonl` 与 middleware，不发送；待 shadow 观察后开 live。
8. 对外接口：Discord / 微信 / QQ 择一
9. 状态工程（全）
10. 记忆改良：海量输入、跨平台关联、压缩摘要、检索优化
11. 信息采集（全）
12. 对外主动信息（主动社交）

## 当前施工顺序（本周 → 下周）
1. ~~3B 压测 → 定 lane 模型~~ ✅ 已完成（`docs/bench3b.md`）。
2. ~~长输入压缩 E2E~~ ✅ 已完成（9127 字 / 30.1s / 两问全对）。
3. ~~Hindsight 抽取 3B 质量实验~~ ✅ 结论：保留 14B。
4. 回归：`bash scripts/run_all_tests.sh` + 截圖四句人工验收（每轮改动后必跑）。
5. ~~B3 收集器+融合、B4 完成通知最小版、B5 spawn/repair 收进收集器~~ ✅ 已完成（2026-10-18 晚：`agents/collector.py` + `task_manager.pop_notify` + `dshmid._inject_notify`；回归 24/24）。
6. 记忆治理 v1：库内重复/坏事实清理（先出报告，再决定是否自动 consolidate）。
7. B4 剩余（T+0 / 里程碑 / SSE 分段）、B5 回流书 schema、B6 动态创建 → 按用户拍板逐级推进，不跳级。
8. 每日 20 行变更摘要 + 测试结果给用户。

## P1（2026-10-19 夜）

- ✅ B9 總裝驗收：`tests/e2e_b9.py` 6 場景 37 項斷言全綠（報告 `docs/B9-验收报告-20261019.md`）。
- ✅ 日誌看板補 B 線狀態卡：B9 最近結果 / Checkpoint / 技能註冊表 / 待審回流書（http://10.0.0.2:8890/）。
- ✅ 人格模板 v2 上線（`prompts/persona/chat_system_v2.md` + `agents/persona.py` 色調閘門）：傲三嬌七、討好頻率抑制、深夜情緒允許柔軟。
- ⏳ P2：搜索語義校驗/alias 表、階段 5-6 基礎、VLM（Gemma 4 via llama.cpp）。
