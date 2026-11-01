# Ryn 开发分支交付说明 · 2026-10-19

目标仓库：`yeogirlyun/rynmesh`（老板 Git）；同步仓库：`yyeogirl/rynmesh`。交付分支仍为 `feature/ryn-integrated-workspace`，版本标签仍为 `0.7.0`。

这批代码把首页、应用和服务页的新界面，与 Agent 自行执行工具、原生模型协议、附件、文件预览和会话控制整合到同一开发分支。用户可以选模型和项目、拖入文件、连续完成任务、停止当前回复、编辑历史提问，并查看生成的 Office/PDF 文件。本文说明本次增量；更早的整支功能、来源与限制见 [10 月 15 日完整交付](2026-10-15-upstream-delivery.md)。

本次整理前 HEAD 为 `347e25d`。远端当前分支的已交付基线为 `c74813dbe5ed01ba265dc47785f52f448e1ff876`；基线之后已有两个本地提交，下列未提交开发在其上继续整理，功能快照保存为 `6ae2017`（165 个文件，15,040 行新增、1,091 行删除）。这组行数包含测试、锁定文件和资源，不等于手写业务代码量。本次保留这些历史和当前分支，不合并其他功能分支。

## 已有两个待同步提交

| 提交 | 交付内容 |
| --- | --- |
| `0146c02` | 首页列表/拓扑、应用中心和插件筛选改版；Craft 原生自动化及系统应用注册；Agent 工具恢复、补丁、浏览器分页、项目/任务菜单、流式回复和每轮计时；中英文与验收记录。 |
| `347e25d` | 服务页拆分为独立组件与样式，整合 Claude 的服务页设计，并补齐交互、无障碍、语言和隔离测试。 |

这两个提交与本次新增源码一起推送，不把它们误写成已经在老板远端的内容。

## 一、Agent 内核与工具执行

`agent_runtime/request_context.py`、`conversation.py`、`tool_router.py`、`turn_state.py`、`model_events.py` 将上下文层次、对话投影、工具配对、回合决策和流事件拆开，接入原有 `AgentRuntime` / `AgentLoop`。模型适配器提供模型响应，Ryn 自己装配提示词、验证工具提案、执行本机操作、写回结果并决定继续或结束。

工具路由按调用 ID 保存结果。工具名、参数或权限不合法时，给对应调用返回受控错误，不把模型提供的参数变成宿主授权。完整只读调用可以在模型流式输出期间提前执行；写操作仍等待完整终态、权限检查和既有精确审批。取消、追加要求、晚到结果和重启恢复由回合状态与已有操作账本共同处理。

`stream_tools.py`、`streamed_reads.py` 保留只读提前执行的去重、顺序和凭据约束。`runtime.py`、`state.py`、`store.py` 减少无变化的复制与写盘；任务仍以加密分文件布局保存。长命令、进程回读、项目入口快照与检查摘要减少重复读取。任意 shell 仍以本机用户身份执行，项目与工具权限不构成操作系统沙箱。

第二轮独立验收曾发现“应用写入异常被误判为明确失败”和“找回收据后 consumer pending 未结算”。第三轮按原操作和原收据复验关闭，未知写入不自动重放。历史失败及描述性性能样本仍保留，见 [Agent 重写验收](../acceptance/agent-codex-rewrite-20261018/README.md)。

## 二、模型协议、CLI 与实际用量

`llm_package/protocol_codecs.py` 与 `protocol_adapter.py` 新增 `openai_responses`、`anthropic_messages`，与原有 `openai_chat` 共用内部消息/工具契约。来源协议独立于厂商名称选择；旧来源缺少协议时沿用 Chat。切换协议使旧探测和模型绑定失效，不自动猜另一个 endpoint 重发请求。

原生 SSE、工具调用、图片输入、工具结果往返和协议终态都做显式转换。Responses 的加密推理续接和 Messages 的原生私有块只保留在绑定协议、来源与模型的加密任务状态中，公开 API、前端消息与摘要不输出这些私有字段。未完成或损坏的流不会释放写操作；已收到的实际 usage 在失败或取消中仍保留。

Codex 使用原生对象协议并保留线程续用、调用方隔离和工具结果配对。Antigravity 把既有 JSON Schema 传入工具提案请求，优先检查 structured output；固定分类的纠错反馈避免把原参数或 provider 原文混入下一轮。提供方原生工具不取得 Ryn 的本机执行权限。

模型上下文容量可自动发现、缓存并更新绑定；显式手动上限及旧设置来源被保留。默认上下文随模型容量调整，未知容量保守回退。预算是 UTF-8/启发式估算与预留，不是精确 tokenizer 或严格实际费用上界。实际用量缺失保持 unknown/pending，不把估算或已知零混成实际计费。来源、公开 API、P2P 与 CLI 的计量边界分别有回归覆盖。

实现契约与限制见 [协议适配架构](../architecture/ai-protocol-adapters.md)、[上下文与业务验收](../acceptance/agent-context-business-20261018/README.md)、[Gemini 工具链验收](../acceptance/gemini-tool-fix-20261018/README.md)。Anthropic 兼容的真实验收来自 GLM/Z.ai；这不代表 Claude 模型已验收。原生 Responses 尚缺真实兼容提供商验收，Claude thinking/adaptive 参数配置也未实现。

## 三、附件、图片与会话控制

新增 `input_attachments.py`、`llm_package/cli_images.py` 及前端附件组件，支持文件选择、图片粘贴、浏览器拖放和 Tauri 本机文件拖入。附件随所属用户消息保存，恢复历史时保留对应关系；视觉输入由所选适配器能力约束，API 和 Codex 接入不能泛化为所有文本 CLI 都支持图片。

停止操作接入后端取消与执行状态。编辑历史用户消息会从该位置创建新的对话分支，明确处理后续消息及正在执行的回合，避免把编辑解释为向原运行无限追加。文件输入、模型切换、缓存更新和历史恢复都有对应前后端测试。

## 四、Office / PDF 预览

`file_preview.py` 对回执绑定的文件做有界、惰性的内容预览；本地解析 OOXML/CSV，不执行宏、公式或文档外部 URL。前端 `OfficePreview.tsx` 展示 Excel 工作表、Word 内容和 PPT 幻灯片，`PdfPreview.tsx` 使用固定版本的 `pdfjs-dist@6.4.299` 本地渲染 PDF；NAS 和 Agent 文件入口共用预览组件。增加该依赖用于浏览器直接阅读 PDF。

预览展示内容与基本结构，复杂图表、嵌入对象和原始分页可能与 Office 不同；没有缓存结果的公式不在预览中计算。旧 XLS/DOC/PPT、ODF、RTF 转换依赖节点上的 LibreOffice；不可用时给出可见提示与原文件入口。PDF 文字摘录另依赖可选 `pypdf`。详见 [附件与文件验收](../acceptance/chat-files-20261018/README.md)。

## 五、Craft 与中文兼容包

Tauri 编辑窗口延长 readiness 等待，并通过文件 drop ACK 与受控重试处理编辑器尚未准备好的情况；相应纯 Node 回归在 `webapp/src-tauri/tests/document_drop.test.mjs`。

`craft_compat.py` 为 GridCraft、WordCraft、PDFCraft 各自固定 `ryn-cjk-1` 兼容包的上游版本、归档 hash 与完整文件清单 hash。安装在原发布包旁，校验实际 payload，不以 receipt 自述代替信任；缺包沿用原版，损坏包拒绝加载。兼容归档为本地显式输入，本次不把二进制包放进 Git，也不自动获取未验证包。

`scripts/craft_gridcraft_cjk.py` 及 [GridCraft 构建记录](../acceptance/gridcraft-cjk-20261018/README.md) 提供固定源码、字体/OFL、patch、工具版本和确定性打包流程。该 builder 只重建 GridCraft；WordCraft/PDFCraft 的 hash 固定与安装验证已在代码中，完整公开重建流程仍待单独整理。GridCraft 空 `inlineStr` 导致部分公式 `#VALUE!` 的上游兼容限制仍存在，字体修复不解决该问题。

## 文档、清单和本机资料

- [本轮验证记录](2026-10-19-verification.md) 区分实际重跑、历史证据、失败和未测项。
- `2026-10-19-upstream-files.tsv` 逐文件列出相对交付基线的状态、类别、增删行与 blob SHA-256；不把日志行数当作业务代码量。
- `2026-10-19-upstream-commits.tsv` 保留基线之后的提交顺序与标题；新增整理提交的完整说明同时写入 Git commit body。
- 新验收目录仅公开审阅报告和明确选择的通用证据。报告的本机绝对链接规范化，原报告与 hash 在本机保留；文中的历史源码/安装包 hash 只对应历史快照。
- pytest 临时节点、任务正文、身份/密钥、数据库、加密状态、依赖目录、源码 ZIP、安装包、机器专用 harness 和用户原工作簿截图留在本机；未删除用户文件。

两份清单以功能提交 `6ae2017` 后、最终文档已暂存的快照生成；为避免自引用，文件清单不列两份清单本身，提交清单不列保存它自身的最后文档提交。最后文档提交仍完整保留在 Git 历史中。

## 交付和验收范围

当前整理只保存并同步开发分支，没有在本轮重新安装客户端、运行收费模型任务、替换部署、创建版本 tag 或发布 Release。此前报告中的安装、真实模型与办公任务结果保持其日期和快照边界；本轮自动化通过不能追认所有历史场景或跨平台安装验收。

只同步同名开发分支，不重写远端历史。远端上传后以实际 HEAD 与两边远端引用一致为准。
