# Ryn 助手稳定性完整修复方案

- 日期：2026-11-09
- 基线：`develop` @ `57b807f`
- 本文合并并取代同日的两份草稿：《对话出错就报废修复方案》和《共享模型和本机模型能力一致修复方案》。
- 主机检查脚本（只读，只输出数量、模型名、错误码）：`artifacts/agent-robustness-20261109/check-agent-robustness.ps1`
- 只写要做什么、怎么判断做对了，不含代码。行号基于 `57b807f`，仅供参考，以函数名为准。

---

## 0. 执行规则（开始前必须读完）

1. **只做本文列出的改动。** 没写到的文件、函数、行为都不改；不顺手优化，不重命名，不格式化无关代码。
2. **分支：** 从最新 `develop` 切出 `codex/agent-stability`。开始前看 `git status`，别人未提交的改动保持原样，不 stash、不 checkout 覆盖、不删除。
3. **每一步单独提交**（提交信息写在各步末尾）。提交前必须跑完该步列出的测试，并全部通过。
4. **对不上就停。** 本文描述和代码对不上、或者改动会牵连本文没提到的文件时，停下来，写进验收文档的“待确认”一节，不要猜着改。
5. **安全底线不能降低：** 结果不确定的写操作，程序永远不能自动重新执行同一个操作（同一个 `operation_id`）。所有改动都在这条底线之内。
6. **不得提交：** `artifacts/`、日志、数据库、缓存、凭据、构建产物。
7. **第一个提交：** 把本文件加入仓库，提交信息 `docs: plan the complete Agent stability fix`。
8. 按 `AGENTS.md` 交付：验收通过后用 `--no-ff` 合并进 `develop`，从 `develop` 的已提交源码打包，记录构建提交号、验证结果和安装位置；更新主机前保留可回滚的安装包。
9. 本文第 R 节（关联问题）涉及线上中心服务和公司插件仓库，**每一项都要先得到主人单独同意**，不包含在本分支里。

---

## 1. 目标

**一句话：基础操作不出错；万一出错，只影响出错的那一步，对话永远能继续；共享模型和本机模型能力一样。**

改完后，下面九条必须始终成立（第 13、14 步用测试固定下来）：

| 编号 | 规则 |
|---|---|
| I1 | 任务只要不在运行中，就必须能：发新消息、换模型、删除/归档。唯一例外：Agent 总开关关闭，或任务已归档（可以恢复）。 |
| I2 | 历史记录里的旧内容（图片、超长结果、换模型前留下的格式）永远不会让新请求失败。发请求前，程序会把历史调整成当前模型能接受的样子。 |
| I3 | 结果不确定的写操作永不自动重做（保留现有保证）。 |
| I4 | 界面显示的状态和后台一致：只有真的在运行、正在停止（最多 5 秒）、正在核对（最多 60 秒）时，才转圈和计时。 |
| I5 | 每一种失败或拒绝都有中文说明，并至少有一个出路按钮；不认识的错误码统一显示“重试 / 换模型 / 新对话”。 |
| I6 | 网络抖动只影响当时那一个请求，不会锁住其它对话，也不需要手动点“恢复”。 |
| P1 | 能不能看图，只由模型本身决定，不由“本机配置”还是“共享来的”决定。 |
| P2 | 共享线路传图片的规格向本机看齐（见 3.7）。 |
| P3 | 同一套场景测试，分别在本机线路和共享线路各跑一遍，结果必须一致。 |

---

## 2. 已确认的问题

### 总表

| # | 现象 | 原因（一句话） | 对应步骤 |
|---|---|---|---|
| 2.1 | 发图片报 `agent_image_input_unsupported`，而 DeepSeek flash、GPT 本来能看图 | 能不能看图按“接入线路”判断，共享来的模型一律算不能看图 | S6、S7 |
| 2.2 | 截图、图片文件、扫描版 PDF，共享模型看不了 | 共享线路单张图片上限只有 128 KB，不支持 GIF | S7 |
| 2.3 | 一次图片报错后，这个对话再发什么都报错 | 每次请求前都检查整段历史，历史里的图片被拒，之后每一轮都失败 | S8 |
| 2.4 | 从 Excel 复制单元格粘贴，变成了图片 | 粘贴时只要剪贴板里有图片，就把文字丢掉 | S9 |
| 2.5 | 停止、重启、超时后，对话锁死：不能发、不能换模型、不能删 | “结果不确定”被做成了整个任务的锁，而且永远解不开 | S1 |
| 2.6 | 插件查询报一个普通错误，对话就锁死 | 插件工具默认都算“写操作”，写操作返回错误被当成“结果不确定” | S2 |
| 2.7 | 点了“取消”的对话，再也不能发消息 | 允许继续的状态里没有“已取消” | S3 |
| 2.8 | 左侧一直转圈、显示“运行中”，其实什么都没在跑 | “核对中”状态永不结束；停止没有时间上限 | S4 |
| 2.9 | 网络抖一下，所有对话都发不了消息，要手动点“恢复” | 前端把“不确定是否送达”做成了全局锁，而且重启后仍然保留 | S5 |
| 2.10 | Agent 说“企业微信插件没有审批查询接口”，其实有 | 插件工具列表是旧快照，从不刷新；插件不可用时被静默去掉 | S10 |
| 2.11 | 长对话突然不能再发消息 | 事件满 1000 条或记录超过 2 MB 就拒绝，而事件只增不减 | S11 |
| 2.12 | 报错只显示一串代码 | 前端没有这些错误码的说明，也没有出路按钮 | S12 |
| 2.13 | 一些已经修好的问题，主机上仍然出现 | 修复已合并进 `develop`，但还没装到主机上 | S16 |

### 2.1 能不能看图，按“接入线路”判断，而不是按模型

判断函数是 `image_capabilities.evaluate_image_capability`。用当前代码实测，同一个模型通过不同方式接入时的结果：

| 接入方式 | 程序判断 | 原因代码 |
|---|---|---|
| 在本机“模型来源”里直接填 API | 能看图 | `verified_api_image_carrier` |
| 本机直接填 API，但服务商的模型列表把它标成只支持文字 | 不能 | `model_metadata_text_only_modality` |
| 别的设备、同事或公司共享来的（peer / provider） | **永远不能** | `peer_image_route_unverified` |
| 本机服务线路（local_service），类型是 openai_compatible | **永远不能** | `unverified_adapter_image_support` |

具体问题有四个：

1. **只有一处在拦。** 用 Ryn 实际生成的附件图片（50 KB JPEG）逐个环节实测共享线路，只有第一处拦截：

   | 环节 | 结果 |
   |---|---|
   | 使用方：任务发请求前，`runtime.py:659` 调用 `require_route_image_support` | **拦截** |
   | 使用方：发往共享方前，`bridge.py:206` 检查旧版线路规格 | 通过 |
   | 共享方：收到后，`llm_package/routes.py:507` 调用 `validate_chat` | 通过 |
   | 共享方：`ProviderService.handle` 调用 `adapter.chat`，转给服务商 | 原样转发 |

   现有测试也互相矛盾：`tests/test_agent_image_carrier_profile.py::test_peer_route_blocks_expanded_image_with_zero_execute_order_and_allows_legacy_small_image` 证明 bridge 能把小图发给共享方；而 `tests/test_agent_image_routes.py:340`–`348` 规定共享线路一律不能看图。

2. **共享方不说。** 共享方发布模型时（`llm_package/routes.py:1444`–`1449`），能力列表里只有文字生成、流式、工具调用等几项，从不说明能不能看图。共享模型的显示名和本机配置的一模一样（都是“{来源名} · {模型名}”），界面上分不出来。

3. **主人没法改。** 模型设置（`model_settings.FIELDS`）里没有“图片输入”这一项。“测试连接”只测连接、模型是否存在、工具调用三项（`model_settings.public` 的 `evidence` 只输出这三项），不测图片。

4. **前端不显示。** 后台已经算出了 `supports_image_input` 和原因，但前端 `AgentModel` 类型（`webapp/src/domain/rynAgent.ts:90`）没有读取。

### 2.2 共享线路传图片的规格比本机小

| 规格 | 本机（`image_input_profile.py:20`–`23`） | 共享线路现在（`chat.py:158`–`166`） |
|---|---|---|
| 单张 | 8 MiB | 128 KiB |
| 每次请求合计 | 12 MiB | 4 张 × 128 KiB |
| 格式 | PNG / JPEG / WEBP / GIF | PNG / JPEG / WEBP |

实测：一张 1600×1000 的截图 PNG 有 4.8 MB，在共享线路上会被拒绝（`peer_image_url_length_exceeded`）。

各类内容在本机和共享线路上的现状：

| 内容 | 本机 | 共享 |
|---|---|---|
| 文字；xlsx、docx、pptx、csv、文字版 PDF 附件（抽取最多 12000 字，并附本地路径） | 正常 | **一样**（`attach_to_message` 不区分线路） |
| 图片附件 | 能看 | 不能 |
| 让 Agent 打开图片文件、扫描版 PDF 来看（`ryn_image_inspect`） | 能看 | 不能 |
| 桌面操作截图、浏览器截图、插件返回的图片 | 能看 | 不能 |
| GIF | 能看 | 不能 |

### 2.3 一张被拒的图片，让对话之后每一轮都失败

- `runtime.py:659` 和 `bridge.py:185` 用 `message_has_image_input` 检查**整段历史**。只要发现图片、而当前线路被判断为不能看图，就报 `agent_image_input_unsupported`，任务 0 秒失败。
- 图片一直留在历史里，所以之后发文字、发 xlsx，也都会失败。
- 反过来：模型能力是“未知”时，图片会发出去。服务商如果拒收，任务失败；下一轮带着同一张图，再失败一次。

### 2.4 从 Excel 粘贴变成了图片

`RynAgentComposer.tsx:163` 的 `onPaste` 只要发现剪贴板里有文件，就当成附件，并取消文字粘贴。Excel、WPS 复制单元格时，剪贴板里同时有文字和一张表格图片，结果文字被丢掉，只剩图片。截图 2 里那条消息带的就是一张表格图片。这是推断，第 0 步在主机上复现。

### 2.5 “结果不确定”把整个任务锁死

**触发条件：** 写操作已经发出、但还没拿到结果时，发生下面任一情况：
- 主人按停止。`runtime.py:2010` 先把任务设成已暂停，随后 `_fail`（`runtime.py:2542`）又改成“中断 + 结果不确定”；
- 程序重启或崩溃（`runtime.py:2403`）；
- 超时、连接中断；
- 插件报错（见 2.6）。

**锁死后：** `control.py:56` 的 `ambiguous`，以及 `:76`、`:85`、`:89` 这几处判断，禁止了发消息、继续、换模型、编辑，**连删除和归档也禁止**，只剩“取消”。

**解不开：** 程序会自动核对 3 次，但本地命令、桌面操作没有完成回执，`local_tools.py:2149` 的核对结果永远是“未知”。界面提示（`rynAgentText.ts:168`）让用户“新建任务”。

**这样反而更不安全：** 新任务完全不知道上一步可能已经执行过，用户只会让它从头再做一遍。

### 2.6 插件报一个普通错误，对话就锁死

- `extensions/adapter.py:523`：插件工具只有主人手动标成只读后才算“读”，否则一律算“写”。插件服务器自己声明的只读（`readOnlyHint`），只在插件设置页显示为“建议”，需要主人点确认才生效。
- `extensions/adapter.py:1457`：“写”工具正常返回了一个错误（`isError`，比如参数不对、查不到数据），也被记成“结果不确定”，于是进入 2.5 的锁死。

### 2.7 已取消的对话不能再用

`control.py:76` 允许继续发消息的状态里没有 `cancelled`。

### 2.8 界面和后台不一致

- `state.py:55` 的 `synchronize`：只要错误码是“结果不确定”，`run_state` 就永远是 `reconciling`（核对中），即使核对早就结束了。前端把它当成运行中：侧栏转圈（`RynAgentTaskIndicator.tsx:24`），本轮显示“运行中 · N 秒”，每 0.5 秒刷新一次（`RynAgent.tsx:371`、`:621`）。
- 按停止时，如果有操作正在执行，状态是 `pausing`（停止中），会一直持续到那个操作自己结束，没有时间上限。

### 2.9 网络抖一下，所有对话都发不了消息

- 新建对话的请求如果没收到明确回复（网络错误或 5xx），前端会设置 `uncertain`。这个标记存在本地存储里（`RynAgent.tsx:226`，键 `PENDING_KEY`），重启后仍然有效。
- 控制操作（停止、继续等）出现同样情况，会设置 `controlUncertain`。
- 这两个标记任何一个存在，`send`（`RynAgent.tsx:738`）就会拒绝**所有**对话的发送。只有等轮询找到那条记录，或者主人手动点横幅上的“恢复”（`:1101`、`:1102`），才会解除。
- 后台对新建和控制请求都会按 `client_request_id` 去重（`runtime.py:1656` 起，`lifecycle.duplicate`；控制请求用 `request_replayed`），所以用同一个编号自动重发是安全的。现在没有自动重发。

### 2.10 插件工具列表是旧快照

2026-11-09 另一个会话已核实，本文已复查代码确认：
- 插件的工具列表 `discovered_tools`，只在登录插件、手动测试或重启插件、启用插件、激活本地服务时才写入（`extensions/store.py:2456`）；Ryn 启动时、每轮对话开始时都不刷新，也完全没有处理 MCP 的 `tools/list_changed` 通知（代码里搜不到 `list_changed`）。
- 不可用的插件服务器会被静默去掉（`adapter._active_mcp_tools`）。
- 发给模型的只有主人选中的技能，插件目录没有说明文字。
- 结果：中心 11-04 才上线的企业微信审批查询工具（`wework_approval_get` 等），在更早登录的设备上根本不存在。Agent 搜不到这个工具，于是说“不支持”，还让用户发截图。这正是本次截图 1 的那个请求。

### 2.11 长对话到上限后不能再发消息

- `runtime.py:1782`：事件满 1000 条后拒绝新消息。`runtime.py:1816`：记录超过 2 MB 也拒绝；编辑消息时的同类检查在 `:1944`、`:1951`。都报 `agent_task_limit`。
- 事件只增不减。流式输出时，每 5 秒左右就存一条 `text_delta`（`loop.py:476`–`511`），本轮结束后也不清理；工具结果也存在事件里。
- 今天的 `artifacts/agent-send-rejected-20261109/` 就是这个情况。

### 2.12 报错只显示代码

`agent_image_input_unsupported`、`agent_task_limit`、`agent_operation_unresolved`、`agent_context_limit` 在前端都没有说明文字。发送失败时统一显示“操作失败”（`RynAgent.tsx:73` 的 `mutationErrorKey`）；任务出错时显示“任务未能完成，错误代码见下方”（`RynAgent.tsx:1161`）。

### 2.13 已修好、但还没装到主机上的修复

下面这些都已经在 `develop` 里，主机上还没装：

| 合并提交 | 解决的问题 |
|---|---|
| `1ef3779` | 助手页“暂时无法刷新”、计时停住 |
| `3284f4c` | 存储满了任务失败（`agent_storage_limit`），改为自动归档旧任务 |
| `e81e26e` | “自动处理”模式什么都要确认 |
| `1afd85a` | 插件页、Agent 记录切换慢 |
| `5bae09d` | 打开对话时不在最新一条消息 |
| `08cabdf`、`23eb988`、`57b807f` | 插件页白屏 / 增值税弹窗、打包速度和体积 |

主机上出现的部分不稳定，可能是这些旧问题。S16 会一次性全部装上。

---

## 3. 设计

### 3.1 原则

1. **出错只针对那一步。** 出错的那一步作为一条工具结果写进历史，告诉模型发生了什么；任务进入可以继续的状态。
2. **结果不确定时，告诉模型并暂停，而不是锁死。** 主人发下一条消息，就等于“我知道了，继续”。模型会收到明确说明：这一步可能全部执行、部分执行或没执行，重做之前先用只读方式检查。程序自己永远不重做。
3. **发请求前，把历史调整成当前模型能接受的样子**，不拿旧内容报错。
4. **能在输入框提前发现的问题，就在输入框提示。**
5. **限制靠压缩和接续解决，不靠拒绝。**
6. **共享方说了算。** 模型能不能看图，由配置这个模型的那台电脑判断，再随模型一起发布出去；使用方照着执行。
7. **网络不确定时，自动用同一个请求编号核对和重发**（后台会去重），而且只影响那一个对话。

### 3.2 为什么不改任务的状态值

结果不确定的任务，仍然保持 `status="interrupted"`、`error="agent_write_outcome_unknown"`，操作记录仍是 `dispatched`。原因是现有的验收脚本（`docs/acceptance/ryn-agent-delivery/real-model/`）和大量测试依赖这些值；长任务续跑那次改状态值，就把这些脚本弄坏了。解锁只靠两件事：放开权限，以及“发消息就等于确认”。

### 3.3 共享方发布图片能力

共享方发布每个模型时（`llm_package/routes.py:1444` 构造 `LLMPackageManifest` 处，以及 CLI 的 `cli_models` 条目），用本机同一个 `evaluate_image_capability` 判断自己的模型，在 `capabilities` 里加入：

| 共享方的判断 | 加入的标记 |
|---|---|
| 能看图 | `image-input` |
| 不能看图（主人设置或图片测试确认的） | `image-unsupported` |
| 未知 | 不加 |
| 新版本（不论判断结果） | `image-carrier:inline-v2` |

`image-input` 和 `image-unsupported` 已经在 `image_capabilities` 的标记表里（`SUPPORTED_CAPABILITY_MARKERS` / `UNSUPPORTED_CAPABILITY_MARKERS`），不需要新增词汇。

### 3.4 使用方怎么判断共享模型

`evaluate_image_capability` 的 peer 分支改为：

| 共享方的标记 | 判断 | 传输方式 |
|---|---|---|
| `image-unsupported` | 不能看图（原因 `provider_declared_image_unsupported`） | 无 |
| `image-input` | 能看图 | 有 `inline-v2` 用 v2，否则用 v1 |
| 都没有 | 未知，**允许发送**（和本机“未知”的处理一样） | 同上 |

另外，`local_service` 线路的类型是 `openai_compatible`、`openai_responses`、`anthropic_messages` 时，按和 `local_api` 相同的规则判断。

### 3.5 本机模型的“图片输入”判断顺序

主人设置 > 图片测试结果 > 服务商模型列表里的 `input_modalities` > 内置已知视觉模型表。

### 3.6 图片测试

- 发一张 16×16 的纯红色 PNG，问“这张图是什么颜色？只回答颜色”。
- 回答里有“红”或 “red”，记 `supported`；服务商返回 4xx 且表明不接受图片，记 `unsupported`；其它情况记 `unknown`。
- 结果写入该模型的 `evidence.image`，包括状态和检查时间。

### 3.7 共享线路的图片规格

| | v2（新增；双方都是新版本） | v1（现有；共享方是旧版本） |
|---|---|---|
| 单张 | ≤ 6 MiB（实施时由 8 MiB 下调，见下） | ≤ 128 KiB，data URL ≤ 180000 字符 |
| 每次请求合计 | ≤ 6 MiB 原始字节（本机是 12 MiB，原因见下） | 每条消息 4 张 |
| 格式 | PNG / JPEG / WEBP / GIF | PNG / JPEG / WEBP |
| 超出时 | 从最早的图片开始压缩 | 每张都压缩到规格以内 |

**（实施记录）为什么改成 6 MiB：** 实施时发现，旧的共享订单把整段对话发了两遍（`chat` 一遍，`prompt` 又一遍）。新版共享方会从 `chat` 重建 prompt，所以 v2 订单的 `prompt` 只发占位符 `[chat]`；再留出加密封装和文字内容的余量，单张和合计都定为 6 MiB。原计划的说明如下。

**合计为什么是 8 MiB：** 8 MiB 原始字节经 base64 编码约 10.7 MiB，再加密封装约 14.2 MiB，必须低于 16 MiB 的共享请求上限（`llm_package/safety.py:18` 的 `MAX_REQUEST`）。这个上限在使用方 `routes.py:3179`、共享方 `routes.py:501` 都会检查。

**实现前必须实测：** 用两个节点，一次请求带 8 MiB 图片，分别走局域网直连（p2p）和中继（relay）。如果有任何一跳的上限更低（例如确认 `peer_http.py:47` 的 2 MiB `MAX_JSON_BYTES` 是否作用在这条路径上），就停下来，把合计上限改成实测能通过的最大值，并写进验收文档。

### 3.8 压缩规则（只在超出共享线路规格时使用）

- 复用 `input_attachments._image` 的做法：长边依次缩到 2048、1600、1280、1024、768 像素，JPEG 质量依次用 82、68、50、35，直到满足当前线路的单张上限；GIF 取第一帧。
- 每压缩一次，记录一条事件 `images_resized_for_shared_route`，内容包括张数、原始大小、压缩后大小、线路版本。
- 界面提示：“共享模型的线路较旧，图片已压缩后发送，细小文字可能看不清。共享方升级 Ryn 后可以传原图。”
- 这里有意改变现有的“不做任何隐式转码”原则：压缩会明确告诉用户，而且只发生在共享线路超出规格时；本机线路不受影响。

### 3.9 新旧版本搭配

| 使用方 | 共享方 | 结果 |
|---|---|---|
| 新 | 新 | 和本机一样，传原图 |
| 新 | 旧 | 能看图；超过 128 KiB 的图片压缩后发送，并提示 |
| 旧 | 任意 | 和现在一样不能看图。使用方必须升级 |

---

## 4. 修改步骤

### 阶段 0：主机核对（不改代码）

**S0**
1. 在主机运行检查脚本，结果写进验收文档：
   - 每个模型的线路（local / peer / provider）、能不能看图、原因代码；
   - 插件工具里有多少被当成写操作；
   - 锁死、假转圈、图片导致失败、已取消、超过上限的任务各有几个；
   - 日志里各错误码出现的次数。
2. 复现 2.4：从 Excel 复制单元格，粘贴进输入框。
3. 记录主机当前安装的是哪一个安装包。
4. 检查公司插件的数据策略是否为 `allowlist`（如果是，共享模型会报 `agent_extension_model_not_allowed`），以及共享模型的发现记录里有没有 `stream-v1`。

### 阶段 1：对话不再报废

**S1 结果不确定不再锁住对话**（`control.py`、`state.py`、`runtime.py`，前端 `RynAgent.tsx`、`rynAgentText.ts`）

1. 状态值不变，原因见 3.2。
2. **核对加时限。** `reconciliation` 进入 `pending` 时写 `requested_at`，进入 `running` 时写 `started_at`。新增 `control.reconciliation_active(row, now)`：`pending` 未超过 60 秒、或 `running` 未超过 120 秒时为真。没有时间戳的旧数据，一律当作已过期。
3. **放开权限**（`allowed_actions`）：`ambiguous` 为真、且核对不在进行中时：
   - `information`、`steer` 可用；
   - `model` 可用（去掉“没有 in_flight”这个条件，换模型不执行任何操作）；
   - `archive`、`delete` 可用，前端删除时确认：“这个任务有一步结果未确认，删除后无法再核对”；
   - `resume`、`edit_message` 仍不可用；
   - 核对进行中时，以上都不可用，原因 `agent_reconciliation_running`。
4. **发消息就等于确认。** `add_message` 在同一个事务里完成：
   - 如果历史里还没有这次调用（`in_flight.call_id`）的结果，补一条错误结果：`{"error":"agent_write_outcome_unknown","outcome":"unknown","not_executed":false,"note":"..."}`。`note` 用英文说明：这一步开始执行后被中断（主人停止、重启、超时或回复丢失），可能全部、部分执行或没执行；重做前先用只读工具检查，不要假定它失败了；
   - 用 `close_queue` 关闭后面排队的调用，清空 `in_flight` 和 `approved`；
   - 操作记录、验证记录保持“未知”；
   - 写入 `row["acknowledged_unknown"]`（`operation_id`、时间、`message_id`），发出事件 `unknown_outcome_acknowledged`；
   - 然后按正常新一轮处理：`error` 清空，`status` 设为 `queued`；
   - **同一个 `operation_id` 不得重新入队。**
5. **显示状态：** `public_task` 生成 `run_state` 时，只有 `reconciliation_active` 为真才返回 `reconciling`，否则返回 `paused`。时间判断不放进 `state.synchronize`。
6. **前端横幅改为：**“上一步（{工具名}）执行中被中断，可能已经部分完成。直接发消息就能继续，Agent 会先检查实际情况，不会自动重做。”删掉“请新建任务”。
7. **旧任务：** 不迁移数据，第 2、3、5 条在读取时生效，旧任务升级后就能继续。
8. **有意修改的旧测试：**
   - `RynAgent.test.tsx` 里的 “never offers resume when a dispatched write has an unknown outcome”：恢复按钮仍不出现，发送按钮改为可用；
   - `tests/` 下断言 `agent_operation_unresolved` 或 `information` 不可用的用例：逐个列进验收文档再修改；
   - 所有断言“不重新执行”的测试一律保留。
9. **新增测试** `tests/test_agent_unknown_outcome_continue.py`：
   - 核对结束后可以发送、换模型、删除，但不能恢复；
   - 发消息后补的结果只有一条，假工具的执行计数不变，模型收到了那段说明；
   - 核对开始未满 60 秒时不能发，满 60 秒后可以；
   - 旧代码写出的锁死存档，升级后可以继续；
   - 停止一个正在执行的写操作后，可以继续，且该操作没有被重新执行。

提交信息：`fix(agent): let the owner continue after an unconfirmed step`

**S2 插件报错按普通工具错误处理**

- **2a（必做）。** `extensions/adapter.py:1457`：写工具返回 `isError` 时：
  - 操作记录写成 `confirmed`，结果里保留 `is_error`。现有的重放检查（`adapter.py:1158`、`:1184`）会把“confirmed 但 is_error”当作不重发，正好符合要求；先确认 `reconcile` 对这种记录的处理，对不上就停；
  - 把错误像读工具出错一样返回给模型，并附英文说明：工具报告了错误，但不能保证完全没有副作用，重试前先检查；
  - 任务继续执行；
  - 发出后传输中断、超时，仍按“结果不确定”处理，由 S1 兜底。
  - 测试：把 `tests/test_agent_extensions_runtime.py` 里 “write isError raises agent_write_outcome_unknown” 改成：返回错误结果、任务继续、执行计数为 1、同一个 `operation_id` 重放时不重发。“传输中断 → 结果不确定”的测试保留。
- **2b。** 插件服务器有 `server_hint_candidates`（声明只读但还没生效的工具）时，在 Agent 页顶部显示一次卡片：“「{插件名}」有 {N} 个工具声明为只读（只查询、不修改），允许它们直接运行吗？[允许] [以后再说]”。点“允许”调用现有的 `apply-server-hints`。不自动信任，保留 10-21 设计里“主人确认一次”的原则。每个服务器的每个发现版本只显示一次。
  - 测试：前端组件测试，点“允许”后提交的工具名和 schema 哈希都正确；同一个发现版本关闭后不再显示。

提交信息：`fix(agent): treat plugin tool errors as ordinary tool errors` / `feat(agent): one-time read-only approval card for plugin tools`

**S3 已取消可以继续，删除和归档始终可用**

- `control.py:76`：没有进行中操作的 `cancelled` 任务，允许 `information` 和 `steer`。`add_message` 把 `cancelled` 当作已结束，开始新一轮。
- `control.py:85` 的 `safe_lifecycle`：除“正在运行”外都允许删除、归档；结果不确定的任务按 S1 处理。
- 测试：取消后发消息能开始新一轮，旧的排队调用不会复活；已取消、已失败、已中断的任务都能删除。

提交信息：`fix(agent): allow continuing and deleting stopped conversations`

**S4 停止有时限，界面只显示真实状态**

- **停止 5 秒内生效。** 按停止时如果有操作正在执行，最多等 5 秒：
  - 本地命令：结束整个进程树（使用现有的 `recover_operation`）；
  - 插件调用：放弃等待，之后迟到的结果只写进操作记录，不进入对话；
  - 仍未确认的，按“结果不确定”结束，然后由 S1 处理。
  - 先确认：工作线程卡在插件调用上时，能不能在不结束线程的情况下放弃等待。做不到就停，写进“待确认”。
- **只显示真实状态。** 侧栏转圈、本轮“运行中”计时、0.5 秒快速刷新，只在 `running`、`pausing`（不超过 5 秒）、`reconciling`（`reconciliation_active` 为真）时出现。中断的那一轮显示“已中断 · N 秒”。
- 测试：
  - 前端：中断 + 结果不确定 + 核对已结束时，没有转圈、没有“运行中”、没有快速刷新；
  - 后台：按停止 5 秒后，任务可以发消息。

提交信息：`fix(agent): bound stop time and show only real running states`

**S5 网络抖动只影响那一个请求**（`RynAgent.tsx`）

- 把 `uncertain`、`controlUncertain` 从全局标记改成按对话记录：键是任务 ID；新建对话还没有任务 ID，就用草稿键。只禁止**那一个**对话的发送，其它对话照常。
- **自动核对：** 出现不确定后，每 2 秒查一次。
  - 新建对话：在任务列表里找相同的 `client_request_id`；
  - 控制操作：用同一个 `client_request_id` 重发（后台会去重，见 2.9）。
- 30 秒内找到或重发成功：自动解除，不需要点“恢复”。
- 30 秒后仍不确定：
  - 新建对话：用同一个 `client_request_id` 自动重发一次；还不行，就显示“没有确认送达”，草稿和附件恢复到输入框，旁边有“重新发送”按钮；
  - 控制操作：显示当前真实状态，旁边有“再试一次”按钮。
- 启动时读到旧版本存下的全局标记：直接转成上面这种按对话记录的状态，并立即开始自动核对。
- 测试：
  - 模拟新建请求网络失败，其它对话仍能发送；
  - 后台其实已经收到时，2 秒内自动解除；
  - 后台没收到时，30 秒后自动重发一次，任务只被创建一个。

提交信息：`fix(agent): resolve uncertain sends automatically and per conversation`

### 阶段 2：模型与图片

**S6 能不能看图，改为按模型判断**

1. `model_settings.FIELDS` 新增 `image_input`，可选 `auto`（默认）、`supported`、`unsupported`。在“模型来源”页的模型设置里显示，旁边写出当前判断结果和原因。
2. “测试连接”增加图片测试（规则见 3.6）。`model_settings.public` 的 `evidence` 要加上 `image`，否则测试结果传不到判断函数（`_inspect_explicit_model_image_metadata` 本来就会读 `evidence.image`）。
3. 判断顺序按 3.5。新增内置表 `KNOWN_VENDOR_IMAGE_INPUT`（模型名前缀）。每一项都必须先在主机上用图片测试确认才能加入，优先确认 deepseek-flash 和主人在用的 GPT 型号。
4. `local_service` 线路按 3.4 末尾的规则处理。
5. 测试：判断矩阵，覆盖线路（本机 API、本机服务）× 设置（自动、支持、不支持）× 图片测试结果（无、支持、不支持）。

提交信息：`fix(models): decide image input per model, not per route`

**S7 共享模型和本机一致**

- **S7a 使用方按共享方的声明判断。** 按 3.4 修改 peer 分支，`runtime.py:659` 不再一刀切拒绝共享线路。`ryn_image_inspect`（`runtime.py` 中的 `inspect_image`）、`materialize_operation_images` 也走同一个判断。
  - 修改 `tests/test_agent_image_routes.py:340`–`348`：没有标记时为“未知、允许发送、`peer_inline_v1`”；`image-input` 时能看图；`image-unsupported` 时不能。
  - 新增测试：共享线路的任务带图片附件，从 runtime 到 `execute_order` 全程不被拦截，收到的图片和原图一致。
  - 只需要使用方升级就生效。
  - 提交信息：`fix(models): judge shared-model image input from the provider's declaration`
- **S7b v1 线路自动压缩。** `bridge.py:206` 原来报 `peer_expanded_image_carrier_unsupported` 的地方，改为按 3.8 压缩后发送，并记录事件。
  - 修改 `tests/test_agent_image_carrier_profile.py` 中“大图被拒、0 次 execute_order”的测试：改为压缩到 ≤ 128 KiB 后发送，有压缩事件，`execute_order` 调用 1 次。
  - 新增测试：GIF 转成第一帧发送。
  - 提交信息：`fix(agent): compress oversized images for older shared models instead of failing`
- **S7c 共享方发布能力。** 按 3.3 实现。图片判断结果变化时要重新发布：确认 `model_settings.fingerprint` 会因 `image_input` 设置或 `evidence.image` 变化而变化，不会就把它们加进指纹。
  - 测试：三种判断结果下，发布的能力标记分别正确。
  - 提交信息：`feat(llm): advertise per-model image input to peers`
- **S7d v2 线路。**
  - 使用方：共享方支持 `inline-v2` 时，在 `bridge.py` 的 peer 分支用 v2 规格调用 `prepare_internal_image_profile` 做本地检查，然后在订单正文里（不是 `chat` 里，因为 `validate_chat` 不接受未知字段）加 `"image_carrier": "inline-v2"`；
  - 共享方：`ProviderService.handle`（`routes.py:507`）和 `handle_stream` 收到 `inline-v2` 时，用自己的本机线路调用 `prepare_internal_image_profile`（规格按 3.7），然后在 `internal_image_scope(profile)` 中调用 `validate_chat`。这样共享方检查图片，和本机用的是同一套代码。
  - 先按 3.7 做两节点实测。
  - 测试：3 张 2 MiB 的 PNG 原样到达共享方的 `adapter.chat`，字节完全一致；合计超过 8 MiB 时最早的图片被压缩；发给旧版本共享方时不带 v2 标记。
  - 提交信息：`feat(llm): full-size image carrier for shared models`
- **S7e 拒收后自动恢复。** 以下情况统一映射为 `agent_model_image_rejected`：
  - 模型服务商返回 4xx，表明不接受图片；
  - 旧版本共享方返回 `images must be bounded inline data URLs`、`image size limit exceeded` 或 `image count limit exceeded`。

  处理方式：本次请求去掉图片重试一次；记录事件 `images_omitted`；把这个模型在本机记为“不能看图”，24 小时后、或共享方的标记变化时失效。记录位置用 `model_profiles` 或模型目录缓存，以现有结构为准，找不到就停。
  - 测试：拒收后任务不失败；下一轮不会再先失败一次。
  - 提交信息：`fix(agent): recover when a model or older peer rejects images`

**S8 历史图片按当前模型调整**

- 新增 `image_capabilities.fit_images_to_route(messages, capability)`，在 `runtime.py:659` 和 `bridge.py:185` 之前调用：
  - 当前线路不能看图：把每个图片部分替换成英文说明：“[An image attachment was not sent: the current model cannot read images. Its file name and local path are listed in this message.]” 附件的名称和本地路径本来就写在消息正文里，Agent 仍然可以用工具处理原文件；
  - 共享线路超出规格：按 S7b / S7d 压缩，不替换；
  - 被替换的图片属于最新一条主人消息时，发出事件 `images_omitted`。前端显示：“当前模型看不到图片，只把文件路径告诉了 Agent。”
- 原有的检查保留，作为最后一道防线。改完后，它们不应再被主人消息或历史中的图片触发。
- 测试：
  - 本机能看图、本机不能看图、未知、共享 v1、共享 v2 五种线路，历史里带图片时都能成功发出请求；
  - 换模型后，历史图片不再导致失败。

提交信息：`fix(agent): adapt history images to the selected model`

**S9 输入框**

- `AgentModel` 类型加上 `supports_image_input`、`image_support`、`image_transport`、`image_capability_reason`。
- **模型选择器：**
  - 能看图的模型显示图片图标，“未知”显示灰色图标；
  - 共享来的模型，名字下方显示“共享自 {设备名或提供方名}”。
- **附图提示：** 所选模型被判断为“不能看图”、且附了图片时，在附件下方提示“{模型名} 看不到图片内容，发送后 Agent 只能拿到文件路径”，并提供“换模型”按钮。只提示，不禁止发送；“未知”时不提示。
- **粘贴：**
  - 剪贴板里的文件全是图片、同时有非空的 `text/plain`（Excel、WPS、Word、网页表格都是这种情况）时，正常粘贴文字，不作为附件；
  - 只有图片（截图）时，照旧作为附件；
  - 有非图片文件时，照旧作为附件；
  - 粘贴的文字超过 20000 字时，存成 `粘贴的表格.tsv` 附件。
- **共享方的“模型来源”页：** 显示“图片输入：支持 / 不支持 / 未知”，提供“测试图片”按钮，并注明“共享出去的模型按这里的结果对外声明”。
- **测试：**
  - 模拟 Excel 剪贴板粘贴，结果是文字；截图粘贴，结果是附件；
  - 不能看图的模型加图片附件时出现提示；
  - 共享模型显示“共享自”。

提交信息：`feat(agent): show image support, sharing source, and paste tables as text`

### 阶段 3：插件能力

**S10 插件工具列表保持最新**（本节对应另一会话 2026-11-09 提出、尚待批准的 A–D 方案。那边如果已经开工，这一步就不在这里重复做，改为引用那边的结果）

- **A 实时刷新。** Ryn 启动时，以及每轮对话开始前（同一个插件服务器 60 秒内最多刷新一次），对已连接的插件服务器重新调用 `tools/list`，更新 `discovered_tools`；收到 `notifications/tools/list_changed` 时也立即刷新。
  - 刷新失败时保留旧列表，并标记“可能不是最新”。
  - 注意：刷新会让 `discovery_revision` 变化。只读声明（`server_hint`）按现有规则处理：schema 不变就保留（`extensions/store.py:2446`–`2453`）。
- **B “暂时不可用”不等于“不支持”。**
  - 不可用的插件服务器不再被静默去掉，在工具目录里列出名字和状态，例如“需要登录”“离线”；
  - 系统提示加一条规则：插件状态为不可用时，要告诉主人插件需要处理（例如“请先登录 Dashboard”），不能说“不支持”。
- **C 插件目录带说明。** `ryn_list_plugins` 和插件目录里，加上插件说明和技能说明。公司 Dashboard 插件的说明要写明支持“企业微信审批查询”，这一处改在公司插件仓库，见 R4。
- **D 测试：**
  - 插件服务器新增一个工具后，下一轮对话就能用；
  - 收到 `list_changed` 后立即可用；
  - 插件需要登录时，Agent 的回复提示去登录，而不是说不支持；
  - 用 `tool_menu.py` 的 BM25 检索，对一组中文问题（含“企业微信 审批”“流程”）验证能找到正确的工具。

提交信息：`fix(agent): keep plugin tool catalogs live and report unavailable plugins`

### 阶段 4：长对话

**S11 长对话不再被拒绝**

- **合并流式事件。** 某一步的 `assistant` 事件或工具调用事件写入后，删除同一步、同一 `context_epoch` 的 `text_delta` 事件。先确认三处都能接受 `seq` 不连续：前端 `pendingAgentText`、`/tasks/{id}/events` 的游标分页、`hydrated_events`。有任何一处不能，就停。
- **压缩旧轮次。** 事件超过 700 条、或记录超过 1.5 MB 时，处理最近 10 轮以前的每一轮：
  - `tool_started`、`tool_result`、`model_started` 等过程事件，合并成一条 `turn_compacted`，内容是工具名和次数、操作总数、起止时间；
  - 主人消息、最终回复、审批决定、错误和中断事件保留；
  - `receipts` 和操作记录不动；
  - 前端显示：“较早的过程已压缩：共 N 项操作（工具 A×3，工具 B×2）”。
- **去掉硬拒绝。** `runtime.py:1782`、`:1816`、`:1944`、`:1951` 改为先压缩。压缩后仍然超限，就**自动接续**：
  - 新建一个对话，标题加“（续）”；
  - 模型、权限、工作目录、项目、技能沿用；资源授权不自动带过去；
  - 新对话的开头是原对话的摘要：有 `context_checkpoint` 就用它，没有就生成一次；
  - 主人这次发的消息放进新对话；
  - 两个对话互相关联，前端自动跳转，并显示：“原对话已很长，已在新对话中继续。查看原对话”。
- **测试：** 1200 条事件、2.5 MB 的任务发消息，结果是先压缩、必要时接续；原对话的最终回复和主人消息都还在；旧的 `operation_id` 不会进入新对话的待执行队列。

提交信息：`fix(agent): compact long conversations instead of refusing new messages`

### 阶段 5：报错说明

**S12 每个错误都有中文说明和出路**

- 前端新增 `agentErrorHelp(code)`，返回中英文说明和可用的操作：`continue`、`switch_model`、`retry`、`new_task`、`open_sources`、`sign_in_plugin`、`delete`。
- 至少覆盖以下错误码：`agent_image_input_unsupported`、`agent_model_image_rejected`、`agent_write_outcome_unknown`、`agent_task_limit`、`agent_operation_unresolved`、`agent_reconciliation_running`、`agent_context_limit`、`agent_model_unavailable`、`agent_model_connection_failed`、`agent_inference_timeout`、`agent_inference_failed`、`agent_model_busy`、`agent_model_protocol_invalid`、`agent_model_protocol_unsupported`、`agent_storage_limit`、`agent_cancelled`、`agent_node_restarted`、`agent_extension_model_not_allowed`、`agent_model_destination_unverifiable`。
- 任务出错区（`RynAgent.tsx:1161`）和发送失败提示（`mutationErrorKey`）都改为使用这张表。不认识的错误码显示“任务没有完成（错误码 xxx）”，以及“重试 / 换模型 / 新对话”三个按钮。
- 发送失败时，草稿文字和附件都保留。
- 测试：表里每个错误码都有中文说明和至少一个操作；任意未知错误码都显示三个按钮。

提交信息：`fix(agent): explain every task error with a way forward`

### 阶段 6：用测试固定下来

**S13 不变式测试**（`tests/test_agent_conversation_invariants.py`）

- 遍历以下所有组合：
  - `status`：9 种；
  - `error`：无、结果不确定、重启、图片、上限、模型不可用；
  - 进行中的操作：无、读、写未发出、写已发出；
  - 核对状态：无、进行中、已结束（结果未知）。
- 断言：只要不在运行中，`information`、`model`、`delete` 都可用（I1 的例外除外）；结果不确定时 `resume` 不可用（I3）。
- 前端补两项：I4 的显示测试，I6 的单对话锁定测试。
- 以后谁要新增一种锁定条件，必须先修改这个测试，并在测试里写明理由。

提交信息：`test(agent): pin conversation-continuity invariants`

**S14 本机 / 共享一致性测试**（`tests/test_shared_model_parity.py`）

- 准备一个假的 OpenAI 兼容服务。同一个服务分别以两种方式接入：本机来源（`local_api`），以及由测试里的共享方节点（`ProviderService`）共享出来（`peer`）。
- 每个场景在两种接入方式下各跑一次，比较两项：
  - 模型实际收到的请求：文字完全一致；图片在 v2 下字节一致，在 v1 下只允许出现 3.8 规定的压缩差别；
  - 任务最终状态。
- 场景：
  1. 纯文字；
  2. xlsx；
  3. docx；
  4. 文字版 PDF；
  5. 扫描版 PDF（调用 `ryn_image_inspect`）；
  6. 50 KB JPEG 附件；
  7. 4.8 MB 截图；
  8. GIF；
  9. 历史里有图片，再换成不能看图的模型；
  10. 共享方声明 `image-unsupported`。

提交信息：`test(agent): pin local and shared model parity`

### 阶段 7：验收与交付

**S15 端到端验收**（VM，安装版，独立 `RYNMESH_HOME`，沿用以前 `_accept` 的方法）

- 准备：
  - 假 OpenAI 兼容服务，提供两个模型：一个文本模型，一个视觉模型；
  - 假插件服务器，提供三个工具：一个返回 `isError`，一个卡住 60 秒，一个运行中会新增工具并发出 `list_changed`；
  - 两个节点，节点 A 共享模型，节点 B 使用。
- 按第 6 节的验收表逐项操作，每项截图，改动前后对比。记录写进 `docs/acceptance/agent-stability-20261109.md`。

**S16 合并、打包、装到主机**

1. 按 `AGENTS.md` 合并进 `develop`，从 `develop` 的已提交源码打包。安装包同时带上 2.13 列出的所有修复。
2. 先保留当前主机版本的安装包，作为回滚。
3. 安装后检查：程序版本、健康状态、插件服务。
4. **共享方和使用方的电脑都要装新版本。**
5. 主机验收（主人）：
   - 在共享方的“模型来源”里，对 DeepSeek flash 和 GPT 各点一次“测试图片”；
   - 在使用方重复第 6 节的场景 1–12；
   - 重新运行 S0 的脚本，和改动前对比。

---

## 5. 需要主人决定的事（括号里是推荐做法）

1. 插件工具返回错误后：（**自动继续，让 Agent 看到错误自己调整**）还是暂停等你？
2. 停止、重启、超时造成结果不确定后：（**暂停，等你发消息再继续**）还是自动继续？
3. 插件声明的只读工具：（**弹一次卡片，由你确认**）还是安装公司插件时自动信任？
4. 共享线路图片超出规格时：（**压缩后发送，并提示**）还是拒绝并提示？
5. 对话太长时：（**先压缩，仍然超限再自动接续到新对话**）。
6. 看图辅助模型（所选模型确实不能看图时，先用另一个模型把图片转成文字）：（**本次不做**）。S6、S7 完成后，DeepSeek flash 和 GPT 都能直接看图，需求会小很多。

---

## 6. 验收表

| # | 场景 | 现在 | 改完后 |
|---|---|---|---|
| 1 | 从 Excel 复制单元格，粘贴进输入框 | 变成图片 | 变成表格文字 |
| 2 | 用本机配置的 DeepSeek flash / GPT 发截图 | 视情况失败 | 能描述图片内容 |
| 3 | 用共享来的 DeepSeek flash / GPT 发截图 | 0 秒失败 | 能描述图片内容；共享方收到的图片字节一致 |
| 4 | 共享模型：让 Agent 打开 3 MB 的 PNG、扫描版 PDF 来看 | 失败 | 正常 |
| 5 | 共享方是旧版本，重复第 3、4 条 | 失败 | 正常，并提示“已压缩” |
| 6 | 选一个确实不能看图的模型发图片，再接着聊 | 当轮失败，之后每轮都失败 | 输入框先提示；Agent 拿到文件路径；后面照常 |
| 7 | 插件工具返回错误 | 对话锁死 | Agent 自己调整，继续执行 |
| 8 | Agent 运行命令时按停止 | 锁死、一直转圈、不能删 | 5 秒内停下；提示“可能已部分完成”；可以直接继续；不会自动重做 |
| 9 | Agent 写文件时强制结束 Ryn，再打开 | 同上 | 同上 |
| 10 | 点“取消”后再发消息 | 不能 | 开始新一轮 |
| 11 | 发送时断网 3 秒 | 所有对话都发不了，要手动点“恢复” | 只影响那一个对话，联网后自动恢复；不会重复创建 |
| 12 | 中心新增一个插件工具后，问 Agent 相关问题 | Agent 说“不支持” | 下一轮就能用；插件需要登录时提示去登录 |
| 13 | 1200 条事件 / 2.5 MB 的对话继续发消息 | `agent_task_limit` | 先压缩，必要时自动接续 |
| 14 | 任何报错 | 只有错误码 | 中文说明，加至少一个按钮 |
| 15 | 已锁死的旧任务（如 Caesar） | 不能操作 | 升级后可以继续、换模型、删除 |
| 16 | 写操作安全 | 不自动重做 | 仍然不自动重做（测试计数证明） |

另外：后台全量测试、前端全量测试与基线对比，不能出现新的失败。

---

## R. 关联问题（不在本分支；每项都要主人单独同意）

| # | 问题 | 已确认的原因 | 建议做法 | 涉及 |
|---|---|---|---|---|
| R1 | Dashboard 反复显示“统一登录待完成” | 中心发现刷新令牌被重复使用，就撤销整个授权；刷新时同步调用企业微信，任何报错都按“已离职”撤销；Ryn 刷新令牌超时只有 10 秒，超时后会再次使用旧令牌（2026-11-09 另一会话核实） | 中心：重复使用给一个宽限期，企业微信暂时性错误不撤销，延长员工缓存；Ryn：令牌请求超时改为 30 秒 | 线上中心服务（生产部署）+ Ryn |
| R2 | 插件页时不时显示“插件服务暂时无响应” | `ExtensionWebApp.tsx` 每 5 秒检查一次，`healthy()` 只等 2 秒，错过一次就卸载整个页面 | 连续错过 3 次才显示错误；插件提供异步的存活检查接口 | Ryn + 公司插件仓库 |
| R3 | 同事用 GPT 时报 `agent_inference_failed` | 还没有现场数据 | 先让同事运行 `artifacts/agent-inference-failed-20261106/get-task-failure.ps1` | 待定 |
| R4 | Dashboard 插件说明里没有“企业微信审批” | 插件声明文件 `plugin-declarations.json` 的说明没写 | 补上说明，配合 S10-C | 公司插件仓库 |

---

## 7. 不在本次范围

- 每个任务单独存储、去掉 24 MiB 总上限（存储方案第二阶段）。
- 编辑“结果不确定”那一步之前的消息。
- 共享线路的图片单独上传后再引用（突破 16 MiB 上限）。只有 v2 的 8 MiB 不够用时才考虑。

---

## 8. 实施记录（2026-11-09，与方案的差异）

- **S4a 停止时限：** 没有做"5 秒后强制结束未返回的操作"。原因：工具执行完成后的保存步骤（`loop.py` 的 `finish`）不检查这一步是否已被别处结束，强制结束会让迟到的结果写重复记录。现有的本地命令和插件调用本来就会在停止时中断，验收时实测停止耗时。
- **S5 网络抖动：** 没有把"不确定是否送达"改成按对话记录（那会牵动大量现有测试和恢复逻辑）。改为：每 3 秒、以及网络恢复时，自动用原请求编号重新核对（节点会去重，不会重复执行），最多 20 次后保留手动按钮。网络一恢复，其它对话就能发了。
- **S10 插件工具清单：** 和"企业微信流程查找"会话分工。本分支只做后台刷新、插件说明和"不可用不等于不支持"的规则（单独提交 `177a889`）；每回合等待核对、启动核对、list_changed、状态行、能力索引由对方在 `claude/plugin-tool-catalog` 上完成。
- **新发现并修复的两个问题（方案里没有）：**
  - `runtime.py` 用了 `math.ceil` 却没有 `import math`：长任务快用完默认时间预算时，再发消息会直接报内部错误。
  - 共享方估算上下文时，把图片的 base64 文字按"一字节一个 token"计算：一张 50 KB 的附件就算作约 6.7 万 token，128k 上下文的共享模型会以"超出上下文"拒收图片。改为每张内嵌图片按固定的 3000 token 估算。
- **S11 长对话：** 先只压缩更早的轮次；如果最近的几轮本身就很大，会把保留的完整轮数逐步减到最少 1 轮，仍然超限才接续到新对话。

