# Ryn Agent 能力与可靠性升级计划（核实纠正版）

> **跨 AI 交接索引**：
> - 实时进度、基线记录、检查单与交接日志见同目录：[`PROGRESS.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/PROGRESS.md)
> - 仓内既有实测与故障注入脚本参考：
>   - [`docs/acceptance/agent-speedups-20261015/README.md`](file:///d:/code/rynmesh/docs/acceptance/agent-speedups-20261015/README.md)
>   - [`docs/acceptance/agent-speedups-20261015/live-results.md`](file:///d:/code/rynmesh/docs/acceptance/agent-speedups-20261015/live-results.md)
>   - [`docs/acceptance/agent-reliability-20261016/live_rg_recovery.py`](file:///d:/code/rynmesh/docs/acceptance/agent-reliability-20261016/live_rg_recovery.py)
>   - [`docs/acceptance/agent-reliability-20261016/live-rg-recovery.json`](file:///d:/code/rynmesh/docs/acceptance/agent-reliability-20261016/live-rg-recovery.json)

---

## 零、 方案设计总原则与实施边界（所有接手 AI 必须遵守）

1. **证据分级标注**：所有重要结论与目标必须明确标注为 **【已确认】**（有当前源码或仓内实测日志直接支撑）、**【合理预期】**（逻辑推导成立、待本轮修改后实测确认）或 **【待验证】**（尚缺埋点数据或对照实验支撑，禁止预先承诺收益）。
2. **保留错误码，扩展诊断字典**：保留所有现有 `AgentError` / `AgentToolRejected` 错误码字符串，仅通过 `correction` 和 `details` 扩展诊断信息；诊断内容严格遵守原工作区访问边界、输出长度上限与凭据脱敏规则。
3. **四维兼容性保障**：每项改动均明确覆盖**行为兼容**、**权限兼容**、**数据兼容**和**会话/崩溃恢复兼容**。
4. **严守实施边界**：
   - **只读与写操作分类边界**：仅由宿主根据工具元数据与字面量只读识别器生成并校验操作分类，绑定工具名、参数摘要、任务与工作区上下文；绝不接受模型或调用参数自报 `"read"` 绕过写保护；对缺少可信分类的历史记录采取保守处理（按写操作核实）。
   - **文件版本校验边界**：`observed_files` 必须明确定义新建、覆盖、删除、移动、任务重启和工作区变化时的状态流转；文件版本校验用于检测写入前已发生的版本变化（**【已确认】** 可拦截校验时刻已存在的外部修改），但**不承诺消除校验后至落盘瞬间的操作系统级外部并发写入竞争**。
   - **Patch 预检原子性边界**：“整个 patch 零写入”严格限定为 `_patch_plan()` 阶段的**定位失败、匹配歧义、区间重叠、路径越界或大小超限等预检失败**（因为预检全部在内存中完成后才开始写盘）；**不扩大为进程崩溃、断电或磁盘 I/O 故障时的跨文件事务回滚保证**。
   - **不自动修改新增代码缩进，不取消必要的版本冲突检查**。
   - **`baseline` 定位边界**：[`local_tools.py:350-450`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L350-L450) 中的 `baseline` 仅提供 `.orig` 原始文件备份与 `change_review` 变更差异审阅材料，**当前并未实现自动回滚能力**。
   - **安全特点客观表述**：Codex CLI 自身具备沙箱、审批和网络控制机制；Ryn 的 P2P 提供端执行隔离、任务级作用域绑定与 `OperationLedger` 对账属于 Ryn 架构特点，**不据此宣称整体安全性优于 Codex**。
   - **保留 P2P 提供端执行隔离**：任何阶段或实验均不得解除远端模型提供端的工具执行隔离。
5. **留痕与保护现有工作**：工作区已有未提交修改必须完整保留并与本轮改动区分；每完成一步必须更新 [`PROGRESS.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/PROGRESS.md)，只将已完成并通过验证的条目记为 `Completed`。

---

## 一、 现状核对：已确认问题、已有能力与待验证假设

### 1.1 已确认的问题（【已确认】，基于当前源码与既有实测核实）

| 编号 | 问题摘要 | 源码位置与实测核实 |
| :--- | :--- | :--- |
| **C-1** | **宿主已识别为只读的 `ryn_shell_execute` 在异常、超时与重启恢复链路中仍按 `effect="write"` 处理，误入 `agent_write_outcome_unknown`** | 1. [`local_specs.py:39`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_specs.py#L39) 将 `ryn_shell_execute` 静态标记为 `effect="write"`；<br>2. [`execution_policy.py:204-228`](file:///d:/code/rynmesh/rynmesh/agent_runtime/execution_policy.py#L204-L228) 的 `decide()` 对 `shell_read(call["arguments"])` 为真的命令返回 `Decision("execute", "read")`，[`loop.py:579-652`](file:///d:/code/rynmesh/rynmesh/agent_runtime/loop.py#L579-L652) 记录 `in_flight["authorization"] = "read"`，但此前 `in_flight["effect"]` 保持 `"write"` 且缺少统一的宿主操作分类绑定；<br>3. [`local_tools.py:641-850`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L641-L850) 的 `LocalTools.execute()` 对所有 `ryn_shell_execute` 统一写入 `OperationLedger`，此前一旦子进程启动后发生异常便抛出 `AgentError("agent_local_operation_unconfirmed")`；<br>4. [`control.py:56-67`](file:///d:/code/rynmesh/rynmesh/agent_runtime/control.py#L56-L67) 的 `ambiguous(row)`、[`runtime.py:1306-1373`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1306-L1373) 的 `reconcile()`、[`runtime.py:1499-1548`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1499-L1548) 的 `_fail()`、[`runtime.py:1451-1485`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1451-L1485) 的 `_reconcile_operation()`、[`runtime.py:1621-1676`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1621-L1676) 的 `_handle_operation_error()` 此前只按 `effect == "write"` 判定，导致此前余额查询任务中只读 shell 异常或中断后进入 `agent_write_outcome_unknown` 无法自动恢复（注：余额查询任务属于用户侧任务观测反馈，未收录于仓内 `live-results.md`，特此区分说明）。 |
| **C-2** | **模型来源配置变化后，已有任务因 `agent_model_destination_changed` 停止，且携带资源作用域重绑时被 `_validate_context` 自锁** | 1. [`runtime.py:254`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L254) 与 [`bridge.py:114-160`](file:///d:/code/rynmesh/rynmesh/agent_runtime/bridge.py#L114-L160) 在当前模型来源签名与任务保存的 `model_destination` 不一致时抛出 `agent_model_destination_changed`；<br>2. [`model_switch.py:36-52`](file:///d:/code/rynmesh/rynmesh/agent_runtime/model_switch.py#L36-L52) 修复前在 `transfer_resource_scope=True` 时直接调用 `runtime._validate_context(selected)`，而该函数内部会因旧 `model_destination` 失配直接抛错，阻碍用户显式确认新来源绑定并保留原工作区/项目范围继续任务。 |
| **C-3** | **磁盘排查真实任务中存在重复扫描、空轮询与跨步长间隙** | 在用户本机真实任务历史（`~/.rynmesh/agent` 中的两次磁盘排查任务 `agent_a655814b906c4fb4bd4ff575ee072bc5` 与 `agent_37c42d2f078b4c9ca599695cfed90cc7`，非仓内 `live-results.md` 表格）中：<br>1. 两次运行均耗时约 279s~281s 后触发 `agent_time_limit`；<br>2. 模型启动后台 PowerShell 枚举后，[`local_processes.py:193-242`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_processes.py#L193-L242) 的 `Processes.read()` 在零等待连续轮询或进程正常退出时缺乏空轮询收敛与完整 `stop_reason`，随后模型又叠加启动新的扫描进程；<br>3. `tool_result` 与下一次 `model_started` 之间存在 20s~70s 间隙（具体归因见 1.3 节 **H-1【待验证】**）。 |
| **C-4** | **`max_output_chars` 校验将工具调用参数计入可见消息长度，拒绝合规大 `ryn_apply_patch`** | 1. 主调用链在 [`output_policy.py:16`](file:///d:/code/rynmesh/rynmesh/agent_runtime/output_policy.py#L16) 中默认 `max_output_tokens = 4096`（`512` 仅为 [`runtime.py:192-193`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L192-L193) 预算预留值与 [`codex_tools.py:168`](file:///d:/code/rynmesh/rynmesh/llm_package/codex_tools.py#L168) 缺参兜底值）；<br>2. [`local_specs.py:32`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_specs.py#L32) 允许 `ryn_apply_patch.patch` 达 `90,000` 字符，[`tools.py:354`](file:///d:/code/rynmesh/rynmesh/agent_runtime/tools.py#L354) 允许本地工具参数达 `96 KB`；<br>3. 但 [`loop.py:216`](file:///d:/code/rynmesh/rynmesh/agent_runtime/loop.py#L216) 检查 `len(encoded(output_policy.visible_message(raw))) > selected["limits"]["max_output_chars"]`（默认 `20,000`，见 [`store.py:49`](file:///d:/code/rynmesh/rynmesh/agent_runtime/store.py#L49)），而 [`output_policy.py:45-47`](file:///d:/code/rynmesh/rynmesh/agent_runtime/output_policy.py#L45-L47) 的 `visible_message(raw)` 包含 `tool_calls` 参数，导致超过约 20 KB 的合规 patch 触发 `agent_output_limit`。 |
| **C-5** | **`ryn_apply_patch` 返回首个匹配而非唯一匹配，且单向游标不支持乱序 hunk** | 1. [`local_patch.py:106-117`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_patch.py#L106-L117) 的 `_seek()` 按 `(原样, str.rstrip, str.strip)` 顺序扫描并在找到**第一个**匹配位置时立即返回，**并不检查或保证匹配唯一性**；<br>2. [`local_patch.py:123-143`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_patch.py#L123-L143) 的 `apply_hunks()` 虽会在找到 `anchor` 后继续搜索完整 `old` 块，但因使用单向递增 `cursor` 边找边改，当模型生成的多个 hunk 按文件位置乱序排列时会失配，且重复上下文缺乏歧义拦截。 |
| **C-6** | **`ryn_file_search` 缺少正则、大小写控制与文件通配过滤；`rg` 依赖宿主环境 PATH** | 1. [`local_tools.py:926-1020`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L926-L1020) 的 `ryn_file_search` 仅支持区分大小写的子串匹配（`args["query"] in line`），不支持 `regex`、`case_sensitive`、`file_pattern`；<br>2. [`local_processes.py:324-328`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_processes.py#L324-L328) 的 `prepare_shell_command()` 通过 `shutil.which("rg")` 查找系统 `rg`，未随桌面包内置 `rg` 二进制（注：[`live_rg_recovery.py:149-150`](file:///d:/code/rynmesh/docs/acceptance/agent-reliability-20261016/live_rg_recovery.py#L149-L150) 主动 monkeypatch 屏蔽了 `rg` 并提示模型优先调用 `rg` 以测试恢复链路，不能将该脚本作为宿主机实际缺少 `rg` 的证据，也不能将其中首轮模型调用的 `8.969s` 全部算作缺 `rg` 的额外开销）。 |
| **C-7** | **`_handle_operation_error` 在可恢复工具校验拒绝后未设置 `_step.clean = True`** | [`runtime.py:1621-1676`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1621-L1676) 的 `_handle_operation_error()` 在记录无副作用错误回执并将状态置回 `queued` 后未设置 `self._step.clean = True`，使 `run_continuously()`（[`runtime.py:1680-1695`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1680-L1695)）退出当前连续步循环并等待下一次 worker 调度。 |

### 1.2 已有能力（【已确认】，必须在升级中保留并复用）

1. **工具目录常驻与增量同步**：
   - [`tool_menu.py:9-16`](file:///d:/code/rynmesh/rynmesh/agent_runtime/tool_menu.py#L9-L16) 已定义工具发现入口 `NAME = "ryn_discover_tools"`、插件发现 `PLUGIN_DISCOVERY_TOOLS = ("ryn_list_plugins",)`（1 个）以及核心工作站工具 `CORE_TOOLS = ("ryn_shell_execute", "ryn_file_read", "ryn_file_search", "ryn_apply_patch", "ryn_file_edit", "ryn_file_write", "ryn_file_list", "ryn_process_read")`（共 **8 个**）；[`runtime.py:210`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L210) 将 `PLUGIN_DISCOVERY_TOOLS`、可选 `ryn_project_inspect` 与 `CORE_TOOLS` 组合为 `pinned_names`。
   - **重要历史约束**（见 [`docs/acceptance/agent-speedups-20261015/README.md:48`](file:///d:/code/rynmesh/docs/acceptance/agent-speedups-20261015/README.md#L48)）：`pin_limit = min(seed_limit, max_context_bytes // 10)` 是为了兼容 `12 KB` 小上下文窗口测试，避免常驻工具挤压对话历史导致额外 compaction。
   - [`codex_threads.py:216-221`](file:///d:/code/rynmesh/rynmesh/llm_package/codex_threads.py#L216-L221) 已支持检测工具 schema 集合增量并发送 `functions_update` 与单行 `Server control state:`。
2. **`ryn_file_search` 已有分页、排序与停止原因标识**：
   - [`local_specs.py:28`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_specs.py#L28) 与 [`local_tools.py:926-1020`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L926-L1020) 已实现 `offset` / `next_offset`（每页 15 条匹配）、按匹配数排序的 `files_matched`（前 25 个文件）、扫描上限（`MAX_SEARCH_FILES = 10000`、`1000` 条匹配、`MAX_SEARCH_SECONDS = 5.0`）、`incomplete` 与 `stopped_reason`（`"match_limit"` / `"file_limit"` / `"time_limit"`）以及 `.gitignore` 剪枝 `pruned_directories`。
3. **写操作账本（`OperationLedger`）与原始备份审阅（`baseline` / `change_review`）**：
   - [`local_tools.py:641-850`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L641-L850) 已实现 `OperationLedger` 落盘与并发去重；[`local_tools.py:350-450`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L350-L450) 已实现首次修改文件前的 `.orig` 备份（`baseline`）及净变更 diff 生成（`change_review`），供完成校验时作为审阅材料。
4. **默认 Token 预算与大结果加密转存**：
   - [`output_policy.py:16`](file:///d:/code/rynmesh/rynmesh/agent_runtime/output_policy.py#L16) 主调用链默认 `max_output_tokens` 已为 `4096`；[`loop.py:680-690`](file:///d:/code/rynmesh/rynmesh/agent_runtime/loop.py#L680-L690) 已支持超长工具结果加密保存并通过 `ryn_read_result` 分段读取。

### 1.3 待验证的假设（【待验证】，需通过埋点测量或对照实验证实）

1. **H-1（磁盘排查 20s~70s 长间隙的具体归因）【待验证】**：在用户本机历史磁盘排查任务（`agent_a655814b906c4fb4bd4ff575ee072bc5` 与 `agent_37c42d2f078b4c9ca599695cfed90cc7`）中，跨步长间隙究竟主要来自模型服务端排队/推理、工具进程等待、加密状态存储写盘还是后台 worker 调度等待，需在阶段 0.3 完成独立分段计时后按实测数据归因。
2. **H-2（提示词按需裁剪对首轮延迟与命中率的影响）【待验证】**：对 [`prompt.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/prompt.py) 非核心段落按需加载的实际耗时影响需通过实测验证。
3. **H-3（Codex 原生工具协议对比 `outputSchema` JSON 提案包装的效果差异）【待验证】**：在相同模型、推理强度、任务、资料与 P2P 权限隔离条件下，接入原生工具协议对完成率、纠错次数和耗时的实际影响仍属待验证假设，作为阶段四独立对照实验安排。

---

## 二、 分阶段实施计划与实施边界

### 阶段零（最高优先级）：修复真实任务暴露的阻断问题与耗时瓶颈

#### Stage 0.1 只读 shell 执行、异常处理与重启恢复修复
- **目标（【已确认】问题修复）**：宿主确认的只读 `ryn_shell_execute` 在异常、超时和重启恢复时，不再被误判为结果未知的写操作（`agent_write_outcome_unknown`）；真正结果不明的写操作继续保留原有 `OperationLedger` 与 `agent_write_outcome_unknown` 核实机制。
- **实现要求与安全边界**：
  1. **统一宿主生成的操作分类**（[`execution_policy.py:95-201`](file:///d:/code/rynmesh/rynmesh/agent_runtime/execution_policy.py#L95-L201)）：
     - 新增由宿主生成并校验的分类函数 `classify_operation(name, arguments, metadata, ...)` 与校验函数 `verified_read_classification(...)`。
     - 分类记录显式绑定：`operation_id`、`task_id`、`tool`（工具名）、`arguments_sha256`、`workspace_root`、`goal_revision`、`scope_revision`、`context_epoch`、`control_revision`、`effect_class`（`"read"` 或 `"write"`）及 `basis`（`"static_read"`、`"literal_shell_read"` 或 `"write"`）。
     - **严禁信任外部自报**：绝不读取或采信 `arguments` 或未校验上下文字典中自报的 `"effect"` / `"authorization"` / `"classification"`；对于 `ryn_shell_execute`，必须且仅能通过宿主 `shell_read(arguments)` 在校验绑定的 `arguments_sha256` 一致且无 `requires_explicit_approval` 时判定为 `"read"`。
  2. **全链路复用同一分类**（[`loop.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/loop.py)、[`local_tools.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py)、[`control.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/control.py)、[`runtime.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py)、[`verification.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/verification.py)）：
     - **执行与账本**（`loop.py`、`local_tools.py`、`local_processes.py`、`operation_ledger.py`）：
       - `loop.py` 在 `authorize()`（[`loop.py:579-652`](file:///d:/code/rynmesh/rynmesh/agent_runtime/loop.py#L579-L652)）中生成宿主 `operation_classification` 并写入 `row["in_flight"]["operation_classification"]` 与 `receipt`。
       - `LocalTools.execute()`（[`local_tools.py:641-850`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L641-L850)）校验上下文中的分类（若存在过期/伪造/摘要不匹配的分类则拒绝按只读处理），并将宿主分类写入 `OperationLedger`。
       - `Processes.start()`（[`local_processes.py:55-124`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_processes.py#L55-L124)）在启动进程时绑定 `operation_id`；当只读 shell 发生超时、取消或中断时终止子进程，并返回带明确 `stop_reason` 的结果或抛出明确的只读中断错误，**绝不伪造成功**。
       - 若只读 shell 在进程启动后发生执行异常，调用 `LocalTools.recover_operation()`（[`local_tools.py:1186-1297`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L1186-L1297)）与 `Processes.recover_operation()`（[`local_processes.py:135-185`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_processes.py#L135-L185)）贯通子进程清理结果（`cleanup_confirmed`、`still_running`、`cleanup_error`）；仅当确认子进程已清理（`cleanup_confirmed=True` 且 `still_running=False`）时，才通过 `OperationLedger.update()`（[`operation_ledger.py:69-90`](file:///d:/code/rynmesh/rynmesh/agent_runtime/operation_ledger.py#L69-L90)）将账本结算为已结束的只读失败（`status="failed"`, `no_mutation=True`, `cleanup_confirmed=True`）并**释放 `OperationLedger` 的 `receipt_reserve` 预留**，同时抛出 `AgentError("agent_read_interrupted")`；若清理抛异常或 `still_running=True`，则保留未知结果保护（抛出 `AgentError("agent_local_operation_unconfirmed")`、保留 `receipt_reserve` 与 `in_flight`），绝不盲目视为恢复成功。
       - **真实崩溃窗口结算（`confirmed` / `not_executed` / `intent`）**：若 `LocalTools.execute()` 已将 `OperationLedger` 更新为 `confirmed` 或 `not_executed`（或停在 `intent` 尚未派发进程），但宿主在 `AgentLoop` 保存工具结果前崩溃重启，`LocalTools.recover_operation()` 校验 `binding` 与 `verified_read_classification` 后直接复用已有账本回执（`ledger_outcome="confirmed"` / `"not_executed"` 及 `settled_result`）完成结算并释放 `receipt_reserve`，不重新执行命令。
     - **防重复启动仍在运行的旧进程与跨进程重启清理**（`local_processes.py`、`local_tools.py`、`runtime.py`）：
       - 恢复或重入检查时，通过 `Processes.running_for_operation()` 检查同一 `(task_id, operation_id)` 是否仍有未退出的旧进程；若旧进程仍在运行，先终止并等待其退出或拒绝重复启动，避免同一操作或恢复后并发跑两个相同进程。
       - 测试与平台验证边界区分三类场景：
         1. **同一宿主进程内复用 `tools` 重建 `AgentRuntime` 的状态恢复测试**（【已确认】通过）；
         2. **独立宿主子进程正常退出（触发 `atexit`）清理子进程并由全新宿主实例 `reconcile` 的测试**（【已确认】Windows 平台通过）；
         3. **独立宿主子进程强制退出（`os._exit(9)` 绕过 `atexit`）验证操作系统内核级子进程清理的测试**（**【已确认】当前 Windows 平台通过 JobObject `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 验证通过**；**POSIX / Linux / macOS 平台绕过 `atexit` 的强制宿主退出清理尚未在对应系统实测，明确标为【未验证】**）。
     - **异常处理、取消、重启恢复与清理暂时失败后的再次检查路径**（`control.py`、`runtime.py`）：
       - `control.recoverable_read_in_flight(row)`（[`control.py:42-53`](file:///d:/code/rynmesh/rynmesh/agent_runtime/control.py#L42-L53)）与 `control.ambiguous(row)`（[`control.py:56-67`](file:///d:/code/rynmesh/rynmesh/agent_runtime/control.py#L56-L67)）：仅当 `flight` 未通过 `verified_read_classification` 且 `flight.get("effect") == "write" and flight.get("dispatched")`（或 `row.get("error") == "agent_write_outcome_unknown"`）时才判定为 `ambiguous`；当只读操作因清理暂时失败保留 `in_flight` 处于 `interrupted` 时，`allowed_actions(row, settings)`（[`control.py:70-95`](file:///d:/code/rynmesh/rynmesh/agent_runtime/control.py#L70-L95)）同时开放 `"resume"` 与 `"reconcile"`。
       - `runtime.reconcile()`（[`runtime.py:1306-1373`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1306-L1373)）、`runtime.action("resume" / "reconcile")`（[`runtime.py:1056-1166`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1056-L1166)）与 `runtime._fail()`（[`runtime.py:1499-1548`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1499-L1548)）：通过 `_recover_read_operation_details()` 与 `_settle_interrupted_read()` 贯通清理与账本状态；若初次清理暂时失败（`still_running=True` 或清理异常），任务停在 `interrupted` 且保留 `in_flight`；待旧进程随后退出后，用户执行 `action("resume")`、`action("reconcile")` 或宿主再次调用 `runtime.reconcile()` 均可重新检查并完成结算恢复，不会永久卡死在 `in_flight`。
       - `runtime._read_may_continue_after_restart()`（[`runtime.py:1487-1496`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1487-L1496)）：比较终身工具调用预算时统一使用 `lifetime["charged_tool_calls"]`（与 [`runtime.py:1570`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1570) 的 `_within_limits` 预算语义一致），兼容旧版任务迁移后 `lifetime["tool_calls"]` 为 `None` 的场景。
       - `runtime._handle_operation_error()`（[`runtime.py:1621-1676`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1621-L1676)）：对通过 `verified_read_classification` 且 `cleanup_confirmed` 的只读 shell 执行异常/超时/取消，生成明确错误回执（`not_executed` 按是否实际启动区分，`no_mutation=True`），让模型感知中断并调整或继续。
     - **历史记录保守处理**：对缺少可信 `operation_classification` 绑定或摘要校验不通过的历史 `in_flight` / `receipt` / `ledger` 记录，一律保守按写操作（`effect="write"`）处理，绝不批量将未知写入改为只读。
     - **Live 验收覆盖范围明确区分（严禁将测试夹具改写工具调用描述为模型自主选择）**：
       - [`live_stage01_read_shell_recovery.py`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live_stage01_read_shell_recovery.py)：在提示词中指定了首选工具与故障后备选路线，属于**提示指定恢复路线的异常恢复测试**；
       - [`live_stage01_natural_read.py`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live_stage01_natural_read.py)：分别独立记录两条子场景：
         1. `unhinted_natural_query`（**无提示自然语言查询**）：不提示工具名与恢复路线，如实记录模型自主选择的工具序列（实测模型自主选择 `ryn_file_list` -> `ryn_file_read`，未触发 shell 故障钩子）；
         2. `injected_restart_state_model_continuation`（**人工注入重启状态后的模型续跑测试**）：模型首轮在无提示下自主提议的是 `["ryn_file_list"]`，**由测试夹具人工将其改写/注入为在途只读 `ryn_shell_execute`** 并模拟宿主重启与 `reconcile()` 生成 `agent_node_restarted` 回执，用于验证模型在收到重启中断回执后能否无提示续跑完成任务（绝不将首轮被夹具改写的 `ryn_shell_execute` 描述为模型自主选择）。

#### Stage 0.2 模型来源配置变化（`agent_model_destination_changed`）的显式重绑与恢复流程
- **目标（【已确认】问题修复）**：修复 [`model_switch.py:36-52`](file:///d:/code/rynmesh/rynmesh/agent_runtime/model_switch.py#L36-L52) 在 `transfer_resource_scope=True` 时因旧 `model_destination` 失配被 `_validate_context()` 自锁的问题，支持任务所有者通过 `model_switch_preview` + `switch_model(..., transfer_resource_scope=True)` 显式确认新来源绑定（可重绑同名模型的更新后来源或切换至新模型）、保留本地 `project` 作用域、递增 `scope_revision` 与 `context_epoch`，并在显式 `resume` 后继续任务；同时严格保留 NAS `resource_grants`（`egress_grants` 绑定特定模型来源时必须走 `update_scope` 重新授权）的隔离不变量，绝不静默切换模型来源。

#### Stage 0.3 磁盘排查中的重叠扫描、空轮询与长调度间隙：分段测量与针对性治理
- **目标（【已确认】空轮询/重叠扫描修复 + 分段计时归因）**：
  1. 在 [`runtime.py:1772-1865`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1772-L1865) 与 [`loop.py:90-258`](file:///d:/code/rynmesh/rynmesh/agent_runtime/loop.py#L90-L258)、[`loop.py:548-854`](file:///d:/code/rynmesh/rynmesh/agent_runtime/loop.py#L548-L854) 中分别测量并记录 `scheduler_wait_seconds`、`preparation_seconds`、`model_call_seconds`、`tool_execution_seconds`、`store_persistence_seconds`；
  2. 在 [`local_processes.py:193-242`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_processes.py#L193-L242) 与 [`local_tools.py:1096-1098`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L1096-L1098) 中修复 `ryn_process_read` 连续同 `offset` 零等待空轮询（自动合并短暂等待并返回 `empty_poll_count` 与 `poll_notice`）与进程退出后 `stop_reason` 缺失（补全 `"exited"` / `"exited_nonzero"`）；
  3. 在 [`local_tools.py:1075-1095`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L1075-L1095) 启动新 `ryn_shell_execute` 时检测同任务仍在运行的后台进程，返回 `active_background_processes` 与 `background_process_notice` 提示，减少重叠全盘扫描。

#### Stage 0.4 明确无副作用错误后的连续执行条件（`_step.clean = True`）
- **目标（【合理预期】减少单步错误回传后的固定轮询等待）**：
  - 仅在无副作用错误已正确回传、任务重新进入 `queued`、无取消/暂停/待审批/待输入、预算允许且非连续重复同一 `(tool_name, error_code)` 错误时，才设置 `self._step.clean = True`，并保留重复错误控制与退避。不将此项优化夸大为解决所有长等待。

---

### 阶段一：工具预算对齐、Patch 容错收紧、版本冲突校验、诊断增强与内置 `rg`

#### Stage 1.1 统一并解耦各层输出与存储预算
- **目标（【已确认】问题修复）**：将面向用户的回答正文上限（`max_output_chars`，默认 `20,000`，保留用户显式设置）与工具调用参数上限（本地工具 `96 KB`、`ryn_apply_patch` `90,000` 字符）及工具结果/存储预算分别定义并对齐，验证大型 patch 的完整执行路径。

#### Stage 1.2 收紧 `ryn_apply_patch` 容错设计
- **目标（【已确认】问题修复）**：
  - 在同一份不可变原始文件快照上定位全部 hunk；
  - 要求每个 hunk 匹配明确唯一（改变当前 `_seek` 返回首个匹配的做法），且修改范围互不重叠；
  - 全部定位成功后按起始位置**倒序**应用，支持乱序 hunk；
  - **预检失败零写入边界**：当出现定位失败、匹配歧义、范围重叠、路径/大小校验失败等预检错误时，在写盘前拒绝，保证整个 patch 不写入任何文件（注：此保证限定于预检阶段，不承诺进程崩溃或磁盘故障时的跨文件操作系统事务回滚）；
  - **不自动修改新增代码缩进**：保留 CRLF/LF 换行符与行尾空白（`rstrip`）兼容；缩进失配时通过 `correction`/`details` 返回受控实际文件片段，由模型修正。

#### Stage 1.3 文件版本冲突检测（`expected_sha256` 与宿主 `observed_files` 版本管理）
- **目标（【合理预期】减少模型漏传长 hash 导致的无效往返，同时保持版本冲突拦截）**：
  - 保留 `expected_sha256` 校验，绝不采用“未传 hash 就直接盲写并以 `baseline` 代替冲突检测”的做法；
  - 在任务状态中维护宿主级文件观测版本表 `observed_files[normalized_path] = {"sha256": ..., "observed_at": ..., "scope_revision": ...}`，并严格定义其生命周期边界：
    1. **读取（`ryn_file_read`）**：记录或更新该文件当前磁盘 `sha256`（即使用分页读取，也记录完整文件的 `sha256`）；
    2. **新建（`ryn_file_write` 新文件 / `ryn_apply_patch` `Add File`）**：写入前若目标路径已存在则拒绝（`agent_patch_file_exists` 或需先读取）；写入成功后将新文件 `sha256` 写入 `observed_files`；
    3. **覆盖/编辑（`ryn_file_edit` / `ryn_file_write` 已存在文件 / `ryn_apply_patch` `Update File`）**：写入前重新计算磁盘当前 `sha256`，与显式传入的 `expected_sha256`（优先）或 `observed_files[path]["sha256"]` 比较；若文件未曾读取且未传 `expected_sha256`，或磁盘 `sha256` 已变化，则拒绝覆盖并返回 `agent_local_file_changed_read_again` 及重新读取提示；写入成功后更新 `observed_files[path]` 为写入后的新 `sha256`；
    4. **删除（`ryn_file_manage(delete)` / `ryn_apply_patch` `Delete File`）**：校验刪除前版本匹配后，从 `observed_files` 中移除该路径（或标记为已删除）；
    5. **移动/复制（`ryn_file_manage(move/copy)` / `ryn_apply_patch` `Move to`）**：校验源文件路径版本匹配、目标路径不存在；完成后更新目标路径 `observed_files[destination] = sha256`，若为移动则移除原路径记录；
    6. **任务重启（`reconcile`）**：`observed_files` 随任务持久化在加密 `AgentStore` 中；任务重启恢复后，后续写入仍会将 `observed_files` 中保存的 `sha256` 与磁盘当前实际 `sha256` 再次比对，若重启期间外部修改了文件则立即检出冲突并要求重新读取；
    7. **工作区或项目绑定变化**：当工作区路径或 `project_generation` 变化时，清空旧工作区的 `observed_files` 记录（且现有策略本来就拒绝跨工作区继续旧任务）。
  - **并发边界声明**：该机制用于检测写入前已发生的版本变化，不承诺消除校验后至操作系统写盘瞬间的极端外部并发写竞争。

#### Stage 1.4 完善工具诊断与搜索（`ryn_file_search` 增强）
- **目标（【合理预期】提升工具失败可诊断性与本地搜索表达力）**：
  - 保留原错误码，在 `correction` / `details` 中提供文件路径、`hunk_index`、`match_count`、字段路径和 $\le 15$ 行受控实际上下文，严格遵守原文件工作区访问范围、输出长度上限（$\le 1600$ 字符）与敏感凭据脱敏；
  - `ryn_file_search` 在保留现有默认值、分页（`offset` / `next_offset`）、`incomplete` 与 `stopped_reason` 的基础上，补充 `regex: bool = False`、`case_sensitive: bool = True`、`file_pattern: str | None = None`，并处理无效正则错误（`agent_invalid_search_regex`）。

#### Stage 1.5 内置 `rg`（ripgrep）随包分发
- **目标（【合理预期】消除宿主机环境是否安装 `rg` 的差异）**：
  - 核实当前桌面打包配置路径为 [`webapp/src-tauri/tauri.conf.json`](file:///d:/code/rynmesh/webapp/src-tauri/tauri.conf.json)（而非 `desktop/src-tauri`），结合 [`pyproject.toml`](file:///d:/code/rynmesh/pyproject.toml) 与 [`local_processes.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_processes.py) 实现 `resolve_rg_executable()`（优先使用随包内置的 `rg` 二进制，回退系统 `PATH`），保留对 `--pre` / `--hostname-bin` 的安全拦截。

---

### 阶段二：保留已有工具与线程机制下的渐进补强
- **Stage 2.1 `CORE_TOOLS` 常驻条件补强**（[`tool_menu.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/tool_menu.py)）：在完整保留 `NAME = "ryn_discover_tools"`、`PLUGIN_DISCOVERY_TOOLS`（1 个）与 `CORE_TOOLS`（8 个）及 `< 48 KB` 小上下文窗口（如 `12 KB` 压缩测试）兼容的前提下，补强常规上下文窗口下核心工具的常驻配额。
- **Stage 2.2 提示词结构化与线程复用验证**（[`prompt.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/prompt.py)、[`codex_threads.py`](file:///d:/code/rynmesh/rynmesh/llm_package/codex_threads.py)）：保留 `Server control state:` 单行格式、系统提示词正文稳定性与历史一致性；以可验证条件验收工具执行、权限回执变化、动态工具加载和同窗追问后的线程复用。

---

### 阶段三：扩充验证计划
- **Stage 3.1 记录现有测试基线**：在修改业务代码前运行并记录相关测试套件的准确命令、通过/失败/跳过数量。
- **Stage 3.2 专项单元与集成测试**：覆盖只读 shell 异常与重启恢复（含防伪造/防重复启动）、模型来源变化后显式重绑继续、重复 anchor/重复完整上下文/乱序及重叠 hunk、缩进保持与预检失败零写入、模型读取后文件被外部修改、搜索过滤/无效正则/超时/分页、错误反馈后继续与继续前取消、大 patch 跨各层预算。
- **Stage 3.3 真实自然语言任务验收**：在隔离测试资料上验收余额查询、磁盘排查和文件修改，如实记录完成情况、首个有用反馈时间、总耗时和人工介入次数。

---

### 阶段四：独立安排原生工具协议对照实验（【待验证】）
- 将 Codex 原生工具协议接入列为独立对照实验，保持模型、推理强度、任务、资料和权限条件可比，严守 P2P 提供端执行隔离；先验证当前二进制与运行环境可用性，再实测对比完成率、纠错次数与耗时，不预先承诺能力跃升。
