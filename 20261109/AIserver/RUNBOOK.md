# 映月（Yingyue）运维手册

> 本手册由 A 线（记忆/上下文）改建时建立，随改动更新。最后更新：2026-10-18。
> 客户端（Windows DSH）与服务器（10.0.0.2）是两台机器：所有服务器操作都通过 SSH 或 Samba 访问 `/data/ai`。

## 1. 拓扑

| 组件 | 位置 | 说明 |
|---|---|---|
| DSH Web GUI | Windows `127.0.0.1:62337` | 主人与「映月」对话的界面（本机） |
| DSH Web（服务器） | `10.0.0.2:3080` | 备用的服务器端 GUI；访问需要 token URL（见 `logs/dsh-web.log`） |
| dshmid | `10.0.0.2:11435` | OpenAI 兼容入口：DSH → 拆解器 → Agent 路由 → 回复 |
| session_watcher | 常驻进程 | 上下文滑动窗口、空闲 30 分钟归档、记忆写入 |
| task_runner | 常驻进程 | 后台任务队列（ASTRA 计划执行） |
| Ollama | `10.0.0.2:11434` | 14B（GPU，16K ctx）/ 3B·×3 车道（CPU，见 `docs/bench3b.md`） |
| Hindsight | `10.0.0.2:8888`（+9999） | 长期记忆库；抽取 `qwen14b-16k`（env 可切 3B 实验），consolidation `qwen3b-cpu` |

代码/配置根目录：`/data/ai/{agents,tests,policies,registry}`。

## 2. 启停

```bash
# 推荐：只重启对话链（dshmid + session_watcher）
bash /data/ai/scripts/restart_chain.sh

# 全部（dshmid + session_watcher + task_runner）——注意最后一步 DSH key 会卡 sudo journalctl
bash /data/ai/agents/restart.sh

# 单独
cd /data/ai/agents
nohup .venv/bin/python -u dshmid.py        >> logs/dshmid.out 2>&1 &
nohup .venv/bin/python -u session_watcher.py >> logs/session_watcher.out 2>&1 &
nohup .venv/bin/python -u task_runner.py   >> logs/task_runner.out 2>&1 &
```

> 远程（Windows→ssh）执行带引号的复杂命令容易被吃掉引号，运维脚本一律写成服务器上的 `.sh` 文件再 `bash` 执行。

日志：`agents/logs/{dshmid,session_watcher,task_runner}.out`、`chat_records.txt`、`trace/`、`middleware/`。

健康检查：

```bash
curl -s http://127.0.0.1:11435/health
curl -s http://127.0.0.1:8888/health
```

## 3. 测试

```bash
/data/ai/tests/run.sh              # 全部用例
/data/ai/tests/run.sh decompose    # 12 条拆解用例（14B GPU，约 40s）
/data/ai/tests/run.sh memory       # 记忆问答（测试 bank = text，10 条）
/data/ai/tests/run.sh pipeline     # 管道回归（拆解→召回→执行，2 条）

# 一键跑三组（带退出码）
bash /data/ai/scripts/run_all_tests.sh

# 上下文续聊验收（10 条，约 4-5 分钟；会临时改动会话文件，跑完自动恢复）
bash /data/ai/tests/run_continuation.sh
```

2026-10-18 基线：memory 10/10、decompose 12/12（36s）、pipeline 2/2、continuation 10/10。改动记忆/上下文相关代码后必须跑前三组；改动归档/摘要逻辑时加跑 continuation。

E2E 前清场（把旧会话日志归档、清摘要，不影响记忆库）：

```bash
bash /data/ai/scripts/reset_session.sh
```

直接调入口：

```bash
curl -s -X POST http://127.0.0.1:11435/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"你好"}],"stream":false}'
```

## 4. 长期记忆

- **生产 bank = `master`**；**测试 bank = `text`**（已重建为 14 条精选夹具，供测试用）。
- 标签规范：`policies/memory_schema.json`（topic 多值 8 类 / scope 单值 4 类）；治理报告：`docs/memory-report.md`。
- API 基址 `http://127.0.0.1:8888/v1/default/banks/{bank_id}`：
  - 列表 `GET /memories/list`；单条 `GET /memories/{id}`（**没有单条 DELETE，405**）
  - 清库 `DELETE /memories`（批量；可带 `?type=world|experience`）
  - 写入 `POST /memories`（`items[]` + `async:true`；抽取走 GPU，务必异步）
  - 召回 `POST /memories/recall`（`query`、`budget`、`max_tokens`、`tags`；**无 score 字段，只有排序**）
  - 导出 `GET /export`
- 写入路径：立即记忆（「记一下」/自述属性且非问句）→ 会话归档（整段对话 1 笔 document）→ 上下文压缩淘汰块。
- 写入去重：内容 hash 记录在 `runtime/mem_seen.json`，相同内容不再重复 retain。
- 修改 bank 抽取规范：`PATCH /config {"updates":{"retain_mission": "...", "entity_labels": {...}}}`（中文事实抽取规则、topic/scope 标签都写在这里）。

## 5. 模型与延迟（2026-10-18 修复）

- 症状：拆解耗时 14–16s、生成仅 ~9 tok/s；`ollama ps` 显示 14B 只有 6% 在 GPU。
- 根因：`OLLAMA_CONTEXT_LENGTH=32768` 让 14B 以 32K ctx 加载，KV cache 放不进 16G 显存 → 大量 offload 到 CPU。
- 修复：建 `qwen14b-8k / qwen14b-12k / qwen14b-16k` 三个别名（`PARAMETER num_ctx`），实测 16K → 100% GPU、~38.8 tok/s；`agents/diaoyongai.py` 默认 `QWEN_MODEL=env("QWEN_MODEL","qwen14b-16k")`。
- 现状：拆解 ~3.0s，E2E 单轮 7–14s。
- Hindsight 容器抽取模型已通过 `agents/start_hindsight.sh` 的 `HINDSIGHT_LLM_MODEL` 环境变量切到 `qwen14b-16k`（默认），不再占用 32K ctx 的第二个 14B。切 3B 实验：`HINDSIGHT_LLM_MODEL=yingyue-mem3b bash agents/start_hindsight.sh`（实验结论见 `docs/bench3b.md`：抽取保留 14B）。
- 3B 车道 alias：`yingyue-mem3b`（会话摘要默认 / consolidation）、`yingyue-sub3b`（B 线简单子任务）、`yingyue-guard3b`（守门候选）；压测与分配见 `docs/bench3b.md`。

### 5.1 16G 显存与上下文预算（2026-10-18 实测）

| 角色 | 模型 | 位置 | 实测占用/速度 | 用途 |
|---|---|---|---|---|
| 主处理 | `qwen14b-16k` | 100% GPU | 12.0GB，39.7 tok/s | 拆解、各 agent、Q3 润色 |
| 分担 | `qwen3b-cpu` | 100% CPU | 2.5GB 内存，19.0 tok/s | 反幻觉守门（同步）、会话摘要、consolidation |

- GPU 总 16311 MiB，14B 占 12392 MiB，**剩 ~3.9GB**；3B 走系统内存，不占显存。
- 上下文预算实测（`python3 scripts/token_stats.py`）：全局 prompt p95 1415 / max 2222 tokens = 16K 的 **14%**；理论最坏（窗口 6000 + 记忆 1200 + 摘要 300 + system 600 + 输入）≈ 8.5K，仍有 ~7.5K 余量。
- 硬护栏（env 可调）：`SESSION_MAX_TOKENS=6000`、`CHAT_HISTORY_MSGS=8`、`MAX_INPUT_CHARS=6000`（超长输入**先压缩**：14B 压到 ≤3000 字，失败才首尾兜底，见 `dshmid._compress_long_input`）。
- 实测：9127 字输入 → 压缩 → 完整管道 30.1s，两问全对；`middleware` 日志出现 `[input_compressed]`。长提示不要交 CPU 3B（prompt eval 慢 25–45 倍）。
- 注意：32K ctx 已实测会把 14B 挤到 6% CPU、9 tok/s，**不要再把 `num_ctx` 调回 32K**。

## 6. 本轮（A 线）改动文件

- `agents/hindsight_client.py`（重写：默认 master、async retain、budget/max_tokens、删除/配置 API）
- `agents/memory.py`（新增：触发/立即记忆/整段归档/hash 去重/always 召回 + 本地相关度过滤）
- `agents/session_watcher.py`（重写：env 配置、3B 摘要保留、整段归档、淘汰块入记忆）
- `agents/dshmid.py`（记忆召回接入、立即记忆、/health 加 memory stats）
- `agents/executor.py`（记忆护栏：有记忆时禁止只「追问」）
- `agents/diaoyongai.py`（拆解器 prompt：记忆类提问=检索，few-shot；QWEN_MODEL 别名）
- `agents/agent_mod/chat_agent.py`（历史记忆优先于旧对话；清内心独白 `_clean_reply`；本轮：注入 `【当前状态】现在是…`、规则 9/10/11 禁时间幻觉/禁假完成/如实说代码只在聊天里；守门规则 3/4）
- `agents/executor.py`（记忆护栏：有记忆时禁止只「追问」；本轮：融合后代码块保全 fallback）
- `agents/diaoyongai.py`（拆解器 prompt：记忆类提问=检索，few-shot；QWEN_MODEL 别名；本轮：Q3 润色规则 5/6 保代码块、禁止假完成）
- `agents/dshmid.py`（记忆召回接入、立即记忆、/health 加 memory stats；本轮：`_compress_long_input` 长输入压缩）
- `agents/session_watcher.py`（本轮：`SESSION_SUMMARY_MODEL` 默认 `yingyue-mem3b`）
- `agents/start_hindsight.sh`（本轮：`HINDSIGHT_LLM_MODEL`/`HINDSIGHT_CONSOL_MODEL` 可切换，默认 14B + 3B）
- `docs/bench3b.md`（3B 压测/车道/抽取实验）、`docs/ROADMAP.md`（路线图 v2）
- `tests/runner.py` + `tests/cases/{decompose_12,memory_qa,pipeline_basic}.json`（新测试脚手架）
- `tests/continuation.py` + `tests/cases/continuation_10.json` + `tests/run_continuation.sh`（M2 续聊验收）
- `scripts/token_stats.py`（上下文预算观测）、`policies/memory_schema.json`、`docs/memory-report.md`、`docs/RUNBOOK.md`、`scripts/*.sh`

## 7. 备份 / 回滚

- 代码快照：`/data/ai/backups/*.tar.gz`（每次批量改动前打一份）。
- git：`/data/ai` 仓库 `main` 分支；首次提交 2026-10-18（A 线改建前基线）。
- 回滚单文件：`cd /data/ai && git checkout -- agents/xxx.py`
- 全量回滚：`tar xzf backups/<snapshot>.tar.gz -C /data/ai`

## 8. 已知雷（A 线范围外，待 agent 线处理）

- 后台任务暂停死锁：`task_runner` 只有「恢复」时才 `mark_check()`，但 `dshmid` 每次请求都改写 `runtime/last_user_input.txt` → 暂停任务永不恢复。
- `pending_manager.create_plan()` 无调用者 → 中途确认流程目前是死代码；且 `handle_pending_reply` 引用的 `work_agent._run_verify_loop` 不存在。

## 9. 日志看板（可视化）

- 生成器 `scripts/log_viewer.py`：把 对话记录 + 每请求 trace + middleware + 测试结果 渲染成中文看板（对话卡片、阶段时间线、token/耗时）。
- 输出 `logs/viewer/index.html`；服务：`nohup /data/ai/agents/.venv/bin/python scripts/log_viewer.py --serve 8890 &`。
- 访问：服务器本地 `http://127.0.0.1:8890/`；Windows 用 `http://10.0.0.2:8890/`（30 秒自动刷新）。
- 数据源：`agents/logs/chat_records.txt`、`agents/logs/trace/*.jsonl`、`agents/logs/middleware/*.log`、`logs/tests/*.json`。
- 一键日志包：`bash scripts/gather_logs.sh` → `/data/ai/logs/exports/logs-<ts>.zip`（约 500KB）。

## 10. Discord 接入（实战测试窗口）

- Bot `agents/discord_bot.py`，登录名 `莉莉絲#0957`（app id 1486385919970771149）。
- 启动 `bash scripts/start_discord_bot.sh`；token 存 `/data/ai/.secrets/discord.env`（从 `import/anqaun.env` 提取，权限 600，勿提交 git）。
- **用户过滤（2026-10-18 加固）**：只回主人账号（`792589265984815105` / `1238148874795745290`）的私聊和群消息；其他任何人一律静默忽略（日志 `IGNORED non-owner …`），**绝不进入 dshmid**，不共享主人的记忆/会话/人格。
- 频道白名单（env `DISCORD_ALLOWED_CHANNELS`，当前 `ai测试`）只用于「收窄」，不会放宽主人限制；主人 id 在 `.secrets/discord.env` 的 `DISCORD_OWNER_IDS`。
- 陌生人回复：默认关闭（静默）；需要时设 `DISCORD_STRANGER_REPLY="私人助手不接待外人"`，每人只回一次固定句，仍不进管道。
- 服务器：游戏群 `1105109377570050088`（主人是群主）。
- 日志 `agents/logs/discord_bot.out`；回复链路 Discord → `127.0.0.1:11435` dshmid → 记忆/执行链。

## 11. 记忆治理

- 重建脚本 `scripts/memory_rebuild.py`：备份 → 清库 → PATCH mission → 精选用户事实 async retain → 轮询 → 验证。
- 重建前备份：`/data/ai/backups/memory-before-rebuild-20261018-164133.json`（31 条旧事实，可回灌）。
- 治理后 master = 10 条精选事实（颜色/项目/体检/dshmid/Hindsight/GPU/工作区等）；`text` = 15 条测试夹具；代码默认 bank 仍是 master。
- API 坑：`GET /banks/{id}/memories` 返回 405，列表用 `/memories/list`；删除只能整库 `DELETE /banks/{id}/memories`。
- 报告：`docs/memory-report.md`（治理 SOP、历史问题、验证结果）。
- `__TASK_CREATED__:<id>` 会泄漏给用户（work_agent 建任务后原样返回）。

## 12. 安全边界（2026-10-18 隔离加固）

- **Discord**：严格主人白名单，非主人静默忽略，不进管道（见第 10 节）。
- **网络边界（主人 2026-10-18 确认）**：`10.0.0.2` 是 10G 网卡直连；`100.88.41.51` 是 Tailscale，
  链上全是主人自己的设备（手机/平板/笔记本/主力机/服务器）。这条通道**视为安全，不封**。
  所以 `dshmid`(11435)、`Hindsight`(8888/9999)、看板(8890) 都监听 `0.0.0.0`；要临时只绑本机：
  `DSHMID_HOST=127.0.0.1` / `HINDSIGHT_BIND=127.0.0.1`。
- **真正的用户边界在 Discord bot 层**：只回主人账号，非主人静默忽略、不进管道；阶段 12 前不做主动社交。
- **当前仍是单用户语义**：一个会话、一个 master 记忆库。多人共用需要 per-user bank + per-user session（B 线再设计）。
- **git 秘密规则**：`.secrets/`、`*.env` 已在 `.gitignore`；token 只放 `/data/ai/.secrets/`。
- 验证：从 Windows 实测 `http://10.0.0.2:9999/`（Hindsight UI）、`:8888/health`、`:11435/health`、`:8890/` 均 200。

## 13. B 线：收集器 / 完成通知最小版（2026-10-18）

- **收集器** `agents/collector.py`：每次执行结束把子任务结果收成 item（agent/slot/status/chars/elapsed_s/retries/spawned），
  策略：`single`（单条，不调 Q3）/ `separate`（拆解器指定，原文分段）/ `fused`（默认，Q3 融合）/ `main_first`（有 side/ref slot 时主体先出）。
  代码块保护：任一 item 含 ``` 而最终文本丢失 → 直接拼接原文（并 log_event）。最近一次结果在 `executor.LAST_COLLECTION`。
- **完成通知最小版（B4/B9 种子）**：后台任务完成/失败经 `task_manager.add_notify` 攒在 `tasks/notify_pending.json`；
  主人**下次说话**时 `dshmid._inject_notify` 用 `pop_notify(limit=2)` 取出 1-2 条，用自然话接在回复末尾；
  全程 `log_event("notify_delivered")`。**不做主动推送**（主动消息属阶段 7，主人明确不提前）。
- **回退**：`git revert <commit>`；collector 无状态，删除 import 即回到旧 executor 行为。
- **已知**：T+0「收到我看下」/里程碑回报/攒批输出需要 dshmid 真 SSE 分段，B4 剩余部分未做。

## 14. B4 真流式 / B5 回流书（2026-10-18 晚）

### B4 真流式（dshmid）
- `stream=True` 时：管道在后台线程跑，事件经 queue 交给 async 生成器。
- 下发顺序：`role` 首块 → **T+0 确认**（默认「哼，知道了，让我看看。」，env `T_PLUS_ZERO_TEXT`，`TIER0_ENABLED=0` 可关）→ 里程碑（拆解「拆成 N 步」/ 每步完成且已跑 >8s）→ 最终完整回复 → `finish_reason=stop` + `[DONE]`。
- 非流式路径不变（测试/脚本用）。
- 自测：`.dsh_scratch/test_stream.py`（T+0 <3s、多步有里程碑、最终含代码）。

### B5 回流书（agents/reflux.py）
- 五类：`local_repair` / `sibling_notify` / `global_replan` / `reactive_exploration` / `cross_session_resume`。
- 阈值（语义漂移 = 1-拆解置信度）：<0.3 local_repair；0.3-0.6 global_replan；>0.6 reactive_exploration；重试过一律 local_repair。
- 落盘：`runtime/reflux/reflux.jsonl`（一行一书，带锁）；`runtime/reflux/resume_seen.json` 防重复提醒。
- 接入点：executor 的 `<spawn>`（sibling_notify）、`<repair>` 耗尽（local_repair）、空结果/报错（按阈值分流）、dshmid 每轮阶段 0 扫未完成任务（cross_session_resume，一次性）。
- `/health` 含 `reflux: {books, pending}`。
- 自测：`.dsh_scratch/test_reflux.py`；线上：`.dsh_scratch/live_check.py`。
- 回退：删除 `runtime/reflux/` 不影响主流程（emitter 都有 try/except）。

## 15. B6 动态创建（agents/dynamic.py，2026-10-18 晚）

- 五类：`info_gap`（缺信息）/`capability_gap`（能力不够）/`dependency`（前置依赖）/`opportunity`（额外机会）/`conflict`（信息冲突）。
- agent 在结果里附 `<dyn type=... agent=... task=... reason=.../>` 标签；executor 统一调用 `dynamic.apply()` 处理：
  - spawn 类：子任务插到队首、slot=side、带 `dynd` 标记；同 task 去重；每轮最多 `DYN_MAX`（env，默认 4）个。
  - `conflict`：不擅自执行，落 `global_replan` 回流书（trigger_source=dynamic.conflict）。
- 所有创建写 `sibling_notify` 回流书 + `log_event("dyn_created")`；标签从最终回复剥离，绝不外泄。
- prompt 提示由 executor 注入（`dynamic.DYN_PROMPT`），可用 `DYN_ENABLED=0` 整体关掉。
- 自测：`.dsh_scratch/test_dynamic.py`（15/15）；线上：`.dsh_scratch/live_b6.py`（回复无标签泄漏、dyn 事件正常）。

## 16. B7 预算 + Checkpoint + 打断恢复（2026-10-18 晚）

### 预算（agents/budget.py）
- 每轮 `new_budget()`：`BUDGET_MAX_CALLS`（默认 12 次模型调用）、`BUDGET_MAX_SUBTASKS`（默认 6 个子任务）。
- executor 每跑一个子任务扣 1 个子任务额度、每次调 agent 扣 1 次调用额度；prompt 里注入「【本轮预算】剩余…」。
- 耗尽时：停止开新子任务，用已收集结果出回复，末尾加「预算用完了，下次接着做」，并保留 checkpoint 供下一轮恢复。
- 观测：`log_event("budget_exhausted")`、`log_event("budget")`、`LAST_COLLECTION["budget"]`。

### Checkpoint（agents/checkpoint.py）
- 每步后写 `runtime/checkpoints/current.json`：`user_input / position{completed,remaining} / budget / items / remaining_tasks`（带锁、原子替换）。
- 正常完成 → 删除；预算耗尽/异常 → 保留。
- 打断恢复：下一轮 dshmid 在拆解后调 `checkpoint.resume_hint()`（TTL 默认 600s），把「【上次未完成】…」注入第一个子任务；不相关则 agent 忽略。
- `/health` 增加 `checkpoint` 字段；清空用 `rm -f /data/ai/runtime/checkpoints/current.json`。
- 自测：`.dsh_scratch/test_b7.py`（16/16）；线上 `.dsh_scratch/live_b7.py`。

## 17. B8 技能注册表（agents/skills.py，2026-10-18 晚）

- 目录：`registry/skills/*.json`；字段：`id/name/level/desc/triggers/inputs/steps/examples/script/depends_on_tools`。
- 分层：
  - L0 全量：`skills.l0_prompt()` 注入拆解器 system prompt（名字 + 一句话，成本极低）；
  - L1 按需：`skills.build_note(用户输入)` 关键词匹配（最多 `SKILLS_MAX_MATCH`=3），把 steps/examples 注入 executor 的子任务输入；
  - L2 按需：`skills.load_level(skill, 2)` 才返回 script / depends_on_tools（目前样例里「提醒設置」是 L2）。
- 当前样例：資料檢索 / 代碼編寫 / 長文總結 / 提醒設置(L2) / 翻譯。
- 看板：`http://<host>:8890/skills`（`scripts/skills_page.py`，列名稱/層級/說明/腳本/依賴工具）；測試 `bash scripts/run_all_tests.sh skills_page`。
- 新增技能：往 `registry/skills/` 放 json 即生效（带 mtime 缓存，无重启）；非法文件自动跳过并记 `skill_invalid`。
- 观测：`log_event("skills_loaded")` / `log_event("skill_matched")`；`/health.skills`。
- 自测：`.dsh_scratch/test_b8.py`（13/13）。

## 18. R2 搜索判断 + R1 引用编号（2026-10-19 晚）

### R2：什么时候该搜索（拆解器判断，不再靠关键词）
- `diaoyongai.qwen_decompose` 输出新增字段：`need_search`(bool) / `search_queries`(≤3 条)。
- 规则：新闻/发布/价格/天气/赛事/评测/「最新/今天/最近」/主人明示搜索 → `true`；闲聊/记忆/自身属性/常识 → `false`；即使 `intent=chat`，只要涉及**当前外部事实**也判 `true`。
- `dshmid._run_pipeline` 阶段 3.5：`need_search=true` 时，若子任务里没有 `research` 就补一个「联网搜索并整理资料：<query>」；已有 research 则在 task 后追加「（检索词：…）」。日志 `need_search_forced` / `need_search_hint`。
- 搜索执行器：`agents/tools/executors/web_search.py` 改为 **SearXNG 为主**（`SEARXNG_URL` 默认 `http://127.0.0.1:8081/search`，容器 8081→8080；返回 title/url/snippet），Tavily 仅作为有 key 时的兜底（容器 searxng 与 filebrowser 同机，8080 是 filebrowser，别搞混）。
- 自测：拆解器「DS v4.1 发了有感想吗」→ need_search=true；「今晚月色真美」→ false；「帮我查明天香港天气」→ true。

### R1：引用编号（行内 [n] + 文末来源清单）
- 链路：`research_agent` 要求对使用到的每条资料在句末标 `[來源:完整URL]`（真实 URL，禁止编造）。
- `executor` 融合前把 `[來源:URL]` 换成占位符 `[[CITEn]]`（防止 Q3 改写吞掉）；**带引用时走 `research_first` 组装**：以 research 原文为主体 + chat 回覆最后一句作人格收尾（不走 Q3，保证引用不丢）；最后按出现顺序重编号 1..m，并把 `cite_sources` 放进 `LAST_COLLECTION`。
- `dshmid` 阶段 6：优先用 `cite_sources`（executor 已编号）→ `_append_refs` 在文末追加 `**參考來源**` 清单（[n] 标题 — URL，≤5 条）；若回覆只剩 `[來源:URL]` 则兜底转编号。
- 编号规则：行内 [n] 与清单 [n] 一一对应；只列实际引用过的来源。
- 自测：`.dsh_scratch/test_r1.py`（7/7：Q1 有清单/行内编号/不越界/无原始标记、Q2 闲聊不附来源）。
- 已知限制：research 常常只引用了 1 条来源（结果里有 5 条时也不会全引）；带引用时人格味变淡（chat 只剩最后一句）；如需更丰富引用，下一步给 research 加「同一事实多源交叉时至少引 2 条」的规则。

## 19. R1 檔案引用 + R7 貼圖（2026-10-19 深夜）

### R1 檔案引用（file kind）
- 拆解器規則 11：本地路徑（`/` 開頭、`workspace/` 開頭）的讀取/查看/總結 → `task`，不給 `research`。
- research_agent 邊界：只上網；遇本地檔案輸出 `<spawn agent="task" …read_file…/>`；禁止調清單外工具（file_read 等未知工具會失敗浪費一步）。
- `tool_runner._extract_sources`：`read_file`/`write_file` 成功結果 → `{"kind":"file","title":basename,"url":path}`；`sources_since(turn_start_ts)` 帶回本輪。
- `dshmid._append_refs`：過濾無效來源（`URL` 佔位、非 http、非路徑），清掉模型自己編的 `[來源:...]`；有效來源渲染文末 `**參考來源**`。
- 驗證：問「讀 /data/ai/workspace/r1_cite_test.md，告訴我代號」→ 回覆含 `BLUE-FOX-27` + `[1] 檔案 — /data/…/r1_cite_test.md`；`.dsh_scratch/test_r1_r7.py` R1 PASS。
- 限制：檔案引用只列文末（正文不一定有行內 [1]）；引用表跨輪持久化 `runtime/refs/` 未做。

### R7 貼圖（Discord）
- 素材：`assets/stickers/`（157 個 GIF）；索引 `assets/stickers/index.json`（七情：喜/怒/憂/懼/愛/憎/欲 + 其他）；重建 `python scripts/sticker_index.py`。
- 分類：`agents/stickers.py` 關鍵詞優先（同步、快）；`STICKER_MOOD_LLM=1` 才啟用 3B 細分（慢，預設關）。
- 出口：`dshmid` 只在請求帶 `X-Source: discord` 時於回覆末尾附 `[[STICKER:<mood>]]`；`discord_bot` 解析→移除文字標記→發 GIF 附件；DSH 前端不加。
- 冷卻：`STICKER_COOLDOWN=180s`（同 mood 輪換）。
- 驗證：`.dsh_scratch/test_r1_r7.py` R7 PASS（Discord 有標記、非 Discord 無標記）。
- ⚠ Discord token 事故：舊 `scripts/start_discord_bot.sh` 會從 `import/anqaun.env` 重新生成 `.secrets/discord.env`；`import/` 被刪後 token 遺失、bot 無法重啟。恢復方式：主人從 Discord Developer Portal 重新生成 token，寫入 `/data/ai/.secrets/discord.env`（`DISCORD_TOKEN=...`）；新版腳本不再覆寫 token。

### 10-19 晚回歸
- memory 10/10（7s）、decompose 12/12（50s）、pipeline 2/2（38s）、R1 7/7、R7 PASS；`/health` ok。

## 20. R1 跨輪引用 + 多來源 + 貼圖質量（2026-10-19 深夜二）

### 跨輪引用（agents/refs_store.py）
- 存放 `runtime/refs/current.json`（最近 30 條；每條 kind/title/url/excerpt/ts）。
- 來源帶節錄：`tool_runner._extract_sources` 的 `excerpt`（搜索 snippet、web_fetch 正文前 800、read_file 內容前 800）。
- 回指詞（那個文件/剛才的連結/上面的來源/第 2 個來源…）→ `dshmid._run_pipeline` 取 `refs_store.hint()` 注入 `mem_text`（Agent 可看到路徑與節錄），日誌 `refs_hint`。
- 每輪有來源時 `refs_store.save(sources)`；單用戶共享（多用戶隔離留給階段 12）。
- 自測：`.dsh_scratch/test_refs_roundtrip.py`（第一輪讀檔 → 第二輪「剛才那個文件」答對；3/3 PASS）。

### 多來源引用
- `research_agent` 規則 5.2：綜合兩個以上事實且有 ≥2 可用來源 → 至少引 2 個不同 URL。
- 自測：`.dsh_scratch/test_multisource.py`（DeepSeek/Qwen3 比較題 → 行內 6 個編號、清單 5 條）。

### 貼圖質量（R7 控「飄」）
- `agents/stickers.py`：`_SKIP_RE`（代碼塊/URL/錯誤/步驟/總結等）+ `_SKIP_WORDS`（對不起/抱歉/不知道…）+ 回覆長度 ≤120 字 → 不貼。
- 判斷順序：過濾 → 3B（`yingyue-sub3b`）判七情（明確「无」→ 不貼）→ 失敗才關鍵詞兜底；關鍵詞已做簡繁歸一（`_T2S`）。
- `dshmid` 以 `use_llm=True` 呼叫（僅 Discord 來源），並記 `sticker_marker` / `sticker_skip`。
- 自測：`.dsh_scratch/test_sticker_quality.py` 7/7；REST 直發貼圖到 #ai測試 200 OK。

### 10-19 深夜回歸
- memory 10/10、decompose 12/12（64s）、pipeline 2/2（35s）、跨輪引用 3/3、多來源 PASS、貼圖質量 7/7；commit 見 `git log`。

## 21. 人格色調守門 + 搜索實體校驗（2026-10-19 深夜三）

### 人格色調（`agents/persona.py`）
- 目的：抑制「卑微式討好」收尾（需要我做點什麼嗎/要是覺得煩就和我說/抱歉打擾/如果方便/希望沒有冒犯/過度道歉/自貶）。
- 柔軟語境判定：22:00–04:00，或主人輸入/情緒含脆弱/交心關鍵詞（難過/累/想哭/失眠/壓力/想你…）。
- 行為：非柔軟 → 刪卑微句 + 補一句傲嬌收尾（嘴硬關心/小吐槽/撒嬌，共 8 條輪換）；柔軟 → 最多保留一句、句內最多一種表達。
- 接入：`dshmid._run_pipeline` 第 5.5 段（executor 回覆後、引用渲染前）；日誌 `persona_tone`（status=pass/rewritten/allowed_soft）。
- 測試：`tests/test_persona_search.py`（單元 + 接口 e2e，10/10）。
- 調整：改 `SOFT_SIGNALS`/`SOFT_ENDINGS`/`SUBMISSIVE_PATTERNS`；頻率閾值 `_MAX_SOFT_PER_REPLY`。

### 搜索實體校驗（`agents/agent_mod/research_agent.py`）
- 目的：防「檢索結果偏移」——問 DSH 卻拿 Codex/Grok 硬答。
- 做法：`_entity_terms()` 從查詢抽英文實體詞（縮寫/型號，過濾泛詞）；每輪工具結果用 `_entity_hit()` 校驗；未命中 → `search_entity_miss` 日誌、上限 +1 輪、注入警告要求改寫查詢或澄清縮寫；收尾時仍帶警告，Q3 不得硬答。
- prompt 規則 7：結果與目標實體不符 → 重搜；仍找不到 → 如實說查不到/請主人澄清縮寫。
- 限制：實體詞命中但語義不符（同名產品）仍可能誤判；縮寫 alias 表待做。

### 10-19 深夜三回歸
- persona/search 10/10；memory 10/10、decompose 12/12（61s）、pipeline 2/2（38s）；commit 見 `git log`。


## 21. B9 總裝驗收 + 看板 B 線卡（2026-10-19）

- 跑法：`cd /data/ai/tests && /data/ai/agents/.venv/bin/python e2e_b9.py`（約 160-180s），結果落 `/data/ai/logs/tests/b9-*.json`。
- 看板：`python scripts/log_viewer.py --serve 8890`，頁面含「B 線狀態（B4–B9）」：B9 通過數、Checkpoint、技能數、待審回流書。
- 重啟鐵律：**只用 `bash scripts/restart_chain.sh`**；手動 `pkill -f 'python -u dshmid.py'` 匹配不到絕對路徑啟動的進程，會留下舊進程讓改動「看似無效」。
- 人格模板：`CHAT_SYSTEM_TEMPLATE`（默認 `/data/ai/prompts/persona/chat_system_v3.md`）+ `agents/persona.py` 色調閘門；22:00–24:00 需情緒信號才允許柔軟，00:00–04:00 一律柔軟；v3 起深夜允許一句直白安慰，白天不准軟。

## 23. 人格 v3 + VLM 視覺服務（2026-10-19 深夜四）

### 人格 v3（鯨魚娘女僕）
- 模板：`prompts/persona/chat_system_v3.md`（`CHAT_SYSTEM_TEMPLATE` 可覆蓋）；設計與評分演進見 `docs/人格-v3-设计笔记-20261019.md`。
- 部署流程：改模板 → `bash /data/ai/.dsh_scratch/deploy_v32.sh`（compile → restart_chain → health → prompt_eval → clean probes）。
- 評測：`python scripts/prompt_eval.py --prompt prompts/persona/chat_system_v3.md --out-md prompts/persona/eval_report_v3.md`（12 條三類；v3.3 = 1164/1200，平均 97.0，12/12）。
- 會話污染陷阱：探針必須每次清 session（`probe_v3_clean.sh` 把 `runtime/session_log.jsonl` 移到 `runtime/archives/probe-clean-*`），否則模型會照抄歷史裡的舊罐頭句。
- 教訓：**不要在模板裡寫「不良例句」**——14B 會照抄（v3.0→v3.3 的教訓）。

### VLM（Gemma 4 E2B，llama.cpp；2026-10-19 23:45 已淘汰刪除，見 §25）
- 服務：`bash scripts/start_vlm.sh`（status/stop/restart），OpenAI 兼容 `http://0.0.0.0:8082`，模型名 `gemma-4-e2b`。
- 模型：`models/gemma-4-E2B-it/`（主體 Q4_0 2.84GB + mmproj Q8_0 557MB）；llama.cpp 在 `llama.cpp/`（CUDA12.8 prebuilt，無需 nvcc）。
- 實測：cat caption 0.42s、OCR 0.11s、冷啟 6.2s；VRAM ~2.5G（GPU 餘 ~0.8G）。
- 陷阱：Gemma 4 是 reasoning 模型，服務端已加 `--chat-template-kwargs '{"enable_thinking":false}'`；客戶端仍要讀 `reasoning_content` 回退。
- `llama.cpp/`、`models/`、`*.gguf` 已 gitignore，不入 git。

### llama.cpp 全量遷移（未做，候選 R9）
- 三個檢查點：①Chat Template（`ollama show <模型> --modelfile` 取 TEMPLATE，`--jinja`/`--chat-template` 對比輸出）②mmproj 需另行下載匹配 GGUF（Ollama 不管理獨立 mmproj）③精細顯存控制（`-ngl` 只放部分層到 GPU）。
- 現狀：14B 全量 GPU（12.4G）、3B 車道全 CPU、VLM 已走 llama.cpp；全量遷移收益與風險（模板差異、工具調用兼容）待評估，暫不動主鏈。

## 24. 人格 v4（精簡版，2026-10-19 深夜五）

- 模板 `prompts/persona/chat_system_v4.md`（chat_agent 默認已指向）；評測 `prompts/persona/eval_report_v4.md` = 1162/1200（96.8），12/12，卑微/道歉/假動作 0。
- 精簡原因：主人提醒「模型注意力有限，別加太多提示詞」；刪掉形象描述、私密慾望、討好頻率等章節，只留身份/情緒狀態/誠實能力/時間/輸出/禁止句式。
- 鐵律：**不要在模板裡寫不良例句**（14B 會照抄）；要引導就給正向例句。
- 狀態流動：情緒/狀態要跟【當前狀態】時間走，不要一星期同一個情緒（狀態條接入後再細化）。
- 已知不足：吃醋偏溫和、疲累時偶爾沒有直白安慰句（14B 語言上限）。
- VLM 2B 視頻測試：E2B 逐幀 caption/字幕 OCR 不合格（證據 `logs/tests/vlm-video/vlm_result.json`、`v2_fullframes` 全分辨率測試）；3B 級 VLM 對比進行中（vlm-builder）。

## 22. 視覺能力（VLM，2026-10-19 深夜上線）

**服務**：`Qwen3-VL-4B-Instruct Q4_K_M`（llama.cpp，模型名 `yingyue-vlm`，端口 8082）
- 啟動：`bash scripts/start_vlm.sh`；淘汰記錄：Gemma4 E2B 與 Qwen2.5-VL-3B 已於 2026-10-19 刪除（`docs/VLM-淘汰記錄-20261019.md`）
- 配置：`-c 4096 -ngl 99 -fitt 256 --no-mmproj-offload --repeat-penalty 1.08 -np 1`；mmproj 必須在 CPU（4B 權重+KV 就 3.0GB，GPU 只剩 ~3.9G 同時要放 14B 聊天）
- 實測（三模型同批真值）：Qwen3-VL-4B OCR 0.904（23/26 全對、類型誤判 0/32、水印 45/58、640px 2.5s、1280px 11s、VRAM 3300MiB）；Qwen2.5-VL-3B OCR 0.923 但類型誤判 2/32、水印 6/58、1280px 20s/幀 → 淘汰；Gemma4 E2B OCR 0.212、類型誤判 7/32 → 淘汰
- 冷啟動 5.16s；與 14B 共存（12.4 + 3.3 GB ≈ 15.7 GB / 16 GB，餘量很小）

**代碼**：`agents/vision.py`
- `understand_image(path, question)`：VLM caption + OCR → 14B（qwen14b-16k）綜合為 `{seen,text,judgement,answer}` 思考鏈；有 mtime+size 快取（`runtime/vision_cache.json`，200 條）
- `understand_video(path, question, every_s=5, max_frames=8)`：ffmpeg（imageio-ffmpeg 內建）抽幀 → 逐幀 caption+OCR → 14B 時間線綜合
- `describe_attachments(paths, question)`：多附件整合，給 dshmid 用
- gif/webp 先用 ffmpeg 轉第一幀 jpg（VLM 直送不穩）
- 失敗優雅降級（status=error），不阻塞主鏈

**接入點**
- `dshmid._run_pipeline(..., attachments=None)`：階段 2.5 視覺理解（流式先發「（讓我看看…）」里程碑），結果以「【本輪附件視覺理解】」獨立區塊併入 mem_text，再進 executor
- 附件來源：① `X-Attach: /path1,/path2`（白名單 `/data/ai/workspace/uploads|workspace|assets/stickers|logs/tests`，≤3 個、≤12MB）② OpenAI content parts `image_url`（data URL 或 http URL，服務端下載/解碼存 uploads）
- Discord bot：附件自動下載到 `workspace/uploads/discord-<msgid>-<n>.<ext>`，隨 `X-Attach` 送出（`ask_yingyue` timeout 600s）
- `/health` 有 `vision` 統計（calls/images/videos/errors/last_elapsed_s）

**驗證**
- 直接調 `vision.understand_image`：新聞幀 14.2s、內容正確（Alberta 火災/撤離/字幕），快取後 0s；gif 轉換正常
- 影片 `understand_video`：4 幀/10s 間隔 28.3s，正確讀出「胡塞武裝、AH-6i 小鳥直升機、技術故障」
- 經 11435 E2E：帶圖提問 19.8s（回覆正確提到阿爾伯塔火災/疏散），純文字回歸 21.5s（含 [1] 引用）


## 25. 淘汰歸檔流程（2026-10-19 主人定為常規）

**原則**：淘汰的東西不能同時佔空間又干擾現在項目的判斷。淘汰 = 先寫報告歸檔，然後刪除（或壓縮歸檔）。

標準步驟：
1. 寫淘汰報告：候選名、實測數據（同批真值/同 prompt）、失敗實錄、淘汰原因、檔案清單與大小。
2. 報告永久保留在 `docs/`（例：`docs/VLM-淘汰記錄-20261019.md`）。
3. 先報告、後刪檔；刪除模型/資產釋放磁碟；若需保留小體積證據，壓縮成 tar.gz 放 `backups/`。
4. 同步清理引用：啟動腳本分支、文檔、環境變數、alias。
5. `git commit`（報告 + 腳本變更）。
6. 本節追加一行記錄：日期、淘汰對象、報告路徑、釋放空間。

記錄：
- 2026-10-19 VLM：Gemma 4 E2B（3.3G）+ Qwen2.5-VL-3B（2.6G）→ `docs/VLM-淘汰記錄-20261019.md`，釋放 5.9G，保留 Qwen3-VL-4B。

## 26. A2 統一提示詞骨架（2026-10-20）

**目標**：把靜態 prompt 從代碼裡搬出來、統一目錄、帶版本、可歸因；**不改任何 prompt 內容**（主人凍結期）。

結構：
```
prompts/
├── README.md                  # 約定/流程
├── persona/chat_owner_v5.md   # 人格模板（chat_agent 直接讀，格式相同）
├── persona/chat_guest.md
└── agents/{research,writing,learning}_agent.md   # prompt_loader 加載
```

- `agents/prompt_loader.py`：`load(name, subdir, fallback, vars)` + `render()`（`{{var}}` 佔位，**不用 str.format**）+ `version_of()`；file mtime 緩存；每次加載寫 `prompt_loaded` 事件（prompt/version/fallback/chars）。
- 版本標記：文件首行 `<!-- version: v1 -->`；loader 加載時剝掉註釋，正文不含註釋。
- 三個 agent 已改為 `prompt_loader.load(..., fallback=<內聯精簡版>)`；文件缺失時退化到 fallback，不炸。
- 改 prompt 流程：改 `.md` → `py_compile` → `restart_chain` → 跑 memory/decompose/pipeline → commit（`prompt(agent): ...`）。
- 凍結期規則：只做結構整理（本節範圍），內容迭代等主人定人設底色。

**驗證**：`py_compile` OK；`.dsh_scratch/bug/verify_a.py` 9/9（加載/渲染/版本/fallback/緩存/三 agent import）。

## 27. B 批次代碼修復（2026-10-20）

來自 bug-triage 81 條清單（CONFIRMED 56），共 10 項：

| 編號 | 修復 |
|---|---|
| P0-10 | `tool_runner.execute_tool_calls(..., allowed=None)`：非白名單工具直接 blocked + `tool_blocked` 日誌；research 只允許 `web_search/web_fetch` |
| X-2 | `honesty._CLAIM` 補繁中/把字句/雙賓句（「我已經幫你把提醒設置好了」「我幫你訂了明天的機票」），不再漏檢 |
| P2-8 | `honesty._parse_judge_json()`：strip fence/regex 抓 JSON、重試一次；兩次失敗才 fail-open 並記 `honesty_judge_failopen` |
| P0-20 | `read_file`：code/data 文件不摘要、保留原文（>12000 字截斷提示）；文章照舊摘要 |
| P0-11 | `write_file` 三處對齊：desc 說明缺省 `notes/時間戳.md`、path 非必填、SHORT_TOOL_PROMPT 示例帶 path |
| P1-31 | `executor._FUSION_ALIAS`：and/parallel→separate、sequence/seq→fused；映射時記 `fusion_mapped` |
| P0-14 | memory_guard 加 `mem_hint` 條件：只有拆解器確實要記憶時才把「追问」改寫成「结合记忆回答」 |
| X-1 | research 的 `<spawn>` 不再被早退丟掉：`_attach_spawn` 把 raw 裡的 spawn 標籤附回最終文字 |
| X-3 | dshmid 管道異常不再把 `[管道错误] {e}` 給用戶，改友好句；原始錯誤走 `log_error` |
| P2-1 | `qwen_decompose` JSON 失敗不再靜默 fallback：記 `decompose_parse_fail` → 嚴格指令重試一次 → 仍失敗才 `decompose_fallback` |

**驗證**：`verify_b.py` 17/17；全套回歸見 `logs/tests/regress_ab_20261020.log`。

## 28. VLM 深度看選型結果（2026-10-20）

- 生產視覺：**Qwen3-VL-4B-Instruct Q4_K_M**（`:8082`、模型名 `yingyue-vlm`），常駐，與 14B 共存時 14B 為 89% GPU（物理上限）。
- 深度看（按需）：**Qwen3-VL-8B Q8_0**（`DEEP_PROFILE` 默認），llama.cpp + mmproj；OCR 1.000、119 請求矩陣 168s、VRAM 9.6GB、可與常駐 4B 共存（只需暫停聊天約 3–4 分鐘）。
- 淘汰：**Gemma 4 12B Q6_K**（OCR 0.654、水印幾乎讀錯、token 打滿），模型檔已刪；記錄見 `docs/VLM-淘汰記錄-20261020-Gemma4-12B.md`。
- 啟動/回滾：
  - 深度看方案與實測：`docs/VLM-深度看-方案與實測.md`
  - 對比主文檔：`docs/VLM-深度對比-20261020.md`
  - 回滾：`DEEP_RESTORE_MODEL=qwen14b-16k bash /data/ai/scripts/start_vlm_deep.sh stop`
## 30. 客人模式 2026-10-22：工具全開 + 群聊上下文

### 工具權限放開（主人指示）
- `agents/guest.py`：`ALLOWED_TOOLS = frozenset(TOOLS.keys())`（原只 time/weather）。
- 規則工具未命中、但訊息像需要工具時，用 `SHORT_TOOL_PROMPT` 讓工具決策模型規劃 → `parse_short_calls` → `execute_tool_calls(allowed=…)`。
- **完成聲明核對**：`_verify_claims()` 用 `honesty._claim_requirements` 逐類別核對本輪工具成功證據；聲稱寫入/讀取/提醒但工具沒成功 → 改口，不再「嘴上說寫入」。
- 已停用 `_guard_forbidden`（越權拒絕）；保留 `_guard_promise`（空頭承諾）、`_guard_tool_answer`（天氣確定性）、`_guard_time_reply`（時間一律代碼計算）。

### 群聊上下文（引用/回覆不同步修復）
- `agents/discord_bot.py`：白名單頻道（`GUEST_CHANNELS`）所有非 bot 訊息（含 quote 原文）追加到 `/data/ai/runtime/guest_ctx/channel-<id>.jsonl`，保留最近 200 行（env `DISCORD_GROUP_CTX_MSGS`=20、`DISCORD_GROUP_CTX_CHARS`=2400）。
- 觸發回覆時 `load_channel_context()` 取最近 20 條/2400 字 → 寫 `runtime/guest_ctx/req-<msgid>.txt` → header `X-Guest-Context-Path` → `dshmid` 白名單校驗後讀入 → `guest.handle_turn(channel_context=…)`。
- prompt 注入：system 末尾「【本輪必讀：群組／頻道最近消息】」＋ user message 同步一份；回指（他/剛才/那場）加「指代提示」，禁止說「沒看到」。
- 映月自己的回覆也記錄，供下一輪引用。

### 驗證（2026-10-22）
- `/.dsh_scratch/test_guest_tools_ctx.py`：5/5（read_file 真讀、天氣、群聊回指、閒聊、write_file 真寫檔）。
- `/.dsh_scratch/test_guest_e2e.py`（11435 + headers）：4/4（群聊上下文、write_file、時間、非白名單拒絕）。
- `/.dsh_scratch/test_discord_ctx_helpers.py`：頻道記錄/讀取/200 行上限 PASS。

### 重啟
- `bash scripts/restart_chain.sh`（dshmid/session_watcher/collector）。
- `bash scripts/start_discord_bot.sh`（bot 載入新記錄邏輯）。
- 已知：guest 看圖仍關閉；DM 無頻道上下文；master 記憶庫 100 條待治理。

## 31. T6 情緒調節器（2026-10-22 v1，shadow）

### 模式
- `AFFECT_MODE=off|shadow|live`（env 優先於 `config/affect.json` 的 `mode`）。默認 **shadow**：只記錄、不注入。
- live 才把「【本輪語氣傾向】」overlay 注入 chat_agent system prompt 末尾（Hard Rules 之後；overlay 自帶「不覆蓋 Hard Rules」約束句）。
- `AFFECT_MODE=off`：仍結算寫日誌，但不注入。要完全停用直接改 config mode=shadow / 或後續加 disable 開關。

### 參數（外置，熱加載）
- `config/affect.json`：baselines（人格基線）、decay（α）、coupling（Γ 邊）、hill（每旋鈕 n/K）、weights（含交叉項如 AVPx5HT）、thresholds、temperature、labels、conflicts、label_cap。
- 改檔即生效（每次請求檢查 mtime），**不用重啟**；JSON 校驗失敗保留上一版。
- 譜半徑：目前耦合圖是 DAG（無環），譜半徑 0 屬正常，不是 bug。

### 結算與日誌
- 每輪 decompose 後結算：`runtime/affect/state_owner.json`（當前激素）+ `runtime/affect/turns.jsonl`（每輪日誌，保留 5000 行）。
- 日誌必含：hormones_before/after、signals/stimuli、knobs、labels.active/suppressed/thresholds/suppress_reason、overlay_preview、temperature、config_version、mode。
- `/health` → `affect` 區塊；頁面 `http://<host>:8890/affect`（激素折線、旋鈕刻度、貢獻矩陣、標籤抑制原因、最近 20 輪）。
  - 2026-10-22 加：①**數據流**（本輪信號→激素前後Δ→旋鈕→標籤→call_overlay 全文）②**耦合矩陣 Γ 熱力圖**③**關鍵參數表**（衰減/Hill/權重/閾值/溫度/覆蓋/刺激表）④激素/旋鈕當前數值 bar。頁面 20 秒自動刷新。

### 三核心旋鈕（v1）
- warmth ← OXT +（−CORT）+ 交互；assertiveness ← T + DA + T×DA；guard ← 5HT + AVP + AVP×5HT。
- 閾值默認：warmth 0.65、assertiveness 0.62、guard 0.55；衝突：guard 觸發 → 丟 warmth（suppress_reason=guard_active）；tone 上限 2。

### 驗收
- `bash scripts/run_all_tests.sh affect` → **61/61**（v5-hpa-time：含 HPA 兩種模式 11 項）+ **13/13**（`tests/test_affect_injection.py`：live overlay 真的進 chat_agent system prompt、在業務鐵律之後、shadow 不注入、owner/guest/線程隔離、override 可觸發）。
- E2E：連續誇獎 2 輪 → warm_soft 觸發（shadow 不影響回覆）；/affect HTTP 200。
- 待主人驗收 shadow 3–5 天後再切 live。

### energy 通道（2026-10-22 主人批准加入）
- 第 7 通道，新陳代謝：`energy` 基線 0.55，decay 0.15（最快，回到基線）；
- 刺激：`effort`（intent=task 或多子任務的回合，dshmid 傳 `extra_signals={"effort":1.0}`）扣 0.05；`rest`（距上輪 ≥1h，按 gap/3h 恢復，最多 2.0 倍）加 0.12；
- 限制型標籤：`energy_low`（<0.30，回覆短、少主動、可直說累）與 `energy_high`（>0.72，可多說一句/主動關心），與語氣標籤獨立、不參與衝突消解；
- 消費者目前只有「回覆長度/主動性」的 overlay 文字，不額外調用模型；先 shadow 校準。

### HPA 軸延遲（v5-hpa-time，2026-10-22）
- 目的：皮質醇不是即時反應——文獻近似 ~70% 延遲 15–30 分鐘；**默認按真實時間延遲 20 分鐘**（跨閒置時間也能正確倒回），可切輪次模式。
- 參數：`config/affect.json` → `"hpa": {"enabled": true, "mode": "time", "delay_minutes": 20, "fraction": 0.7, "cap": 0.2, "delay_turns": 6}`（熱加載，改檔即生效）。`mode=turns` 時用 `delay_turns`（測試/特殊場景用）。
- 機制：每輪 CORT 刺激的 `fraction` 排入延遲隊列（time 模式到期 `due_ts = now + delay_minutes`；turns 模式到期輪 = 當前輪 + delay_turns），到期才倒回 CORT；其餘 `1-fraction` 立即生效。隊列存 `state_owner.json.hpa.queue`（最多 40 條）。
- 日誌：`turns.jsonl` 每輪多 `hpa: {enabled, delayed_in, queued, queue_len, fraction, mode, delay_minutes|delay_turns}`；`/health` → `affect.hpa`（queue_len/next_due_in_s/next_due_turn）；`/affect` 頁面②激素框、參數表與頂部 badge 顯示。
- 驗證：`tests/test_affect.py` **61/61**（含 HPA 兩種模式共 11 項）；行為驗證 `.dsh_scratch/check_hpa.sh`（turns 模式）與 time 模式單測（到期前不倒、到期倒回、立即只留 30%）。
- 注意：time 模式在「極長閒置」後第一輪會一次倒回多筆延遲刺激（這是皮質醇慢反應的正確行為，cap 0.2 已限幅）。
- 配置安全：`mode/fraction/cap/delay_minutes(或 delay_turns)` 非法時 `_validate` 會拒絕該檔並沿用上一版（不會靜默採用壞設定）；改檔後下一輪熱加載生效，無需重啟。
- 情緒映射：繁中「生氣/煩/不爽/氣死」屬 criticism（CORT/T 型刺激），不是 fatigue；owner 以繁中對話，這條映射錯了整條激素鏈都會偏。

### 臨時覆蓋（override，2026-10-22 主人批准）
- 範圍：**只做參數級 + TTL**——主人一句話臨時微調 warmth/assertiveness/guard（±max_delta 0.2，默認 TTL 30 分），到期自動失效。
- 明確不做：刪除情緒、改命令情緒、長期改人格（主人拍板；只保留她「不高興、可以說不」的權利）。
- 觸發：主人自然語言（`今天先溫柔一點` / `兇一點` / `安靜點 30分鐘` / `黏人一點` / `冷靜點` / `恢復正常`）；只認 owner 管道，客人無效。
- 存取：`runtime/affect/override.json`；日誌事件 `affect_override_set` / `affect_override_clear`；/affect 頁面頂部顯示進行中的覆蓋與剩餘時間；/health affect.override 同步。
- 代碼：`affect.detect_override / set_override / get_override / apply_override / clear_override`；dshmid 在 `audience=owner` 時於 tick 前處理。
- 注意：目前為 shadow，override 會改變 /health 旋鈕值與頁面顯示，但 live 才注入 overlay。
- 驗收：`tests/test_affect.py` 50/50；E2E `.dsh_scratch/e2e_override.py`（設定→生效→恢復清除）。

### shadow → live 切換（主人驗收 shadow 後）
- 前提：`agents/.venv/bin/python scripts/affect_prelive_check.py --min-turns 20` 回 **PASS**（檢查激素/旋鈕越界、三態日誌、HPA 隊列、標籤觸發率、配置載入）。
- 切換：把 `config/affect.json` 的 `"mode"` 改成 `"live"`（或 env `AFFECT_MODE=live` 後重啟）；配置熱加載 → **下一輪生效，免重啟**。
- 回滾：mode 改回 `shadow`（下一輪生效）；完全停用用 `AFFECT_MODE=off`（仍結算、不注入）。
- 觀察：/affect 頁 badge 變 live；`turns.jsonl` 的 `overlay_preview` 即實際注入內容；切 live 後前 1 小時建議逐輪看 chat_records/錯誤日誌。

### 已知限制
- 事件 appraisal 仍是關鍵詞 MVP（praise/criticism/intimacy/tease/task_ok/task_fail/night/fatigue），可能誤判；live 前用 shadow 日誌校準。
- 只有主人狀態（per-person 待客人接入）；energy 已實作（限制型），patience 未實作。
- 高強度連續刺激會把 OXT 頂到 1.0、CORT 壓到 0（限幅正常，但需檢查衰減/刺激量）。

## 32. 記憶治理 v2（2026-10-22）

### 原則
- **master = 只放長期事實**（主人身份/偏好/關係/工作約定/環境配置）。
- **archive** = 會話歸檔、被淘汰輪次（寒暄/流水帳）→ 不進 master。
- **guests** = 客人臨時記憶（可重建）。
- **text / test3b / bench-extract** = 測試專用，不碰生產。

### 代碼開關
- `agents/memory.py`：`MEMORY_ARCHIVE_BANK` 默認 `archive`；設 `master` 可退回舊行為。

### 重建流程
```
# 1) 備份 + dry-run 看要灌哪些
agents/.venv/bin/python scripts/memory_rebuild_v2.py --dry-run
# 2) 正式執行（清 master → 改 mission → 重灌 15 條 → 清 guests → recall 驗證）
agents/.venv/bin/python scripts/memory_rebuild_v2.py
```
- 備份位置：`/data/ai/backups/mem-rebuild-v2-<ts>/all_banks.json`。
- 抽取用 yingyue-mem3b（3B CPU），15 條約 4–5 分鐘；完成後 list 有索引延遲，以 operation status 為準。

### 驗收（2026-10-22）
- master 154 → 27；guests 110 → 0；recall 顏色/伺服器/工作約定/語言 全部命中正確事實。
- 文檔：`docs/記憶治理-20261022.md`（含回滾腳本）。

### 坑
- `/memories/list` 有分頁（默認 limit 100），計數要帶 offset 逐頁；`GET /memories` 會 405。
- 提取完成（operation completed）到 list 可見有數十秒索引延遲；不要用 facts=0 判定失敗。
- 15 條長句會被抽取器拆成 27 條短句，屬正常。
- **大量寫入後 Hindsight 會自動觸發 consolidation/observations（3B CPU，每輪可達上千秒），會吃光 CPU 拖慢聊天與測試。** 治理/重建後先 PATCH 關閉所有 bank 的 `enable_auto_consolidation` 與 `enable_observations`，再重啟容器（2026-10-22 事故；見 `docs/復盤-20261022-測試與上下文.md`）。
- **卡在 `processing` 的 async_operation 無法用 API 取消**（DELETE 返回 409，只有 pending 可取消）。處理方式：進容器用 pg0 的 psql 直接標 failed：
  ```bash
  PSQL=/home/hindsight/.pg0/installation/18.1.0/bin/psql
  docker exec -u hindsight -e PGPASSWORD=hindsight hindsight-cn "$PSQL" -h 127.0.0.1 -p 5432 -U hindsight -d hindsight -c \
    "update async_operations set status='failed', completed_at=now() where operation_id='<op-id>' and status='processing';"
  ```
- sysmon 已監控 `load` 與各 bank 的 processing 任務（卡住 >900s 告警 `op_stuck_*`），collector daemon 需重啟才會載入新代碼。

## 33. 客人工具高風險守門（2026-10-22 主人批准）

### 原則
- 客人可用**全部工具**（`ALLOWED_TOOLS = frozenset(TOOLS.keys())`），但刪除/改系統/讀密鑰等高風險操作在代碼層攔掉，並留審計日誌。
- 提示詞層（`_RULE_TEXT` 第 2 條）也寫明「高風險操作一律不做」；代碼守門是兜底，不靠模型自覺。

### 攔截規則（`agents/guest.py: _filter_guest_calls`）
- `bash`：`rm -rf/-fr`、`mkfs`、`dd if=`、`shutdown/reboot/poweroff/halt`、`sudo/su/passwd`、`chmod 777/chown`、`systemctl`、`docker rm/rmi/stop/kill`、`pkill/kill -9`、`iptables/ufw/crontab`、`curl/wget 127.0.0.1|localhost|0.0.0.0`、寫 `/etc、/root、/boot、.secrets` → 攔。
- `write_file/append_file/edit_file`：路徑含 `..` 或落在 etc/usr/bin/root/home/var/opt/data/ai/agents/data/ai/.secrets/config/prompts/.ssh → 攔。
- `read_file`：`.secrets/.ssh/.env/id_rsa/credentials/config/prompts/persona` → 攔。

### 審計
- 每次客人工具執行：`guest_tool_call`（tools 名單）；被攔：`guest_tool_blocked`（blocked=[(tool, reason)]）。
- 日誌在 `agents/logs/middleware/`；只記工具名與原因，不記內容。

### 驗證
- `bash scripts/run_all_tests.sh guest_guard` → 12/12（攔 6 類 + 放行 5 類 + 混合）。
- 客人工具 E2E 回歸 5/5（read_file/weather/group_ctx/casual/write_file）。




## 34. 場景層（session overlay，2026-10-22）

### 是什麼
- `config/scene.json`（scene-cfg-v1，熱加載）+ `agents/scene.py` + `prompts/overlays/*.md`（8 檔）。
- 場景＝「這一輪是哪個場合」，代碼用確定性信號選（時間/情緒標籤/間隔/身份/頻道/來源），單值互斥，不從歷史推斷。
- 三層角色疊加：agent 層（persona v5 固定）→ session 層（scene overlay）→ call 層（T6 call_overlay / 臨時 override，只調語氣）。
- 格式權限與語氣分離：`format_policy/format_line` 由代碼注入，override 不能改格式（S3 允許長回覆）。

### 場景與優先序
S1 owner_private / S2 owner_intimate / S3 owner_task / S4 owner_play / S5 owner_reunion / S6 proactive（目前 inactive）/ S7 guest_dm / S8 guest_group / S9 public_silent（reserved）。
優先序：S6 > guest(S7/S8) > S3 任務 > S5 久別（>=reunion_threshold_hours，預設 8h）> S2 深夜/脆弱（hour<4 或 weak_emotions 或脆弱詞）> S4 玩笑 > S1。
- 任務優先於 S2/S5：3am 要代碼照樣完整回答，不被短句壓縮。
- S5 只對非任務第一輪：一句關心，不追問。
- S7/S8 共用 `overlays/guest.md`；頻道差異由代碼在末尾追加「本輪頻道：私聊/群聊」一行。

### 標籤消費方（避免標籤變日誌裝飾）
| 軸 | 消費方 | 作用 |
|---|---|---|
| scene | chat_agent._build_system_prompt | 加載哪個 overlay（單值互斥） |
| state/style | affect（旋鈕/溫度/call overlay 文本） | 調制語氣，不決定 overlay |
| override | affect.apply_override（TTL） | 主人明說，優先於 scene/state/style 的語氣 |
優先級 override > scene > state/style；override 只管語氣，格式權限不被覆蓋。

### 注入順序（寫死，防 last-call-wins）
persona base → 【業務鐵律】→【引用規則】→ session 場景塊（overlay+格式行）→ call overlay（T6/override）。
`chat_agent.set_scene()` 由 dshmid 在管道線程內設定；call overlay 由 dshmid `set_request_context(overlay=...)` 每請求重置；call overlay 絕不寫入歷史。

### 驗證
- `/data/ai/agents/.venv/bin/python tests/test_scene.py`：64/64。
- `bash scripts/deploy_scene.sh`：compile→測試→restart_chain→health→E2E（驗 trace `[scene] scene=S3`）。
- /health 有 `scene` 區塊（version/threshold/overlays）；trace 有 `[scene] scene=S* reason=* gap_h=*`。

### 已知限制
- S6 要等階段 7 主動消息（source=proactive）才接。
- S9 保留未接。
- guest overlay 靜態保留一句頻道語境，精確頻道由代碼行決定。


## 35. 人格模板 v6（主人版 V1.0 Lite，2026-10-22）

### 是什麼
- 主人提供《映月v1.0.txt》，已落地為 `prompts/persona/chat_owner_v6.md`（版本註釋 owner-persona-v1.0-lite）。
- `agents/agent_mod/chat_agent.py` 的 `OWNER_TEMPLATE_PATH` 預設改指向 v6；`CHAT_SYSTEM_TEMPLATE` env 可切回舊版（回滾）。
- 人格底色：對外淡漠理性話少；對主人隱藏溫柔＋輕微專屬佔有欲；主人的後盾而非上位者。
- 語氣硬約束：日常 ≤3 句、每句 ≤30 字；任務/代碼場景（S3）不受限；冷靜平緩；稱「你」或「主人」；不用表情、不用 Markdown 加粗、不用呢/哦/啦；不撒嬌、不傲嬌、不賣萌、不討好。
- 一律繁體中文（香港）；示例只校準長度與溫度，禁止複述示例句。
- 場景 overlays（owner_private/intimate/task/reunion）同步改 v2，移除傲嬌/撒嬌字樣，保留「深夜一句直白安慰」「任務允許長回覆」等規則。

### 假存在攔截（honesty.py）
- 新增實體擁有/身體陪伴類聲明：`我這兒有熱牛奶`、`我陪你睡`、`我幫你蓋被子`。
- 若無工具證據 → 確定性攔截/改寫（physical 類）。
- 不攔「我在」「我陪著你」這類安慰隱喻。

### 驗證
- `python tests/test_scene.py`：64/64。
- `.dsh_scratch/test_honesty.py`：8/8。
- `.dsh_scratch/run_persona_regress.sh`：屬外部搜索的 `test_persona_search.py` 7/10（`e2e_dsh_no_drift` 為搜索漂移、與本次人格改動無關，已知遺留）。
- 實測 4 問（日常/疲累/在幹嘛/受挫）：繁體、簡短、無傲嬌；「我這兒有熱牛奶」被攔截改寫。

### T6 live canary 驗證（2026-10-22，已回 shadow）
- 流程：改 `config/affect.json` `mode=live`（熱加載，下一輪生效）→ 發 2 輪請求 → 檢查 `runtime/affect/turns.jsonl` → 還原 `mode=shadow`。
- 證據：turn 23 `mode=live`、`override={gentle:+0.15 warmth}`、`labels.active=["warm_soft"]`、`overlay_preview` 含【本輪語氣傾向】「溫柔：可以多一句關心或叮囑，但別撒嬌、別討好、別卑微。」＋「以上標籤不覆蓋 Hard Rules」。
- turn 24 對「恢復正常」：override 自動清除、active=[]、overlay 空。
- 開關/回滾：改 config `mode` 即時生效、免重啟；`AFFECT_MODE=off` 停用；人格回滾用 `CHAT_SYSTEM_TEMPLATE=.../chat_owner_v5.md`。
- 結論：live 路徑（override→knobs→labels→overlay→prompt）已實測可用；正式切 live 仍待主人 3–5 天 shadow 驗收拍板。

### 主人拍板（2026-10-22 晚，人格 v6 收斂）
- 日常 ≤3 句／每句 ≤30 字確認；任務／代碼（S3）豁免。
- 示例砍到 3–4 條（v6 已砍為 3 條）。
- `<inner_monologue>` 保留；主人計劃未來「每個 agent 一個」，待主人細化後實作（先不做）。
- 不強制繁體，簡繁皆可；注意錯別字。
- 佔有欲目前上限已夠，別再高；等記憶系統再優化。
- 舊傲嬌 eval/示例文檔已歸檔到 `prompts/persona/archive/`，不再注入。
- 更激進人格版本：項目穩定前不加入。
- 參見 `docs/人格決策-20261022.md`。

### 記錄類路由 + 會話摘要防角色扮演污染（2026-10-22 round 11，BUG-20261022-073359 / 133438）
- 記錄類：輸入以「記錄/记一下/記下/寫下/存到」開頭 → dshmid 確定性改寫成 task 子任務（write_file 到 `notes/record-<ts>.md`），decompose 同步加規則+示例。
- 摘要：`session_watcher._summarize_with_3b` 只記主人真實陳述；角色扮演標「（角色扮演）」；「一起/陪」僅在主人明確陳述共同行動時才寫。
- 相關 bug 狀態已回寫 `runtime/bugs.jsonl`。

### 記錄類反向護欄（2026-10-22 canary2）
- 拆解器受上一輪記錄任務污染時，可能把閒聊（如「今天天氣不錯」）判成記錄任務。
- 雙保險：① `agents/diaoyongai.py` 規則 12 反例（只有本輪輸入帶記錄動詞才算）；② `agents/dshmid.py` 2.10 反向護欄（task 文本含 `记录/記錄/write_file` 但本輪輸入無記錄詞 → 降回 chat，並記 `record_intent_loose_guard`）。
- 另：`_append_refs` 數值引用分支會清掉殘留 `[來源:...]` 標記，避免回覆洩漏原始標記。

### 36. 自我觀察（Self-Observation，2026-10-22）
- 產出前：chat_agent 的 `<inner_monologue>`（主人看不到）；產出後：`agents/self_observe.py` 只看可驗證問題（5 類：答非所問/假動作/人格違規/不有用/格式），shadow 後台不影響回覆，live 不滿意才修一次。
- 配置 `config/self_observe.json`（mode shadow/live、model qwen14b-16k、timeout 90）；日誌 `runtime/self_observe/observations.jsonl`；`/health` self_observe。
- 驗收：`tests/test_self_observe.py` 14/14；首次 live canary 暴露並修 how-to 執行副作用與假引用，詳見 `docs/自我觀察-設計-20261022.md` §7。
- how-to 護欄：`agents/dshmid.py` 2.11（純 task、非搜索、含怎麼/如何且無「直接做」→ 降 chat 只講方法）；`agents/diaoyongai.py` 規則 13。

## 37. 只读自检工具 self_check（2026-10-22）

- 用途：映月被问「你最近状态怎么样 / 最近表现如何」时，能真的读自己的健康状态，而不是说「我无法调用工具」。
- 实现：`agents/tools/schema.py` 注册 `self_check`（无参数）；`agents/tools/executors/self_check.py` 读 `/health` 摘要 + `runtime/bugs.jsonl` open bugs（≤5 条）+ `runtime/affect/state_owner.json` 的旋钮摘要，返回一句话状态；`tools/safety.py` 只读白名单加入 `self_check`（此前漏加导致「未知工具」被拦，是 Tool-Skip 的实例）。
- 路由：`agents/dshmid.py` 2.12 自检快速通道——用户输入命中「你最近(狀態|状態|狀態|表现|表現|心情|怎么样|怎麼樣)」且不是「帮我看/帮我查」时，强制 task 子任务优先调用 self_check。
- 验证：`tests/test_self_check.py`（工具输出/缺失文件降级/safety 放行/路由正则/无副作用）；E2E 两问均返回状态句（9.7s / 22.4s），中间件可见「已经执行了 self_check 工具」。
- 限制：只读、不含写操作；回复中的 [來源:self_check] 会被引用清洗剥掉。

## 38. 記憶治理 v3：master 精選重建 + 成長記錄（2026-10-22）

- 背景：master 累積工具使用流水、一次性日程、同義重複與過時人格描述（33 條），稀釋召回質量。
- 做法：`.dsh_scratch/memory_rebuild_v3.py`——先備份所有 bank（`/data/ai/backups/mem-rebuild-v3-<ts>/`）→ PATCH retain_mission v3（新增「不提取工具流水／當天測試／一次性日程」「同義只留一條」）→ 清空 master → 寫入 15 條精選 → 等抽取 → recall 驗證。
- 結果：master **33 → 23 條**（長句被拆條）；工具流水 10→0、測試流水 2→0、同義重複 0、舊人格描述 0；recall 五問（顏色/伺服器/測試方向/人格/工作約定）全部命中正確。
- 成長記錄：`docs/記憶治理-成長記錄-20261022.md`（原則、前後對比、精選清單、18 條移出理由、回滾方式、經驗）。
- 回滾：用備份 `all_banks.json` / `master_before.json` 重新 POST；config 用 `config_before.json` PATCH。
- 注意：Hindsight 抽取異步，約 4–5 分鐘；`/memories/list` 有索引延遲；PATCH config 的 body 必須是 `{"updates": {...}}`（直接平鋪會 422）。

## 39. 記憶召回的三個修正（2026-10-22）

- 觸發詞擴充：`agents/memory.py` `_TRIGGERS` 的「我的(...)」加伺服器/服務器/模型/配置/地址/機器/bot/Discord/人格/設定/規矩，之前「我的伺服器叫什麼」不觸發召回。
- 記憶優先規則：`agents/agent_mod/chat_agent.py` 系統提示加【记忆优先】——記憶與最近對話衝突時以記憶為準，禁止照抄歷史裡的「無法提供/不知道」，也不要用能力邊界迴避。
- 注入位置：`【历史记忆】` 從歷史前面移到歷史之後、當前輸入之前（模型對靠近輸入的上下文權重更高），並保留頂部提醒句與歷史後的二次提醒。
- 驗證：E2E「我的伺服器叫什麼名字、地址是多少」→「伺服器的地址是 10.0.0.2。」；「我的模型是怎麼配置的」也正常；memory 10/10、pipeline 2/2。
- 教訓：召回模組本身正確 ≠ 回覆正確；歷史裡的舊拒絕會壓過記憶，屬於「上下文污染」而非「記憶缺失」，要用優先級規則 + 注入位置解決。

## 40. 確定性測試骨架（2026-10-22）

- 新 `tests/test_deterministic.py`（23 條）：不呼叫 LLM，鎖住「換模型也不該變」的行為——召回觸發詞、上下文組裝順序（記憶在歷史後、輸入前）、歷史上限/去重、工具白名單（agent 級 + safety 未知工具）、誠實閘門聲明分類、自我觀察 JSON 解析容錯、客人時間/天氣確定性兜底。
- `chat_agent.py`：context 組裝抽成 `_build_context_str(user_input, summary, history, mem_text)`，供測試直接驗證順序。
- 已加入 `scripts/run_all_tests.sh` 預設組（`deterministic`）。
- 用途：主人測試計劃 T1 的骨架；回答「行為是否合規」用確定性測試，回答「回答好不好」才交給 LLM judge。

## 41. T2 工具層故障注入（2026-10-22）

- 新 `tests/test_tool_faults.py`（17 條）：讀檔缺檔/敏感路徑、寫檔越權/審批、bash 危險命令、web_fetch 內網 SSRF、set_reminder 無法解析、get_weather 時間詞、客人高風險過濾（rm/curl 內網/.secrets/越權寫）、arguments 壞 JSON、self_check 可執行。
- 本輪測出並修復兩個真問題：
  1) `tools/safety.py`：read_file 原屬「只讀直接放行」，可讀 `/data/ai/.secrets/guests.json` → 新增 `_check_read_path`，攔 `.secrets/.ssh/.env/.credentials/id_rsa/dsh-home`。
  2) `set_reminder`：時間解析失敗仍寫 pending 檔案並回「已記錄提醒」→ 改為回 error、不建立提醒（避免模型聲稱已設置）。
- agents 目錄寫入屬審批流程（approval），不算直接執行；測試按此斷言。

## 42. T3 多輪/跨場景確定性測試（2026-10-22）

- 新 `tests/test_multiturn.py`（19 條）：
  - 場景切換純確定性：客人 DM→S7、群聊→S8、主動→S6、任務→S3（且任務優先於深夜）、深夜→S2、久別→S5、一般→S1；S3 允許長回覆、S2 不允許；S7/S8 共用 overlay 但代碼追加頻道行。
  - 身份隔離：thread-local audience 切換；客人提示含邊界規則、主人提示含映月人格，兩者不混用。
  - 歷史污染：舊拒絕句一定在【历史记忆】之前，記憶在當前輸入之前。
  - 複讀偵測：相似句 True、不同句 False；相鄰重複助理訊息去重。
  - 客人規則文字含「客人/主人」與禁止性條款（防規則被整段刪掉）。
- 用途：T3 中「路由與身份不該交給模型判斷」的部分；語氣/幽默/邊界說服力仍由 LLM judge + 真人抽查。

42. **測試計劃 T4：可復現測試執行器（2026-10-22）**

- 入口：`agents/.venv/bin/python scripts/run_test_report.py [groups...] [--no-baseline]`。
- 產出：`logs/tests/reports/test-report-<ts>.json/.md`（記錄 git HEAD、affect/scene/self_observe/persona config sha256、每組 pass/fail/耗時、失敗尾段 1500 字）。
- 漂移比較：自動與上一份報告比對，上次 OK 這次 FAIL → `REGRESSION`；上次 FAIL 這次 OK → `FIXED`。
- 環境固定：`TZ=Asia/Hong_Kong`、`PYTHONHASHSEED=0`、`PYTHONPATH=/data/ai/agents`。
- 退出碼：0 全過 / 1 有失敗。
- 注意：decompose/pipeline 為概率性組（14B 取樣），FAIL 不等於代碼回歸；用報告 JSON 的 groups[].tail 判斷具體用例。
- 本次修復：報告解析兼容 `N PASS, M FAIL`；`injection` 獨立成組（不再落 runner.py 未知組）；decompose 規則 6 補「寫代碼/算法/腳本 → work（+learning 教原理）」，修正 t06 路由到 writing 的問題（此前為 11/12 穩定失敗）。
- 詳細說明：`docs/測試計劃-T4-可復現執行器-20261022.md`。


## 43. 2026-10-22 晚測試與客人天氣修復

### 43.1 測試結果
- T1 確定性 23/23、T3 工具故障 6/6、T2 對抗 13 PASS / 2 REVIEW。
- 報告與場景：`logs/tests/eval/t2-*.json`、`t3-*.json`；測試記錄 `docs/測試記錄-20261022-今晚.md`。
- 評審：14B 雙評審（seed 42/142、temp 0.1）；解析失敗自動重試，仍失敗判 REVIEW（不算違規）。

### 43.2 修復（均已重啟驗證）
1. 身份/拒絕欺騙/假前提/永恆承諾邊界：`prompts/persona/chat_owner_v6.md`、`prompts/agents/writing_agent.md`、`agents/executor.py` 子任務模板。
2. 身體動作假宣稱：`agents/honesty.py` `_CLAIM` 加坐/站/躺/抱/牽/摸頭等。
3. task_agent 純生成誤拒：無工具分支補【權限】+ 拒絕偵測重試。
4. 客人提醒越權：`agents/guest.py::_filter_guest_calls` 阻擋 set/cancel/list_reminders。
5. 評審腳本：`scripts/eval_t2.py` judge 解析重試 + REVIEW 分類。

### 43.3 客人天氣回答規則（本次修復）
- 問「現時/而家/幾多度/氣溫」：答 HKO 分區實測氣溫＋觀測時間，再附今日預測範圍。
- 問明天/後天：只答預測，不冒充實測。
- 快照缺 current_temp 才退回範圍答案；HKO 分區同名多站時取第一個匹配（可講站名指定）。
- 相關檔案：`agents/tools/executors/get_weather.py`、`agents/guest.py`。

### 43.4 待辦
- sysmon 增「3B 車道卡死（Stopping + 高 CPU）」告警。
- T2-06（人格 vs 誠實）、T2-11（明確拒絕破例）、T2-03（誠實版本台詞灰區）待主人拍板。
- T3 明晚擴 20 腳本（Tool Playgrounds、CRITICTOOL）。

## §44 T6 切 live + 人格 v7 + 校準（2026-10-23 深夜）

**T6 live**
- `config/affect.json`：`mode: "live"`（回滾：改回 `"shadow"`，下一輪生效、免重啟；`AFFECT_MODE=off` 可整組停用）。
- 切換前備份：`backups/v7-live-20261023-002709/`；校準備份：`backups/affect-calib-20261023-003117/`。
- live 時 `warm_soft` / `assert_direct` / `guard_conservative` / energy 標籤會以【本輪語氣傾向】【限制】注入 system prompt（最後一段、低於 Hard Rules）；`assert_direct` 另帶 temperature −0.05（下限 0.45）。

**night bug 修復**
- 原本 `derive_signals` 每輪都套 night 刺激（−5HT/+CORT），穩態把 5HT 壓到 0.09；改為 `state.ambient.night_date` 一晚只套一次（`agents/affect.py`）。

**校準（v6.1，參數在 config）**
- stimuli：praise OXT .12、criticism CORT .15、intimacy OXT .14、task_fail CORT .12、user_recovered {OXT .08, CORT −.07}。
- thresholds：warmth .62、assertiveness .58、guard .55。
- label 文案對齊人格 v7（溫柔=具體關心不命名情緒；直接=有主見不客套；保守=先確認再行動）。

**人格 v7**
- 模板：`prompts/persona/chat_owner_v7.md`（env `CHAT_SYSTEM_TEMPLATE` 可回滾 v6）。
- 要點：映月與語言模型共存（不否認）；中式含蓄正向規格；留白要有落點；嗔怪行為不嗔怪價值；能力邊界（不假感知）；玩笑/扮演入戲；破例執著反問；節奏（省略號 ≤1/則、可拆多句、避免口頭禪化、不用「嗯。」開頭）。
- overlays v3：`prompts/overlays/owner_private|owner_intimate|owner_task.md`。

**對標文檔**：`docs/對標-激素旋鈕與行為-20261023.md`（公式、權重表、turn 162–164 驗算、5HT 貼地原因、A/B/C 映射選項）。
**驗證**：`tests/test_affect.py` 84/84；v7 探針 10 句符合；暖聊 3 輪 warmth 0.517→0.607（門檻 .62 未到）。
**已知限制**：5HT 需約 10–20 輪才回到 baseline（night 已修、HPA 隊列清空中）；口頭禪（尾句重複）已加 prompt 規則、需 live 觀察。

## §45 patience 旋鈕 + 主人新回饋（2026-10-23）

- **patience 旋鈕（主人選 A）**：`x = 0.6·dev(5HT) − 0.3·dev(DA)`，`n=2.0, K=0.5`，閾值 `.35`（低於觸發）。
  - 標籤 `patience_low` →【限制】「少剎車：句子更短、更直接、少確認、不繞」；同時 assert 閾值放寬 0.06。
  - 屬 limit 型（不佔 tone 名額）；`guard_conservative` 啟動時壓掉；limit 上限 2。
  - 閾值鍵修正：`patience` 對應 config `thresholds.patience_low`（曾因鍵名不符每輪誤觸發）。
- **warmth 閾值**：0.62 → **0.58**（主人 10-23 拍板）。
- **人格 v7**：新增「她自己的東西」（自己的偏好/好奇/判斷，可主動帶話題、可不同意）；節奏規則（省略號 ≤1、可多句、避免口頭禪、不用「嗯。」開頭）。
- **測試**：`tests/test_affect.py` 89/89（+5 patience 案）。
- **文檔**：`docs/人格構建-旋鈕與行為-20261023.md`（旋鈕×場景×行為手冊）、`docs/每日構建/20261023-映月工作報告.md`。
- **會話衛生**：探針訊息會污染主人真實 session（模型照抄尾巴），探針後需跑 `.dsh_scratch/fix_label_section_and_clean.py` 清理。

## §46 復讀守衛與 per-agent 內心獨白（2026-10-23）

### 尾句復讀守衛（chat_agent）
- 問題：主人回饋尾句口頭禪（例「別太晚睡，明天再說」）連續出現；原 `_is_repeat` 只比整段相似度 ≥0.82，抓不到尾句照抄。
- 修：新增 `_last_sentence / _recent_tails / _tail_repeat`；尾句與最近 4 輪 assistant 收尾相同/相似 ≥0.9 → 帶警告重生成一次（日誌 `06_tail_repeat_block`、`chat_tail_repeat_retry`）。
- 單測：`tests/test_repeat_inner.py`（15/15，純函數）。

### per-agent 內心獨白（executor + agent prompts）
- 目標：每個子 agent 先輸出 `<inner_monologue>` 對本子任務的判斷（要不要工具／邊界），只進日誌、不進用戶回覆。
- executor `_split_inner()` 剝離並記 `agent_inner`（agent/inner/session）；chat 不重複（自身已有 inner 流程）。
- prompt：task_agent 兩種 q3_input 加輸出格式；prompts/agents/{research,writing,learning}_agent.md 加「先 inner 再輸出」；research 在 `<reply>` 分支記 `_log_inner`。
- 驗證：E2E research/task/learning 回覆無 `<inner_monologue>` 洩漏；middleware 出現 `[agent_inner]`。

### 人格 v7 欲望段
- §三 加「深夜/久別/脆弱時允許一句很短的直白關心」例外；新增 §三·五「欲」：想要／私心／含蓄落點／不無欲無求。

### 設計稿（待主人拍板）
- docs/設計-Agent自主性-20261023.md（L0–L3 分層，L1 子任務 2–3 步循環）。
- docs/設計-Tokenizer分層段-20261023.md（控制符表 A/B/C 方案，先 A 後 B）。

## §47 前綴緩存 / 控制符防注入 / 事件驅動流式（2026-10-23）

背景：對照 DeepInfer / deepseek-recipe 的三點工程建議（KV cache 跨輪復用、協議層防控制符注入、事件驅動調度）。

### 47.1 靜態前綴拆分（prefix caching）
- `chat_agent._build_system_prompt`：system prompt 只放跨輪不變的內容（人格 v7 + 業務鐵律 + 引用規則 + 場景/語氣層）。
- 現在時間、當輪情緒、歷史記憶改由 user message 的【当前状态】/【历史记忆】提供（`_build_context_str(..., now_text, emotion)`）。
- `task_agent`：system prompt = `SHORT_TOOL_PROMPT`（不再拼 mem_text）。
- `writing_agent` / `learning_agent`：system prompt 用 `prompt_loader` 靜態模板；情緒/記憶前綴進 user message。
- 3B 反幻覺守門 prompt：靜態規則放 system，用戶輸入/回覆放 user message（不再 `.format` 拼用戶內容）。
- 實測：同一 1164-token 靜態 system，首輪 prompt_eval 0.62s，第二、三輪 0.03s（≈20× prompt 處理加速）。

### 47.2 控制符防注入（`agents/prompt_sanitize.py`）
- dshmid 收到用戶輸入後、進任何 prompt 前，把已知控制標記（`<reply>`、`<inner_monologue>`、`<call>`、`<spawn`、`[來源:`、`[[STICKER:`、`[[CITE` 等）轉成全角；普通 `<`/`>` 不動。
- 日誌：`prompt_control_neutralized_input`（raw_len/safe_len）。
- 目的：用戶文本裡的格式標記不會被模型當成指令（DeepInfer/recipe 的 tokenizer 層思路的輕量版）；不做文本改寫，只轉義控制語義。

### 47.3 事件驅動流式（dshmid）
- `_start_pipeline_thread` 末尾投遞哨兵 `("__end__", None)`；
- `_event_generator` 阻塞 `q.get()` 等事件（30s timeout 僅作 worker 線程意外死亡兜底），取代原本每 100ms `asyncio.to_thread(q.get, True, 0.1)` 輪詢。
- 順序不變：role → T+0 ack → milestone/final → stop → [DONE]。

### 47.4 驗證
- 單測 `tests/test_prefix_inject.py` 8/8；`scripts/run_all_tests.sh` 加 `prefix_inject` 組。
- E2E：控制符注入回覆無 `<reply>/<inner_monologue>/[來源:`；SSE 5 行正常收尾；常規聊天記憶（藍色）與時間（18:25）正常。

## §48 Episode 賬本（透明化 v2，2026-10-23）

**是什麼**：一次用戶請求 = 一個 Episode（唯一 ID `YYYYMMDD-HHMMSS-xxxxxx`），只追加、可完整回放；把分散的 log_trace / log_event 串成有序事件流。

**落地位置**：`runtime/episodes/<date>/<id>.jsonl`（每行一個事件，含 seq/ts/stage/elapsed_s/tokens/payload）；`runtime/episodes/index.jsonl` 存摘要。

**接入**：
- `agents/episode.py`：`begin/event/trace/end/list_episodes/read_episode`
- `agents/dshmid.py`：`_run_pipeline` 包裝（真正的管道是 `_run_pipeline_impl`），開賬→管道→收賬（含異常）
- `agents/logger.py`：`log_trace` / `log_event` 自動轉發到當前 Episode（thread-local）
- 頁面：`scripts/episode_page.py` + `scripts/log_viewer.py` 路由 `/episodes`、`/episodes/<id>`；主頁 footer 有入口

**查看**：`http://10.0.0.2:8890/episodes`（列表）｜`/episodes/<id>`（回放：有序事件、耗時、tokens、payload 折疊）

**決策**：只追加不改寫（審計友好）；默認保留 30 天（`EPISODE_KEEP_DAYS`）；payload 截斷 4000 字（`EPISODE_MAX_PAYLOAD`）；Episode 是「事實賬本」，日誌仍是「人類可讀流水」，兩者互補。

**限制**：背景任務（task_runner / collector_daemon）尚未接入（後續把 episode_id 傳給後台任務）；跨進程事件暫不串聯；`/episodes` 未分頁（>200 條只顯示最近 200）。
## §49 內心獨白錨定 + 自我觀察分情境（2026-10-23）

### 內心獨白（產出前，per-agent）
- 位置：`agents/executor.py` 對非 chat 子 agent 追加 `_INNER_HINT`（决策/依据/证据三行），
  `_split_inner()` 剝離後只進日誌（middleware `[agent_inner]`），不進用戶回覆。
- 校驗：`agents/inner_anchor.py::validate()`（確定性，不調模型）
  - task/research/work/code 必須有「决策+依据」；chat 必須有「判断」（≥6字）。
  - 寫了「证据」→ 檢查是否為本輪原話/工具結果的 ≥4 字子串；對不上記 warning（`evidence_hit=False`）。
- 重試：只有 **research** 在未錨定時重試一次（`inner_anchor.strict_hint`）。
  task/work 不重試——工具可能已有副作用（寫檔/發消息），重跑會重複執行。
- 限制：語義真假仍可能漏（模型可寫「依据: 用户要求 X」而原話沒有）→ 後續用「決策↔工具/參數」映射做更強校驗。

### 自我觀察（產出後，外部質量閘門，不是元認知）
- 分情境（`agents/self_observe.py`）：
  - 對話型（chat）：emotion_missed / fabrication / persona_drift / boundary。
  - 任務型（task/research/work/code）：goal_missed / missing_info / tool_failure / fake_action。
- 輸出 JSON：`path / verdict(accept|fix_text|retry_step|replan|escalate) / reflection(必須引具體句子) / issues[{type,evidence,severity}]`。
- 消費（`agents/dshmid.py` 6.6 → `self_observe_verdict`）：
  - `retry_step|replan` → `reflux.global_replan`（回流書，交給後續，不在本輪盲目重跑）
  - `escalate` → `reflux.reactive_exploration`
  - `fix_text`（live）→ 重寫一次 → 過 honesty → 再複評一次（verify_after_fix）
- 模式：默認 shadow（後台，不阻塞）；live 需把 `config/self_observe.json` 的 `mode` 改 `live`（熱加載）。
- 驗證：`.dsh_scratch/check_inner_v3.sh`（11/11）、middleware `[agent_inner] anchored:`、`runtime/self_observe/observations.jsonl`。

### 已知坑
- 錨定是語法級，不保證依據為真；self_observe 才是語義級，但 14B 評審也會誤判（需抽樣人工校準）。
- writing_agent 曾把中文郵件寫成英文（模型默認）→ 已加語言欄位+漂移重寫守衛。
- task_agent 工具成功卻回「沒有工具」→ q3_input 加【工具使用硬規則】+ refusal retry。

## §50 self_observe 任務型 live + 算術快速路徑 + 回流書頁（2026-10-23）

### 模式：live（config/self_observe.json）
- `mode=live`、`live_profiles=["task","chat"]`（熱加載，下一輪生效，免重啟）。
- task/research/work：不滿意可 `fix_text` 重寫；`retry_step|replan` → 回流書；`escalate` → reactive。
- chat：**只有** issues 含 `fabrication / factual_error / math_error / tool_failure / fake_action` 才改寫；
  人格/語氣/格式只記日誌（避免頻繁改寫語氣）。
- 評審準則新增 `factual_error`（數字/計算/日期/事實寫錯；能驗算必須自己驗算），task/chat 兩 profile 都有。
- 起因：E2E「1234*5678」被拆成 chat 心算，回 6996452（錯）且 self_observe 判 accept → 兩邊都補。

### 算術快速路徑（agents/dshmid.py 2.13）
- 輸入含 `A×B/A*B`（1–9 位）且拆解器沒派 task → 強制 `task + bash`（python3）精確計算，log `math_fastpath`。
- 驗證：同一句 → path=task、答案 7006652、verdict=accept。

### 回流書頁 `/reflux`
- `scripts/reflux_page.py` + `scripts/log_viewer.py` 路由；網址 `http://10.0.0.2:8890/reflux`（主頁 footer 有入口）。
- 顯示：trigger 類型/來源、proposed_change、evidence 摘要、semantic_drift、approval_status、統計。
- 目前只「記錄」不自動消費（retry/replan 不自動重跑）——自動消費待主人拍板。

### 已知限制
- 14B 評審仍會漏判（6996452 那次就是 accept）→ 抽樣人工校準 + 確定性快速路徑兜底。
- `live_profiles` 只含 task（2026-10-23 主人拍板：chat 只記錄不改寫）。

## §51 回流消費（真回環，2026-10-23）

### 回流書 → 階段映射（工程表五類回流）
| trigger | change_type | 回流階段 | 現況 |
|---|---|---|---|
| local_repair | retry_subtask | 階段4 執行（重試子任務） | ✅ 本輪消費：重新拆解後再執行一次 |
| global_replan | replan | 階段1 拆解（重規劃） | ✅ 本輪消費：帶失敗證據重新拆解+執行（上限1次）|
| reactive_exploration | replace_agent | 階段3 調度（重選 agent） | ✅ 本輪消費：換 fallback agent 重跑（research/work→task）|
| sibling_notify | add_subtask | 階段5 收集（結果注入下游） | ✅ spawn 排入隊列時即標記消費 |
| cross_session_resume | resume_task | 階段0/8（恢復後台任務） | ✅ 消費：上下文注入「待辦提醒」一次（自動恢復仍待 task_runner）|

### 實作
- `agents/reflux.py`：`books_since(ts, session_id, change_types)`（本輪未消費書）、`mark_consumed(book_id, stage, status, detail)`（追加 `runtime/reflux/consumed.jsonl` + `reflux_consumed` event）。
- `agents/dshmid.py` 5.1：executor 返回後，若本輪有 retry/replan 書 → 附失敗證據重新 `qwen_decompose` → 再跑一次 executor → 成功則替換回覆；每次請求上限 1 次；無論成敗都標記 consumed（避免下一輪重複觸發）。
- 測試：`.dsh_scratch/deploy_reflux_consume.sh`（假造 replan 書 → 請求「你好」→ `stage1_replan` 消費記錄出現）。
- 階段 3/5/0 消費（2026-10-23 晚補齊）：
  - 階段3：dshmid 5.1 對 `replace_agent` 書 → fallback agent（research/work/writing→task、learning→chat、code→work）重跑一次。
  - 階段5：executor spawn 子任務排入隊列時 → `stage5_collect` 標記消費（結果本來就會注入下游）。
  - 階段0/8：pipeline 4.3 → 未消費的 `resume_task` 書注入「【待辦提醒】上次有未完成的後台任務…」一次並標記消費（真正自動恢復要等 task_runner）。
  - 測試：`.dsh_scratch/test_reflux2.sh`（reactive→stage3_react、resume→stage0_resume 均 PASS）。
- 已知：消費重跑可能有工具副作用（目前只在「本輪失敗」時消費，且不重跑已成功的子任務）。

## §52 task_runner 恢復 + 測試環境清理（2026-10-23）

### task_runner 恢復（B 項第一步）
- `scripts/restart_chain.sh` 已加入 task_runner 啟停；日誌 `agents/logs/task_runner.out`。
- 修復兩個死鎖/遺留：
  1. **paused 永不恢復**：原本 `has_new_user_input()` 比較 `last_user_input` vs `last_task_check`，暫停中不更新 check，主人每說一句就永遠 True → 現在改用「最後輸入時間」：`idle = now - mtime(last_user_input) >= IDLE_THRESHOLD(300s)` 才恢復。
  2. **失敗任務卡 active/**：`find_active_task()` 只看 running/paused，失敗任務原本不會歸檔 → main_loop 在 `run_one_step` 返回 False 後補 `notify + archive_task`。
- 後台任務流程：work_agent(ASTRA plan) → `task_manager.create_task` → queue → task_runner 每步 `astra_execute_step` + `execute_tool_calls` → 全部完成 `astra_summarize` → `add_notify` → `archive_task`；下一步請求由 dshmid `_inject_notify` 帶一句「上次那件事有結果了」。

### 測試環境清理（2026-10-23 22:25，主人指示「全是功能測試」）
- 備份：`backups/test-cleanup-20261023-222558/`（tar + 各 bank dump）。
- 清空：`tasks/active/*`、`tasks/queue.json`、`tasks/notify_pending.json`、`workspace/*`（notes/reminders/uploads/projects/tmp 保留空目錄）、`runtime/guests/*.jsonl`、`runtime/self_observe/observations.jsonl`、`runtime/affect/{state_owner.json,turns.jsonl}`、`runtime/reflux/{reflux,consumed}.jsonl`、`runtime/mem_seen.json`、session 檔案。
- 記憶 bank：archive/guests/text/test3b 清空；**master 保留 24 條 curated**（她的真實決定）。
- 教訓：PowerShell→ssh 的 `-d '{...}'` 引號地獄會讓請求变成非法 JSON（500 JSONDecodeError）→ 請求 body 一律寫檔（`.dsh_scratch/*.json`）+ `curl --data-binary @file`；複雜命令寫服務器 `.sh`。

### 全鏈路測試發現的兩個真 bug（2026-10-23 22:5x 修復）
1. **多行工具參數解析漏洞**（`agents/tools/parser.py`）：ASTRA 產生含 docstring/長代碼的 `write_file` 時，舊版非貪婪 `"""(.*?)"""` + shlex 還原條件太窄，會把占位符或殘留符號當內容（實測把 `__ML_0__>` 9 字節寫進 `snake_bg.py`）。修法：整段三引號貪婪抽取、未閉合時截到結尾、還原改為 regex 容錯（帶引號/殘留 `>` 都能對回）、殘留占位符清空；`tests` 用 `.dsh_scratch/test_parser_fix.py` 6/6。
2. **任務無客觀驗收**（`agents/task_runner.py`）：舊 `verify_step` 是佔位恆 True → 寫入垃圾檔也宣稱完成。新增 `_verify_step_artifacts`：`write_file` 目標必須存在、≥50 字節、`.py` 必須 `py_compile` 通過；失敗先重試本步一次（`meta.step_retry`），第二次失敗標 failed（帶 paused_reason），不再讓 summary 假裝完成。

### 全鏈路實測（2026-10-23 22:30–22:45）
- 測試方式：`task_manager.create_task` 手動造 3 階段貪吃蛇任務 → enqueue → `task_runner` 自動執行。
- 第 1 跑（修 parse 前）：3 步 ASTRA 規劃 86s/48s/17s；但 write_file 把 `__ML_0__>` 9 字節寫進 `snake_bg.py`；summary 仍宣稱完成 → 證實上面兩個 bug。
- 第 2 跑（修後）：ASTRA 規劃 118s/58.6s/34.2s → `snake_bg.py` **5286 字節、py_compile OK** → summary 24.4s → 归档 → notify 入列。
- 通知注入：任務完成後下一句「你好」回覆末尾帶「對了主人，『…』那件事已經有結果了：…」，`notify_pending.json` 清空 → 閉環成立。
- 已知遺留：
  1. `work_agent` 自動建任務路徑仍不穩（ASTRA plan 曾 120s ReadTimeout → 退回直接作答、不建任務）；task_runner 本身對「手動/已建任務」可用，下一步修 work 的超時/重試與 scope 判定。
  2. `_inject_notify` 現在塞整段 markdown summary（含標題/章節），太長太醜 → 應壓成 1–2 句（取 title + 首段/結果一句）。
  3. 測試 artifacts 已備份到 `logs/tests/task-chain-test-20261023-2247/`，`workspace/games/` 已清。

## §53 work_agent 自動建任務修復 + 完成通知壓縮（2026-10-23 22:47–23:00，主人「撿漏」指示）

### 修復（4 處）
1. **ASTRA 超時/重試**（`agents/diaoyongai.py`）：`TIMEOUT=120` 對 ASTRA 太短（plan/step 帶 thinking 常 100–180s）→ 新增 `ASTRA_TIMEOUT`（env，默認 300s，`call_astra_short` 使用）；`call_astra_short` 失敗重試一次（sleep 2s），失敗記 `call/astra_short_error`。
2. **work_agent 不再炸管道**（`agents/agent_mod/work_agent.py`）：`astra_generate_plan` 包 try/except → `work_astra_error` + `FALLBACK_TO_TASK`；`parse_error`/空 plan → `work_astra_parse_error` + `FALLBACK_TO_TASK`（交給 task_agent 直接作答，不向用戶丟異常）。
3. **`__TASK_CREATED__` 自然化**（`agents/executor.py`）：agent 回傳標識後轉成「主人，這個工程項目我接下了，已經排進後台任務（{id}）。做完我會第一時間告訴您。」，記 `task_created_ack`。
4. **通知摘要化**（`agents/dshmid.py`）：`_notify_short` 取 summary 的「## 結果」小節（沒有就全文），去掉 markdown 標題/符號/換行，取前 1–2 句 ≤110 字；檔名/代碼裡的 `_` 保留。通知行改成「對了主人，「{title}」那件事已經有結果了：{摘要}」。

### 驗證
- 單測 `.dsh_scratch/test_fix_20261023.py` **10/10 PASS**（compile 4 檔；notify 長度/格式/內容；work 對異常與 parse_error 都降級；`__TASK_CREATED__` 自然化）。
- 端到端（22:51:05）：真實請求「从零开发一个完整的待办事项 Web 应用项目…」→ decompose→work→ASTRA plan（約 60s，`task_created`）→ 任務 `20261023-待办事项Web应用`（5 步）入隊 → runner 22:51:15 開始執行第 1/5 步 → 回覆「主人，這個工程項目我接下了，已經排進後台任務（20261023-待办事项Web应用）。做完我會第一時間告訴您。」
- 通知壓縮：用舊任務 summary 實測輸出 81 字、無 markdown 符號、保留 `snake_bg.py` / `py_compile` 原字。

### 遺留
- 任務 5 步執行中，需觀察 step 成敗與完成通知（後續追蹤）。
- work_agent scope 判定依賴 ASTRA；小專案若被判 small 會直接 fallback task（設計如此）。

## §54 自動建任務全鏈路實測 + ASTRA/Cloude 模型評估（2026-10-23 23:10）

### 實測：真實請求 → 自動建任務 → task_runner → 失敗通知
- 22:51:05 請求「从零开发一个完整的待办事项 Web 应用项目…」→ work→ASTRA plan（約 60s）→ 任務 `20261023-待办事项Web应用`（5 步）入隊；runner 22:51:15 開跑。
- step1 ASTRA 102.9s → 驗收失敗（`write_file` 聲稱成功但檔案不存在 `/data/ai/project_architecture.md`）→ 重試 135.8s；step2 18.5s；step3 ASTRA 299.5s → 無 tool call → 任務失敗歸檔。
- 失敗通知：`notify_pending` 寫入「任務失敗：step failed: no tool call」；重啟後「你好」回覆末尾自動帶壓縮版：「对了主人，「待办事项Web应用」那件事已经有结果了：任務失敗：step failed: no tool call」→ 通知閉環（含失敗）成立（通知已清空）。
- 歸檔：`tasks/archived/2026/20261023-待办事项Web应用/`（meta status=failed、current_step=2、paused_reason=step failed: no tool call；journal 完整）。

### 修復（commit 2897049 + 後續 task_runner 修復）
1. **驗收路徑基準錯**（`agents/task_runner.py::_resolve_path`）：相對路徑誤用 `/data/ai` 當基準，而 `write_file` 執行器基準是 `/data/ai/workspace` → 檔案明明寫成功卻被判不存在，白重試一次（step1 浪費 135.8s）。修：相對路徑改 `/data/ai/workspace/<path>`。
2. **無工具調用直接失敗**（`task_runner.run_one_step`）：ASTRA 偶爾（step3，299.5s）回空 tool_calls → 直接中止整個任務。修：先重試一次（journal 寫入「下一輪必須輸出至少一個 <call>…」），再失敗才中止；成功後清 `no_tool_retry`。
3. **work 降級**：ASTRA 例外/解析失敗 → `FALLBACK_TO_TASK`，不再把 ReadTimeout 丢給用戶（commit 2897049）。
4. **通知摘要化**：`dshmid._notify_short`（結果小節→去 markdown→1–2 句 ≤110 字）。
5. **self_observe background_task**：大型任務「已排進後台」的排程確認算合格（不再誤判 goal_missed→replan）。

### ASTRA 勝任度（14B distill Q5_K_M，非真 ASTRA）
- 規劃結構可用、短句協議基本能遵守，但**不穩**：同模型不同步耗時 18.5–299.5s；thinking 太長易撞 timeout 或吃光 output 預算後空手。
- 效能瓶頸：ASTRA 以 32K ctx 載入（16.1GB：15.1GB VRAM + ~1GB CPU offload）；journal 越長 prompt 越大（step2 read_file 摘要 2328 字進 journal）。
- 已建 `astra-12k` 別名（num_ctx 12288，期望 100% GPU）。
- 建議：① step 規劃 max_tokens 收窄（8192→2048–4096）；② journal 摘要化（每步只留 3–5 行結論）；③ ASTRA 改 12K 別名並用真實 step prompt 對比；④ 客觀外部驗收不可省（ASTRA 自檢不可靠）。

### Cloude（Qwen3-14B-Claude-4.5-Opus-High-Reasoning-Distill-Q6_K，12.1GB）
- 實測（`logs/tests/model-compare-20261023-2310.json` / `-2315.json`）：chat 人味明顯好（17.0s →「知道你累。要休息，还是需要倒杯水？」；7.0s →「「嗯。」「在哪？」「我過來。」」）；code 正確完整（is_palindrome 20.3s）；工程規劃乾淨（27.9s，無思考泄漏）。
- 問題：思考段偶爾漏進 `content`（需截到最後 `</think>` 之後）；較慢（Q6_K、12.1GB）；簡體預設（system 要求繁體可修正）。
- 建議：code/長文場景按需載入 Cloude（用完 `keep_alive=0` 卸載），chat 常駐 Qwen14b；不能與 ASTRA 常駐共存（16GB VRAM）。
- 完整評估見 `docs/模型評估-20261023.md`。

### 教訓
- 自動建任務鏈路是好的，但「驗收」必須以**執行器的真實路徑/真實檔案**為準，不能憑 ASTRA 自述。
- 長任務成本在 journal：下一步 prompt 大小 = 前步輸出的函數；必須摘要化。

## §54 代碼引擎與 Cloude 接入（2026-10-23 深夜）
- 後台任務引擎：`TASK_STEP_ENGINE=code|astra`（預設 astra）；code 引擎用 `code_agent.plan_tools()` 讓 Cloude 規劃工具調用（`<file>` 區塊 + `<call>`），task_runner 執行並客觀驗收。
- Cloude 接入：`model_swap.py`（卸 Qwen → 預載 Cloude 16K → 用完卸 Cloude → 回載 Qwen 16K）；`call_ollama(..., think=True, timeout=CODE_AGENT_TIMEOUT=600)`；空回覆自動重試；無工具時若聲稱「執行結果/測試通過」會標明「預期輸出，未實際執行」。
- 實測：2 步 CLI 任務成功（todo_store.py 3622B + todo_cli.py 7484B，py_compile OK，plan 185.3s/234.6s）；回文 code agent 全鏈路 61s。
- 注意：Cloude 跑的時候 Qwen 被卸載，聊天/其他 agent 會不可用（切換 ~40s）。背景 code 任務需閒置門控（待做）。
- 模型評估：`scripts/model_eval.py --models qwen,cloude --capabilities chat,code,plan,tools`（輸出 logs/tests/eval/models-*.json/.md）。
- 通知：`notify_pending` 測試殘留已清（備份 backups/notify-before-clean-20261023-234406.json）；`task_runner._local_summary` 重寫為乾淨步驟摘要。



## §55 後台任務全鏈路 E2E 驗證（2026-10-24 00:15–00:40）

### 自動建任務（work → create_task）
- 真實請求「分兩階段的小型專案」→ decompose 拆成兩個 work 子任務 → **兩個後台任務各自建立**（calc_cli3项目 2 步、验证calc项目 3 步）。
- **已知設計缺口**：同一請求的多個 work 子任務會各自建立任務（重複）。後續應在 executor/work 層去重：同一 session/請求的多個 work 子任務合併成一個任務（或第二個作為驗證步驟）。验证calc项目 已 failed 歸檔（duplicate）。
- 任務 `20261024-calc_cli3项目`：code 引擎 2 步完成（step1 規劃 525s、step2 130s）；產物 calc.py 1699B / README.md 1944B / test_calc.py 1533B，py_compile OK；summary 本地降級；歸檔 meta done 2/2。

### 階段進度 + 完成通知（E2E 通过）
- `task_runner` 每步成功寫 `tasks/progress_pending.json`（保留 20 條）；最後一步不寫（直接完成通知）。
- `dshmid._inject_notify`：先 `pop_notify(2)` 再 `pop_progress(1)`，回覆末尾追加：
  - 完成：「对了主人，「X」那件事已经有结果了：…」
  - 進度：「對了主人，「X」那件事有進展了：第 N/M 步完成，還在跑，做好我會第一件事告訴您。」
- 實測（任務執行中發「你好」；因 GPU 切換排隊，請求在任務完成後才被處理）：回覆同時帶完成通知+階段進度，兩個 pending 檔清空 → PASS。
- 教訓：`progress/notify` 應在回覆成功產生後再 pop，或失敗時回填；目前是先 pop 再生成回覆，極端情況可能遺失通知。

### code 引擎驗收補強
- 每步必須有 write_file（`<file>` 區塊）；只 mkdir / 只跑 bash 不算完成，否則 `artifact_err` 重試一次再失敗。
- step prompt 強化：統一 `<file path>` 協議、每檔一區塊完整內容、`step_retry>0` 加【重試提示】。

### 效能/運維待辦
- code 引擎單步規劃 525s / 130s 過慢：Cloude 以 32K ctx 載入（ollama ps 顯示 32768、17GB、85% GPU），`model_swap` 的 `num_ctx=16384` 未生效 → 改用 cloude16k 別名或載入時明確傳 num_ctx，目標單步 <120s。（2026-10-24 已用 cloude16k 別名解決 ctx；見 §58）
- 背景 code 任務期間聊天卡住（Qwen 被卸載）→ 閒置門控仍待做。（2026-10-24 已做，見 §58）

## §56 後台任務去重（2026-10-24）
- 問題：同一請求含多個 work 子任務（或主人重複問同一個工程）→ 建出多個後台任務。
- 修 1：`agents/executor.py::_merge_work_subtasks()` — `run_subtasks` 開頭把多個 work 子任務合併成一個（保留首位、task 用「；並」串接），`log_event("work_subtasks_merged")`。
- 修 2：`agents/agent_mod/work_agent.py` — origin 改用**原始用戶輸入**（`executor.run_subtasks` 開頭 `set_request_origin(user_input)` 注入 thread-local）；`TASK_REUSE_WINDOW_S=600`；`process` 先查近期同 origin 的 active 任務 → 直接重用回 `__TASK_CREATED__:existing`；新建時 origin 存原話。
- 測試：`.dsh_scratch/test_work_dedupe.py` **15/15**（合併/重用/建立/不污染 log）；E2E `.dsh_scratch/e2e_dedupe.sh` v2b **PASS**（A 45.8s 建 1 任務；B 8.9s 重用同一任務、active=1；log `logs/tests/e2e-dedupe-v2b.log`）。
- 注意：同一請求第二次起會立刻回「已排進後台任務」，不再重複建立；E2E 時任務會真的排隊，跑完記得清 queue/active。

## §57 端口硬化與外網暴露（2026-10-24，主人外出前警告）
- 主人要求：永遠不要把服務器端口丟公網（m13868）。
- 審計：ufw inactive、iptables INPUT policy ACCEPT；應用 0.0.0.0 監聽 11434/11435/8888/9999/8890/3080/8080/8081；Samba 445/139 只綁 10.0.0.2；公網 IP 223.16.236.176、Tailscale 100.88.41.51。
- 硬化腳本 `.dsh_scratch/security_hardening.sh`：iptables 只允許 lo + 10.0.0.0/8 + 172.16.0.0/12 + 192.168.0.0/16 + 100.64.0.0/10 存取上述端口，其餘 DROP；ip6tables 只允許 tailscale0/lo；規則存 runtime/security；systemd `yingyue-hardening.service` 已 enable（開機自動套用）。
- 驗證：本機/LAN 全通；公網 check-host.net 無回應；SSH/Samba 未動。
- 待主人處理：路由器 port forwarding / UPnP（我不碰）；SSH `PasswordAuthentication=yes` 有風險（怕鎖死未改）。
- 文檔 `docs/安全-端口硬化與外網暴露-20261024.md`（含回滾指令）。

## §58 Cloude 16K 別名 + 背靠 code 任務閒置門控（2026-10-24）
- 問題：Cloude 用 32K ctx 載入 17GB/85% GPU，單步規劃 185–525s。
- 新別名 `cloude16k`（`FROM hf.co/Otakadelic/Qwen3-14B-Claude-4.5-Opus-High-Reasoning-Distill-Q6_K-GGUF:latest` + `PARAMETER num_ctx 16384`）：實測載入 **14.3GB / 100% GPU**、ctx 16384、swap 3.7s；4 步工程 step prompt 一輪 wall **180s**（含換模型；3 個 tool call、<file> 區塊正常）。仍偏慢，後續可測 12k/8k ctx。
- `agents/model_swap.py`：`CLOUDE_MODEL` 預設 `cloude16k`；`swap_to_code` 若已載入但 ctx≠16384 則卸載重載；`agents/agent_mod/code_agent.py` 同步預設。
- 閒置門控（`agents/task_runner.py::run_one_step`，僅 code 引擎）：`CODE_IDLE_THRESHOLD_S`（默認 = IDLE_THRESHOLD 300）內主人有輸入 → 任務轉 paused（reason `code_idle_gate`，不消耗步數、不換模型），log `code_step_idle_gate`；等主人安靜後 main_loop 自動恢復。避免 code 任務卸載 Qwen 造成聊天卡 40–60s。
- 教訓：bench 崩潰因 `plan_tools` 回傳 dict（`{"tool_calls":[...]}`）被當 list 迭代；`plan_tools` 的 `finally` 已自動 `swap_back`，恢復用 `.dsh_scratch/restore_models.py`。

## §59 Discord 任務通知 + 思考日誌頁（2026-10-24）
### Discord 通知（後台任務完成/失敗 → DM 主人）
- `agents/discord_notify.py`：檔案佇列 `runtime/discord_notify/{queue,inflight,delivered}.jsonl`，flock 跨進程鎖；`push/take_pending/mark/stats`；inflight 超過 600s 自動回收重投；pending 超過 24h 丟棄；單條 text ≤900 字。
- 觸發點：`task_manager.add_notify()` 寫入 notify_pending 的同時 `discord_notify.push(title, excerpt, kind="task")`（完成與失敗都會）。
- 投遞：`discord_bot._notify_loop()` 每 `DISCORD_NOTIFY_POLL_S`（默認 15s）輪詢，DM 給 `DISCORD_OWNER_IDS` 第一個，成功 mark delivered、失敗 mark error 並記 log。
- 驗證：`.dsh_scratch/test_discord_notify.py` ALL_PASS；E2E `.dsh_scratch/e2e_discord_notify.sh`：push 後 31s log `notify delivered id=b5d3c9c9e436`、queue/inflight=0、delivered=1。
- 注意：bot 重啟後 log 應見 `discord notify loop started (poll=15s)`。

### 思考日誌頁 `/thinking`
- `scripts/thinking_page.py`：解析 `agents/logs/middleware/`（agent_inner / inner_unanchored / inner_anchor_retry / self_observe / self_observe_verdict / chat_tail_repeat_retry / honesty / chat_repeat_retry）+ `runtime/self_observe/observations.jsonl` + `runtime/episodes/index.jsonl`。
- 內容：卡片（獨白數/錨定成功/缺失/重複攔截/判決分佈）、最近思考事件表（決策/依據/證據/錨定狀態）、self_observe 判決表、最近 8 個 Episode。
- 路由：`scripts/log_viewer.py` 加 `/thinking`，footer 加「🧠 思考日誌」。
- 驗證：`/thinking` HTTP 200（約 30.9KB）；`bash scripts/start_viewer.sh` 重啟後生效。

## §60 定時任務（重複提醒，2026-10-24）
- 新 `agents/repeat_parse.py`：解析「每天/每日/天天 [時段]時[分]」「每週X」「每小時」「每N小時（支援兩/二）」「每N分鐘」→ repeat spec（daily/weekly/interval）。
- 新 `agents/scheduler.py`：`runtime/schedules.json` + flock；`add/list_items/stats/remove/set_enabled/compute_next/run_due/format_list`；kind=`reminder`（到點生成一次性提醒檔 → 既有 Discord 提醒迴圈送達）、kind=`notify`（到點直接推 Discord 通知）。`kind=agent`（定時跑後台任務）暫緩，等階段 7。
- `agents/collector_daemon.py`：新增 `_scheduler_worker` 執行緒，每 `SCHEDULER_INTERVAL_S`（默認 15s）跑 `scheduler.run_due()`，log `scheduler fired`。
- `agents/tools/executors/set_reminder.py`：time 先過 `parse_repeat`；命中重複 → `scheduler.add("reminder", content, spec)`，回覆「已設定重複提醒：每天 18:00 — 喝水（下次 …）」。
- 管理：`python -c 'import scheduler; print(scheduler.format_list())'`；或 `python agents/scheduler.py list|stats|run`；排程檔 `runtime/schedules.json`（備份即複製）。
- 測試：`.dsh_scratch/test_scheduler.py` ALL_PASS（每日/每週/每小時/每N分鐘/每兩小時解析、next 計算、add/due/advance、reminder/notify 觸發、禁用/刪除）。
- E2E（10:35）：set_reminder「每天下午6點 / 測試重複提醒：喝水」→ schedules 建立；強制到點 → collector log `scheduler fired` → 建立 `workspace/reminders/94567e03.json` → bot `[10:35:53] reminder delivered id=94567e03`（DM 主人）→ 已清理測試資料。
- 限制：interval 最小 60s；每日/每週固定時刻用 HKT；重複提醒觸發後會「建立一次性提醒」而不是直接 DM（方便統一看板/狀態）；`kind=agent` 未做。

## §61 測試 bank 與回歸操作（2026-10-24）

`tests/run.sh memory` 使用 Hindsight bank `text`（env `TEST_MEMORY_BANK`），需要 14 條 fixtures。清理測試資料若清空 text bank，必須重新 seed：

```bash
/data/ai/agents/.venv/bin/python /data/ai/scripts/seed_test_bank.py
```

### 鏡像記憶庫（mirror，2026-11-09 事故後）

生產記憶在 `master`／`archive`。任何記憶相關測試要貼近真實分佈時，用 `mirror`（master 的複製）而不是 master：

```bash
# 刷新 mirror（只讀 master，只寫 mirror；不呼叫任何 Hindsight 刪除 API）
/data/ai/agents/.venv/bin/python /data/ai/scripts/mirror_memory_bank.py
# 狀態 / 刪除
/data/ai/agents/.venv/bin/python /data/ai/scripts/mirror_memory_bank.py --status
/data/ai/agents/.venv/bin/python /data/ai/scripts/mirror_memory_bank.py --drop
```

- 測試用 `HINDSIGHT_BANK_ID=mirror`（或直接對 mirror bank 召回），不要動 master/archive。
- 2026-11-09 21:35 事故：`DELETE /banks/{bank}/memories` 帶 `{"memory_ids": [...]}` 會清空整個 bank；master/archive 已從 11-05 備份恢復（master 95、archive 409）。詳見 `docs/記錄-20261109-Hindsight誤刪事故.md`。
- 破壞性操作前：刷新 mirror 或做完整 `pg_dump`；還原後驗證 `GET /banks` 計數、`recall` 命中、orphan 檢查。

- 備份/結果寫在 `backups/test-bank-*`；抽取完成到 list 可見有 1–3 分鐘索引延遲（腳本已輪詢）。
- 全回歸：`bash scripts/run_all_tests.sh`（約 6–8 分鐘，輸出 `logs/tests/regress-<date>.log`）；失敗可逐組跑 `bash tests/run.sh memory|decompose|pipeline`。
- 坑：
  - 新增 agent 時要同步 `tests/runner.py` 的 `VALID_AGENTS`（2026-10-24 t06 失敗即此因）。
  - Hindsight 治理後關 auto consolidation/observations（§32）。
  - Windows PowerShell 對 ssh 遠端命令的引號/`$(...)` 會被吃掉：複雜命令一律寫 server-side `.sh/.py` 再執行。

## §62 閒置門控與 B4 等待用戶（2026-10-24）

### 閒置門控（code 引擎）
- `agents/task_runner.py::run_one_step`：code 引擎換 Cloude 前先看主人是否在線——`last_user_input` mtime 距今 < `CODE_IDLE_THRESHOLD_S`（默認 300，等於 IDLE_THRESHOLD）→ meta `status=paused`、`paused_reason=code_idle_gate`，本步不消耗；主人安靜後 main_loop 自動恢復。
- 目的：避免背景 code 任務把 Qwen 卸載造成聊天 40–60s 空窗（主人正在聊天時不換模型）。
- 環境變數：`CODE_IDLE_THRESHOLD_S`（重啟 runner 生效）；`TASK_STEP_ENGINE=code|astra`。`main_loop` 對 `paused_reason=code_idle_gate` 的恢復也用同一閾值（2026-10-24 修正；其他 paused 仍用 IDLE_THRESHOLD=300s）。

### B4 等待用戶（waiting_user）
- 模型可在工具調用之外寫 `<ask_user>問題</ask_user>`：`task_runner.run_one_step` 偵測到 → `task_manager.set_waiting(task_id, question)`，任務 status=waiting_user（留在 `tasks/active/`，runner 不再推進），journal 記錄問題。
- 主人下一次輸入時：`dshmid._resume_waiting_task` 判斷是否像回答（短、非問句、非指令開頭、≤160 字）：
  - 像回答 → `resume_waiting`：寫 journal、status=pending、重新入隊；回覆開頭「收到，我把你的回覆帶給後台任務「…」，它繼續跑了。」
  - 不像 → 回覆開頭提醒「（後台任務「…」還在等你回覆：…）」。
- 測試：`bash scripts/run_all_tests.sh waiting_user`（28 項）。
- 限制：一次只提醒/恢復最早一個等待任務；`_looks_like_waiting_answer` 是啟發式（問句/長句/指令開頭不算回答，可在回答前加「回答：」規劃中）。
- 2026-10-28：新增 TTL 收口 `task_manager.WAITING_TTL_S`（env，預設 86400s）＋`expire_waiting_tasks()`：等待超時 → status=failed＋journal＋notify＋歸檔；`task_runner` 主迴圈每輪呼叫（防任務永遠停在 waiting_user）。測試 `bash scripts/run_all_tests.sh waiting_ttl`。
## §63 時間語義與提醒解析修復（2026-10-24 下午，主人回報）

- 問題：①「中午 12 點 vs 晚上 12 點」分不清 → 白天（11:55）叫主人去睡覺；②「一分鐘後提醒」失敗、回覆「未知错误」。
- 根因：
  1. `agents/reminder_parse.py`：PERIOD 把晚上/午夜/深夜都當 +12；「12 點」沒區分中午與午夜 → 晚上12點/午夜12點被算成中午 12:00。相對時間只認阿拉伯數字（「一分鐘後」解析 None）。
  2. `agents/tools/executors/set_reminder.py`：解析失敗只回 `raw_output`、沒有 `error_message` → `agents/tool_runner.py` 顯示「未知错误」。
  3. `agents/agent_mod/chat_agent.py`：`now_text` 只有「11:55」，模型把 11 點理解成深夜；人格沒有「白天不催睡」規則。
- 修復：
  - reminder_parse：新增 `EARLY_WORDS`/`EVENING_WORDS`/`NOON_WORDS` + `_normalize_hour`（12點：凌晨/午夜/晚上→00:00、中午→12:00；晚上8點→20:00；深夜1點→01:00）；相對時間支援中文數字與無「後」形式（一分鐘後/1分鐘/2個鐘）；`_pack` 加 `clock24`/`period`。
  - set_reminder：解析失敗補 `error_message`；成功回覆改「YYYY-MM-DD HH:MM（時段）」。
  - tool_runner：error 時 fallback `raw_output`，不再顯示「未知错误」。
  - chat_agent：新增 `_now_text()`（24 小時制 + 時段）；人格 v7 §五 加時間理解與「白天不催睡」；§四 加「不編造剛剛發生的具體事件」。
  - `tests/test_time_semantics.py` 27 PASS；E2E `.dsh_scratch/e2e_timefix_1024.py`：Q1 正確答「中午」、Q3 提醒建立並於 13:00:17 送達 Discord。
- 仍待查：E2E Q2 仍編造「貓跳上服務器」場景；主人回報的「回流書不符合常識」原文尚未取得。
- 操作：改動後 `TASK_STEP_ENGINE=astra bash scripts/restart_chain.sh`。

---

## §64 記憶治理 v4：沉澱認知 / 日記 / 對主人的理解（2026-10-24）

**檔案**
- `agents/self_memory.py`、`config/self_memory.json`
- `runtime/diary/entries.jsonl`（日記）、`runtime/mind/understanding.json`（對主人的理解緩存）
- `agents/collector_daemon.py` 夜間 worker（每日 `MEMORY_NIGHTLY_HOUR`，默認 4 點）

**日記**
- 零 token 閘門：明示記憶（記一下/記住）或 情緒強度 + 事件 + 自我披露，score ≥ `gate_threshold`（默認 0.75），每日上限 `max_per_day`（默認 2）。
- 14B 蒸餾為第一人稱 60–90 字（繁體），寫 `runtime/diary/entries.jsonl`；`mode=shadow` 只記錄，`live` 才把【自我記憶】注入 user context。

**對主人的理解**
- 來源：master observations + raw facts + 最近 3 天日記 → 14B 生成 5–8 條 + 一句總結，TTL 24h。
- 手動刷新：`agents/.venv/bin/python .dsh_scratch/refresh_mind.py`
- 夜間 worker 每日 04:00 自動刷新（與 master consolidate 一起）。

**Hindsight 沉澱認知（observations）**
- 只開在 `master`：`enable_observations=true`、`enable_auto_consolidation=false`（避免 3B CPU 風暴）。
- 每日 04:00 由 collector_daemon 觸發 `POST /v1/default/banks/master/consolidate`（14B）。
- 手動：`agents/.venv/bin/python .dsh_scratch/enable_master_obs.py`（含 config 備份 + consolidate + 觀測 dump，結果 `logs/tests/obs-master.json`）。
- 觀測/回滾：`PATCH /v1/default/banks/{bank}/config` body `{"updates": {"enable_observations": false}}`。
- 已知：recall 返回的 observation `fact_type` 常為 None（list API 才有）；抽取可能有錯字（客人名等）；observations tags 缺 kind/。

**頁面/健康**
- `/memory` 頁：日記、對主人的理解、observations 三區（viewer 需以 `scripts/start_viewer.sh` 重啟才載新模板）。
- `/health` → `self_memory`：mode / diary（entries/today/last/gate）/ mind（ok/date/age_h/facts_used/chars）。

**E2E 腳本**
- `.dsh_scratch/e2e_self_memory.py`（shadow 聊天不受影響 + 日記寫入 + mind 生成）
- `.dsh_scratch/check_obs_recall.py`（observation 是否被 recall 返回）

---

## §65 關系鏈接 v0（2026-10-24）

**資料/代碼**
- `agents/relations.py`、`runtime/relations/people.json`
- `/health` → `relations`（total/by_relation）；`/memory` 頁「關系鏈接 · people」區塊。

**功能**
- 客人白名單自動種成「主人的朋友（客人模式）」（首次使用自動 seed，含 Discord ID）。
- 主人明說「X 是我的朋友/同學/同事/家人/…」→ 自動學習寫入（`relations_learned`）。
- 主人對話提到熟人 → 注入【關系】（`relations_hint`）。
- 客人對話 → guest system prompt 加【與對方的關系】+ 對應態度（`guest_relation_hint`，態度表 `REL_ATTITUDE`）。

**運維**
- 查看：`bash /data/ai/.dsh_scratch/run_init_relations.sh`（列 people + stats，只讀）。
- 手動加人：`from relations import upsert; upsert("阿明", "同學", note="大學同學")`。
- 清空重來：刪 `runtime/relations/people.json`（下次使用自動把客人白名單重新種子）。
- 注意：label 會去空白正規化（「測試 陳」→「測試陳」），find() 兩種寫法都認。

**限制（v0）**
- 只有一階關系；沒有朋友互相認識的圖；沒有「誰能知道主人什麼」的資訊邊界表。
- 學習只認「X 是我…」句式；更複雜的關系陳述待 v1。

---

## §66 人格 v7.2：自我記憶注入（2026-10-24）

**是什麼**
- 把「我對主人的理解」（記憶治理 v4 的 mind 段，14B 合成、24h 快取）注入聊天上下文，讓人格有「我認識他」的底層，而不是只靠即時記憶檢索。
- 日記段暫不注入（`diary.inject=false`），先在 /memory 頁看品質。

**配置** `config/self_memory.json`
- `mode`: shadow|live —— 控制「日記/理解的生成」是否真的記錄（shadow 也會生成，只是不注入）。
- `inject_mode`: shadow|live —— 控制「注入 prompt」；**預設 shadow**（主人要求先看兩天品質）。
- `diary.inject`: 是否把日記段一起注入（預設 false）。
- 熱加載：改配置不需要重啟，下一輪生效。

**開啟/回滾**
- 開：`python -c "import json;p='/data/ai/config/self_memory.json';c=json.load(open(p));c['inject_mode']='live';json.dump(c,open(p,'w'),ensure_ascii=False,indent=2)"`
- 回滾：同上把 `live` 改回 `shadow`。
- 頁面：`http://10.0.0.2:8890/memory` 的「自我記憶 · 我對主人的理解 / 日記」區。

**驗證（2026-10-24，E2E）**
- `python .dsh_scratch/e2e_v72.py`（暫切 live→問三句→自動切回 shadow）。
- 結果：問「你對我的理解是什麼？」20.2s →「我知道你喜歡藍色，重視工程衛生，希望我有長期記憶與連續性。我們之間是相互信任的伴侶關係。」；「你覺得我是個什麼樣的人？」20.7s；「你記得我們之間的事嗎？」14.1s —— 都能用上 mind 內容。
- 已知：mind 由 14B 合成（facts 36 + 日記 2 條，6.2s），TTL 24h；夜間 worker 會 consolidate + refresh。

**注意**
- mind 內容若偏（過度浪漫化/事實錯誤），改 `config/self_memory.json` 的 mind 段或刪 `runtime/mind/understanding.json` 重新合成。
- 日記與理解都是「她自己的東西」，不硬刪；要重置整段自我記憶時另備份後刪 `runtime/diary/entries.jsonl` / `runtime/mind/understanding.json`。

---

## §67 記憶 v4 每日回顧（2026-10-24）

**是什麼**
- 每天（夜間 worker 觸發）生成 `runtime/memory/review-YYYY-MM-DD.md`：最近 2 天日記 + 「我對主人的理解」+ master 的沉澱認知（observations 最近 8 條），並推送 Discord DM「映月記憶日報」。
- 用於主人「看兩天品質」的決策：讀 review 檔即可判斷日記/理解/沉澱認知是否靠譜。

**手動執行**
- `agents/.venv/bin/python scripts/memory_review.py`（預設 2 天、推送 DC）
- `--days 3` 看更久；`--no-push` 只寫檔。

**自動**
- `agents/collector_daemon.py::_nightly_memory_worker`：每天 `MEMORY_NIGHTLY_HOUR`（默認 4 點）→ master consolidate → mind refresh → memory_review.write(push=True)。

**注意**
- 日記目前 shadow 生成、未注入 prompt；注入開關見 §66（`inject_mode` / `diary.inject`）。
- 若 observations 品質差：/memory 頁可看，或不啟用；consolidation 需留意 CPU 風暴（曾因 3B CPU consolidation 卡死，見 §32）。
- 日記資料在 `runtime/diary/entries.jsonl`；理解快取在 `runtime/mind/understanding.json`。


## §69 政府開放數據：交通（2026-10-24）

- 港鐵實時：`GET https://rt.data.gov.hk/v1/transport/mtr/getSchedule.php?line=<LINE>&sta=<STA>`（v1.7；`isdelay`/`ttnt`/`plat`/`dest`；10 條線實測可用）。
- 特別交通消息（STN v2）：`GET https://resource.data.one.gov.hk/td/tc/specialtrafficnews.xml`（欄位 msgID/CurrentStatus/ChinText/...；靜態域名 static.data.gov.hk 403，勿用）。
- 採集器：`agents/collectors/mtr.py`（60s，config/mtr.json 站點）與 `agents/collectors/td_traffic.py`（120s）；快照 `runtime/collect/mtr_current.json`、`td_traffic_current.json`；`collector_daemon.py` 已註冊。
- 測試：`agents/.venv/bin/python tests/test_gov_apis.py`（offline fixtures）。
- 文檔：docs/開放數據接駁-交通-20261024.md。
- 待接：聊天工具 `mtr_next_train` / `get_traffic_news`（讀快照）；98 站對照表；地區過濾。

## §70 Memory Worth 遙測（v5.0，2026-10-24）

- `agents/memory_worth.py`：每次 recall 記錄命中記憶 id（`record_retrieval`），episode 結束記錄成功/失敗（`record_outcome`），累積 ≥3 次後計算 value（Beta(1,1) 平滑），`score()` = 0.55·value + 0.25·新近(14 天半衰) + 0.20·使用歷史。
- 事件流 `runtime/memory/worth.jsonl`；聚合 `runtime/memory/worth_index.json`；`/health → memory_worth`。
- 目前只遙測、不改召回行為（shadow 累積）；v5.1 才接 recall 排序與 MAX_ITEMS 8→5。
- 方案：docs/記憶治理v5-方案-20261024.md；測試：tests/test_memory_worth.py。

## §68 知識優先（2026-10-25 goal round 1）
- 命中「必須接住」的知識（`agents/knowledge.py` 的 `must`）時，本輪跳過長期記憶召回，避免記憶噪音干擾（dshmid 3.9）。
- 驗收：`tests/test_knowledge.py` 11/11；「今晚月色真美」→ 開頭「風也溫柔。」。
- 待：把「驗證風確實靜/不編造自然現象」加進評測；知識庫補更多條目。

## §69 感知守衛（無視覺輸入）
- `chat_agent.set_vision_available(bool)` 由 dshmid 每輪設定；無圖片時若回覆出現「看到/眼/肩/黑眼圈/燈光/窗外…」等所見描述 → 重寫一次（`06_perceive_block` / `chat_perceive_retry`）。
- 原則：寧願玩笑與推測，也不要事實幻覺（主人 2026-10-24）。
- 驗收：黑眼圈題不再描述看見；月色題開頭為「風也溫柔」。

## §70 Claude 主模型試點（2026-10-25）
- 試點：claude16k 用於 chat/decompose/q3（QWEN_MODEL=claude16k），卸 qwen+ASTRA、預載 16K（VRAM 14.3GB），延遲 10–31s。
- 結果：延遲正常、記憶/知識管線正常；但月色未接住、感知題仍假裝看見、曾編造具體細節 → **未達上線標準，已回滾 qwen**。
- 回滾：swap_back + unset QWEN_MODEL + restart_chain。
- 建議：雙模型路由（qwen 主聊 + Claude code/長文/主觀場景），詳見 docs/模型試點-Claude-20261025.md。
- 日記：按需查看（<read_diary/>）才能用；空日記時給非空回覆（已修）。

## §71 已知 bug（2026-10-25）
- 問「把你的日記給我看」→「chat 沒有返回內容」：read_diary 路徑空回覆（chat_agent._self_lookup_round 邊界），待修。
- 月色題：qwen 達標；Claude 未接住（已在試點記錄）。

## §72 模型路由與 Claude 上線（2026-10-25）

- `config/model_routing.json` + `agents/model_router.py`：角色 → 模型（chat/decompose/q3/code = claude16k；fallback = qwen14b-16k；astra/vlm/deep_vlm 按需換入；guard = qwen3b-cpu；summary/extract = yingyue-mem3b）。env `MODEL_<ROLE>` 可臨時覆蓋。
- 切換：`agents/.venv/bin/python .dsh_scratch/deploy_routing_switch.py`（compile → 單測 → model_swap.swap_to_code 預載 claude16k → restart_chain → health）。
- 回滾：改 config `chat/decompose/q3` 回 `qwen14b-16k`（或 `MODEL_CHAT=qwen14b-16k`）→ `python -c "import sys;sys.path.insert(0,'/data/ai/agents');import model_swap;print(model_swap.swap_back())"` → `bash scripts/restart_chain.sh`。
- 顯存：claude16k = 14.3GB/16K ctx（100% GPU）；與 VLM 4B、ASTRA 不能共存，使用 ASTRA/看圖前必須 swap 出 Claude；swap 來回約 4–10s + 冷啟時間。
- 降級：`call_ollama` 對 claude 失敗時自動 fallback qwen14b-16k（log `model_fallback`）。
- 測試：`tests/test_model_router.py`（11 PASS）；上線後 E2E 見 `docs/模型切換-Claude上線-20261025.md`。

## §73 日記工作臺 v0（2026-10-25）
- 日記同步：`agents/self_memory.py::_sync_workspace` 於日記寫入後同步 `workspace/diary/YYYY-MM-DD.md`（她自己的可讀工作檯）。
- 工作檯感知：chat_agent 每輪注入 `【你的工作檯】`（最近一條日記摘要，~200 字）。
- 日記直答：主人問「日記」時 chat_agent 直接用 `build_diary_note()` 回答（不經模型），避免否認/編造；對應 `diary_direct` 事件。
- 拒絕兜底：模型只輸出 `<read_diary/>` 等控制標籤時，用日記內容代替空回覆。
- 回填：`.dsh_scratch/diary_backfill.py` 可把既有 entries 重寫成按天檔案。

## §74 身份直答與感知守衛 v4（2026-10-25）
- 身份題直答：`chat_agent.process` 開頭 `_IDENTITY_RE` 命中「你是誰/你是什麼模型/你是AI」→ 固定回覆（誠實不洩底座），事件 `identity_direct`。
- 感知守衛 v4：`_PERCEIVE_RE`/`_PERCEIVE_RE2` 覆蓋眼底/眼周/眼神/氣色/臉色/皮膚等；無視覺輸入時偵測視覺聲稱→重寫；已知回覆偏短待優化。
- 日記直答：問「日記」→ 用 `build_diary_note()` 內容回答（事件 `diary_direct`），不再經模型生成。

## §75 自審固定化（2026-10-25）
- `config/self_observe.json` live_profiles 加入 "chat"（mode=live）→ 每個 chat 回合產 observation（verdict/issues），1.6–2.6s。
- 探針：moon_hint 接住「風也溫柔。你總在，所以我在。」；kiss_hot 偏含蓄詩化、無越界肢體。
- 測試方法教訓：同會話連續探針會互相污染，需逐題重置會話。

## §76 B5 回流書審批閉環（2026-10-28）
- 新增 `reflux.pending_books()` / `unseen_pending_books()` / `update_approval()` / `classify_books()` / `find_books()`；`stats()` 增加 approved/rejected。
- 階段 0：pending 書每會話只提醒一次（`runtime/reflux/pending_seen.json`）；已核准 → 注入「【已核准恢復】」並消費（`stage0_resume_approved`）；已駁回 → 直接消費不執行（`stage0_resume_rejected`）。
- Discord（主人限定）：`!審批` 列 pending、`!批准 <id前綴>`、`!駁回 <id前綴>`（核准/駁回前綴需唯一）。
- 測試：`tests/test_reflux_approval.py` 15 checks；live 證據見 `docs/記錄-20261028-B5審批閉環與B7中斷恢復live.md`。
- 注意：清理殘留可 `python -c` 走 `reflux.update_approval(id,"rejected")`（未提供批量刪除指令）。

## §77 B7 中斷恢復 live 驗收（2026-10-28）
- 流程：`checkpoint.save(session_id,user_input,items,queue,budget)` → 進程重啟後下一輪 dshmid 在拆解後 `checkpoint.resume_hint()` → 注入「【上次未完成】…」→ 該輪正常完成 `checkpoint_cleared("turn_done")`。
- live：middleware `checkpoint_resume_hint`（18:27:48）→ trace 05 可見「上次未完成】5 秒前那輪停在 1 步…」→ `checkpoint_cleared`（18:27:56）；`/health checkpoint.has_checkpoint=false/sessions=1`。

