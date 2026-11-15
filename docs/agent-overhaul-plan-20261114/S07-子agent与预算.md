# S07 子agent与预算：改动项

共 23 项。行号按基线 develop a8ec8d81。


基线：`C:\Users\sguser\ryn-worktrees\agent-overhaul-plan`（develop a8ec8d81）。下文路径都相对仓库根。

## S07-I1 子代理通知只在真正写进父历史后才算送达
- 问题：`loop.py:843-852` 每次请求把未读通知临时拼在请求尾部，只取最后 8 条；`loop.py:666` 在 finish 里调用 `drain_subagent_notifications_in_tx(data, row)` 不带 target_ids，把全部未读（含第 9 条以前没展示的、模型请求期间新到的）都标成已送达。通知从没写进 `row["messages"]`，下一步或压缩后父代理再也看不到。Codex 把代理间消息作为历史条目写入，落盘后才算送达（core/src/session/mod.rs:4330-4423）。
- 涉及行：01-M37, 01-M140, 06-M99, A03-N3, A04-N1, 07-M67
- 要改：
  - `rynmesh/agent_runtime/subagents.py:1039-1050` `drain_subagent_notifications_in_tx` —— 保持接口；新增 `pending_for_request(row) -> list[dict]`：取全部未读，超过 8 条时前面的合并成一条摘要（每条 昵称/路径/状态，一行），最后 8 条给完整标签；返回本次要展示的通知 id 集合和渲染文本。
  - `rynmesh/agent_runtime/loop.py:843-852` `_context` —— 改调 `pending_for_request`，尾部消息加私有键 `subagent_notification_ids`（`prompt.py:116-118` 的 `provider_messages` 只放行固定键，私有键不会发给模型）。
  - `rynmesh/agent_runtime/loop.py:621-666` `_model_turn` 的 `finish` —— 从本次请求的 `messages` 里取那条尾部消息的 id 集合；在 `row["messages"].append(...assistant...)`（:638）之前插入一条持久消息 `{"role":"user","content":<同样文本>,"ui":{"kind":"subagent_notification"},"context_epoch":...}`；:666 改为 `drain_subagent_notifications_in_tx(data, row, target_ids=shown_ids)`，没展示的留到下次。
  - 同一 finish 的 complete 分支（:672-677 旁）—— 若决定结束但还有未读通知（请求期间新到），仿 plan_reconciliation 把状态改回 queued 并记 `turn_decision action=continue reason=subagent_notification`，下一步立即注入（对应 07-M67）。
  - `rynmesh/agent_runtime/loop.py:2109-2120` ryn_wait_agent 分支保持按 target 取出；:2117-2119 改成同时把父队列里同 id 的通知标已送达（现在只改了子行 last_notification 的副本）。
- 新增测试：`tests/test_agent_subagents.py`：(1) 10 条未读，一次请求后历史里有 1 条通知消息，含 2 条摘要 + 8 条标签，全部标已送达；(2) 模型请求期间新到 1 条通知，finish 后它仍为未读，且任务回到 queued 再跑一步；(3) 压缩后通知仍在 checkpoint 之前的历史里可被摘要；(4) 发给模型的消息里没有 `subagent_notification_ids` 键。
- 依赖：无（S14 需要把 `ui.kind=subagent_notification` 渲染成系统卡片或隐藏）。
- 兼容和风险：旧任务队列里已标已送达的通知不补发。新增的 user 角色消息不是主人输入：查找"最后一条主人消息"的地方（`loop.py:771-773` `_refresh_plugin_catalogs_at_turn_start`、`loop.py:788-792` `_context` 的 last_user_idx）要跳过 `ui.kind=subagent_notification`，否则会误判新回合。前缀缓存：通知进历史后不再每次变动尾部，反而更稳。
- 工作量：中
- 影响：高

## S07-I2 子代理用量计入整棵树，根任务硬上限管住全部后代
- 问题：`subagents.py:719-721,754-757` 子代理复制父任务的 limits 和 limit_sources 后各算各的；`budget.reserve`（`budget.py:247-271`）和 `budget.settle`（`budget.py:274-320`）只看本行记录。主人设了硬上限时，每个子代理都能再花一整份；已结束的子代理会被自动关闭腾位（`subagents.py:623-631`），所以总花费没有上限。预算提醒（`budget.py:341-358`）只看本任务步数，没有树级和时间提醒。Codex 整棵树共享 rollout 预算（core/src/agent/control/budget.rs:12-17）。
- 涉及行：01-M9, 01-M11, 01-M50, 01-M74, 02-M92, 06-M127, 08-M40, 34-M117
- 要改：
  - `rynmesh/agent_runtime/budget.py` 新增 `tree_totals(data, row)`：找 `subagent_meta.root_task_id` 对应的根行，沿 `subagent_ids` 自行遍历（口径同 `subagents.py:525-542` `_collect_tree_subagents`，含已关闭；不能 import subagents，因为 `subagents.py:13` 已 import budget，会循环导入）把根和全部后代的 `_totals(..., lifetime=True)` 相加，不另存副本，避免双写。
  - `budget.py:247-271` `reserve` —— 增加可选参数 `tree=None`（调用方传 `tree_totals` 结果和根行）：根行某项是硬上限（`is_hard(root, key)`）且 树合计 + need 超过根上限时，抛对应的 `EXHAUSTED_CODES`（如 agent_token_budget_exhausted）；默认（软）上限不做树级拦截。
  - `rynmesh/agent_runtime/runtime.py:823-846` `_meter_model.admit(data)` —— 行有 `subagent_meta` 时先算 tree 再调 reserve；:832 的 available 计算同时扣掉树合计。
  - `rynmesh/agent_runtime/loop.py:1618` 和 `loop.py:1786` 工具 reserve —— 同样传 tree。
  - `budget.py:341-358` `notice` 改为 `notice(row, tree=None)`：树级 token/模型调用剩余 ≤40%/≤15% 各提醒一次，按 `max_total_seconds` 硬上限用到 60%/85% 各提醒一次（02-M92）；`budget_notice_level` 分键存（steps/tokens/seconds），只有在写进工具结果消息的同一事务里才更新（现有 `loop.py:1703-1706`、`loop.py:1890` 已是同事务写入，保持）。`loop.py:743-745` `_budget_notice` 改收 data。
  - 根任务树超限时：`budget.stop_for_limit`（`budget.py:384-409`）停当前子代理；根任务下次 reserve 自然失败。不在子代理里直接停根，避免跨行抢锁。
- 新增测试：`tests/test_agent_runtime_budget.py`：(1) 根任务 owner 设 max_total_tokens=10000，两个子代理各用 6000，第二个的下一次 reserve 抛 agent_token_budget_exhausted；(2) 默认软上限时子代理不受树级拦截；(3) 已关闭子代理的用量仍计入；(4) 树级 60% 提醒只出现一次，且写在工具结果的 server_note 里。
- 依赖：无
- 兼容和风险：存储格式不变（树合计现场计算）；旧任务没有 root_task_id 时退回本行计算。每次 reserve 要遍历树，树最多十几行，开销可忽略。界面预算面板仍显示本任务用量，若要显示树合计归 S14。
- 工作量：中
- 影响：中

## S07-I3 spawn 时校验子模型和推理强度，支持子代理默认模型
- 问题：`subagents.py:155,381` 的 model_id 只做 1-128 字符检查，`:669` 原样写进子任务，错名要到子代理第一次请求才以 agent_model_unavailable 失败。`:156,382-384` 强度只认 low/medium/high；`:670,760-761` 子代理继承父任务的 reasoning_effort。子模型不是 codex_cli 或没声明该档位时，`runtime.py:658-661` 在首个请求抛 agent_reasoning_effort_unsupported，它不在 `runtime.py:90-92` 的可重试集合，子代理直接失败。另外 `subagents.py:771` 无论子模型是否换了都复制父任务的 model_destination（`runtime.py:516` 会按子 model_id 比对目的地，按代码推断换模型时 peer/项目目的地会报 agent_model_destination_changed）。界面只有一个全局默认模型（`webapp/src/screens/ModelPool.tsx:212-220`）。
- 涉及行：01-M14, 01-M24, 01-M26, 01-M84, 09-M53, 52-M131, A01-N2, A22-N1, B07-27, 26-M29
- 要改：
  - `rynmesh/agent_runtime/store.py:37-47` `DEFAULT_SETTINGS` 加 `default_subagent_model_id: ""`、`default_subagent_reasoning_effort: None`；`runtime.py:1256-1290` `update_settings` 校验：模型须 available 且 agent_supported（同 :1274-1276），强度须在 `llm_package/reasoning.py:4` EFFORTS 里。
  - `subagents.py:156` schema 去掉 enum，改为字符串；`:382-384` 只用 `llm_package.reasoning.validate_effort` 检查名字。
  - `rynmesh/agent_runtime/loop.py:2021-2022` `_internal_tool`（事务外，可用 `self._model_metadata`，`runtime.py:1360-1363`）对 ryn_spawn_agent 解析：model = 参数 > `default_subagent_model_id` > 父任务 model_id（fork_turns=all 时忽略参数和设置，沿用父模型，26-M29）。查不到、不可用或 agent_supported 不是 True 时抛 `AgentToolRejected("agent_subagent_model_unknown")`，correction 列最多 5 个可用模型 id（取 `self.models()` 中 available 且 agent_supported 的，按 id 排序）。
  - 同处解析强度：显式参数不支持（`reasoning.validate` 抛错，`rynmesh/agent_runtime/reasoning.py:8-17`）→ 拒绝并在 correction 列出该模型 `supported_reasoning_efforts`；继承来的父强度或设置强度不支持 → 静默丢弃（01-M24、01-M26）。结果写进 nargs 的 `resolved_model_id`、`resolved_effort`、`effective_effort`（无显式时取模型 `default_reasoning_effort`，01-M84）。
  - `subagents.py:669-671,760-762` `spawn_agent_in_tx` —— 用解析结果；`meta["display_effort"]` 写真实生效值，不再写假的 "medium"/"low"。
  - `subagents.py:771-773` —— 子模型与父不同时 model_destination 改用 `runtime._destination(子模型)`（事务外解析好传入），并且不复制 `model_destination_confirmed`。
  - `webapp/src/screens/ModelPool.tsx:212-220` 旁加"子 agent 默认模型"选择（写 default_subagent_model_id）；压缩模型入口归 S03，这里不做（B07-27 后半）。
- 新增测试：`tests/test_agent_subagents.py`：(1) 错误 model_id → 当场 agent_subagent_model_unknown，correction 含 ≤5 个 id；(2) 父任务 effort=high，子模型为 openai 兼容线路 → 子任务不带 reasoning_effort，首个请求不失败；(3) 显式 effort=xhigh 且子模型不支持 → 拒绝并列出支持档；(4) 设置了 default_subagent_model_id 且模型没传 model_id → 用设置；(5) fork_turns=all 时忽略 model_id；(6) 换模型后子任务 model_destination.catalog_model_id 等于子模型。
- 依赖：无（S14 做设置界面文案）。
- 兼容和风险：新设置项要加进 `update_settings` 允许集合（`runtime.py:1257` 拒绝未知键）。旧子任务不受影响。会让以前"能派出去但跑起来才失败"的调用变成当场拒绝，属于期望行为。
- 工作量：中
- 影响：高

## S07-I4 ryn_wait_agent：有结果即返回，超时范围对齐
- 问题：描述写"省略 targets 就等任一子代理"（`subagents.py:197-199`），实现 `loop.py:1963-1965` 要所有目标都不在 queued/running 才返回，一个卡住就要等满超时。超时 schema 1-120（`subagents.py:210`），超过 120 被 `loop.py:2015` 的 jsonschema 校验拒为不带说明的 agent_invalid_control_proposal（行里说"悄悄改成 30"不对：:416-418 的改 30 只在绕过 schema 时生效）。叶子子代理没有直接子代理时，`subagents.py:552` 把 all 退回整棵树，会去等兄弟和父代理。Codex 等待 10 s-1 h、默认 30 s，任一目标到终态即返回（core/src/tools/handlers/multi_agents/wait.rs:123-219）。
- 涉及行：02-M93, 09-M43, 09-M44, 09-M70, 10-M79, A01-N1, A06-N1, G22-23
- 要改：
  - `subagents.py:210` timeout_seconds 改 `minimum 1, maximum 3600`，描述写"默认 30，10-3600"。
  - `subagents.py:416-418` —— 不合法类型报 `agent_invalid_tool_arguments` 并给 correction；小于 10 抬到 10，返回 `timeout_note`。
  - `loop.py:2014-2022` —— 子代理工具先跑 `validate_subagent_tool` 再跑 jsonschema，让越界值拿到带 correction 的拒绝，而不是笼统的 agent_invalid_control_proposal。
  - `loop.py:1947-1967` `_check_wait_state` —— done 条件改为：任一目标在父任务 `subagent_notifications` 里有未读通知，或所有目标都不在 queued/running。初始检查（:1970）同样适用，已读过的旧完成不会让它立即返回。
  - `loop.py:2122-2141` 结果加 `still_running`（仍在跑的目标昵称/路径）和 `timeout_note`；`timed_out` 布尔值标出是否超时返回。
  - `subagents.py:545-556` `resolve_subagent_targets` —— 目标是 all 且调用者没有直接子代理时返回空列表；`loop.py:1961-1962` 空列表时结果写 "no subagents to wait for"。
  - `subagents.py:197-199` 描述改成与实现一致。
  - 等待机制（E102-3）：现在 `loop.py:1981-1990` 在 worker 线程里每 50 ms `store.inspect` 一次，有排队子任务时在等待线程里递归 `self.run_once()`（`runtime.py:3190`），而 run_once 会认领任意排队任务，不一定是要等的子代理，还可能再认领另一个在等待的父任务，形成嵌套。只释放运行槽（`loop.py:1979`）不释放线程：`routes.py:85-87` 只有 6 个 worker 线程，对应 `runtime.py:261` 的 6 个槽。几个父代理同时长等待，就只能靠这种嵌套去跑子代理。改为"挂起"而不是"阻塞"：
    - `loop.py:2021-2024` 初始检查未满足时，不进 `_wait_for_subagents`；在 apply 事务里给父行写 `row["subagent_wait"] = {call_id, targets, deadline（now()+timeout 的 ISO 墙钟时间，重启后仍有效）, started_at}`，保留 queue[0] 这次 wait 调用，状态回 `queued`，本步结束，线程立即释放。
    - `subagents.py` 新增 `wait_ready(data, row)`：没有 subagent_wait、已到 deadline、有 `pending_user_messages`/`pending_control`、或满足上面的 done 条件时返回 True。
    - `runtime.py:3239` claim 里在 `retry_policy.ready(row)` 旁加 `subagents.wait_ready(data, row)`，未就绪的父任务跳过；`runtime.py:3205-3212` 的 peek 里 `queued` 同样只算就绪的，避免空转事务。
    - 重新认领后再次执行同一个 wait 调用，这时初始检查即满足，按上面规则组装结果并清掉 `subagent_wait`；超时返回时 `timed_out: true`。等待时间不计入父任务 elapsed_seconds（沿用 `loop.py:2058` 扣 wait_elapsed 的口径）。
    - 删除 `_wait_for_subagents` 的轮询循环和递归 run_once（`loop.py:1974-1996`）。子任务由普通 worker 认领。
  - 目标解析越权（G22-23）：`subagents.py:545-591` `resolve_subagent_targets` 新增参数 `scope`（`"tree"` 默认 / `"descendants"`）。第二步按名字的树内兜底（:571-585）遍历整棵树（`_collect_tree_subagents`，:515-538），只排除调用者自己，所以会命中兄弟、堂兄弟，也会命中调用者的父子代理。`close_agent_in_tx`（:922）、`interrupt_agent_in_tx`（:893）改传 `scope="descendants"`：只在调用者的后代里找（沿 `subagent_ids` 向下走，新增 `_collect_descendants(data, row)`），找不到抛 `agent_subagent_not_found`，correction 写"只能关闭/中断自己的子代理"。`send_message`/`followup_task`/`wait` 保持 `tree`：按路径给树内其他代理发消息是 :571 注释写明的用法，Codex 也允许；followup 拒绝父代理由 I8 处理。根任务（无 `subagent_meta`）的后代就是整棵树，行为不变。
- 新增测试：`tests/test_agent_subagents.py`：(1) 两个子代理，一个完成一个仍在跑 → wait 在完成后返回，still_running 含另一个；(2) timeout_seconds=600 被接受；(3) timeout_seconds=5 被抬到 10 并带 timeout_note；(4) 叶子子代理 wait all → 立即返回空且说明原因；(5) 已读过的完成通知不触发立即返回；(6) 父任务 wait 挂起后 run_once 不认领它，子代理完成后认领并返回结果；(7) 6 个父任务同时 wait，各自的子代理仍能被 worker 跑完（不再依赖嵌套 run_once）；(8) 挂起期间主人插话 → 父任务立即可认领，结果 steered=true；(9) 节点重启后 subagent_wait 仍有效（deadline 用墙钟）。；(10) 叶子子代理 close/interrupt target=all → 返回空或 not_found，兄弟状态不变；(11) 子代理按名字 close 自己的父子代理 → agent_subagent_not_found；(12) 根任务经路径 close 孙代理仍成功；(13) 子代理 send_message 按路径发给兄弟仍成功。
- 依赖：I1（未读通知的判定共用同一队列语义）；I17（中断子代理时正确结算，wait_ready 依赖子任务状态）。
- 兼容和风险：挂起期间父任务状态是 queued，界面会显示成"排队"，需要在 `public_task` 投影 `waiting_for_subagents`（`runtime.py:136-139` 附近）让 S14 显示"等待子代理"。旧版本留下的 subagent_wait 字段在旧代码里会被忽略，wait 调用会照旧阻塞执行，不会出错。单线程测试调用方（原来为它们做了递归 run_once）要改为显式多次 run_once。close/interrupt 变严后，原来靠"子代理关兄弟"的调用会被拒并带 correction；根任务的操作不变。
- 工作量：中
- 影响：高
- 本轮修订：补上 E102-3，等待从"worker 线程里 50 ms 轮询加递归 run_once"改为挂起父任务、由 claim 门控唤醒；原"延长到 1 小时不占 worker"的前提不对，因为占着线程，已改正。
- 本轮修订（F2）：补 close/interrupt 只能作用于后代（原版只覆盖 all 与 wait，按名字兜底仍可关到父子代理和兄弟）。

## S07-I5 父代理上下文里列子代理名册和可选子模型目录
- 问题：`prompt.py:226-231` 非子代理任务只附通用 `MULTI_AGENT_COORDINATOR_GUIDANCE`（`subagents.py:97-104`），没有名册；压缩后父代理只能再调 ryn_list_agents 才知道有哪些子代理和 ID。ryn_spawn_agent 有 model_id 参数，但模型不知道可选值。Codex 在环境上下文列直接子代理（最多 8 个、≤1024 字节，core/src/session/world_state.rs:40-41,137-170）和可选模型目录（≤1000 字节，core/src/context/world_state/model_catalog.rs:35-89）。
- 涉及行：01-M7, 01-M49, 01-M78, 07-M109, 07-M120, 03-M122, 03-M123, 07-M116
- 要改：
  - `prompt.py:188` `authority(row)` 只拿到 row，读不到子任务行。在父行维护精简名册 `row["subagent_roster"]`：`{child_id: {agent_path, nickname, role, status, closed}}`，在 `subagents.py:785-796` `spawn_agent_in_tx`、`subagents.py:1431-1443` `on_subagent_settled_in_tx`（已拿到 parent_row）、`subagents.py:922-950` close、`subagents.py:983-1036` resume 里更新；只在这些点变，避免频繁打破前缀缓存。
  - `prompt.py:226-231` 非子代理分支：在协调指引后附 `<subagents>`，只列 `row["subagent_ids"]` 里的直接子代理，未关闭在前、按 agent_path 排序，最多 8 行、总 ≤1024 字节，每行 `- <agent_path>: <昵称> (<role>, <status>)`，放不下写"更多见 ryn_list_agents"。
  - 可选模型目录：`loop.py:828-836` `_context` 已在每次请求解析 `model_profiles.for_task`；同处取可用于子代理的模型（available 且 agent_supported，按 id 排序，最多 8 个，每行 id + 一句能力 ≤250 字符，总 ≤1000 字节）放进 prefix_row 私有键 `_subagent_model_catalog`，`authority` 渲染在名册后。模型列表变化时随请求重算，不需要撤销通知。
- 新增测试：`tests/test_agent_subagents.py`：(1) spawn 2 个后 authority 含两行名册，格式和排序正确；(2) 12 个直接子代理时只列 8 行且 ≤1024 字节；(3) 孙代理不出现在根的名册里；(4) 子代理自己的 authority 不含名册；(5) 模型目录 ≤1000 字节且只含 agent_supported 模型。
- 依赖：I3（目录里的模型与 spawn 校验口径一致）。
- 兼容和风险：旧父任务没有 subagent_roster 时按 subagent_ids 显示空名册，下次 settle 补上；不需要迁移。authority 变长约 1-2 KB，`tool_menu` 的 authority_bytes（`runtime.py:400-402`）会自动算进去。
- 工作量：中
- 影响：中

## S07-I6 委派模式设置：默认只在明确要求或明显可并行时派子代理
- 问题：`prompt.py:32` 系统提示无条件鼓励"to delegate subtasks in parallel"，`prompt.py:226-231` 每个顶层任务都附鼓励委派的 `MULTI_AGENT_COORDINATOR_GUIDANCE`，没有开关。小任务也会被拆给子代理，多花步数和费用。Codex 默认 ExplicitRequestOnly（protocol/src/config_types.rs:338-372），有三种模式（core/src/context/multi_agent_mode_instructions.rs:16-57）。
- 涉及行：03-M75, 03-M126, 06-M115, 21-M64, 26-M20
- 要改：
  - `store.py:37-47` `DEFAULT_SETTINGS` 加 `subagents_mode`，取值 `proactive` / `explicit` / `off`；`runtime.py:1256-1290` `update_settings` 校验枚举。默认值建议 `explicit`（与 Codex 一致），文案为"只在主人或技能明确要求，或任务有多个相互独立且耗时的部分时才派子代理"；默认值请老板确认，机制不受影响。
  - `prompt.py:32` —— 把"to delegate subtasks in parallel"改成中性的工具清单，并按 `prompt.py:56-84` 的做法保留旧文本常量 `_PRE_SUBAGENT_MODE_SYSTEM` 供迁移识别；`prompt.py:17` PROMPT_VERSION 升一级。
  - `subagents.py:97-104` 拆成 `COORDINATOR_GUIDANCE_PROACTIVE`（现文案）和 `COORDINATOR_GUIDANCE_EXPLICIT`；`prompt.py:229-231` 按设置选择（`loop.py:799-836` 构造 prefix_row 时带入设置值）。
  - `off`：`runtime.py:425` `_menu` 传入的内部工具去掉 `SUBAGENT_TOOL_SPECS`；`loop.py:443-454` router_specs 同样不补；`loop.py:2021` 收到子代理工具时拒绝 `agent_subagents_disabled`。已存在的子代理照常结算。
  - 主人选最高推理强度（`row["reasoning_effort"]` 为 xhigh/max/ultra）时按 proactive 处理（06-M115）。
- 新增测试：`tests/test_agent_subagents.py`：(1) 默认设置下 authority 用 explicit 文案；(2) proactive 用现文案；(3) off 时菜单和 router 都没有 ryn_spawn_agent，调用被拒；(4) 固定了旧 SYSTEM 的任务按 PROMPT_VERSION 迁移到新文本，自定义 base 不动。
- 依赖：无（S14 加设置开关）。
- 兼容和风险：改系统提示会使旧任务的前缀缓存失效一次。默认改成 explicit 会减少主动委派，是产品取舍，默认值由老板确认。
- 工作量：小
- 影响：中

## S07-I7 子代理工具描述和角色指引补全
- 问题：`subagents.py:133-140` 的 spawn 描述只有一句用途和 fork 说明，没有 Codex 的四节规则（先定关键路径、阻塞任务自己做；子任务具体自包含、写集不相交；少调 wait、不重做；core/src/tools/handlers/multi_agents_spec.rs:692-770）。没有"Available roles"清单（`:148` role 只有 enum），没有 fork_turns 取舍说明（`:149`），协调指引不给并发数和深度（`:87-88` 的数字只在报错时出现），不说所有子代理共用同一工作区，不说信任 explorer 结果和复用已有子代理。awaiter 指引（`:335-339`）缺"不改写任务、没有退出状态不报完成、被问状态报完继续等"。
- 涉及行：09-M50, 01-M135, 01-M136, 06-M114, 26-M26, 26-M27, 09-M52, 09-M54, 52-M5, 52-M6, 52-M7
- 要改：
  - `subagents.py:133-140` ryn_spawn_agent 描述（Ryn 自己的措辞）：关键路径和阻塞下一步的工作自己做；每个子任务写清目标、路径、约束、要报告什么；worker 写集不相交；不要重做已委派的工作；少调 ryn_wait_agent；只有主人或技能明确要求才设 model_id/reasoning_effort（与 I3 一致）。"Available roles:" 每角色一句：explorer 只读快查可并行、结果可信可复用；worker 必须给 write_scope；awaiter 只用于长测试/构建；reviewer 见 I13；default。
  - `subagents.py:148-149` 参数加 description：role 不需要时省略；fork_turns none=只看 prompt，all=继承对话文字（见 I9）。
  - `subagents.py:165,181` send_message / followup 描述加"后续任务高度依赖某子代理已有上下文时复用它，不要新派"。
  - `subagents.py:97-104` 协调指引补：最多 `MAX_OPEN_SUBAGENTS` 个同时打开（已结束未关闭的也算，满时自动关最早结束的），深度上限 `MAX_SUBAGENT_DEPTH`；所有子代理与你共用同一工作区，编辑立即可见；信任 explorer 结果，相关问题复用已有 explorer。删掉与 spawn 描述重复的句子。数字用常量拼接。
  - `subagents.py:335-339` awaiter 文本加三句："Do not change, reinterpret or optimize the task; keep waiting until a terminal state or an explicit stop." "Keep calling ryn_process_read; never report completion without an exit status." "If asked for status, report it and keep waiting."
  - `rynmesh/agent_runtime/tool_menu.py:210-211` 子代理配额 3600 字节按新描述的实际字节数调整。
  - 长度上限（E104-65）：`subagents.py:147`（spawn prompt）、`:172`（send_message message）、`:188`（followup prompt）、`:271`（resume prompt）的 maxLength 8000 提到 32000。这些字段是模型写的任务说明，不进工具 schema 体积，不影响 `tool_menu` 配额。
- 新增测试：`tests/test_agent_subagents.py`：(1) SUBAGENT_TOOL_SPECS 总字节不超过新配额；(2) 协调指引里的数字等于常量；(3) awaiter role_guidance 含三句新规则；(4) 20000 字的 spawn prompt 被接受，32001 字被拒；现有 `ALL_INTERNAL_SPECS` 大小测试同步更新上限。
- 依赖：I6（"未经明确要求不派"的策略句放在 I6 的模式文案里）；I13（reviewer 角色说明）。
- 兼容和风险：`assert_schema_unchanged`（`loop.py:2004-2013`）对排队中的旧调用可能报一次 agent_tool_definition_changed，重试即可。描述变长，小窗口模型仍受 `tool_menu.py:147-151` 现有规则保护。
- 工作量：小
- 影响：高

## S07-I8 子代理可以给父代理发消息，消息带来源信封
- 问题：`subagents.py:545-552` 解析目标时 `_collect_tree_subagents` 不含根，子代理不能给根父代理发消息，只能结束时汇报。`subagents.py:813-826` send_message 和 `:845-859` followup 把父消息原样当作 user 消息；followup 还经 `owner_update`（`:859`）改写子任务目标，子代理分不清是父代理还是主人。`:814-815` target='all' 只发给第一个匹配者，不报错。Codex 用带收发路径和类型的信封（core/src/agent/control/delivery.rs:19）。
- 涉及行：01-M45, 09-M51, 01-M55, A06-N2, G22-24, G22-31, G26-43
- 要改：
  - `subagents.py:545-556` `resolve_subagent_targets` —— 调用者是子代理时，`parent`、`/root`、父的 agent_path 解析到父行（带标记返回）。
  - `subagents.py:813-842` `send_message_in_tx` —— 目标是父行时，不进父的 `pending_user_messages`，而是追加到父的 `subagent_notifications`（`kind: "message"`，带发送者昵称/路径），由 I1 的流程在父的下一次请求写入历史，不开新回合；目标 'all' 时拒绝 `agent_invalid_tool_arguments`（correction 说明逐个发送）。
  - `subagents.py:845-890` `followup_task_in_tx` —— 目标是父行时拒绝。
  - `subagents.py:819-820,851-852` —— 发给子代理的内容外包 `<agent_message from="<发送者 agent_path 或 /root>" kind="message|new_task">…</agent_message>`；`format_notification_tag`（`:1053-1064`）为 kind=message 的通知输出 `<agent_message>`。
  - `subagents.py:165` 描述补"可按路径给其他代理发消息，target=parent 发给父代理"。
  - 主人 @昵称 路由（E103-10）：`runtime.py:2044-2068` 的 try 块用 `except Exception: pass` 吞掉所有错误（如子代理已关闭时 `send_message_in_tx` 抛 agent_subagent_closed，`subagents.py:817-818`），消息只进了父任务，主人以为已送到。改为：只捕获 `AgentToolRejected`/`AgentError`，给父任务记事件 `subagent_route_failed`（token、错误码），并把失败列表放进本次返回的 `public_task`（新字段 `subagent_route_failures`，只在本次响应里有）；匹配到已关闭的子代理时同样记失败（原来 `:2061` 静默跳过）。其他异常照常抛出，不再吞掉。界面在发送后提示"@X 未送达：已关闭"（S14）。
  - 单目标工具（G22-24）：`subagents.py:814-815`、`:846-847`、`:985-986` 三处 `targets[0]`，加上 `:554-556` 的 all 展开。新增 `_single_target(data, row, target, tool)`：target 为 all/空或解析出多个 → `AgentToolRejected("agent_invalid_tool_arguments")`，correction 说明逐个发送；解析出零个 → `agent_subagent_not_found`，列出当前可用昵称。`send_message_in_tx`、`followup_task_in_tx`、`resume_agent_in_tx` 都改用它。原来空列表时 `targets[0]` 抛 IndexError，上层（`loop.py:2096-2103` 一带的工具事务）是否把它转成可读错误未确认，这里直接消除。
  - 约束来源（G22-31）：`state.py:145-151` `owner_update` 注释写明"只在已认证的主人入口之后调用"，却被 `subagents.py:857-859`（followup）和 `:997-998`（resume）用来把父模型写的文字存成 `source="user"`、`required=True` 的目标约束，且 `goal["constraints"]` 每次追加不替换。新增 `state.parent_update(row, message, parent_path)`：同样 `record_goal_history`、revision+1、重置 criteria/plan，约束项 `source="parent"`、`required=True`；先删掉该子代理之前所有 `source=="parent"` 的约束再追加新的（followup 是换任务，不是叠加）。两处调用改用它，主人约束（source=user）不动。实现时 `grep "source"` 核对读取约束来源的地方（提示词和完成检查），对 `parent` 来源按必须项处理。
  - @ 昵称路由支持中文（G26-43）：`runtime.py:2046` 的 `re.findall(r"@([A-Za-z0-9_-]+)", content)` 改为 `r"@([\w-]+)"`（Python 3 的 `\w` 含中文）。`:2058` 的 `tok_low not in {nick_low, tname_low}` 保持精确匹配：`@张三的方案` 取到整段"张三的方案"，匹配不到昵称就不路由也不报失败（只对精确匹配到却已关闭或发送失败的报 `subagent_route_failed`）。昵称 `NICKNAMES`（`subagents.py:19`）是英文，中文名靠 I20 让 task_name 保留中文后才能被 @。
- 新增测试：`tests/test_agent_subagents.py`：(1) 子代理 send_message target=parent → 父队列多一条 kind=message 通知，父状态不变；(2) followup target=parent 被拒；(3) target=all 被拒；(4) 子代理收到的父消息带 from="/root" 信封；(5) 主人消息 @已关闭子代理 → 返回 subagent_route_failures 且有事件，消息仍进父任务。；(6) followup target=all、resume target=all、send_message 零候选 → 都是 AgentToolRejected，不是 IndexError；(7) followup 后子代理 goal.constraints 里是 source=parent 且旧的 parent 约束被替换，主人约束保留；(8) 主人消息 `@回归` 且存在 task_name=回归 的子代理 → 路由成功；`@张三的方案` 不匹配任何昵称 → 不路由、不报失败。
- 依赖：I1（父端写入历史的路径）。 另依赖 I20（中文 task_name），@ 中文名才有对象。
- 兼容和风险：信封改变子代理看到的文本，旧任务不受影响。父代理在 ryn_wait_agent 时，按 I4"有未读通知即返回"会被子代理来信唤醒，是期望行为。约束来源改成 parent 后，已有子任务里旧的 source=user 约束保持原样；若有逻辑把 source!=user 的约束当可忽略项，会漏掉 parent 约束，实现时逐处核对。
- 工作量：小
- 影响：中
- 本轮修订（F2）：补三项：followup/resume 也拒 all 并把空结果报成 not_found（原只改了 send_message）；followup/resume 不再冒充主人写 source=user 约束；@ 路由正则支持中文。

## S07-I9 fork_turns=all 正确继承父历史并控制大小
- 问题：`subagents.py:684-695` fork 时只复制有文字的 user/assistant 消息，工具调用和结果全丢；不看父任务的 `context_checkpoint`，被压缩的旧消息也原样复制；没有大小上限，子代理第一次请求就可能超窗口或立即压缩。`FORK_ISOLATION_PROMPT`（`:90-95`）说"继承的工具调用是历史记录"，与实际不符。Codex 父有新式压缩时沿用父基线（core/src/agent/history.rs:253-268），历史按最后 N 条用户消息和助手文本预算裁剪（tools/src/response_history.rs:9-71）。
- 涉及行：01-M111, 08-M22, 11-M38, 11-M39, A05-N8
- 要改：
  - `subagents.py` 新增 `_fork_history(parent_row, budget_bytes, last_turns)` 替换 `:685-695` 的循环：父有 `context_checkpoint` 时先放一条 information 消息装 checkpoint 摘要，只取 `through` 之后的消息（口径同 `loop.py:784-787`）。
  - 工具调用和结果一起继承（E104-59）：assistant 消息连同 `tool_calls` 复制，对应的 `role=tool` 消息连同内容复制。call id 统一改写为 `fork_<原id>`，assistant 的 `tool_calls[].id` 和 tool 的 `tool_call_id` 同步改，避免与子代理自己的调用冲突，也不会被当成待执行队列（只进 messages，不进 `queue`）。去掉 `provider_state`、`reasoning_content`，它们绑定父任务的上游会话（`loop.py:18` 会校验 provider_state）；去掉 `resource_origin`。工具结果里如果是父任务大结果的引用（`results.format_reference_result` 产生的 result_id，子任务用 `results.path`（`results.py:137-140`）按自己 task_id 读不到），替换成"父任务大结果，未继承，需要时重新执行读取"。成对检查：只有 assistant 调用与 tool 结果都在预算内才一起保留，缺一个就两个都丢，保证提供方不会收到孤立的 tool 消息。
  - 大小：从最新往回按"一轮"（user 到下一个 user 之前）累加，总字节不超过 `budget_bytes`；`budget_bytes` 由事务外按子模型窗口的一半算好传入（`loop.py:2021-2022` 预校验处，可用 `self._context_limit`，见 `loop.py:2029`）。单条工具结果超过预算的 1/8 时截断到该长度并注明"已截断"；放不下的更早轮次整轮丢弃。
  - `subagents.py:114,366-371` FORK_MODES 增加整数档 "1".."20"：从倒数第 N 条用户消息起复制（11-M38）；schema `:149` 改为 pattern。
  - `subagents.py:90-95` FORK_ISOLATION_PROMPT 保留"继承的工具调用是历史记录，进程句柄和工具会话不继承"（现在与实际一致），并加一句"被截断或未继承的大结果需要自己重新读"。
- 新增测试：`tests/test_agent_subagents.py`：(1) 父有 checkpoint → 子首条是摘要，through 之前的消息不出现；(2) 父历史 2 MB → 子初始历史 ≤ 预算；(3) fork_turns="2" 只带最后两轮；(4) 父的 ryn_file_read 调用和结果出现在子历史里，id 改为 fork_ 前缀且成对；(5) 预算边界处只剩调用没有结果时两者都被丢掉；(6) 子历史里没有 provider_state；(7) 父大结果引用被替换为说明；(8) 子代理第一次请求经 `prompt.provider_messages` 后消息序列合法（每个 tool 消息前都有对应调用）。
- 依赖：I3（子模型在预校验处确定，窗口按子模型算）。
- 兼容和风险：只影响新派生的子代理；整数档是 schema 扩展，旧调用不受影响。继承工具结果会让子代理初始上下文变大，由预算控制；不同提供方对历史 tool 消息格式要求不同，测试 (8) 要在 OpenAI 兼容和 Anthropic 两种适配器上跑。
- 工作量：中
- 影响：中
- 本轮修订：原设计只继承文字、用占位行代替工具结果，子代理看不到父任务已读过的文件和命令输出（E104-59）；改为成对继承工具调用和结果，改写 call id，并按预算压缩。

## S07-I10 子代理结果超长时标注截断并给出读取方法
- 问题：`subagents.py:1410-1426` 完成通知的 summary 截到 4000 字符，没有截断标记，父模型不知道被截。完整结果其实在子任务的 `result` 里，`ryn_wait_agent` 返回的 `final_reply`（`loop.py:2137`，来自 `subagents.py:1339`）是全文，但没有大小上限，也没告诉父模型去那里读；`ryn_list_agents` 只给 160 字（`subagents.py:1234`）。
- 涉及行：A01-N3, A05-N5
- 要改：
  - `subagents.py:1419-1430` `on_subagent_settled_in_tx` —— summary 超过 4000 时保留前 4000 并追加 `[truncated: N of M chars; call ryn_wait_agent targets=["<agent_path>"] reply_offset=4000 for the rest]`，通知加 `summary_truncated: true` 和 `result_chars`。
  - `subagents.py:406-419` 和 schema `:204-211` ryn_wait_agent 加可选 `reply_offset`（整数 ≥0）。
  - `loop.py:2126-2141` wait 结果的 `final_reply` 按 `reply_offset` 分页，每页上限 16000 字符，附 `final_reply_chars`、`next_reply_offset`（读完为 null）。
- 新增测试：`tests/test_agent_subagents.py`：(1) 10000 字结果 → 通知 summary 含截断标记；(2) wait reply_offset=4000 返回余下部分且 next_reply_offset 正确；(3) 短结果不带标记。
- 依赖：无
- 兼容和风险：不用 `results.save`（`results.py:143-157` 会再次读 store，不适合在结算事务里调用），直接读子任务行，存储不变。旧通知不补标记。
- 工作量：小
- 影响：中

## S07-I11 子代理需要审批时转给主人，权限原样继承
- 问题：`loop.py:1596-1601` 子代理遇到任何需要审批的操作都直接抛 agent_subagent_approval_blocked。为了让 confirm_changes 下的 worker 能写，`subagents.py:676-677` 把子任务权限放宽成 workspace（`execution_policy.py:239-256` `decide` 同时看任务和全局设置，只在"任务比全局严"时真的放宽）。结果：default/awaiter 在 confirm_changes 下任何写都被拒；explicit_approval 工具（组件安装等）在任何模式下都不能由子代理执行。Codex 审批统一路由（core/src/tools/approvals.rs:586-634），子代理原样继承审批策略（core/src/agent/child_config.rs:176-203）。automatic 模式本来就不问，不受影响。
- 涉及行：01-M23, 08-M26, A05-N6, B08-11, B06-14
- 要改：
  - `loop.py:1596-1601` —— 删掉子代理直接拒绝；子任务走现有 `waiting_approval` 路径（:1602-1609），并调用新增的 `subagents.on_subagent_waiting_in_tx`：给父任务追加 `kind: "approval_needed"` 通知并记事件 `subagent_approval_requested`。
  - `subagents.py:1297-1383` `project_subagent_summary` —— 增加 `pending_approval` 投影（工具名、标题、预览、approval_id）。
  - `subagents.py:673-679` —— 去掉 `confirm_changes + worker → workspace` 放宽：explorer/reviewer 仍为 read_only，其余原样继承父 permission_mode。
  - 主人批准走现有 `runtime.action`（`runtime.py:2163-2171` 起）对子任务 id 操作，用子任务版本做 `check_version`。
  - `webapp/src/screens/RynAgentSubagents.tsx:789` `SubagentRightDrawer` 和 `:221-236` `SubagentHeaderPill` —— 等待审批的子代理显示批准/拒绝卡，复用父任务审批组件；`webapp/src/domain/rynAgent.ts:528-533` 旁加按子任务 id 调 action 的封装。
  - ryn_request_input 仍对子代理禁止（`loop.py:2166-2170`），`subagents.py:314` 不变：子代理缺信息时由父代理转述，这与 Codex 一致（子代理审批策略继承父回合，core/src/agent/child_config.rs:179-188，没有单独的提问通道）。但拒绝要让主人看得见（B08-11）：在 `loop.py:2166-2170` 抛错前调用 `subagents.on_subagent_waiting_in_tx(..., kind="input_blocked")`，把被拒的提问摘要（prompt 前 200 字）作为通知送给父任务，并记在子任务 `subagent_meta.blocked_requests`（最多 5 条）；`project_subagent_summary` 投影这个列表，抽屉里显示"子代理想问的问题（已转给主智能体）"。
  - 面板内直接和子代理对话、处理它的待办（E116-17、B06-14）：现在抽屉底栏只有"中断当前轮次"和"在主对话中 @昵称"（`webapp/src/screens/RynAgentSubagents.tsx:1139-1150`），头部把等待审批/等待输入算作"需关注"（`:232-234`），但没有处理入口。
    - 后端：`runtime.py:2163-2190` `action` 增加 `message_subagent`（允许键集合 `:2164` 加 `message`），参数 subagent_id + message。按 `runtime.py:2062-2066` 的同一规则执行：子代理在运行就 `send_message_in_tx`，否则 `followup_task_in_tx`；用 I8 的信封标为来自主人（`from="owner"`），和父代理消息区分。已关闭的子代理返回 agent_subagent_closed，界面提示先恢复。
    - 前端：`SubagentRightDrawer` 底栏（`RynAgentSubagents.tsx:1139-1150`）加输入框，直发给所选子代理；`blocked_requests` 每条旁边有"回答"按钮，把回答预填进这个输入框；待审批时显示批准/拒绝卡（见上）。`webapp/src/domain/rynAgent.ts:528-533` 旁加 `messageSubagent(task, id, text)`。
    - 父代理要知道主人越过它给子代理发了话：同一事务给父任务追加 `kind: "owner_message_to_subagent"` 通知（摘要前 200 字），按 I1 写进父历史。
- 新增测试：`tests/test_agent_subagents.py`：(1) confirm_changes 下 default 子代理写文件 → 子任务 waiting_approval，父队列有 approval_needed 通知；(2) 对子任务 approve 后写执行；(3) automatic 下不产生审批；(4) worker 子任务的 permission_mode 等于父任务；(5) 子代理调 ryn_request_input 仍被拒，但父队列收到 input_blocked 通知、子任务 blocked_requests 有记录；(6) action=message_subagent 对运行中的子代理进入 pending_user_messages，对已完成的触发 followup，对已关闭的报 agent_subagent_closed；(7) message_subagent 后父队列有 owner_message_to_subagent 通知。界面：`webapp` 组件测试覆盖抽屉输入框发送、回答按钮预填。
- 依赖：I1（approval_needed 通知送达）；I8（信封）；S14 审批卡和输入框样式；S14-I17（恢复/关闭/再派按钮，与本项的输入框放在同一底栏）。
- 兼容和风险：已存为 workspace 的旧子任务保持不变。子任务进入 waiting_approval 后不在 queued/running，按 I4 规则父代理的 wait 会返回，通知要写明"等主人审批"。界面没做好前子代理会停在 waiting_approval，比直接拒绝更难察觉，后端和界面要一起上。
- 工作量：大
- 影响：中
- 本轮修订：补上 E116-17，在子代理面板内加输入框直接发消息、给被拒提问加回答入口；B06-14 原标"不做（已有 @昵称）"，现在由本项的面板输入框覆盖（恢复按钮在 S14-I17），映射改到本项。

## S07-I12 子任务树随父任务归档、删除、恢复，清理孤儿子任务
- 问题：`lifecycle.py:59-75` `_archive_in_tx` 和 `lifecycle.py:149-160` delete 分支只对父 task_id 做 `data["tasks"].pop`，子任务留在 `data["tasks"]`。全 agent_runtime 只有这两处会移出任务；`subagents.py:785` 只进不出。`runtime.py:1571,1581` `list_tasks` 过滤掉有 parent_task_id 的行，子任务在界面上看不到、删不掉。`runtime.py:1770` 和 `lifecycle.py:117` 的 200 上限、`storage_relief.py:17-18` 的 190/180 水位都把子任务算进去，而 `storage_relief.py:52-53` `candidates` 又跳过所有子任务和有子任务的父任务。常用子代理的用户最终会 agent_task_limit 无法新建任务，自动归档也降不下水位。`subagents.py:785` spawn 不查 200 上限。Codex 归档父线程时连带子孙（thread-store/src/local/archive_thread.rs:42-64；app-server/src/request_processors/thread_processor.rs:1816-1914）。
- 涉及行：38-M60, 44-M95, 45-M27, 45-M32, A18-N5, A19-N5
- 要改：
  - `rynmesh/agent_runtime/lifecycle.py` 新增 `_descendants(data, row)`：沿 `subagent_ids` 递归，返回后代 id 列表（先子后根的顺序）。
  - `lifecycle.py:138-145` change 的 archive 分支 —— 先对后代调 `subagents.cascade_stop_subagents_in_tx(data, row, close=True)`；后代里有 in_flight 或 `ambiguous` 的拒绝 agent_task_busy；然后对每个后代调 `_archive_in_tx`（各自的 archive store，`archive_reason="parent_archived"`），最后归档根。单个后代失败只记 `archive_child_skipped` 事件，不阻断父。
  - `lifecycle.py:146-160` delete 分支 —— 同一事务里给后代写墓碑并从 `data["tasks"]`、`data["archives"]` 移除；返回 `{"id", "deleted": True, "descendant_ids": [...]}`。
  - `lifecycle.py:116-133` restore 分支 —— 根恢复时把 `archive_reason="parent_archived"` 的后代一起恢复；200 上限按整树数量检查（45-M32）。
  - `runtime.py:1635-1655` `_lifecycle` —— 对 `descendant_ids` 逐个做 `results.purge`、`lifecycle.purge_archive`、`forget_task`；`:1638-1641` 恢复前的 `_relieve_storage` 的 `need_tasks` 用整树数量。
  - `rynmesh/agent_runtime/storage_relief.py:44-57` `candidates` —— 去掉对 parent_task_id 和 subagent_ids 的无条件跳过；以顶层任务为单位，整树都 `eligible` 时作为一个候选，`bytes` 为整树合计，并带 `tree_ids`；`runtime.py:1034-1038` 计数按整树减；`lifecycle.py:77-94` `archive_many` 对候选的整树归档。
  - `subagents.py:612-617` `spawn_agent_in_tx` —— `len(data["tasks"]) >= 200` 时拒绝 `agent_task_limit`（correction：关闭或归档旧任务）；`loop.py:2021-2024` 事务外对 ryn_spawn_agent 先调 `self._relieve_storage("spawn", need_tasks=1)`（`runtime.py:1019`）。
  - `runtime.py:2589-2680` `reconcile` —— 启动时找父已不在 `data["tasks"]` 的子任务：父在 archives 里就把它归档进去，父已删除就写墓碑，父完全找不到就归档并标 `archive_reason="orphan"`；修复现有用户已经积累的孤儿。
- 新增测试：`tests/test_agent_runtime_lifecycle.py`：(1) 归档有 2 层子任务的父 → data["tasks"] 里没有后代，各自有 archive 文件；(2) 删除父 → 后代都有墓碑且 results 被清；(3) 恢复父 → 后代一起回来；(4) 后代有 in_flight → 归档被拒。`tests/test_agent_storage_relief.py`：(5) 190 个任务里 100 个是子任务 → 自动归档能降到 180 以下；(6) 构造孤儿子任务后 reconcile 清理。`tests/test_agent_subagents.py`：(7) 200 个任务时 spawn 被拒。
- 依赖：无
- 兼容和风险：存储格式不变（每个子任务仍是普通 archive store，旧版本能读）。旧版本恢复父任务时不会带回子任务，只影响子代理卡片显示。reconcile 一次性迁移要在测试里用真实旧数据（多层、父已删）验证，不能误删顶层任务：只处理有 parent_task_id 的行。
- 工作量：中
- 影响：高

## S07-I13 新增 reviewer 审查子代理角色
- 问题：`subagents.py:113` ROLES 只有 default/explorer/worker/awaiter，没有审查角色。`verification.py:21-34` 的检查器只判目标是否达成，不找代码缺陷；`knowledge.py:17` 只有"Review the final diff"一句自查。explorer 只拦写工具（`subagents.py:500-508`），不拦联网和再派生。结果是纯文本，`subagents.py:1426` 截到 4000 字，长审查丢 findings，中断时没有专门提示。Codex 审查子代理只读、无初始历史、关联网和多代理、结果结构化并三级容错解析（core/src/tasks/review.rs:66-286），有完整审查准则（prompts/templates/review/rubric.md:34-95；skills/src/assets/samples/review-agent/SKILL.md:1-57）。
- 涉及行：08-M1, 08-M2, 08-M3, 08-M4, 08-M5, 08-M6, 08-M7, 52-M270, 52-M271, 52-M272, 52-M273, 52-M274, 52-M275, 52-M276, 52-M374, 52-M376, 52-M377, 52-M378, 52-M379
- 要改：
  - `subagents.py:113` ROLES 和 `:148` role enum 加 `reviewer`。
  - `subagents.py:666` —— reviewer 强制 `fork_turns="none"`（08-M4）。
  - `subagents.py:674` —— reviewer 与 explorer 一样 `child_perm="read_only"`。
  - `subagents.py:493-508` `check_subagent_tool_policy` —— reviewer 拒绝写工具、`ryn_spawn_agent`、`ryn_web_search`（`web_search.py:39` NAME），错误码 `agent_subagent_read_only`（08-M1）。
  - `subagents.py:305-347` `role_guidance` 加 reviewer 分支（Ryn 措辞）：流程——读适用的 AGENTS.md，看完整 diff 和周边代码，找到第一个问题后继续看完，查相关测试和调用点确认每条属实（52-M376）；比较基准——有上游且领先就用上游，`git merge-base HEAD <ref>` 后看 `git diff <merge-base>`，解析不了先试 upstream 再报告不可用（52-M377）；只报同时满足五条的：实质影响、可操作、本次引入、能从代码演示、作者会修；不报猜测、既有问题、有意改动和风格（52-M270/271/378）；规则支撑的问题引用该 AGENTS.md 最小行范围（52-M272）；每条行范围不超过 5-10 行且与 diff 重叠（52-M273）；标题前缀 [P0]-[P3] 并按严重度排序（52-M274）。reviewer 用专用报告格式替代 `SUBAGENT_REPORT_GUIDANCE`（`:108-111`）：每条"[Px] 标题 — 路径:行"加一段说明；无问题写 "No findings."；最后写整体是否正确和一两句理由、测试缺口（52-M275/379）；也允许输出 JSON（findings/priority/code_location/overall_correctness/confidence，52-M276）。
  - `rynmesh/agent_runtime/execution_policy.py:21` `GIT_SAFE_SUBCOMMANDS` 加 `merge-base`（现为 status/diff/log/show/rev-parse）；否则 read_only 的 reviewer 跑 `git merge-base` 会被 `decide` 拒（`execution_policy.py:121-124`、`:248-249`）。这一处属于 S04 文件，随本项一起改。
  - `subagents.py` 新增 `parse_review_output(text)`：先整段 `json.loads`；失败取首个 `{` 到末个 `}` 再解析；再失败按"[Px] 标题 — 路径:行"逐行解析成 findings；都不行把全文作为 `overall_explanation`（08-M6）。
  - `subagents.py:1393-1444` `on_subagent_settled_in_tx` —— role=reviewer 且 completed 时把解析结果放进 `notif["review"]`，summary 上限放宽到 16000；interrupted/cancelled/paused 时 summary 写"审查被中断，需要重新发起"（08-M5/08-M7）。`format_notification_tag`（`:1053-1064`）带出 review 字段。
  - `store.py:37-47` 加设置 `review_model_id`；I3 的模型解析对 reviewer 改为 参数 > review_model_id > default_subagent_model_id > 父模型（08-M3）。
  - `prompt.py:32`（I6 改写后的工具清单）角色列表补 reviewer；`subagents.py:133-140` 描述的 "Available roles" 里写 reviewer 用途（I7）。
  - `verification.py:243-277` `parse_assessment` 的同样三级回退属于 S10，不在本项改。
- 新增测试：`tests/test_agent_subagents.py`：(1) reviewer 调 ryn_file_write / ryn_spawn_agent / ryn_web_search 都被拒；(2) reviewer 传 fork_turns=all 实际为 none；(3) reviewer 子任务 permission_mode=read_only，`git merge-base HEAD main` 被判为读；(4) parse_review_output 四种输入（纯 JSON、前后带文字的 JSON、[P1] 文本行、无格式文字）；(5) 完成通知含 review.findings；中断时 summary 为重跑提示；(6) 设了 review_model_id 时 reviewer 用它。
- 依赖：I3（模型解析顺序）；I7（角色描述）；S04（GIT_SAFE_SUBCOMMANDS）。
- 兼容和风险：新角色是增量，旧子任务不受影响。role enum 变化会让排队中的旧 spawn 调用报一次 schema 变化。审查准则文本较长（约 1.5 KB），只在 reviewer 子代理的 authority 里出现，不进父代理请求。
- 工作量：中
- 影响：中

## S07-I14 深度到顶不再提供 spawn 工具；驱逐按最近使用顺序并留记录
- 问题：`subagents.py:650-655` 深度超限只在调用时拒绝；`tool_menu.py:147-151` 只按窗口大小过滤子代理工具，不看深度，到顶的子代理仍看到 ryn_spawn_agent，浪费一步。常驻满额时 `subagents.py:624-631` 按树遍历顺序取第一个已结束的关掉，不是最近最少使用；没有事件，spawn 结果也不说关了谁。Codex 驱逐按 LRU 并跳过受保护的（core/src/agent/control/residency.rs:153-194,225,309）。
- 涉及行：09-M37, 01-M64, 01-M65, G22-26, G23-29
- 要改：
  - `runtime.py:425` `_menu` —— 行的 `subagent_meta.agent_path` 深度已到 `MAX_SUBAGENT_DEPTH` 时，传给 `tool_menu.project` 的内部工具去掉 ryn_spawn_agent（新增 `subagents.internal_specs_for(row, specs)` 做过滤）；`loop.py:2007-2012` 的 schema 快照放行规则保持，调用仍按 `subagents.py:651-655` 拒绝。
  - `subagents.py:624-631` —— `settled_open` 按 `meta.last_used_ms`（新字段，在 send/followup/wait/settle 时更新，旧行回退 `settled_at_ms`）升序取最早的；关闭时记父任务事件 `subagent_evicted`（id、昵称、路径），spawn 返回值（`:797-810`）加 `evicted`。
  - 恢复也占位（G22-26）：`subagents.py:983-990` `resume_agent_in_tx` 直接 `meta["closed"]=False`，不查并发上限，已关闭的子代理能逐个恢复到 6 个以上。把 `:620-639` 的"数开着的、满了就驱逐或拒绝"抽成 `_ensure_open_slot(data, parent_row)`（返回被驱逐的行或 None），spawn 和 resume 都先调用；resume 只在目标当前是 closed 时才占位（本来开着的不占）。resume 结果同样带 `evicted`。
  - 菜单门槛统一（G23-29）：`tool_menu.py:147-151` `project` 用 `max_context_bytes >= ADDITIVE_CAPACITY`（48000）决定是否带子代理工具；`tool_menu.py:285-286` `discover` 的非 lazy 分支用 `capacity_bytes >= ADDITIVE_CAPACITY // 2`，而 `capacity_bytes`（`tool_menu.py:94-97`，`loop.py:2037` 传入）是 窗口-权限字节-256，口径也不同；`:369-377` 又把 BM25 命中的子代理工具写进 `always_available`。窗口在约 24000-48000 之间时，模型被告知可用，实际没暴露。改为：`discover` 新增关键字参数 `subagents_exposed: bool`，`loop.py:2036` 调用处用与 `runtime.py:_menu` 同一个判断（窗口达标且深度未到，走本项的 `subagents.internal_specs_for`）；`discover` 里 `all_internal`/`no_subagent_internal` 的选择（:281-286）和 `matched_internal`（:369-377）都只看这个布尔值，删掉 `//2` 那条；`project` 的 :150 也改走同一个函数，门槛只留一份。与 S06-I6 在 `loop.py:2036` 的同一处改动合并实现。
- 新增测试：`tests/test_agent_subagents.py`：(1) 深度 2 的子代理菜单里没有 ryn_spawn_agent，深度 1 的有；(2) 6 个已结束子代理，最近被 wait 过的不被驱逐；(3) spawn 结果含 evicted 且父事件有记录。；(4) 6 个子代理都已关闭，再 resume 第 7 个时驱逐已结束的或拒绝，开着的数量不超过上限；(5) resume 一个本来就开着的 → 不占位；(6) 窗口 30000 字节：project 不含子代理工具，discover 的 query 命中 ryn_spawn_agent 也不进 always_available；窗口 60000：两处都有。
- 依赖：I15（深度上限可配置时取设置值）。；与 S06-I6 同改 `loop.py:2036`。
- 兼容和风险：菜单变化只影响深层子代理；新字段可缺省。resume 满额时可能驱逐别的已结束子代理，返回值里写清；旧任务无影响。
- 工作量：小
- 影响：低
- 本轮修订（F2）：补两处：resume 恢复已关闭子代理也要走并发上限（原只改了 spawn 的驱逐）；菜单里子代理工具的暴露门槛在 project/discover 两处不一致，统一成一个判断。

## S07-I15 子代理设置项：并发上限、深度上限、额外开发者指令
- 问题：`subagents.py:87-88` 并发 6、深度 2 写死；没有设置项给子代理追加开发者指令（`subagents.py:305-347` `role_guidance` 只有固定文本）。Codex 可配置并发、max_depth、子代理开发者指令（config/src/config_toml.rs:764-814；core/src/agent/child_config.rs:154-162）。
- 涉及行：01-M22, 24-M24, 24-M69
- 要改：
  - `store.py:37-47` `DEFAULT_SETTINGS` 加 `max_open_subagents: 6`（1-12）、`max_subagent_depth: 2`（1-3）、`subagent_developer_instructions: ""`（≤4000 字节）；`runtime.py:1256-1290` `update_settings` 校验范围。
  - `subagents.py:622-639` 和 `:650-655` 改读设置（`spawn_agent_in_tx` 已在 `:617` 合并 settings）；常量保留为默认值。
  - `subagents.py:305-347` `role_guidance(meta, extra="")` —— 在 `SUBAGENT_REPORT_GUIDANCE` 前追加设置里的额外指令；`prompt.py:226-228` 调用处传入（prefix_row 带入设置值）。
  - I7 协调指引里的数字改读设置。
- 新增测试：`tests/test_agent_subagents.py`：(1) max_open_subagents=2 时第 3 个触发驱逐或拒绝；(2) max_subagent_depth=1 时孙代理被拒；(3) 额外指令出现在子代理 authority 里且超 4000 字节被设置校验拒绝。
- 依赖：无
- 兼容和风险：默认值与现在一致，旧设置无需迁移。自定义角色（24-M69 提到的）不在本项，Ryn 暂不开放。
- 工作量：小
- 影响：低

## S07-I16 任务运行中也能调高预算上限
- 问题：`control.py:145` 只有任务在 paused/completed/failed 且无在途操作时才允许调预算，`runtime.py:1544-1545` 据此拒绝；`webapp/src/screens/RynAgentBudget.tsx:58,108-109` 运行中按钮不可用。主人看到快到上限只能先停再改。费用一栏固定写"未知"（`RynAgentBudget.tsx:104`；`budget.py:204` cost_status 恒为 unknown），没有可信价格表，不在本项做。
- 涉及行：B07-29
- 要改：
  - `runtime.py:1531-1555` `update_budget` —— 运行中（queued/running/waiting_*）只接受"每项都不低于当前值"的调整，跳过 `allowed_actions["budget"]` 检查但仍做 `check_version`；不改 `control_revision`，不打断当前请求；新上限在下一次 `budget.reserve` 生效。调低仍只允许在停止时。
  - `control.py:145` 增加 `possible["budget_raise"]`（运行中可调高）。
  - `webapp/src/screens/RynAgentBudget.tsx:58,108-109` —— 运行中显示"调高上限"，输入的最小值为当前上限。
  - 单段上限放宽（E103-8）：`runtime.py:1281-1283` 设置里 max_steps 限 1-128、max_seconds 限 10-3600；`runtime.py:1536-1538` 任务的 run_max_seconds 限 10-3600。默认来源的段到点会自动续段（`budget.py:448-474` `continue_segment`），但主人一旦设了（成为 owner 硬限），长任务一段最多 1 小时、128 步就停。改为 max_steps 1-1024、max_seconds 与 run_max_seconds 10-86400（与 `budget.py:12-13` 生命周期上限的顶一致，单段不会超过总时长）；`RynAgentBudget.tsx:59` 前端校验同步改成 86400。默认值（`store.py:43` 64 步/900 秒）不变。
- 新增测试：`tests/test_agent_runtime_budget.py`：(1) running 任务调高 max_total_tokens 成功且 in_flight 不变；(2) running 任务调低被拒 agent_invalid_transition；(3) 调高后下一次 reserve 用新上限；(4) update_settings 接受 max_steps=512、max_seconds=7200，拒绝 1025/86401；(5) update_budget 接受 run_max_seconds=14400。
- 依赖：I2（树级上限也读根任务的新值）。
- 兼容和风险：调高是放宽，不会造成越权；`set_owner_limits` 把提交项标为 owner 硬上限，与现有语义一致。
- 工作量：小
- 影响：低


## S07-I17 中断、关闭、级联停止子代理时保留在途操作，不伪造"未执行"
- 问题（抽查复核，已在基线代码确认）：`subagents.py:900-908` `interrupt_agent_in_tx`、`subagents.py:930-938` `close_agent_in_tx` 先调 `close_queue(child, event, "agent_cancelled", keep_flight=False)`，再把 `in_flight` 置 None；`subagents.py:1466-1471` `cascade_stop_subagents_in_tx` 直接把 `in_flight` 置 None。父任务暂停、停止、取消时会经 `runtime.py:2198,2217,2238` 调到这里，主人在界面中断子代理也走这里（`runtime.py:2176-2189`）。后果：
  1. `keep_flight=False` 时 `control.py:163-176` 不保留在途调用，给已经派出去的写操作也写一条 `{"error":"agent_cancelled","not_executed":true,"no_mutation":true}` 工具结果，这是假记录。
  2. `in_flight` 清空后，`control.py:63-74` `ambiguous()` 和重启时的 `runtime.py:2601-2680` `reconcile` 都看不到这次写，"结果未知"标记和后续对账全部丢失。
  3. 工具实际跑完后，`loop.py:1697-1720` 的 finish 会给同一 call_id 再追加一条真实结果（同一调用两条结果），并在已被清空的 queue 上 `pop(0)`。按代码推断会抛 IndexError，整个事务回滚，真实结果和 `verification.record` 都没写进去（未实测）。
  主人对顶层任务的 pause/stop/cancel 不这样做：`runtime.py:2218-2238` 保留 in_flight，写 `pending_control`，等在途操作返回后由 `control.settle`（`control.py:180-190`，经 `memory_integration.settle_in_tx`，`memory_integration.py:546-548`）再收尾。子代理路径没有沿用这套语义。
- 涉及行：无输入行（协调方抽查发现，F 阶段补入）
- 要改：
  - `subagents.py` 新增 `_stop_child_in_tx(data, child, *, kind, close)`，`kind` 为 `pause` 或 `cancel`，三处统一调用：
    - 有 `in_flight` 时：不清 in_flight；`close_queue(child, event, "agent_cancelled")` 用默认 `keep_flight=True`，只给未派出的调用写未执行记录；状态改为 paused/cancelled，`pending_control = {"kind": kind}`，记事件 `paused`/`cancelled`（`operation_in_flight=True`）；`pending_approval`、`pending_input` 照旧清掉。
    - `ambiguous(child)` 为真（已知写结果未知）时：只写 `pending_control`，不改状态和错误，同 `runtime.py:2221-2227`。
    - 没有 in_flight 时：维持现在的直接结算，再调 `control.settle(child, event, data=data)`。
  - `subagents.py:900-908`、`:930-938`、`:1466-1471` 改调上面的函数。`close=True` 时照旧设 `meta["closed"]`。
  - `on_subagent_settled_in_tx`（`subagents.py:1393-1444`）在中断时仍立即发通知，但通知加 `operation_in_flight: true`，文字说明"最后一个操作仍在完成，结果稍后补报"。
  - `loop.py:1697-1720` 工具 finish 在 `memory_integration.settle_in_tx` 之后，行有 `parent_task_id` 且状态已是 paused/cancelled 时再调一次 `subagents.on_subagent_settled_in_tx`（它按 event_seq 去重，`subagents.py:1402-1404`），把在途操作的真实结果补报给父任务。
  - 防御：`loop.py:1715` 的 `row["queue"].pop(0)` 改成按 call_id 移除，queue 里没有时不抛错（`loop.py:1730-1731` 的 `_tool_rejection` 已是这种先核对再处理的写法）。
- 新增测试：`tests/test_agent_subagents.py`：
  1. 子代理派出 ryn_file_write 后（in_flight.dispatched=True）父代理 ryn_interrupt_agent：子任务仍有 in_flight；该 call_id 没有 not_executed 记录；工具返回后只有一条真实结果，并有 verification 记录。
  2. 同样场景改成节点重启：reconcile 把子任务标 interrupted + agent_write_outcome_unknown（或 node_restarted），并安排对账。
  3. 主人取消父任务时级联关闭，在途写不被伪造。
  4. 在途操作返回后父任务收到第二条通知（补报）。
  5. 没有在途操作时行为与现在一致。
- 依赖：无（I4 的 wait_ready 依赖子任务状态正确，所以 I4 依赖本项）。
- 兼容和风险：只改内存中的状态转换，存储字段不变。中断后子任务要等在途操作返回才真正停下，界面会多显示一会儿"正在停止"，与顶层任务一致。不修会出现假的"未执行"记录，属于数据正确性问题，建议优先做。
- 工作量：中
- 影响：高

## S07-I18 默认预算不设硬顶；升级前任务的旧默认上限不再按硬上限处理
- 问题：
  - E104-54：默认来源的上限可以自动加长（`budget.py:219-233` `_room`），但不能超过 `budget.py:12-13` `RANGES` 的顶（1024 次模型调用、4096 次工具调用、2000 万 token、86400 秒）。到顶时 `_room` 返回 False（`:225-226`），`reserve` 抛 exhausted（`:253-263`），长时间的自动任务以 failed 结束。`time_remaining`（`:335-337`）也把默认来源的时间封在 86400。Codex 只有配置了共享预算才限制（core/src/agent/control/budget.rs:12）。
  - E104-55：升级前的任务在 `budget.py:144-159` `initialize` 里，只要有历史就把每项标成 `legacy_unknown`；已有 budget 但没有 `limit_sources` 的也落到 `legacy_unknown`（`:38-41`）。`is_hard`（`:44-45`）把它当硬上限，老任务继续时停在旧默认值（128 次模型调用、3600 秒）。单段上限同理：`segment_source`（`:58-65`）对旧任务也是 legacy_unknown，`hard_run_limit`（`:68-75`）会在 64 步、900 秒处停，而不是自动续段。
- 涉及行：无输入行（F 阶段漏项 E104-54、E104-55；S02 转入 E116-11）, G22-42
- 要改：
  - `budget.py:219-233` `_room`：`is_hard` 为假时不再检查 `ceiling`，按 `max(previous + DEFAULT_LIMITS[key], used + need)` 加长。`RANGES` 只用于校验主人设的值（`validate_limits`，`:134-141`）。
  - `budget.py:236-244` `ensure_default_room` 去掉 `RANGES[key][1] - used` 的截断。
  - `budget.py:330-338` `time_remaining`：默认来源时返回 `float("inf")`。调用方 `run_seconds_left`（`:493-497`）和 `runtime.py:3241-3244` 的 claim 检查都能处理无穷大，实现时逐处确认。
  - `budget.py:209-216` `public` 的 `default_ceilings` 对默认来源返回 null；`webapp/src/screens/RynAgentBudget.tsx:52-56` `sourceLabel` 在 ceiling 为 null 时只写"默认，随进展自动延长"。
  - 安全兜底不变：默认段的无进展停止（`budget.py:448-474` `continue_segment` 返回 `agent_no_progress`）和主人设的硬上限仍然生效。
  - 旧任务迁移，放在 `budget.py:144-173` `initialize` 的已有 budget 分支：某项来源为 legacy_unknown、数值等于 `DEFAULT_LIMITS[key]`、并且行内保留的事件里没有任何 `budget_changed`（`runtime.py:1551` 只有主人改预算时才写这个事件）时，改标为 `default`，并记一次 `budget_source_migrated` 事件。数值不等于默认值，或有 budget_changed 的，保持硬上限。单段的 `limits_source` 按同样规则处理 max_steps/max_seconds（对照 `store.py:43` 的 64/900）。
  - 兜底入口：迁移后仍是 legacy 硬上限的任务，停在这个上限时（`budget.stop_for_limit`，`budget.py:384-409`，outcome.budget_stop.source=legacy_unknown），界面预算卡显示"解除旧上限"。`runtime.py:1531-1555` `update_budget` 增加 `release_legacy: true`，把 legacy_unknown 的项改成 default，不改数值。
  - 主人可设上限的范围放宽（E116-11）：`budget.py:12-13` `RANGES` 的上界同时是主人能填的最大值。前端 `webapp/src/screens/RynAgentBudget.tsx:30` 把同样的数写死在 `ranges` 里。放宽到模型请求 100000、工具调用 400000、Token 20 亿、累计时间 2592000 秒（30 天），下界不变，仍由 `validate_limits`（`budget.py:134-141`）校验，防止误填。`public`（`budget.py:209-216`）新增 `ranges` 字段，前端改读后端值，不再写死。单轮时限的 3600 秒上界在 S07-I16 里放宽（E103-8），这里不重复。
  - 停止文案：`RynAgentBudget.tsx:17-18` `budgetStopMessage` 对 source=default 的提示是"已达到系统允许的最大累计额度…可缩小范围后新建任务"。取消默认硬顶后默认来源不会再因额度停下，这条改成只在旧版本数据里出现时的兼容文案；owner 来源的提示加"一键续额"按钮。
  - 到顶前先提示（E116-11）：`public` 新增 `near_limit`，列出用量达到 85% 的主人硬上限项。`RynAgentBudget.tsx` 在预算卡和对话顶部显示"即将达到你设的 X 上限"，带"一键续额"按钮：调用 `updateBudget`，把该项提高一个 `DEFAULT_LIMITS[key]` 的步长。运行中可调高依赖 S07-I16。给模型的提醒由 S07-I2 的树级/时间提醒负责。
  - 两套自动加量合一（G22-42）：`runtime.py:1975-2018`（`add_message` 在任务已结束时收到主人新消息）自带一套加量：token 阈值 `min(500000, max(1000, limit//4))` 加 `wrap_reason`，模型/工具次数"用光才加"，秒数阈值 `min(60, max(5, limit*0.1))`，增量取 `DEFAULT_LIMITS`，上界取 `budget.RANGES`。规则与 `budget._room`（:219-233）准入时加量不同，而且这里的 `min(RANGES…)` 在本项取消默认硬顶后仍会把默认来源卡住。统一：`budget.py` 新增 `extend_defaults_for_owner_turn(row, wrap_reason)`，把 `runtime.py:1975-2018` 整段搬进去并去掉上界截断（与 `_room` 一致），事件仍是 `budget_auto_extended`、`reason="default_policy"`；`runtime.py` 该处只调用它并取回 `auto_extended`。阈值保持原值，行为变化只有"不再封顶"。末尾对 hard 来源的 `agent_token_budget_exhausted` 检查保持。
- 新增测试：`tests/test_agent_runtime_budget.py`：
  9. 默认来源任务已结束、token 用量接近 2000 万，主人发新消息 → `add_message` 把 max_total_tokens 加到超过 2000 万，事件 reason=default_policy；owner 来源同样用量 → 不加，仍拒绝。
  1. 默认来源的模型调用用到 1024 后仍能继续 reserve，并有 budget_auto_extended 事件。
  2. 主人设的 max_total_model_calls=50 到 50 仍拒绝。
  3. 默认来源 time_remaining 为无穷大，owner 来源按值算。
  4. 旧任务 limits=128、无 budget_changed 事件 → 迁移为 default；旧任务 limits=300 → 仍为 legacy 硬上限；有 budget_changed 事件 → 不迁移。
  5. release_legacy 后该项变 default，数值不变。
  6. 无进展任务仍在两个空段后以 agent_no_progress 停止。
  7. 主人设 max_total_model_calls=50000 被接受，100001 被拒。public() 返回 ranges，且与 RANGES 一致。
  8. 主人硬上限用到 85% 时 public().near_limit 含该项；一键续额后上限增加一个默认步长。前端组件测试覆盖：ranges 来自后端；near_limit 横幅和按钮可用。
- 依赖：I2（树级预算只拦根任务的硬上限，与本项口径一致）；I16（同一个 update_budget 接口，运行中续额依赖它）。
- 兼容和风险：取消默认硬顶后，自动模式的失控任务只靠无进展检测和主人硬上限兜底，花费可能更高。这与"自动处理即全自动"的老板决定一致，也与 Codex 默认不限一致。`budget.py:17-20` 的注释原本明确不从"值等于默认"推断意图，本项把推断限定在"无任何主人改预算事件"的情况，并提供手动解除入口。如果事件被压缩、证据丢了，会按默认处理，有小概率把主人恰好设成默认值的上限放宽。放宽 RANGES 只扩大主人可填的范围，不改默认值（`budget.py:10-11`）；旧任务保存的上限数值不变。auto-extend 搬家后，`add_message` 的事件和返回的 `public_task.budget` 应与原来一致，测试对比旧用例断言。
- 工作量：中
- 影响：高
- 本轮修订：并入 S02 转来的 E116-11（影响高）：放宽主人可设的累计上限范围，前端改读后端 ranges，到顶前提示并一键续额；影响由中调为高。
- 本轮修订（F2）：补 `runtime.add_message` 里第二套自动加量（原版只改了 `budget._room`，另一套仍按 RANGES 封顶且阈值不同）：搬进 budget.py 统一成一处，同样去掉封顶。

## S07-I19 write_scope 边界收紧，并核对 shell 写入
- 问题（E104-63）：
  - `subagents.py:500-522` `check_subagent_tool_policy` 只对四个文件工具（ryn_file_write/edit/manage、ryn_apply_patch）检查 write_scope，worker 用 shell 写文件不受限。
  - `subagents.py:449-464` `_matches_scope_entry` 过宽：`:460-461` 只要目标路径以 "/"+scope 结尾就算匹配（`src/a.py` 会匹配 `vendor/x/src/a.py`）；`:462-463` 不带斜杠的项按文件名匹配任意目录下的同名文件（`config.json` 匹配全仓所有 config.json）。
- 涉及行：无输入行（F 阶段漏项 E104-63）, G22-21
- 要改：
  - `subagents.py:449-464` `_matches_scope_entry`：只保留完全相等（`:454`）、目录前缀（`:456`）、显式通配（`:458`）三种匹配，删掉 `:460-463` 两条后缀和文件名匹配。想匹配任意目录下的文件要显式写 `**/name`。`subagents.py:150-154` 的 write_scope 描述写明"相对工作区根的路径或通配"。
  - shell 写入事后核对：shell 结果里带 `workspace_files`（`local_tools.py:430-439` `_command_artifacts`，由 `workspace_artifacts.command_files`（`workspace_artifacts.py:207-229`）给出，绝对路径，最多 64 条）。在 `loop.py:1697-1720` 工具 finish 里，行有 `subagent_meta.write_scope` 且结果含 workspace_files 时，用 `_matches_scope_entry` 逐条核对，越界路径放进模型可见结果的 `write_scope_violations`，记事件 `subagent_write_scope_violation`，并给父任务追加一条通知。不自动回滚：文件变更可在 file_review 撤销，且 command_files 的归属标记是 `observed_during_command`（`workspace_artifacts.py:223-224`），兄弟代理同时写的文件也可能被算进来，不能当作确定越界。
  - `role_guidance`（`subagents.py:329-334`）的 worker 写集说明补"shell 命令也不得写 write_scope 以外的文件，越界会报告给父代理"。
  - 路径规范化（G22-21）：`subagents.py:438-446` `_normalize_rel_path` 只换斜杠、剥工作区前缀、去 `./`，不处理 `..`，所以 `src/../x` 以 `src/` 开头就匹配 scope `src`（:456 的目录前缀判断），实际写的是 `x`。改：换斜杠后用 `posixpath.normpath` 折叠 `.`/`..`；折叠后以 `../` 开头或等于 `..`（逃出工作区）返回哨兵 `None`。`_matches_scope_entry`（:449）在 :450 之前先判断目标：规范化为 `None` 一律不匹配（包括 scope 为 `*`/`**` 的整仓写入，:452-453 不再放行逃出工作区的路径）；`workspace_root` 非空而目标是不在其下的绝对路径时也不匹配（:441-444 剥不掉前缀，现在会被当成相对路径参与 `**`/文件名匹配）。scope 项自己含 `..` 时，在 `validate_subagent_tool` 的 write_scope 解析处（`subagents.py:372-380`）拒绝（`agent_invalid_tool_arguments`，correction 要求用工作区内相对路径）。
- 新增测试：`tests/test_agent_subagents.py`：
  4. 目标 `src/../x` 在 scope=["src"] 下被拒；`src/../src/a.py` 折叠后在 scope=["src"] 下通过；目标 `../outside/a.py` 即使 scope=["**"] 也被拒；工作区外的绝对路径被拒；scope=["../x"] 在 spawn 校验时被拒。
  1. scope=["src/a.py"] 时 `vendor/x/src/a.py` 被拒；scope=["config.json"] 只匹配根目录的 config.json；scope=["**/config.json"] 匹配任意目录。
  2. worker 用 shell 在 scope 外创建文件，结果含 write_scope_violations，父队列有通知。
  3. scope 内的 shell 写入没有违规标记。
- 依赖：I1（违规通知送达父代理）。
- 兼容和风险：收紧匹配后，旧任务里依赖"文件名匹配任意目录"的 worker 可能被拒写，错误里有 correction，可改用 `**/` 写法。shell 核对只是报告，不阻断。后台进程的文件变化在进程结束读取时才出现，那时才核对。目标是工作区外绝对路径时，原来在宽范围 scope 下能写，现在会被拒，属预期收紧；`workspace_root` 为空时无法判断绝对路径，保持原行为。
- 工作量：小
- 影响：低
- 本轮修订（F2）：补路径规范化：`..` 不再被当成前缀的一部分，逃出工作区的目标和含 `..` 的 scope 项一律拒绝（原版只删了后缀/文件名匹配和核对 shell）。

## S07-I20 子代理 task_name 保留中文，名字无效时让模型改，不静默换成 subtask
- 问题（G22-27）：`subagents.py:287-289` `_slug_task_name` 用 `[^a-zA-Z0-9_\-]+` 替换，中文名整体变成空串后回落 `subtask`，`:656-663` 再加 `_2`、`_3` 后缀。结果 `ryn_list_agents` 和界面里一排 `subtask`、`subtask_2`，模型和主人都没法按名字找或 @。Codex 对非法名字直接回错误让模型重写（core/src/tools/handlers/multi_agents_common.rs:148-157）。
- 涉及行：G22-27
- 要改：
  - `subagents.py:287-289` `_slug_task_name`：正则改为 `[^\w\-]+`（`\w` 含中文、数字、下划线），仍 `.strip("_").lower()` 并截 48 字符；去掉回落 `subtask`，清理后为空返回空串。
  - `subagents.py:354-357,386` `validate_subagent_tool`：task_name 清理后为空（例如全是符号）→ 抛 `AgentToolRejected("agent_invalid_tool_arguments", correction="task_name 需要含字母、数字或汉字，例如 fix_login 或 登录修复")`。`spawn_agent_in_tx:647` 里 `args.get("task_name") or nickname` 的昵称回落保留（昵称是英文，不会为空）。
  - `subagents.py:146` schema 描述补"可用中文，不能只含符号"。
  - `subagents.py:656-663` 路径去重不变（大小写不敏感比较对中文无影响）；`agent_path` 用 `/` 连接，`\w` 不含 `/`，不需要转义。
  - `runtime.py:2046` 的 @ 路由正则由 I8 改成 `\w`，两边配合后 `@登录修复` 才能路由。
- 新增测试：`tests/test_agent_subagents.py`：(1) task_name="登录修复" → 子代理 task_name 与 agent_path 末段都是"登录修复"；(2) 同名再派 → "登录修复_2"；(3) task_name="!!!" → 当场拒绝并带 correction；(4) 混合名 "fix登录" 保留；(5) `resolve_subagent_targets` 能按中文 task_name 和 `/root/登录修复` 路径找到。
- 依赖：无（I8 的 @ 中文路由用到它）。
- 兼容和风险：旧子任务里已有的 `subtask_N` 不变；`format_notification_tag`（:1053-1064）用 `ensure_ascii=False`，中文路径原样输出。回滚到旧版本后读到中文 task_name 不报错，只是匹配规则回到 ASCII。
- 工作量：小
- 影响：中

## S07-I21 父任务的子代理通知队列只保留最近 N 条已送达
- 问题（G22-33）：`subagents.py:1432-1435` 往父任务 `subagent_notifications` 追加，`:1039-1050` `drain_subagent_notifications_in_tx` 只打 `delivered=True`，整个文件没有任何删除。队列随任务持久化，每次 wait 和 I1 的 `pending_for_request` 都要遍历，子代理多、反复 followup 的长任务里只增不减。
- 涉及行：G22-33
- 要改：
  - `subagents.py` 新增 `_prune_notifications(queue, keep=50)`：保留全部未读，已送达的只留最近 50 条（按 `created_at`），其余删除；在 `:1432-1435` 追加之后和 `:1039-1050` drain 之后各调用一次。
  - 不动子行 `meta["last_notification"]`（:1428 附近，`ryn_list_agents` 用）。
  - 不为删除写事件，避免事件膨胀。
  - 前端没有读这个队列（`webapp/src` 里无引用，只有 `loop.py`、`subagents.py` 用），不需要界面配合。
- 新增测试：`tests/test_agent_subagents.py`：(1) 120 条已送达 + 3 条未读 → prune 后剩 3 + 50，未读全在；(2) 超出的删最旧的；(3) 被删通知对应的子代理，`ryn_wait_agent` 仍读得到 last_notification。
- 依赖：I1（先把通知写进历史，再删已送达副本才不丢信息）。
- 兼容和风险：旧任务第一次 drain 时一次性裁剪，只丢已送达的旧副本。
- 工作量：小
- 影响：低

## S07-I22 预算记录按轮合并，旧数据修补只在加载时做一次
- 问题（G22-38）：`budget.py:270` `reserve` 每次模型或工具调用往 `state["records"]` 追加，整个文件不删除。`_totals`（:180-206）、`reserve`（:251）、`ensure_default_room`（:241）、`exhausted_hard_limit`（:81）、`time_remaining`（:333）、`stop_for_limit`（:364）每次都遍历全部记录；`initialize`（:160-170）的 else 分支每次调用还再扫一遍做旧数据修补。记录随任务行持久化，长任务（I18 取消默认硬顶后更长）里任务行越来越大，每次调用开销随次数线性增长。
- 涉及行：G22-38
- 要改：
  - `budget.py` 新增 `compact(row, keep_recent=200)`，在 `new_round`（:176-178）里调用：只合并 `round < 当前轮` 且 `status != "reserved"` 的记录进 `state["archived"]`：`{model_calls, tool_calls, input_tokens, output_tokens, cached_input_tokens, charged_tokens, charged_seconds, unknown_input, unknown_output, has_cached}`。当前轮和最近 200 条保持原样（`context_compaction.py:82-90` 倒着找最近已结算的模型记录做校准，`budget.py:127` 倒着找 reserved 记录）。
  - `_totals`（:180-206）lifetime 范围把 `archived` 加进各项（model_floor、tool_floor、known_*、charged_*、unknown 判定、has_cached）；当前轮范围不含 archived。`time_remaining`（:333）的 `used` 同样加 `archived.charged_seconds`。
  - 旧数据修补（:160-170）：只在 `state.get("repaired")` 不为真时扫描一次，完成后写 `state["repaired"] = True`。
  - `public`（:209-216）的 `lifetime`、`current_round` 输出结构不变。
- 新增测试：`tests/test_agent_runtime_budget.py`：(1) 造 3 轮共 600 条记录，`new_round` 后 records ≤ 当前轮 + 200，`public(row)["lifetime"]` 与合并前逐项相等；(2) reserved 记录不被合并，settle 仍找得到；(3) 合并后 `time_remaining`、`exhausted_hard_limit` 与合并前一致；(4) 修补只在第一次 initialize 执行；(5) `context_compaction` 用量校准在合并后仍取得到最近记录。
- 依赖：无（与 I18 同文件，先后均可）。
- 兼容和风险：存储新增 `archived`、`repaired`；回滚到旧代码会忽略 archived，汇总只会低估用量。同一轮内记录仍会增长，本项不处理。
- 工作量：中
- 影响：低

## S07-I23 服务方没返回输出用量时，按文字内容估算输出 token，不再按 JSON 字节数
- 问题（G22-39）：`runtime.py:879-882` 把整条响应消息用 `encoded()`（`store.py:151-152`，UTF-8 的紧凑 JSON）的字节数当 `response_bytes`，`budget.py:298-303` 在服务方没给 `output_tokens` 时直接令 `out = response_bytes`（每字节 1 token，`output_estimate_basis="response_utf8_bytes"`）。中文每字 3 字节、实际约 1-1.5 token；英文约 4 字节 1 token；字段名和引号也被算进去。charged_tokens 偏大，更早碰到 token 硬上限，也影响 `context_compaction` 的校准。
- 涉及行：G22-39
- 要改：
  - `budget.py` 新增 `estimate_message_tokens(message)`：遍历消息里的文本字段（content、tool_calls 的 arguments 字符串），CJK 字符（`\u2e80-\u9fff`、全角标点）按 1.5 token/字，其余字节按 3 字节 1 token，向上取整，偏保守。
  - `runtime.py:879-888`：在算 `response_bytes` 的同处额外算 `response_tokens_est`，传给 `_settle_usage`（:890）和 `budget.settle`；`budget.py:298-303` 优先用它，没有时才回落 `response_bytes`；`output_estimate_basis` 写 `"response_text_estimate"`。`output_tokens` 仍保持 None，不冒充服务方计费（沿用 :299-300 注释）。
- 新增测试：`tests/test_agent_runtime_budget.py`：(1) 3000 个汉字的响应：旧算法约 9000+，新算法约 4500；(2) 3000 字节纯英文约 1000；(3) 服务方给了 output_tokens 时不走估算；(4) 没传估算值时回落 response_bytes，与现在一致。
- 依赖：无。
- 兼容和风险：估算变小后同样任务晚触发 token 上限；仍高于真实分词，硬上限不会失效。旧记录的 `output_estimate_basis` 值保留。
- 工作量：小
- 影响：低
