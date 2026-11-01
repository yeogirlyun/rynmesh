# Stage 3.3 自然语言任务实测验收与 Stage 4 原生工具协议对照实验报告

- **记录时间**：2026-10-16
- **实测模型**：`gpt-6.1-sol`（通过本地 `codex_cli` app-server 隔离运行，`reasoning_effort="low"`）
- **提示词约束**：全部自然语言验收均**不在提示词中指定工具名或恢复路线**（`prompt_hints_tools_or_recovery: false`），如实标注隔离测试夹具范围与未测项。

---

## 一、 Stage 3.3 自然语言任务实测汇总

| 任务类型 | 实测脚本与 JSON 产物 | 自然语言提示词（无工具提示） | 状态 / 步数 | 首个有效结果时间 | 总耗时 | 人工介入次数 | 自主工具序列与结果核实 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1. 余额查询（无提示自然语言查询）** | [`live_stage01_natural_read.py`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live_stage01_natural_read.py) / [`live-stage01-natural-read.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage01-natural-read.json)（`unhinted_natural_query`） | `"请告诉我当前工作区里记录的示例账户余额和币种。"` | `completed` / **3 步** | `~10,578 ms`（Step 1 `ryn_file_list`） | **`16,563 ms`** | **0** | 模型自主选择 `["ryn_file_list", "ryn_file_read"]`，0 错误，返回 `"示例账户余额为 **123.45 CNY**（币种：**CNY**）。"` |
| **2. 磁盘空间排查（无提示自然语言查询）** | [`live_stage03_disk_investigation.py`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live_stage03_disk_investigation.py) / [`live-stage03-disk-investigation.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage03-disk-investigation.json) | `"请排查当前工作区目录的磁盘空间占用情况，找出占用空间最大的前 3 个子目录和最大的前 3 个具体文件（包含相对路径与准确字节数），并简短说明哪些属于可清理的缓存或日志。"` | `completed` / **3 步** | **`21,500 ms`**（首轮流式文本 `8,359 ms`） | **`29,687 ms`** | **0** | 模型自主选择 `["ryn_shell_execute", "ryn_process_read"]`，空轮询 `0` 次、重叠扫描 `0` 次，前 3 大目录与前 3 大文件字节数覆盖率 `3/3` |
| **3. 多类同名方法定位与补丁修改（无提示自然语言修改）** | [`live_stage33_acceptance.py`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live_stage33_acceptance.py) / [`live-stage33-file-modification.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage33-file-modification.json) | `"请在这个项目里查找 EnterpriseInvoice 的税率计算逻辑，将它的默认税率从 0.05 改为 0.08，不要改动 PersonalInvoice 的税率，修改后确认文件内容并简短说明改了哪个文件。"` | `completed` / **5 步** | **`11,156 ms`**（Step 1 `ryn_file_search` 完成，`run_continuously` 首轮批处理返回时间 `20,984 ms`） | **`30,000 ms`** | **0** | 模型自主选择 `["ryn_file_search", "ryn_file_read", "ryn_file_list", "ryn_apply_patch", "ryn_file_read"]`，`tool_error_count: 0`；在同一个文件存在两个同名 `def compute_total` 且原 `tax_rate = 0.05` 相同的条件下，精准将 `EnterpriseInvoice` 改为 `0.08` 并保留 `PersonalInvoice` 为 `0.05` |

> **边界与局限说明**：上述三项自然语言验收均在受控隔离工作区夹具上实测通过；这证明在当前工具链与提示词下模型能够无提示完成文件搜索、后台进程读取与同名方法补丁修改，但不据单次夹具成功承诺任意复杂宿主环境或全盘大目录场景下的 100% 成功率。

---

## 二、 Stage 4 独立对照实验：Codex 原生 `dynamicTools` 协议 vs JSON 提案包装

- **实验脚本**：[`docs/acceptance/agent-upgrade-20261016/live_stage4_protocol_experiment.py`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live_stage4_protocol_experiment.py)
- **实验原始产物**：[`docs/acceptance/agent-upgrade-20261016/live-stage4-protocol-experiment.json`](file:///d:/code/rynmesh/docs/acceptance/agent-upgrade-20261016/live-stage4-protocol-experiment.json)
- **对照条件**：
  - 模型：`gpt-6.1-sol`（`codex.exe app-server --stdio --strict-config`）
  - 推理强度：`reasoning_effort="low"`
  - 协议 Schema 预检：`codex_session._check_protocol` 验证通过（`ThreadStartParams.json` 支持 `dynamicTools`、`ephemeral`、`environments`；`TurnStartParams.json` 支持 `outputSchema`）
  - 相同任务提示词：`"请查找工作区中 service_config.py 里的 DEFAULT_TIMEOUT_SECONDS，将它的值从 15 改为 30，完成后简短告诉我修改结果。"`
  - 相同安全隔离约束：提供端运行在空临时目录、`sandbox="read-only"`、`approvalPolicy="never"`，工具仅能在请求端（Origin Client）执行。

### 对照实测结果

| 对照维度 | Arm A：`json_proposal_wrapper`（现行生产方案：`codex_tools.chat` + `outputSchema` + `codex_threads` 复用） | Arm B：`native_dynamic_tools`（原生 `dynamicTools` + `item/tool/call` 拦截） |
| :--- | :--- | :--- |
| **任务完成状态** | **`completed`（成功修改文件并回读验证）** | `completed`（**未调用工具，文件未修改**，`file_updated_correctly: false`） |
| **执行步数** | **5 步** | 1 步（直接文本回复拒绝执行） |
| **总耗时 (`total_elapsed_ms`)** | **`21,797 ms`** | `14,094 ms`（首轮即终止） |
| **单步推理耗时 (`wall_ms`)** | Step 1（冷启动建线程）：`8,015 ms`<br>Step 2–5（热线程复用）：**`3,250 ms` / `3,734 ms` / `2,265 ms` / `3,469 ms`** | Step 1（无池化）：`13,938 ms` |
| **线程复用统计 (`thread_pool_stats`)** | `{"started": 1, "resumed": 4, "fallback": 0, "rebuilt": 0}` | `{"started": 0, "resumed": 0, "fallback": 0, "rebuilt": 0}` |
| **工具调用序列** | `["ryn_file_search", "ryn_file_search", "ryn_file_read", "ryn_apply_patch", "ryn_file_read"]` | `[]` |
| **工具错误/重试次数** | **0** | 0（未发起调用） |

### 实验结论与架构决策

1. **P2P 提供端只读沙箱与原生 `dynamicTools` 的冲突（实测已确认）**：
   - 为保证 P2P 提供端安全，`codex app-server` 必须以 `sandbox = "read-only"` 与 `approvalPolicy = "never"` 启动（[`codex_session.py:77-78, 496`](file:///d:/code/rynmesh/rynmesh/llm_package/codex_session.py#L77-L78)）。
   - 在 Arm B（原生 `dynamicTools`）下，`codex app-server` 注入的内置沙箱环境提示词会使模型认为当前环境禁止工具/写入操作，导致模型直接回复 `"当前环境工具执行不可用，未能读取或修改文件..."` 而放弃调用 `ryn_client_*` 动态工具。
   - 相比之下，Arm A（`json_proposal_wrapper`）通过 `outputSchema` 将客户端工具调用建模为纯 JSON 数据提案，配合显式声明客户端工具由请求端执行的提示词（[`codex_session.py:474-476`](file:///d:/code/rynmesh/rynmesh/llm_package/codex_session.py#L474-L476)），在完全保持提供端 `read-only` 沙箱隔离的同时实现了 **100% 正常工具提议与文件修改**。
2. **多轮工具循环中的线程复用优势**：
   - 在 Arm B 中，当拦截 `item/tool/call` 立即结束提供端 turn 时（[`codex_session.py:552`](file:///d:/code/rynmesh/rynmesh/llm_package/codex_session.py#L552)），提供端线程处于未完成工具调用状态，下一轮只能重新启动无状态新进程；
   - 在 Arm A 中，每轮 `turn/start` 均正常返回 JSON 并完成 turn，`codex_threads` 池可在后续 4 步中 100% 复用热线程（`resumed: 4`），将单步模型调用耗时从冷启动的 `8.0s` 降至 `2.2s–3.7s`。
3. **最终决策**：**保留现行 `codex_tools.chat`（JSON `outputSchema` 提案包装 + `codex_threads` 增量会话池）作为生产方案，不切换为原生 `dynamicTools` 协议。**
