# Ryn Agent 升级任务进度与跨 AI 交接记录（PROGRESS & HANDOFF）

- **文档创建时间**：2026-10-16
- **总体方案文档**：[`docs/acceptance/agent-upgrade-20261016/PLAN.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/PLAN.md)
- **当前所处阶段**：**全部阶段已完成并验证（Completed）** —— 包含 **Doc Fix**、**Baseline**、**Stage 0.1**、**Stage 0.2**、**Stage 0.3（含计时去重修正）**、**Stage 0.4**、**Stage 1.1–1.5**、**Stage 2.1–2.2**、**Stage 3.2–3.3** 以及 **Stage 4（独立对照实验）**。

---

## 一、 基线环境与工作区已有修改记录（保护现有工作）

1. **AGENTS.md 检查**：仓库根目录 `d:\code\rynmesh\AGENTS.md` 不存在（仅 `.codex-tmp/finance-agent-compare-20261014/` 临时测试夹具下存在无关文件）。
2. **Git 基线状态**：
   - `HEAD` 提交：`c74813dbe5ed01ba265dc47785f52f448e1ff876`（`fix: harden craft studio workspace and agent loop`）
   - 运行环境：Windows / `.venv\Scripts\python.exe`（`Python 3.12.10`）
   - **工作区预存未提交修改（本轮开始前已存在，严禁 reset、覆盖或清理）**：
     - 已修改跟踪文件（17 个）：
       - `rynmesh/agent_runtime/control.py`
       - `rynmesh/agent_runtime/execution_policy.py`
       - `rynmesh/agent_runtime/local_processes.py`
       - `rynmesh/agent_runtime/local_specs.py`
       - `rynmesh/agent_runtime/local_tools.py`
       - `rynmesh/agent_runtime/loop.py`
       - `rynmesh/agent_runtime/projects.py`
       - `rynmesh/agent_runtime/prompt.py`
       - `rynmesh/agent_runtime/routes.py`
       - `rynmesh/agent_runtime/runtime.py`
       - `rynmesh/agent_runtime/store.py`
       - `rynmesh/agent_runtime/tool_menu.py`
       - `rynmesh/agent_runtime/tools.py`
       - `rynmesh/agent_runtime/verification.py`
       - `rynmesh/agent_runtime/workspace_artifacts.py`
       - `tests/test_agent_run_timing.py`
       - `tests/test_desktop_control.py`
     - 未跟踪文件/目录：
       - `docs/acceptance/agent-reliability-20261016/`
       - `rynmesh/agent_runtime/diagnostic_log.py`
       - `rynmesh/node_apps/craft_*.py`
       - `tests/test_agent_execution_efficiency.py`、`tests/test_agent_local_execution_reliability.py`、`tests/test_agent_local_processes.py`、`tests/test_agent_workspace_artifacts.py`、`tests/test_craft_*.py`
       - `webapp/src/pages/craft-*.ts` 等前端文件
3. **Stage 0.1 相关测试基线（修改业务代码前实测）**：
   - 执行命令：
     ```powershell
     .\.venv\Scripts\python.exe -m pytest -q tests/test_agent_local_execution_reliability.py tests/test_agent_local_processes.py tests/test_agent_local_tools.py tests/test_agent_codex_loop.py tests/test_agent_runtime.py
     ```
   - 统计结果：**`122 passed, 1 failed, 2 skipped in 143.59s`**
   - 预存失败（单独记录，不改写为通过、不降低断言）：
     - `tests/test_agent_local_tools.py::test_windows_accessibility_write_and_readback`（Windows UIA 桌面控件枚举在当前会话后台 WinForms 窗口上未检出 `automation_id == "ryn_test_input"`，与本次升级无关）。
   - 跳过项（2 个）：
     - `tests/test_agent_codex_loop.py::test_actual_ripgrep_search_is_automatic_in_legacy_strict_mode`（系统 PATH 未找到 `rg` 时跳过）
     - `tests/test_agent_codex_loop.py::test_automatic_read_ignores_ripgrep_preprocessor_config`（系统 PATH 未找到 `rg` 时跳过）

---

## 二、 跨 AI 接手须知（Handoff Instructions）

1. **先读两份核心文件**：
   - 先读本文件（[`PROGRESS.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/PROGRESS.md)）查看“三、任务进度总表”中全部已完成项与实测产物。
   - 再读 [`PLAN.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/PLAN.md) 与 [`live-results.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-results.md) 了解各阶段实现细节与实测对照数据。
2. **硬性工程边界**：
   - 保留原错误码，仅通过 `correction` 和 `details` 扩展诊断。
   - 只读与写严格由宿主分类并绑定工具名、参数摘要及任务上下文；不接受调用参数自报 `"read"`；缺少可信分类的历史记录按写操作保守处理。
   - 严禁盲写与自动修改新增代码缩进；保留版本冲突检查。
   - 严守 P2P 提供端执行隔离；保护工作区预存未提交修改。
   - 只将已完成并验证的项目标记为 `[x] Completed`；严禁将测试夹具改写的工具调用描述为模型自主选择；未测平台明确标为 **【未验证】**。

---

## 三、 任务进度总表（Checklist）

| 阶段 / 编号 | 任务名称 | 涉及主要文件 | 状态 | 验证测试 / 产物 |
| :--- | :--- | :--- | :--- | :--- |
| **Doc Fix** | **核实并纠正 `PLAN.md` 与 `PROGRESS.md`（12 项事实与实施边界）** | `PLAN.md`, `PROGRESS.md` | `[x] Completed` | [`PLAN.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/PLAN.md) |
| **Baseline** | **记录提交、工作区差异与 Stage 0.1 相关测试基线** | `PROGRESS.md` | `[x] Completed` | `122 passed, 1 failed, 2 skipped` |
| **Stage 0.1** | **宿主确认只读 shell 执行、异常、超时、崩溃窗口回执复用与重启恢复修复** | `execution_policy.py`, `operation_ledger.py`, `loop.py`, `local_tools.py`, `local_processes.py`, `tools.py`, `control.py`, `runtime.py`, `verification.py` | `[x] Completed` | `22 passed` (`test_agent_local_execution_reliability.py`) + 提示指定恢复测试 [`live-stage01-read-shell-recovery.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage01-read-shell-recovery.json) + 分类自然语言/注入续跑验收 [`live-stage01-natural-read.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage01-natural-read.json) |
| **Stage 0.2** | **模型来源变更 (`agent_model_destination_changed`) 显式重绑与恢复** | `model_switch.py`, `models.py`, `tests/test_agent_projects.py` | `[x] Completed` | `80 passed`（6 个模型切换/来源/项目作用域测试套件） |
| **Stage 0.3** | **磁盘排查分段计时（含去重修正）、`ryn_process_read` 防空轮询/补全 `stop_reason` 与重叠扫描提示** | `loop.py`, `runtime.py`, `local_processes.py`, `local_tools.py`, `tests/test_agent_execution_efficiency.py`, `tests/test_agent_local_processes.py` | `[x] Completed` | `33 passed` + 实测 [`live-stage03-disk-investigation.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage03-disk-investigation.json) |
| **Stage 0.4** | **受控条件下无副作用错误连续执行 (`_step.clean=True`) 与退避** | `runtime.py`, `tests/test_agent_execution_efficiency.py` | `[x] Completed` | `33 passed`（`test_agent_execution_efficiency.py` + `test_agent_speedups.py` + `test_agent_local_processes.py`） |
| **Stage 1.1** | **解耦回答文本 (`max_output_chars`) 与工具参数预算，验证大 Patch** | `output_policy.py`, `loop.py`, `tests/test_agent_apply_patch.py` | `[x] Completed` | `25 passed`（`test_agent_apply_patch.py` 含 `>32 KB` Patch 端到端测试） |
| **Stage 1.2** | **收紧 `ryn_apply_patch`（原始快照唯一匹配、无重叠倒序应用、不改缩进）** | `local_patch.py`, `tests/test_agent_apply_patch.py` | `[x] Completed` | `25 passed`（`test_agent_apply_patch.py`） |
| **Stage 1.3** | **文件版本冲突检测（保留 `expected_sha256` + 宿主 `observed_files` 生命周期）** | `local_tools.py`, `local_specs.py`, `state.py`, `runtime.py`, `tests/test_agent_apply_patch.py` | `[x] Completed` | `25 passed`（含跨重启与工作区切换测试） |
| **Stage 1.4** | **完善工具诊断与 `ryn_file_search` 过滤增强** | `store.py`, `tools.py`, `loop.py`, `runtime.py`, `local_patch.py`, `local_tools.py`, `local_specs.py` | `[x] Completed` | `25 passed`（`test_agent_apply_patch.py`） |
| **Stage 1.5** | **内置 `rg` 随包分发（`resolve_rg_executable()` + `pyproject.toml`）** | `local_processes.py`, `pyproject.toml`, `tests/test_agent_apply_patch.py` | `[x] Completed` | `25 passed`（`test_resolve_rg_executable_prefers_bundled_binary_and_falls_back_to_path`） |
| **Stage 2.1** | **补强 `CORE_TOOLS` 在常规窗口下的常驻配额（兼容 12 KB 测试窗口）** | `tool_menu.py`, `tests/test_agent_apply_patch.py`, `tests/test_agent_tool_menu.py` | `[x] Completed` | `test_agent_tool_menu.py` + `test_agent_apply_patch.py` 全部通过 |
| **Stage 2.2** | **提示词结构化与 Codex 线程复用验证** | `prompt.py`, `codex_threads.py`, `tests/test_agent_apply_patch.py` | `[x] Completed` | `test_codex_thread_reuses_session_across_tool_execution_permission_updates_tool_loading_and_followup` |
| **Stage 3.2** | **新增专项单元与集成测试汇总验证** | `tests/test_agent_*.py` | `[x] Completed` | **`200 passed, 2 skipped in 93.62s`**（10 个核心测试套件） |
| **Stage 3.3** | **真实自然语言任务验收（余额查询、磁盘排查、文件修改）** | [`live-results.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-results.md), [`live-stage33-file-modification.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage33-file-modification.json) | `[x] Completed` | 3 项无提示自然语言任务全部 `completed`（人工介入 `0` 次） |
| **Stage 4** | **（独立实验）Codex 原生工具协议可用性探测与对照实验** | [`live_stage4_protocol_experiment.py`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live_stage4_protocol_experiment.py), [`live-stage4-protocol-experiment.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage4-protocol-experiment.json), [`live-results.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-results.md) | `[x] Completed` | 对照实验完成，确认保留 `codex_tools.chat` + `codex_threads` 生产方案 |

---

## 四、 分步执行与验证日志（Execution Log）

### 2026-10-16 11:02 — 完成文档核实纠正（Doc Fix）
- **实际修改文件及目的**：
  - [`docs/acceptance/agent-upgrade-20261016/PLAN.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/PLAN.md)：按当前源码与仓内日志逐项纠正 12 项事实描述，并补充 `observed_files` 生命周期与实施边界。

### 2026-10-16 11:05 — 完成测试基线记录（Baseline）
- **测试命令**：
  `.\.venv\Scripts\python.exe -m pytest -q tests/test_agent_local_execution_reliability.py tests/test_agent_local_processes.py tests/test_agent_local_tools.py tests/test_agent_codex_loop.py tests/test_agent_runtime.py`
- **测试结果**：`122 passed, 1 failed, 2 skipped in 143.59s`（预存失败为桌面 UIA 测试 `test_windows_accessibility_write_and_readback`，已单独记录）。

### 2026-10-16 11:49 & 14:15 — 完成 Stage 0.1 初版与首批三项审查修复
- **核实后的真实函数名、错误码与行号**：
  1. [`rynmesh/agent_runtime/execution_policy.py:95-201`](file:///d:/code/rynmesh/rynmesh/agent_runtime/execution_policy.py#L95-L201)：新增宿主侧统一分类函数 `classify_operation(...)` 与校验函数 `verified_read_classification(...)`。
  2. [`rynmesh/agent_runtime/runtime.py:1265-1314`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1265-L1314)：`_read_may_continue_after_restart(row, flight, data)` 使用 `lifetime["charged_tool_calls"]` 与预算语义保持一致，兼容历史迁移任务 `lifetime["tool_calls"]` 为 `None` 的场景（测试：`test_legacy_migrated_task_read_shell_restart_recovery`）。
  3. [`rynmesh/agent_runtime/local_processes.py:168-218`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_processes.py#L168-L218) 与 [`rynmesh/agent_runtime/local_tools.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py)：`Processes.recover_operation` 捕获 `terminate(proc)` 异常并记录 `cleanup_error` 与 `still_running`；`LocalTools.execute` 仅在 `cleanup_confirmed=True` 且 `still_running=False` 时结算只读失败并抛出 `AgentError("agent_read_interrupted")`，否则抛出 `AgentError("agent_local_operation_unconfirmed")` 保留未知结果保护。
  4. [`rynmesh/agent_runtime/operation_ledger.py:69-90`](file:///d:/code/rynmesh/rynmesh/agent_runtime/operation_ledger.py#L69-L90)：在 `OperationLedger.update(operation_id, status, **values)` 中，当 `status == "failed"`、`row.get("no_mutation") is True`、`row.get("cleanup_confirmed") is True` 且通过 `verified_read_classification(...)` 校验时释放 `receipt_reserve` 预留。

### 2026-10-16 14:21 — 完成 Stage 0.2 实施与验证（模型来源变更 `agent_model_destination_changed` 显式重绑与恢复）
- **实际修改文件及目的**：
  1. [`rynmesh/agent_runtime/model_switch.py:36-105`](file:///d:/code/rynmesh/rynmesh/agent_runtime/model_switch.py#L36-L105)：新增 `_validate_transferable_scope(runtime, row, control_state=None)`，解决本地 `project` 作用域任务在 `transfer_resource_scope=True` 时被旧 `model_destination` 自锁的问题，同时保留 NAS `resource_grants` 必须重新授权的隔离不变量。
  2. [`rynmesh/agent_runtime/models.py`](file:///d:/code/rynmesh/rynmesh/agent_runtime/models.py) 与 [`tests/test_agent_projects.py`](file:///d:/code/rynmesh/tests/test_agent_projects.py)：新增 `test_project_destination_change_rebinds_same_or_new_model_and_resumes_explicitly`，6 个模型切换/项目作用域套件共 `80 passed`。

### 2026-10-16 15:30 — 完成 Stage 0.1 四项收尾修复与核实
- **收尾修复详情**：
  1. **修复真实崩溃窗口（`OperationLedger` 已 `confirmed` / `not_executed` / `intent`，但 `AgentLoop` 尚未保存工具结果时重启）**：
     - `LocalTools.recover_operation` 与 `AgentRuntime._recover_read_operation_details` / `_settle_interrupted_read` 校验账本 `binding` 与 `verified_read_classification`，直接复用已落盘的 `confirmed` 或 `not_executed` 账本回执，不重新执行命令（测试：`test_restart_reuses_confirmed_and_not_executed_ledger_receipts_without_rerun`）。
  2. **清理暂时失败后提供可执行的重新检查与恢复路径（防止永久保留 `in_flight`）**：
     - `recoverable_read_in_flight(row)` 对因清理暂时失败而保留 `in_flight` 的只读中断任务开放 `"resume"` 与 `"reconcile"`（测试：`test_temporary_cleanup_failure_allows_later_resume_or_reconcile_after_process_exits`）。
  3. **补充 Windows 平台绕过 `atexit` 的隔离宿主强制退出测试，并明确未测平台状态**：
     - 新增 `test_isolated_windows_host_forced_exit_bypasses_atexit_and_cleans_up_child`（调用 `os._exit(9)` 验证 JobObject 内核清理）。**POSIX / Linux / macOS 平台绕过 `atexit` 的强制宿主退出清理尚未在对应操作系统实测，明确标为【未验证】**。
  4. **修正验收名称与记录方式（区分自然语言自主查询、人工注入重启状态续跑与真实宿主退出）**：
     - 在 [`live-stage01-natural-read.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage01-natural-read.json) 中分别记录 `unhinted_natural_query` 与 `injected_restart_state_model_continuation`。

### 2026-10-16 16:35 — 完成 Stage 0.3（含分段计时去重修正）与 Stage 0.4（无副作用错误连续执行与退避）
- **Stage 0.3 计时去重修正与核实**：
  1. [`rynmesh/agent_runtime/loop.py:259-270, 568-593, 858-901`](file:///d:/code/rynmesh/rynmesh/agent_runtime/loop.py#L259-L270)：
     - `_model` 将 `preparation_seconds` 与 `preparation_persistence_seconds` 分离，并在流式回调中记录 `stream_persistence_seconds`；
     - `_tool` 与 `_tool_batch` 将 `prep_persistence_seconds` 与 `preparation_seconds` 严格分离；并行只读批处理仅在首项汇总共享阶段耗时，后续项标记 `batch_shared=True` 并单独记录 `call_execution_seconds`。
  2. [`docs/acceptance/agent-upgrade-20261016/live-stage03-disk-investigation.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage03-disk-investigation.json) 去重后精确五阶段耗时（`model="gpt-6.1-sol"`，`total_elapsed_ms=29,687 ms`，`first_useful_result_ms=21,500 ms`，`empty_poll_count_total=0`，`overlapping_scan_count=0`）：
     - `model_call_seconds`：**`28.407s`**（扣除流式写盘 `0.124s` 后的纯模型推理时间）；
     - `tool_execution_seconds`：**`0.719s`**；
     - `store_persistence_seconds`：**`0.313s`**；
     - `preparation_seconds`：**`0.079s`**（扣除 `preparation_persistence_seconds` 后的纯内存准备时间）；
     - `scheduler_wait_seconds`：**`0.015s`**。
- **Stage 0.4 实施与验证**：
  - [`rynmesh/agent_runtime/runtime.py:1685-1748`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L1685-L1748)：在 `_handle_operation_error` 中，当工具错误已确认为无副作用（`result.get("no_mutation") is True or record_status == "confirmed"`）、任务状态仍为 `queued`、未超出步数/时间/Token/调用预算、无挂起控制或审批、且**非同一工具同一错误码连续重复失败**（`consecutive_same_error == 0`）时，置 `self._step.clean = True`，使 `run_continuously()` 立即进入下一轮模型纠错而无需等待下一轮 worker 调度 tick；若同一 `(tool, error)` 连续重复出现，则自动附加退避纠错提示并置 `self._step.clean = False` 退避。
  - 新增单元测试：[`tests/test_agent_execution_efficiency.py`](file:///d:/code/rynmesh/tests/test_agent_execution_efficiency.py) `test_settled_no_side_effect_tool_error_continues_immediately_in_run_continuously_and_backs_off_on_repeat` 与 `test_batch_timing_and_error_timing_do_not_double_count`。
  - 测试结果：`.\.venv\Scripts\python.exe -m pytest tests/test_agent_execution_efficiency.py tests/test_agent_speedups.py tests/test_agent_local_processes.py -q` -> **`33 passed in 11.03s`**。

### 2026-10-16 17:12 — 完成 Stage 1.1–1.5 与 Stage 2.1–2.2 实施与专项测试
- **Stage 1.1（解耦 `max_output_chars` 与工具参数预算）**：
  - [`rynmesh/agent_runtime/output_policy.py:11-26`](file:///d:/code/rynmesh/rynmesh/agent_runtime/output_policy.py#L11-L26) 与 [`rynmesh/agent_runtime/loop.py:237, 592`](file:///d:/code/rynmesh/rynmesh/agent_runtime/loop.py#L237)：定义 `MAX_TOOL_CALL_ARGUMENT_BYTES = 96 * 1024`（`96 KB`）；`visible_message()` 仅校验可见回答文本（`content` 及未知字段）不超过 `max_output_chars`（默认 `20,000`），而单个 `tool_call` 参数允许最高 `96 KB`（整体响应仍受 `MAX_RAW_RESPONSE_BYTES = 256 KB` 约束），使最高 `90,000` 字符的 `ryn_apply_patch` 端到端顺畅执行（测试：`test_large_apply_patch_exceeds_max_output_chars_and_executes_end_to_end`）。
- **Stage 1.2（收紧 `ryn_apply_patch` 定位与唯一匹配）**：
  - [`rynmesh/agent_runtime/local_patch.py:182-380`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_patch.py#L182-L380)：所有 hunk 统一在原文件不可变快照上定位；要求完整上下文匹配唯一（`match_count == 1`，支持同名 `@@ anchor` 下凭唯一 hunk body 定位，拒绝 `match_count > 1` 歧义匹配）；检查修改区间无重叠（`_hunks_overlap`）；按 `(start_pos, end_pos, hunk_index)` 倒序应用以支持乱序 hunk；完整保留 `+` 新增行原始缩进，严禁自动改写缩进；任何 hunk 预检失败均保证零写入（测试：`test_duplicate_anchor_with_unique_context_succeeds_and_duplicate_full_context_is_rejected`、`test_out_of_order_hunks_succeed_and_overlapping_hunks_are_rejected_without_writes`、`test_patch_preserves_added_indentation_verbatim_and_reports_bounded_diagnostics`）。
- **Stage 1.3（文件版本冲突检测与 `observed_files` 生命周期）**：
  - [`rynmesh/agent_runtime/state.py:20, 74`](file:///d:/code/rynmesh/rynmesh/agent_runtime/state.py#L20)、[`rynmesh/agent_runtime/runtime.py:604, 660`](file:///d:/code/rynmesh/rynmesh/agent_runtime/runtime.py#L604)、[`rynmesh/agent_runtime/local_specs.py:25-31`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_specs.py#L25-L31) 与 [`rynmesh/agent_runtime/local_tools.py:193-244, 345-417, 505-595`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L193-L244)：在任务状态中持久化维护 `observed_files[normalized_path]`，跨 `ryn_file_read`、`ryn_file_write`、`ryn_file_edit`、`ryn_apply_patch`、`ryn_file_manage` 更新或清理；当文件已被任务读取观察后，`ryn_file_edit` 可省略显式 `expected_sha256` 并自动使用宿主记录的观察哈希校验；若文件在读取后被外部进程修改，则 `ryn_file_edit` 与 `ryn_apply_patch` 均拒绝写入并返回 `agent_local_file_changed_read_again`（测试：`test_observed_file_version_tracks_reads_detects_external_changes_and_survives_restart`）。
- **Stage 1.4（工具诊断增强与 `ryn_file_search` 扩展）**：
  - [`rynmesh/agent_runtime/store.py:67-76`](file:///d:/code/rynmesh/rynmesh/agent_runtime/store.py#L67-L76)、[`rynmesh/agent_runtime/tools.py:39-61`](file:///d:/code/rynmesh/rynmesh/agent_runtime/tools.py#L39-L61)、[`rynmesh/agent_runtime/local_tools.py:254-342`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_tools.py#L254-L342)：在不改变原错误码的前提下，通过 `AgentToolRejected(..., correction=..., details=...)` 返回 `path`、`hunk_index`、`match_count`、`field_path` 及脱敏受控片段（$\le 15$ 行、$\le 1600$ 字符）；
  - `ryn_file_search` 新增 `regex: bool = False`、`case_sensitive: bool = True`、`file_pattern: str | None = None`，无效正则抛出 `agent_invalid_search_regex` 并附带诊断说明（测试：`test_file_search_regex_case_sensitive_file_pattern_and_invalid_regex_diagnostics`）。
- **Stage 1.5（内置 `rg` 随包分发支持）**：
  - [`rynmesh/agent_runtime/local_processes.py:26-70, 357`](file:///d:/code/rynmesh/rynmesh/agent_runtime/local_processes.py#L26-L70) 与 [`pyproject.toml:119`](file:///d:/code/rynmesh/pyproject.toml#L119)：新增 `resolve_rg_executable()`，优先查找随包目录（`rynmesh/bin`、PyInstaller `_MEIPASS`、Tauri sidecar 同级 `bin`），回退系统 `PATH`，并在 `child_environment()` 与 `literal_read_command()` 中生效，同时保留对 `--pre` / `--hostname-bin` 的安全拦截（测试：`test_resolve_rg_executable_prefers_bundled_binary_and_falls_back_to_path`）。
- **Stage 2.1 & 2.2（`CORE_TOOLS` 常驻配额补强、提示词结构化与 Codex 线程复用验证）**：
  - [`rynmesh/agent_runtime/tool_menu.py:90-97`](file:///d:/code/rynmesh/rynmesh/agent_runtime/tool_menu.py#L90-L97)：在 `max_context_bytes >= ADDITIVE_CAPACITY`（`48 KB`）的常规窗口中将 `pin_limit` 提升为 `min(seed_limit, max(14000, max_context_bytes // 6))`，确保携带 `INTERNAL_SPECS` 时 `PLUGIN_DISCOVERY_TOOLS` 与全部 8 个 `CORE_TOOLS` 完整常驻，同时完全保留 `< 48 KB`（如 `12 KB` 压缩测试）窗口的无常驻替换行为；
  - [`rynmesh/agent_runtime/prompt.py:37-38`](file:///d:/code/rynmesh/rynmesh/agent_runtime/prompt.py#L37-L38)：保持 `Server control state:` 单行 JSON 格式与系统提示词结构稳定，补充搜索过滤、唯一补丁上下文及根据错误 `details`/`correction` 直接修正调用的简明指引；验证工具执行、权限回执更新、动态工具加载与同窗追问下的 `codex_threads` 复用（测试：`test_core_tools_remain_pinned_with_internal_specs_in_moderate_additive_windows`、`test_codex_thread_reuses_session_across_tool_execution_permission_updates_tool_loading_and_followup`）。

### 2026-10-16 17:16 — 完成 Stage 3.2 汇总测试、Stage 3.3 自然语言验收与 Stage 4 独立对照实验
- **Stage 3.2 专项单元与集成测试汇总**：
  - 执行命令：
    ```powershell
    .\.venv\Scripts\python.exe -m pytest tests/test_agent_apply_patch.py tests/test_agent_tool_menu.py tests/test_agent_codex_loop.py tests/test_agent_output_policy.py tests/test_agent_execution_efficiency.py tests/test_agent_speedups.py tests/test_agent_local_processes.py tests/test_agent_local_execution_reliability.py tests/test_agent_projects.py tests/test_agent_runtime.py -q
    ```
  - 结果：**`200 passed, 2 skipped in 93.62s`**。
- **Stage 3.3 自然语言任务实测验收**（详见 [`live-results.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-results.md) 与 [`live-stage33-file-modification.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage33-file-modification.json)）：
  - 在不提示任何工具名或恢复路线的前提下，使用 `gpt-6.1-sol` 完成自然语言多类同名方法搜索与补丁修改任务：5 步完成（总耗时 `30,000 ms`，Step 1 首个有效搜索结果 `11,156 ms`，人工介入 `0` 次，工具错误 `0` 次），自主执行 `["ryn_file_search", "ryn_file_read", "ryn_file_list", "ryn_apply_patch", "ryn_file_read"]`，精准将 `EnterpriseInvoice` 税率从 `0.05` 改为 `0.08` 并保留 `PersonalInvoice` 为 `0.05`。
- **Stage 4 独立对照实验**（详见 [`live-results.md`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-results.md) 与 [`live-stage4-protocol-experiment.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage4-protocol-experiment.json)）：
  - 在相同模型（`gpt-6.1-sol`）、相同推理强度（`low`）与相同 P2P 提供端只读沙箱约束下对比：
    - `json_proposal_wrapper`（现行生产方案）：**5 步完成（`21,797 ms`）**，`file_updated_correctly: true`，线程复用 `started: 1, resumed: 4`，复用了热线程将 Step 2–5 单步耗时降至 `2.2s–3.7s`；
    - `native_dynamic_tools`（原生 `dynamicTools`）：因提供端 `sandbox="read-only"` 与 `approvalPolicy="never"` 触发模型内置只读环境拒答，Step 1 未调用任何工具即终止（`file_updated_correctly: false`），且无法跨步复用线程。
  - **实验结论**：保留现行 `codex_tools.chat`（JSON 提案包装 + `codex_threads` 会话复用）作为生产方案，不切换为原生 `dynamicTools`。
