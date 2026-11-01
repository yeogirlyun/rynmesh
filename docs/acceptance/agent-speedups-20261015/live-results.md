# Agent 提速主机验收结果 — 2026-10-15

最新状态：第 7 节的 **P2 复测已通过**，同窗追问及 shell/写入后的线程续用已确认。第 1–6 节保留首次验收的历史结果与缺陷定位；其他广泛测试失败仍按原记录保留。交付摘要见 [P2 交付说明](../../changes/2026-10-15-agent-speedups-p2.md)。

**首次验收结论（P2 修复前）：部分通过，尚不能整批验收通过。** 独立 Codex 适配器实测的后续步骤中位数降低约 80%，补丁、多读并行、长输出首尾、存储迁移及增量轮询均得到验证；真实 Agent 的系统提示会随权限回执变化，导致同一工具循环和同窗追问重新建线程，F 的提速目标未通过。该轮仅测试和报告，未修改业务代码或测试代码，未安装、commit 或 push。

## 运行环境和证据边界

| 项目 | 实际环境 |
| --- | --- |
| 日期、时区 | 2026-10-15，Asia/Hong_Kong（UTC+08:00） |
| 仓库 | `D:\code\rynmesh`，当前工作区；没有用 Git 还原不存在的 v4 前态 |
| Python | 仓库 `.venv\Scripts\python.exe`，3.12.10 |
| CPU | Intel Core i7-9700K，3.60 GHz，8 核 / 8 线程 |
| 内存 | 约 32 GiB |
| Codex CLI | `0.162.0-alpha.17.2` |
| 实际模型 | 独立隔离 app-server 的 `thread/start` 返回 `gpt-6.1-sol`，provider 为 `openai`；Agent 选择本机 Codex CLI 默认模型 |
| 推理 effort | 使用隔离 CLI 默认值，返回 `null`；适配器不加载用户 config，因此不能把用户 config 中的 effort 当作本次实际配置 |
| 隔离后端 | 全新 home、network、workspace，只监听 `127.0.0.1:18973`；Agent enabled，permission_mode=`workspace`，不连接注册表/局域网发现 |
| 安装版保护 | 没有覆盖或写入 `%LOCALAPPDATA%\Ryn`；现有 Agent 数据及解密密钥只复制后使用，原文件哈希前后相同 |

临时证据根目录为 `C:\Users\子陵\AppData\Local\Temp\ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c`，下文简称 **TEMP**。原始用户任务内容、标题、账户资料和金额均未写入本报告；存储副本保持加密，数据一致性只记录数量和摘要比较结果。

为并行完成验收，`pytest -n auto`、前端测试、参考运行时及真实推理曾重叠运行；广泛 Python 组为 22:22:32–22:32:15，8 个 worker，前端初跑另有 7 个 worker。绝对延迟和固定时限测试受负载影响，失败已定向复跑。不能把本次结果当作独占主机的性能上限。冷启动和追问每模式只有 3 个样本，p90 沿用原脚本算法，n=3 时等于最大值。

## 1. 自动化测试

全部使用仓库 `.venv\Scripts\python.exe`。未修改业务代码或测试源码，未安装依赖、未 commit/push。前端使用已存在依赖，`npx --no-install vitest` 与请求的 vitest 命令相同但禁止自动下载。reference 的 pytest 临时插件只保留错误类型，删除异常原文。

| 原始测试组 | 通过 | 失败 | 跳过 | 耗时 | 时间（+08:00） |
| --- | ---: | ---: | ---: | --- | --- |
| 用户指定 11 文件 | 221 | 2 | 0 | 181.87 s | 22:21:46–22:24:51 |
| `-n auto tests -k "agent or codex or cli_ or inference"` | 1686 | 27 | 4 | 580.23 s | 22:22:32–22:32:15 |
| `npm run lint` | — | 0 | — | 约 249 s，exit 0 | 22:22:49–22:26:58 |
| `vitest run src/screens src/domain` | 607 | 22 | 0 | 268.79 s；75 文件中 71 通过、4 失败 | 22:27:14–22:31:43 |
| `tests -k test_agent_reference` | 62 | 1 | 2 | 406.18 s；4090 deselected | 22:23:38–22:30:29 |

原始结果保留，不能把补跑结果写成整个原组全绿。广泛组使用 8 个 pytest workers，前端原组使用 7 个 fork workers，并与其他验收脚本有重叠；曾观测到物理内存仅余约 4.2 GiB/31.9 GiB，因此固定 50 ms、2–3 s、5 s 等时限的结果受机器负载影响。

| 定向补跑 | 通过 | 失败 | 跳过 | 耗时 | 说明 |
| --- | ---: | ---: | ---: | --- | --- |
| 前端原失败 4 文件，`--maxWorkers=1` | 97 | 0 | 0 | 110.28 s | 22 项原失败全部包含其中；没有重跑完整 629 项 |
| privacy 进程超时/输出上限 + Windows 桌面原失败 | 2 | 1 | 0 | 37.55 s | 进程 2 项通过；桌面不能获得焦点 |
| reference late_receipt 唯一原失败 | 1 | 0 | 0 | 16.54 s | 原 AssertionError 未复现，未见 BrokenPipe |
| 其他 broad 相关文件/用例 | 34 | 10 | 0 | 53.57 s | 持续失败恰为 catalog fixture 5 项和 timing 已知 5 项 |
| source / sharing 3 文件原样单进程 | 24 | 4 | 0 | 20.18 s | 验证 opt-in 的测试前置遗漏；显式 opt-in 诊断对照 4 项通过 |
| release_resume reference 两参数 | 2 | 0 | 0 | 70.68 s | crash-ready 约 17.7 s 生成，没有 worker 退出证据 |

广泛组 4 个 skip：`test_drop_p2p`、`test_native_transport` 在收集阶段跳过，以及 `test_codex_isolation_live` 的 False/True 两参数需要显式启用本地 Codex 二进制测试。reference 组的 2 个 skip 也都是前两项收集跳过，不是参考 worker 用例被跳过。已知的 12 个首消息 BrokenPipe 在本机均未重现；完整参考组的唯一失败是 AssertionError，单跑已通过。两套 Node（22.22.1 与 24.19.0）均能成功载入固定 reference 的 worker、core、contracts、storage；没有 worker 启动导入错误。

### 每个后端失败的定位与归因

下表包含广泛组的全部 27 个失败；参数用例逐行列出。source/reference 的定向复核已合并在下表。没有提交前态，表中的“非这批改动证据”仅表示直接错误发生在相关测试替身或独立代码路径，不声称已从历史版本证明其既有。

| 首次失败用例 | 定位、复跑和判断 |
| --- | --- |
| `test_agent_consumer_process_proof.py::test_unrelated_and_self_reported_identity_is_harmless` | `tests/test_agent_consumer_process_proof.py:25,29,35` 子进程 write_text 非原子；父进程仅等 exists 就读 JSON，首次 JSONDecodeError；单跑通过。测试文件写入/读取竞态，非业务代码回归证据。 |
| `test_agent_local_tools.py::test_background_process_stop_and_task_isolation` | `tests/test_agent_local_tools.py:418` 3 s 内 Powershell 未输出 started；独立重跑通过，受启动负载影响。 |
| `test_agent_document_cases.py::test_each_document_case_mounts_only_frozen_files_and_keeps_exact_scope[F07]` | 首次 F07 根列表在 `tests/test_agent_document_cases.py:46` / `resources.py:368` 报权限变化，读超时审计在 `test_agent_document_cases.py:103` 断言失败；fixture `document_cases.py:146` 将 HTTP timeout 设为 0.05 s，重负载时连元数据/GET 派发都可能未完成。整个文件单跑 22 项通过，未确认产品回归。 |
| `test_agent_document_cases.py::test_f07_exact_file_grant_produces_actual_timeout_and_collects_failure` | 首次 F07 根列表在 `tests/test_agent_document_cases.py:46` / `resources.py:368` 报权限变化，读超时审计在 `test_agent_document_cases.py:103` 断言失败；fixture `document_cases.py:146` 将 HTTP timeout 设为 0.05 s，重负载时连元数据/GET 派发都可能未完成。整个文件单跑 22 项通过，未确认产品回归。 |
| `test_agent_reference_late_receipt.py::test_native_commit_cancel_original_late_receipt_and_same_session_readback` | `tests/test_agent_reference_late_receipt.py:18` 用例中 AssertionError；:87、:96 有固定 15 s 等待。首次脱敏未保留断言帧，无法确定触发哪项断言；单跑 1 passed / 16.54 s。可能受并发时序影响，未确认持续回归或 worker 退出。 |
| `test_agent_peer_scope.py::test_unverified_or_other_recipient_catalog_cannot_authorize_scope_or_switch[flag]` | `tests/test_agent_peer_scope.py:111` 将 list_models 替换为无参数 lambda；`models.py:214` 传入 selected_model_id，先抛 TypeError，未到预期权限拒绝。单跑五项均复现，属测试替身接口不兼容。 |
| `test_agent_peer_scope.py::test_unverified_or_other_recipient_catalog_cannot_authorize_scope_or_switch[recipient]` | `tests/test_agent_peer_scope.py:111` 将 list_models 替换为无参数 lambda；`models.py:214` 传入 selected_model_id，先抛 TypeError，未到预期权限拒绝。单跑五项均复现，属测试替身接口不兼容。 |
| `test_agent_peer_scope.py::test_unverified_or_other_recipient_catalog_cannot_authorize_scope_or_switch[digest]` | `tests/test_agent_peer_scope.py:111` 将 list_models 替换为无参数 lambda；`models.py:214` 传入 selected_model_id，先抛 TypeError，未到预期权限拒绝。单跑五项均复现，属测试替身接口不兼容。 |
| `test_agent_peer_scope.py::test_unverified_or_other_recipient_catalog_cannot_authorize_scope_or_switch[permission]` | `tests/test_agent_peer_scope.py:111` 将 list_models 替换为无参数 lambda；`models.py:214` 传入 selected_model_id，先抛 TypeError，未到预期权限拒绝。单跑五项均复现，属测试替身接口不兼容。 |
| `test_agent_peer_scope.py::test_unverified_or_other_recipient_catalog_cannot_authorize_scope_or_switch[native]` | `tests/test_agent_peer_scope.py:111` 将 list_models 替换为无参数 lambda；`models.py:214` 传入 selected_model_id，先抛 TypeError，未到预期权限拒绝。单跑五项均复现，属测试替身接口不兼容。 |
| `test_agent_query_cases.py::test_real_encrypted_world_and_actual_presence_classifier[Q02]` | `tests/test_agent_query_cases.py:30`。本地 API catalog 在 `rynmesh/agent_runtime/models.py:143` 按适配器契约设置 `agent_supported=True`，而旧 fixture 预期工具探针不支持时为 False；单进程稳定复现。与 Codex 复用、连续调度、状态布局无直接因果证据。 |
| `test_agent_query_cases.py::test_real_encrypted_world_and_actual_presence_classifier[Q03]` | `tests/test_agent_query_cases.py:30`。本地 API catalog 在 `rynmesh/agent_runtime/models.py:143` 按适配器契约设置 `agent_supported=True`，而旧 fixture 预期工具探针不支持时为 False；单进程稳定复现。与 Codex 复用、连续调度、状态布局无直接因果证据。 |
| `test_agent_planner_policy.py::test_mapping_precedes_snapshot_and_real_outer_gate_enforces_it[ryn]` | `tests/test_agent_planner_policy.py:63`。本地 API catalog 在 `rynmesh/agent_runtime/models.py:143` 按适配器契约设置 `agent_supported=True`，而旧 fixture 预期工具探针不支持时为 False；单进程稳定复现。与 Codex 复用、连续调度、状态布局无直接因果证据。 |
| `test_agent_planner_policy.py::test_mapping_precedes_snapshot_and_real_outer_gate_enforces_it[reference]` | `tests/test_agent_planner_policy.py:63`。本地 API catalog 在 `rynmesh/agent_runtime/models.py:143` 按适配器契约设置 `agent_supported=True`，而旧 fixture 预期工具探针不支持时为 False；单进程稳定复现。与 Codex 复用、连续调度、状态布局无直接因果证据。 |
| `test_agent_render_inspect.py::test_real_render_reports_a_loop_that_jumps_back` | 首次 `rynmesh/agent_runtime/local_render.py:476` 等待 Chromium Page.navigate 超时；同一真实渲染用例单跑通过。环境/负载依赖，未确认这批回归。 |
| `test_agent_local_tools.py::test_windows_accessibility_write_and_readback` | `tests/test_agent_local_tools.py:681` 首次缺 elements；独立重跑可完成 UIA set_value/readback，随后 `local_desktop.py:212` 明确 agent_computer_focus_blocked（测试行 685），被 `local_tools.py:621` 包装为操作结果不确定。需可以获前台焦点的桌面，属于环境限制。 |
| `test_agent_query_cases.py::test_real_encrypted_world_and_actual_presence_classifier[Q01]` | `tests/test_agent_query_cases.py:30`。本地 API catalog 在 `rynmesh/agent_runtime/models.py:143` 按适配器契约设置 `agent_supported=True`，而旧 fixture 预期工具探针不支持时为 False；单进程稳定复现。与 Codex 复用、连续调度、状态布局无直接因果证据。 |
| `test_agent_run_timing.py::test_reconciliation_uses_original_tick_and_clears_live_marker[False]` | `tests/test_agent_runtime.py:80（由 run_timing reconciliation 用例调用）`。已知时钟替换问题：fixture 第 22 行仅替换 runtime.time，runtime.py:1428 产生假 started，loop.py:230/459 用真实 monotonic 计算时间。广泛与独立补跑恰为原已知五项，无第六项。 |
| `test_agent_run_timing.py::test_reconciliation_uses_original_tick_and_clears_live_marker[True]` | `tests/test_agent_runtime.py:80（由 run_timing reconciliation 用例调用）`。已知时钟替换问题：fixture 第 22 行仅替换 runtime.time，runtime.py:1428 产生假 started，loop.py:230/459 用真实 monotonic 计算时间。广泛与独立补跑恰为原已知五项，无第六项。 |
| `test_agent_run_timing.py::test_owner_api_exposes_live_snapshot_but_rejects_injected_timing` | `tests/test_agent_run_timing.py:212`。已知时钟替换问题：fixture 第 22 行仅替换 runtime.time，runtime.py:1428 产生假 started，loop.py:230/459 用真实 monotonic 计算时间。广泛与独立补跑恰为原已知五项，无第六项。 |
| `test_agent_sharing_recovery_case.py::test_r02_actual_restart_keeps_approval_and_performs_one_atomic_share` | `tests/test_agent_sharing_recovery_case.py:96` checker_facts 为空；默认 store.py:35 verify_text_responses=False，loop.py:273/278 不运行 checker。fixture 未显式 opt-in；原样稳定复现，仅在临时包装器显式 opt-in 后通过。未见本批提速因果证据。 |
| `test_agent_source_drafts.py::test_real_runtime_source_workflow_approvals_and_registered_verifiers[128-4-True-False]` | `tests/test_agent_source_drafts.py:317` 探针计数分别实际 2/6、预期 4/8；默认 verify_text_responses=False，未请求 source_drafts.py:489 的保存后验证。fixture 设置 tests:299 遗漏 opt-in；显式 opt-in 诊断对照通过。未见本批提速因果证据。 |
| `test_agent_source_drafts.py::test_real_runtime_source_workflow_approvals_and_registered_verifiers[128-8-True-True]` | `tests/test_agent_source_drafts.py:317` 探针计数分别实际 2/6、预期 4/8；默认 verify_text_responses=False，未请求 source_drafts.py:489 的保存后验证。fixture 设置 tests:299 遗漏 opt-in；显式 opt-in 诊断对照通过。未见本批提速因果证据。 |
| `test_agent_source_fixture.py::test_secure_owner_form_actual_http_probes_and_saved_private_models` | `tests/test_agent_source_fixture.py:120` completed 但 outcome unverified；tests:91 未启用 verify_text_responses。loop.py:273/278 按默认正常结束；显式 opt-in 对照通过。未见本批提速因果证据。 |
| `test_agent_release_resume.py::test_r03_real_worker_restore_keeps_original_operation_and_one_put[reference]` | `tests/test_agent_release_resume.py:178` 固定 45 s 内 crash-ready.json 未生成，原 crash.log 为空；reference 两参数串行复跑 2 passed，R03 约 17.7 s 生成 crash-ready。固定时限受资源影响，没有 worker 实际退出证据；未确认持续回归。 |
| `test_agent_run_timing.py::test_approval_pause_restart_and_new_execution_preserve_clock_semantics` | `tests/test_agent_run_timing.py:66`。已知时钟替换问题：fixture 第 22 行仅替换 runtime.time，runtime.py:1428 产生假 started，loop.py:230/459 用真实 monotonic 计算时间。广泛与独立补跑恰为原已知五项，无第六项。 |
| `test_agent_run_timing.py::test_input_answer_changes_budget_round_but_not_run_limit_clock` | `tests/test_agent_run_timing.py:98`。已知时钟替换问题：fixture 第 22 行仅替换 runtime.time，runtime.py:1428 产生假 started，loop.py:230/459 用真实 monotonic 计算时间。广泛与独立补跑恰为原已知五项，无第六项。 |

指定 11 文件组另一个失败（广泛组未失败）：`test_inference_privacy.py::test_process_timeout_and_output_limits_kill_worker` 的 9 MiB stdout / output limit 参数，`tests/test_inference_privacy.py:304` 预期输出限额，首次在 2 s 截止前未读完，`process_guard.py:100` 先返回超时。资源释放后两个参数均通过；此调用直接测试 process_guard，不走 Codex 会话复用，无需 RYN_CODEX_SESSION_REUSE=0 对照。所有 `test_codex_thread_reuse`、`test_codex_tool_proposals` 与指定 Agent Codex loop 测试在原组中通过。

### source / sharing 和参考 worker 的补充定位

四个 source/sharing 失败的链路为 `store.py:35` 默认 `verify_text_responses=False` → `runtime.py:901` 快照 → `loop.py:273` 仅显式开启时运行独立验证。历史 fixture 的设置调用遗漏 opt-in，v4 验收说明已声明该要求。三个文件原样单进程运行为 24 passed / 4 failed；只在临时 `sitecustomize.py` 包装设置调用加入 `verify_text_responses=True` 后，这四项为 4 passed / 15.68 s。该诊断没有改仓库源码或测试，不替代原始失败计数。详细命令与每项定位见[诊断证据](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/tests/source-failures.md)。

本机没有发生参考 worker 首消息 BrokenPipe，因而没有一个复现出的异常退出可供解释。两套 Node 的导入探针均退出 0、未发现启动错误类型；没有记录上游错误原文。late-receipt 首跑是测试 AssertionError，R03 是固定就绪时限断言，二者独立重跑通过。原 R02 参考用例在重负载下 122.916 s、串行 37.882 s，亦支持时段负载影响。详细证据见[参考复核](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/store/reference-failure-followup.md)。

### 前端原始 22 个失败（4 文件）

以下每项均在失败文件单 worker 的 97 passed 补跑中覆盖。原跑表现包含 5 s 超时、异步渲染等待与交互断言失败；单 worker 无修改重跑全部通过，未确认 since_revision 前端逻辑回归。保留原始 22 failed，不能将结果改写为整个 629 项单跑通过。

| 文件/定位 | 原始失败用例 |
| --- | --- |
| `src/screens/AISources.test.tsx:69:27` | keeps a failed test editable and never marks it successful |
| `src/screens/RynAgent.test.tsx:78:1` | keeps multiple project conversations grouped and restores a separate new draft for each project |
| `src/screens/RynAgentReasoning.test.tsx:78:1` | shows the supported model's actual low default and submits it when creating a task |
| `src/screens/RynAgentSending.test.tsx:94:42` | shows a pending create immediately and preserves the next draft after acknowledgement |
| `src/screens/RynAgentSending.test.tsx:105:39` | shows a pending followup immediately and preserves the next draft after acknowledgement |
| `src/screens/RynAgentSending.test.tsx:127:3` | removes a rejected 'followup' bubble for HTTP 400 and restores only an untouched draft |
| `src/screens/RynAgentSending.test.tsx:152（测试定义）` | retains a 'create' bubble and recovery control after an uncertain 'network' response |
| `src/screens/RynAgentSending.test.tsx:152（测试定义）` | retains a 'create' bubble and recovery control after an uncertain '500' response |
| `src/screens/RynAgentSending.test.tsx:152:3` | retains a 'followup' bubble and recovery control after an uncertain '500' response |
| `src/screens/RynAgentSending.test.tsx:164:39` | retains a 'followup' bubble and recovery control after an uncertain 'network' response |
| `src/screens/RynAgentSending.test.tsx:190:39` | releases an uncertain 'create' after definitive HTTP 400 recovery rejection without losing a newer draft |
| `src/screens/RynAgentSending.test.tsx:175（测试定义）` | releases an uncertain 'create' after definitive HTTP 409 recovery rejection without losing a newer draft |
| `src/screens/RynAgentSending.test.tsx:175:3` | releases an uncertain 'followup' after definitive HTTP 400 recovery rejection without losing a newer draft |
| `src/screens/RynAgentSending.test.tsx:190:39` | releases an uncertain 'followup' after definitive HTTP 409 recovery rejection without losing a newer draft |
| `src/screens/RynAgentSending.test.tsx:213:39` | returns a legacy follow-up to an editable draft after 'network' without leaving an unrecoverable pending message |
| `src/screens/RynAgentSending.test.tsx:200:3` | returns a legacy follow-up to an editable draft after '500' without leaving an unrecoverable pending message |
| `src/screens/RynAgentSending.test.tsx:245（测试定义）` | preserves a deliberately retyped identical next draft after create retry succeeds |
| `src/screens/RynAgentSending.test.tsx:245（测试定义）` | preserves a deliberately retyped identical next draft after followup retry succeeds |
| `src/screens/RynAgentSending.test.tsx:245:73` | preserves a deliberately retyped identical next draft after create discovery succeeds |
| `src/screens/RynAgentSending.test.tsx:280:39` | keeps a pending create scoped to its conversation while another task is open |
| `src/screens/RynAgentSending.test.tsx:271:42` | keeps a pending followup scoped to its conversation while another task is open |
| `src/screens/RynAgentSending.test.tsx:310:39` | deduplicates a follow-up accepted by a refreshed snapshot before POST resolves without mistaking an earlier identical message for its receipt |

### 复现与证据

Python 各组按用户给定文件清单及选择表达式执行。复现单个失败可从仓库根运行 `.venv\Scripts\python.exe -m pytest -q -rfE <上表完整 tests/用例路径>`；广泛组另外加 `-n auto`。原始日志：`focused.log`、`broad.log`/`broad.xml`、`frontend-lint.log`、`frontend-vitest.log`、`reference.log`/`reference.xml`。补跑证据：`frontend-retest.log`、`focused-retest.log`、`reference-retest.log/xml`、`broad-retest.log/xml`。均位于 [TEMP/tests](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/tests/automated-results.md) 所在目录。原始 11 文件列表为 `test_agent_apply_patch.py test_agent_speedups.py test_agent_store_layout.py test_agent_tool_menu.py test_agent_codex_loop.py test_agent_local_tools.py test_codex_thread_reuse.py test_codex_tool_proposals.py test_inference_privacy.py test_cli_agent.py test_agent_peer_cli.py`（都在 tests/）。

前端补跑命令：`cd webapp; npx --no-install vitest run src/screens/AISources.test.tsx src/screens/RynAgent.test.tsx src/screens/RynAgentReasoning.test.tsx src/screens/RynAgentSending.test.tsx --maxWorkers=1`。

catalog 稳定失败可用 `.venv\Scripts\python.exe -m pytest -q -rfE tests/test_agent_query_cases.py tests/test_agent_planner_policy.py` 复现。timing 可用 `.venv\Scripts\python.exe -m pytest -q -rfE tests/test_agent_run_timing.py` 复现同样 5 项。桌面可用 `.venv\Scripts\python.exe -m pytest -q -rfE tests/test_agent_local_tools.py::test_windows_accessibility_write_and_readback`，当前宿主会拒绝前台焦点。

本轮测试在仓库 real-model/ 下生成的 7 个未跟踪 fixture 目录已逐个核对绝对路径并移至 `generated-evidence/`，移动清单为 TEMP/tests 下的 `generated-evidence/moved-manifest.json`，保留证据且不留额外仓库产物。初始 3 个用户修改文件未动。


## 2. Codex 线程复用实测

执行用户指定脚本，只用 `--out` 将证据写到 TEMP：

```powershell
.venv\Scripts\python.exe docs/acceptance/codex-session-reuse-20261015/measure_steps.py --live --rounds 3 --stream --out <TEMP>\codex\results
```

三个交替轮次顺序为 `0→1、1→0、0→1`，每模式 24 步，共 **48/48 步成功**。每轮首次问答有 7 步，最终回答后同窗追问 1 步。

| 模式 | 分组 | n | 中位数 s | p90 s | 最大 s |
| --- | --- | ---: | ---: | ---: | ---: |
| reuse=0 | 首步 | 3 | 16.766 | 16.858 | 16.858 |
| reuse=1 | 首步 | 3 | 12.781 | 17.843 | 17.843 |
| reuse=0 | 后续步骤，含追问 | 21 | 13.343 | 18.921 | 21.578 |
| reuse=1 | 后续步骤，含追问 | 21 | 2.686 | 3.858 | 4.296 |
| reuse=0 | 首轮后续，排除追问 | 18 | 13.640 | 21.171 | 21.578 |
| reuse=1 | 首轮后续，排除追问 | 18 | 2.671 | 3.858 | 3.860 |
| reuse=0 | 同窗追问首步 | 3 | 13.171 | 17.421 | 17.421 |
| reuse=1 | 同窗追问首步 | 3 | 3.093 | 4.296 | 4.296 |
| reuse=0 | 全部步骤 | 24 | 13.640 | 18.921 | 21.578 |
| reuse=1 | 全部步骤 | 24 | 2.781 | 12.671 | 17.843 |

后续步骤中位数降低 **79.87%**，同窗追问首步降低 **76.52%**。首步最大值没有改善。复用模式累计 `started=3 / resumed=21 / fallback=0 / rebuilt=0`，三个脚本窗口各只启动一个进程和线程，包括最终回答后的追问；禁用模式没有使用线程池。

首个流式文字时间（只统计 `content` 非空的响应）：

| 模式 | 分组 | 文字样本 n | 中位数 s | p90 s | 最大 s |
| --- | --- | ---: | ---: | ---: | ---: |
| reuse=0 | 首步 | 3 | 16.078 | 16.203 | 16.203 |
| reuse=1 | 首步 | 3 | 12.156 | 17.078 | 17.078 |
| reuse=0 | 后续，含追问 | 6 | 13.164 | 18.000 | 18.000 |
| reuse=1 | 后续，含追问 | 6 | 2.420 | 3.811 | 3.811 |
| reuse=0 | 首轮最终回答 | 3 | 14.703 | 18.000 | 18.000 |
| reuse=1 | 首轮最终回答 | 3 | 2.280 | 2.655 | 2.655 |
| reuse=0 | 同窗追问 | 3 | 11.625 | 16.516 | 16.516 |
| reuse=1 | 同窗追问 | 3 | 2.561 | 3.811 | 3.811 |

纯工具响应没有首字样本。保活 RSS 共 23 个有效样本、1 个缺失：最小 122.59 MiB，中位 133.37 MiB，最大 150.05 MiB；缺失是脚本 `tasklist` 采样返回空值，不是推理失败。4 个线程按最大单进程 RSS 粗估约 600 MiB，但本脚本没有同时保留 4 个线程的实测。

证据：[原始 48 步 JSON](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/codex/results/steps-20261015T142954Z.json)、[分组统计](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/codex/summary.json)。脚本使用固定系统提示，因此本项通过不代表真实 Agent 的动态提示也能续用，见第 5 项。

## 3. 隔离后端端到端 Agent

通过真实回环 HTTP API 配置 CLI 服务、启用 Agent、选择本机 Codex CLI 模型并设置新工作区。A–G 依次执行，F 使用 C 的原 task；G 为新 task，另加 `F_after_G` 检查原窗口。每轮完成后 GET 导出完整任务 JSON，并实际运行 `step_gaps.py`。F 的完整导出包含 C，故另生成只含该轮事件的 `F-round.json` 统计；原始完整导出也保留。

总耗时从 POST 前至终态 GET；首字从 POST 前至第一个真实 `text_delta`，可能是进度文字，不一定是最终答案的首字。每 0.2 秒轮询带来约一个间隔的终态观测误差。模型段是 `model_started → assistant`；工具后间隙是 `tool_result → model_started`。

| 场景 | 总耗时 s | 首字 s | 模型步数 | 模型段中位 s | 工具后间隙中位 s | discover 次数 | 压缩次数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A 问候 | 17.172 | 16.735 | 1 | 16.372 | — | 0 | 0 |
| B 只读 OS | 56.641 | 20.246 | 2 | 23.014 | 1.131 | 0 | 0 |
| C 多文件创建及改名 | 73.250 | 17.465 | 5 | 9.912 | 0.484 | 0 | 0 |
| D 多读和列目录 | 38.297 | 18.653 | 2 | 16.814 | 1.209 | 0 | 0 |
| E 大量失败输出 | 44.234 | 16.903 | 2 | 18.077 | 0.264 | 0 | 0 |
| F 同窗追加单元测试 | 55.719 | 20.252 | 3 | 15.497 | 0.618 | 0 | 0 |
| G 新窗问候 | 14.969 | 14.367 | 1 | 14.166 | — | 0 | 0 |
| F_after_G 原窗读历史 | 19.312 | 19.111 | 1 | 18.544 | — | 0 | 0 |

各场景均到达 completed，审批请求均为 0。9 个工具后间隙总体中位数 **0.616 s**，范围 **0.264–1.209 s**。

| 判据 | 结果和证据 |
| --- | --- |
| A / B | 问候和只读 OS 检查成功；B 使用本地 shell，无审批，无 discover |
| C | `ryn_apply_patch` 两次：一次同时增加两个文件，一次改函数名并同步调用方；读回和 AST 对照正确；没有额外工具发现步骤 |
| D | 同一模型响应中的两个 `ryn_file_read` 的 `tool_started` 均为 `14:27:58.328611Z`；源码使用线程池执行该连续读取批次。目录列表用 shell，在 0.123 s 后独立开始，不能声称三个操作全部并行 |
| E | 合成失败脚本实际输出 125,097 字符，模型可见 output 为 6,078 字符，含首部和尾部标记；末尾错误类型为 `ValueError`，模型无需额外分页即可识别；退出码 1 |
| F 文件结果 | 使用 patch 添加单元测试，实际运行 1 项测试并通过 |
| F 线程提速 | **失败**：追问首步又 started，适配器调用耗时 19.446 s，比 C 首步 17.852 s 更慢；首次写入后的第二步再次 started，13.137 s；第三步才 resumed，5.733 s |
| G 窗口隔离 | G 的 window 不同，公开历史只有自己的两条消息；G 完成后 F 原 PID 仍在保活池。原窗再次提问能正确读出此前测试文件信息，未观察到串窗；但该轮又因同一提示变化重新建线程，不能宣称原线程已续用 |
| 增量轮询 | 同 revision 的 GET 返回 `unchanged=true`，仅 `id/revision/allowed_actions/budget/run_state/run_timing` 等实时字段，无 messages/events；紧凑 JSON 序列化约 2.9 KB，完整 G 导出约 8.6 KB（两者缩进口径不同，未据此计算压缩比例） |

后端及其 app-server 子进程树已在验收后停止，确认 `127.0.0.1:18973` 端口关闭。证据：[逐场景聚合](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/e2e/summary.json)、[step_gaps 实际输出](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/e2e/step-gaps.txt)、[只含检查布尔值的结果](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/e2e/verification-summary.json)。同目录保留临时 harness 和各次 GET 导出。

## 4. 真实数据副本迁移与存储性能

安装版实际 home 的 Agent 数据位于 `C:\Users\子陵\.rynmesh\agent`，不是安装程序目录。复制 agent 目录及解密所需密钥后，只在 TEMP 的副本打开新版 `AgentStore`。

| 检查 | 结果 |
| --- | --- |
| 迁移前尺寸 | agent 7 文件，共 1,804,846 bytes；`sessions.json` 1,737,970 bytes；旧状态解密后 1,303,404 bytes |
| 任务一致性 | 15/15 可读；任务数量、标题摘要和完整任务载荷均一致；不记录标题或内容 |
| 只读 | 根仍为 `ryn.agent.v1`，不触发迁移 |
| 首次写入 | 空事务 217.7 ms，根变为 `ryn.agent.v2`，`tasks/` 下 15 个加密任务文件 |
| 重新打开 | 15 个任务全部可读且一致 |
| 单事件 | 25.8 ms，仅目标任务 blob 轮换及根文件变化；其余 14 个任务文件连 mtime 都不变 |
| 单事件重复测量 | 10 次，中位 25.1 ms，最大 89.2 ms |
| 删除 | 目标 blob 被删除，剩余 14 个任务文件不变，重新打开后任务数为 14 |
| 原数据保护 | 原 agent 全部文件和原密钥长度/SHA-256 前后不变 |
| 官方 `profile_store.py 20` | 6.8 MB 明文合成状态，单事件事务中位 99.7 ms、最大 211.2 ms；退出码 0 |

任务文件使用内容摘要命名；一次更新会删除旧 blob、创建新 blob，不能只按固定文件名比较。存储功能全部通过。

说明偏差：验收 README 第 19 行仍写 `task-state/`，实际常量 [store.py:29](D:/code/rynmesh/rynmesh/agent_runtime/store.py:29) 为 `tasks/`。没有修改此说明文件。证据：[副本验收](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/store/store-results.md)、[脱敏聚合和尺寸](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/store/summary.json)。

## 5. 发现的线程复用缺陷及复现

**P2：动态权限提示导致真实 Agent 在首个 shell/write 后及同窗追问时完整重播种。** 这是本批线程复用与现有 v4 提示的集成缺陷。没有已提交的改动前快照，因此不伪称做过 Git 前后回归；但本批明确要求的 F 行为已由实测证伪。

复现步骤：

1. 使用全新隔离 home，启用 Agent，选择本机 Codex CLI、workspace 权限及全新工作区。
2. 创建 C 场景，观察第一个 shell/write 后的第二次模型调用；线程已在保活池内，却由 started 再次变为 started。
3. 完成 C 后，对同一 `/api/local/agent/tasks/{id}/messages` 发送 F，带当前 revision；原窗口和历史都不变。
4. 观察 F 首步和首次 patch 后的第二步均建立新 PID；首步没有明显提速。再开 G 时 F 原线程仍在池中，因此不能归因于跨窗口挤占。

定位链：

- [runtime.py:967](D:/code/rynmesh/rynmesh/agent_runtime/runtime.py:967) 处理追问，经过 [state.py:95](D:/code/rynmesh/rynmesh/agent_runtime/state.py:95) 增加 goal revision。
- [operation_permissions.py:18](D:/code/rynmesh/rynmesh/agent_runtime/operation_permissions.py:18) 按当前 goal 筛选 write receipts；首个 shell/write 后从空变非空，追问时旧回执不属新 goal 又变空。识别到的只读 shell 也保守记为 write receipt。
- [prompt.py:104](D:/code/rynmesh/rynmesh/agent_runtime/prompt.py:104) 因该列表是否为空，在普通 prose 条件追加/移除 235 字符的固定权限说明。
- [codex_threads.py:127](D:/code/rynmesh/rynmesh/llm_package/codex_threads.py:127) 只允许同标签 JSON 状态行变化，遇到普通 prose 变化返回 `None`；[codex_threads.py:284](D:/code/rynmesh/rynmesh/llm_package/codex_threads.py:284) 随后关闭旧线程，重新完整播种。

离线对照已确认 C→F、F→F_after_G 的 canonical 历史都是精确前缀，task 相同，scope_revision/context_epoch 不变；权限事实分别 3→0、2→0。仅在临时诊断内存中剔除这段可选 prose 后，`state_lines` 就接受变化。该定位实验没有修改业务代码或真实模型请求。

B/C/D/E 首个 shell/write 后也复现该问题；C 后三步及 F 第三步提示稳定时可以 resumed。固定提示的第 2 项脚本没有这个触发条件，所以适配器测量通过和端到端 F 失败同时成立。无需改变 kill switch 即可定位；没有将 stateless 模式必然重建线程误当作对该缺陷的通过对照。

证据：[脱敏线程审计](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/store/thread-reuse-audit.json)、[离线复现脚本](C:/Users/子陵/AppData/Local/Temp/ryn-agent-acceptance-16a0912ecc244b13becd2c775e26595c/store/audit_thread_reuse.py)。未自行修复。

## 6. 改动前后对比与未验证范围

| 指标 | 改动前参考 / 本次 stateless 对照 | 本次结果 | 可下的结论 |
| --- | --- | --- | --- |
| Codex 后续步 | 历史中位约 17 s、最长 97 s；本次 stateless 13.343 s | 复用 2.686 s，中位降低 79.87% | 固定提示的配对脚本明显加速，真实 Agent 仍有第 5 项缺陷 |
| Codex 同窗追问 | 本次 stateless 13.171 s | 脚本复用 3.093 s；真实 F 首个 adapter 调用 19.446 s | 脚本成功，真实集成未通过 |
| 工具后间隙 | 历史中位 1.2–6.4 s | 本次总体中位 0.616 s | 观测改善；不是同机前态代码的严格 A/B |
| A 总耗时 | 局域网 Gemini 历史 20.6 s | Codex 17.172 s | 模型及路径不同，只并列记录 |
| B 总耗时 | 历史 64.7 s | Codex 56.641 s，0 审批 | 本次完成且无审批，不把差值完全归因于提速 |
| 存储事务 | VM 合成历史 414–419 ms；VM 新版 56–60 ms | 主机合成 99.7 ms；真实小副本 25.1 ms | 跨机/尺寸/负载不同，不宣称精确同机加速倍数；已验证仅写目标任务 |

没有验证混合版本节点间的复用、30 分钟真实空闲过期、真实大窗口长任务的压缩收益；本次所有端到端场景压缩次数为 0，相应预算逻辑由指定自动测试覆盖。G 验证了最小场景的窗口隔离与保留，未进行攻击性跨窗口数据泄漏测试。前述未通过或只在复跑通过的自动用例不会被改写为首跑通过。参考 late-receipt 首次失败已清洗掉断言帧，只能给出用例和候选时限行号，无法精确还原首跑断言位置；独立重跑没有复现。

最终只新增本报告；初始已修改的三个验收说明文件保持原状。本次测试生成的未跟踪 fixture 证据另存 TEMP，未提交或推送。


## 7. P2 复测

复测时间：2026-10-15 22:57 +08:00。**结论：本次 P2 修复通过三个验收标准。** C 在 shell 及 patch 后均续用；F 首步续用且明显快于 C 首步；B 在 shell 后续用。本节追加复测结果，保留以上首次验收记录，未把先前其他测试的失败改写为通过。

本轮使用全新 TEMP home、network 和 workspace，后端仅绑定 `127.0.0.1:18974`，选择本机 Codex CLI 默认模型，启用 Agent、权限 `workspace`，`RYN_CODEX_SESSION_REUSE=1`。仅按 C → 同一 task 的 F → 独立 B 顺序执行；自动测试在真实场景开始前已完成。没有访问或操作 `%LOCALAPPDATA%\Ryn`、`C:\Users\子陵\.rynmesh`，没有复制现有 Agent 数据。证据位于本次全新临时目录：

`C:\Users\子陵\AppData\Local\Temp\ryn-agent-p2-retest-f6bcf7eb02954480ad8eeffe5d6aafff`。

### 自动化测试

```powershell
.venv\Scripts\python.exe -m pytest -q -rfE tests/test_agent_speedups.py tests/test_codex_thread_reuse.py tests/test_agent_codex_loop.py tests/test_agent_default_workspace.py
```

**80 passed / 0 failed / 0 skipped / 0 errors，14.22 s，exit 0。** 包含新增回归测试 `test_system_prompt_changes_only_in_state_lines_after_a_write`。`prompt.py:104` 当前无条件附带权限说明，只有 JSON 状态的 `operation_permissions` 按需变化。[测试输出](C:/Users/子陵/AppData/Local/Temp/ryn-agent-p2-retest-f6bcf7eb02954480ad8eeffe5d6aafff/tests/pytest-result.txt)。

### 三个场景

总耗时与首字均从 POST 前计时；首字为首个真实 `text_delta`，可能是进度文字。模型段为 `model_started → assistant`，工具后间隙为 `tool_result → model_started`。终态轮询间隔 0.2 s。F 的完整 GET 导出包含 C，故另保存 `F-round.json` 用本轮事件计算 F；完整导出及分轮文件均实际运行了 `step_gaps.py`。

| 场景 | 终态 | 总耗时 s | 首字 s | 模型步数 | 模型段中位 s | 工具后间隙中位 s |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| C | completed | 38.359 | 12.334 | 5 | 5.050 | 0.193 |
| F | completed | 17.328 | 3.396 | 3 | 4.614 | 0.217 |
| B | completed | 23.141 | 10.662 | 2 | 10.621 | 0.193 |

三个场景的工具发现、上下文压缩、审批请求均为 0。C 执行了 shell、两次 `ryn_apply_patch` 以及最终文件读回；F 以 patch 添加测试后运行检查；B 执行一次只读 OS shell。

### 每次模型调用及线程状态

以下为适配器 `chat()` 单次墙钟耗时，与上表模型事件段的边界不同。T1 表示 C/F 共享的同一 PID 和线程摘要，T2 表示 B 的独立线程。CLI setup 还有一次无 scope 的自测，不计入这 10 次场景调用。

| 场景内调用 | 状态 | 适配器耗时 s | 线程 | 增量消息数 / 完整会话消息数 |
| --- | --- | ---: | --- | ---: |
| C1 | started | 13.737 | T1 | 首次播种 |
| C2 | resumed | 4.641 | T1 | 1 / 3 |
| C3 | resumed | 4.142 | T1 | 1 / 5 |
| C4 | resumed | 4.650 | T1 | 1 / 7 |
| C5 | resumed | 6.648 | T1 | 2 / 10 |
| F1 | resumed | 5.725 | T1 | 1 / 12 |
| F2 | resumed | 4.171 | T1 | 1 / 14 |
| F3 | resumed | 4.153 | T1 | 2 / 17 |
| B1 | started | 11.733 | T2 | 首次播种 |
| B2 | resumed | 8.643 | T2 | 1 / 3 |

序列：C `started → resumed → resumed → resumed → resumed`；F `resumed → resumed → resumed`；B `started → resumed`。共 **started=2 / resumed=8 / fallback=0 / rebuilt=0**。C2 在首个 shell 后、C3/C4 在两次 patch 后均 resumed；F1 延续 C 的 T1，B2 延续 B 的 T2。

### 耗时对比和提示审计

| 指标 | 上次 P2 缺陷现场 s | 本轮 s | 状态变化 |
| --- | ---: | ---: | --- |
| C 首步适配器 | 17.852 | 13.737 | 首次 started |
| C shell 后适配器 | 14.638 | 4.641 | started → resumed |
| F 首步适配器 | 19.446 | 5.725 | started → resumed |
| B shell 后适配器 | 18.990 | 8.643 | started → resumed |

本轮 F 首步比本轮 C 首步低 **58.3%**，比上次 F 首步低 **70.6%**。这满足“同窗追问明显更快”的本轮配对标准；与上次绝对耗时的差值仍受上游时段及上次并发负载影响，仅一轮 C/F/B 不用于宣称稳定分位数。

8 次 resumed 前，诊断均确认历史精确延续、控制消息角色一致、`Session.continuation()` 接受变化；C/F 期间 PID 和线程摘要保持相同。每次变化都只位于**第 1 条控制消息的第 2 行，类型为带标签 JSON 状态行**，普通 prose 变化数为 **0**。F1 只新增 1 条消息，而当前完整会话主体为 12 条，没有重新播种完整历史。未发生需要进一步定位的提示文字变化。这里只保存位置、类型、长度和布尔值，未贴提示或任务正文。

[场景聚合](C:/Users/子陵/AppData/Local/Temp/ryn-agent-p2-retest-f6bcf7eb02954480ad8eeffe5d6aafff/summary.json)、[逐调用与提示审计](C:/Users/子陵/AppData/Local/Temp/ryn-agent-p2-retest-f6bcf7eb02954480ad8eeffe5d6aafff/audit-summary.json)、[step_gaps 输出](C:/Users/子陵/AppData/Local/Temp/ryn-agent-p2-retest-f6bcf7eb02954480ad8eeffe5d6aafff/step-gaps.txt)、[原始调用统计](C:/Users/子陵/AppData/Local/Temp/ryn-agent-p2-retest-f6bcf7eb02954480ad8eeffe5d6aafff/inferences.jsonl)。同目录保留 C/F/B 的 GET JSON 及本轮临时 harness。包装器原样调用业务方法，提示比较与 continuation 诊断不改变请求或线程状态。

隔离后端及其 app-server 子进程已停止，端口 18974 已关闭。复测前后核对 959 个业务/测试/前端源码文件摘要，无变化；已有修复保持原样。仅向本报告末尾追加本节，没有改业务或测试代码，没有 commit 或 push。
