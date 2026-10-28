# T05/T06/T07：资源范围与业务集成短契约

日期：2026-10-09。状态：只读梳理后的接口提案，与 runtime 负责人协调；尚未实现，不改变当前 G1 测试代码。依据交付计划、执行流程及现有 NAS、网盘、来源服务。当前阶段不加入整机工具、删除/搬移或生产子 Agent。

## 1. runtime 与工具边界

采用后端构造的 `ExecutionContext`：

```text
task_id, goal_revision, control_revision, scope_revision
owner_id, consumer_node_id
model_destination: {catalog_model_id, provider_peer_id, source_id}
resource_grants: [{grant_id, connection_id, connection_access_generation,
                  root_resource_ref, actions, recursive, byte_limit, allowed_formats}]
egress_grants: [{destination, origin_grant_ids, scope_revision}]
```

- owner/node 来自现有本机控制身份与节点身份，不从模型参数取得，也不凭空新增多用户账户体系。
- Context 是不可由模型改写的快照。模型不能通过普通消息或工具参数授予权限。`resource_grants` 与 `egress_grants` 真值来自本机 owner 的专用控制接口。
- `preview/execute` 保留现有参数及 `operation_id/expected_preview/cancelled`，资源工具额外接收 `context`。metadata 声明 `scope_context_supported`、`effect`、`resource_action`、出站分类；新增资源工具缺 context 必须拒绝。既有五工具可逐项迁移，不暗中把无 context 当无限范围。
- 执行前核对控制/目标/范围版本、当前业务权限及资源版本。撤权、用户更正、源重配使相关旧审批或引用失效；当前动作是否已经提交以服务端派发顺序为准。
- 只读结果不能自动扩大到写入。创建文件与目录分别授权；覆盖、删除、分享和更换模型接收方不包含在新建授权内。

## 2. 连接、目录与 AI 出站权限

现有 `NasPlugin` 具有 enabled、source.id/root、writable、allow_ai 和部分云盘 write_authorized。它们是底层必要条件，不能替代任务内授权。

- 新增稳定 `connection_access_generation`：只随连接启停、root、账户/重新授权身份、allow_ai、writable 等访问语义改变。自动刷新 access token 不改变该版本；不能用会随刷新变化的整份密文 hash 代替。
- 元数据访问和正文发送应分开授权。向模型提供文件内容必须满足 `allow_ai` 与任务出站授权；不能包装普通 `operate(..., 'read')` 绕过 `allow_ai`。
- SMB/WebDAV/Baidu 的相对路径继续复用服务校验，但还需任务目录边界。Google/OneDrive 的路径包含资源 ID，字符串以授权目录开头不能证明对象归属；须后端签发资源引用并核验 provider 祖先链/当前父目录，拒绝伪造路径指向其他对象。
- `resource_ref` 关联 connection、canonical locator、授权根、来源版本、可得的 etag/mtime/hash；模型只传引用 ID 与有限的文件名，不把任意绝对文件路径变成授权。
- 列表分页、搜索及提取标注覆盖范围、是否截断、未检查/离线/格式不支持。现有 local_search 仅有已存本地内容，不冒充跨盘全文索引。
- 读取返回前、从大结果/缓存回读时，以及每次模型出站前重新核对来源授权和当前 destination。持久 tool 历史、摘要及结果缓存也不能绕过撤权再次发送内容；需通过带 origin 的内容引用/上下文组装统一控制。已经发给模型的内容不能追撤。

## 3. 文件新建、操作记录与结果核验

建议业务适配器提供：

```text
preview(name, args, context) -> reviewed target, versions, proposed effect
execute(name, args, operation_id, expected_preview, context, cancelled) -> receipt
reconcile(operation_id, context) -> {outcome: executed|not_executed|unknown, evidence}
verify(receipt, criteria, context) -> verified checks / unmet checks / unknown
```

运行时先持久 intent，再派发。Receipt 至少包含 operation_id、origin、目标资源引用、计划内容 hash/size、provider object/upload/operation ID、实际资源版本/hash、确定结果。远端操作引用及授权上传地址可能敏感，只在内部加密记录，不能原样进入模型或日志。

同一 operation_id 的查询/恢复不能创建新副作用。目标当前同名或内容相同并不足以证明本次操作发生；需原 operation 与资源关联证据。不具备足够可查证据时保持 unknown，不能自动重写。回读必须核对真实对象、路径/父目录、内容 hash、必需产物与原资料未修改；模型“写好了”不是证据。

可复用的连接器保证有所不同：

| 后端 | 已有基础 | 补齐边界 |
| --- | --- | --- |
| SMB | 独占临时文件，完整写入后 rename，目标存在则失败 | 持久临时/目标与 operation 关联，重启核对，业务回执 |
| WebDAV | PUT `If-None-Match: *`，MKCOL | 服务端行为核验、未知结果查询、版本与内容校验 |
| OneDrive | 冲突失败策略、上传会话 | 保存内部 upload/object 引用及状态，处理完成后回执丢失 |
| Baidu | 列表预查、上传块校验、rtype 冲突策略 | 核实具体服务端冲突/恢复保证，不能仅凭客户端预查宣称原子性 |
| Google | 列表预查、创建后真实对象 ID | Google 允许同名对象，现有 list→create 不具跨客户端同名原子唯一性；需预分配对象 ID/可查询 operation 标记等专门方案与验收，仍要明确并发同名限制 |

第一阶段只承诺已验证的文本/Markdown 新文件、目录与必要提取。DOCX/XLSX/OCR 不能因“已有 PDF 提取器”自动变成支持；Windows 子进程提取仍缺内存硬上限，需要如实限定格式/大小及失败行为。

## 4. AI 来源草稿与凭据引用

- 安全表单在本机 owner 控制接口创建 opaque `credential_ref`，绑定 owner/task/source或draft/有效期。真实密钥进入现有加密凭据服务或专用加密草稿仓库；模型只见 has_credential 与引用状态。不得输出密钥、上游 raw 异常或认证头。
- `SourceDraft` 保存 draft_id、draft_version、expected_source_revision、可审阅字段、credential_ref、测试证据 fingerprint；不在公开 Task 内保存真实凭据。新增来源默认不分享。
- 测试使用精确草稿，不改变当前生效来源。fingerprint 绑定连接/协议/模型/参数/凭据引用版本；测试后编辑使旧证据失效。普通聊天、模型存在、结构化工具能力分开记录。
- 提交在业务锁内 CAS 检查 source_revision、draft_version、授权与测试 fingerprint，复用 SourceStore 保存与发布。来源测试和提交之间的并发修改不得被覆盖。
- 保存前测试失败不改原有效配置。保存后复测失败必须展示已保存的当前状态；恢复是另一个有版本与授权的动作，不能无条件回滚覆盖并发修改。
- 现有 source PUT/prepare/save 缺完整草稿生命周期与 CAS，不能直接作为 Agent 的“先测后存事务”。密钥引用也不能成为访问别的来源或换数据接收方的通行证。

## 5. 后续 store/routes 分工

以下是拟议接口语义，路径由 runtime/routes 负责人最终定稿，不是当前可调用 API：

- Task 增加 scope 与 revision、resource/egress grants 及内容 origin 引用。权限更新用 owner-only scope GET/PUT，绑定 expected_control_revision、expected_scope_revision；普通 messages 不承载权限授予。
- owner 资源选择/resolve 接口签发受控引用，正文与产物读取接口再次验证任务范围及最新连接权限。只给模型与 UI 展示可用的 artifact_id、来源、状态和打开链接；内部 locator/上传凭据按需隐藏。
- Source draft/credential-ref/test/commit 独立 owner 控制接口；模型工具只能使用已授权的草稿和凭据引用，不能收明文密钥。
- Runtime 持久 operation/receipt/outcome，集成层提供业务查询与核验。大结果独立加密保存；T10 负责归档/删除/容量。T05 的撤权及出站控制不能等到 T10 才补。
- 后续联合测试覆盖：伪造目录前缀与对象 ID、allow_ai 关闭、运行中撤权、缓存/历史绕过、同名冲突、远端写成功但回执丢失、旧审批、草稿测试后编辑、并发来源更新、来源复测失败。

本提案与 runtime 已协调的 ExecutionContext 边界一致；正式字段迁移与实现待 G1 放行后按 T02/T05 负责人统一冻结。本文件未使用 heldout 变体进行工具或提示词调参。

## 6. 2026-10-09 首批实现范围

`agent_runtime/resources.py` 与 `RynTools` 已实现 `prepare_scope`、`scope_catalog`、`validate_context` 及三个只读工具 `ryn_list_resource_roots`、`ryn_list_resources`、`ryn_read_resource`。Runtime 负责 owner-only scope 事务、可信 context 注入、每次模型/摘要请求前校验，以及 scope 改变后的历史隔离；这些接线需要联合验收，不能仅据工具单测宣称完整 T05 已交付。

- 只支持 SMB、WebDAV、Baidu 的路径式连接；Google/OneDrive 暂拒绝 Agent 资料授权，直到真实对象祖先校验完成。现有手动 NAS 功能不变。
- 文件必须经现有 `ai` 读取路径，要求底层 `allow_ai`。仅 UTF-8 文本扩展名白名单，最大读取 256 KiB，输出最多 2000 字符并明确截断。目录输出最多首 20 项，明确分页未覆盖；尚无连续分页/offset，因此不是完整 T06/F08 验收。
- 资源引用复用现有 NAS 加密密钥，带随机 grant_id、owner/node 与相对定位；子项另绑定 task_id。根引用由每次 owner scope 准备产生的独立随机 grant 绑定任务，runtime 不得接收客户端提交的 canonical grants 或将别的任务 grants 复制给新任务。两个独立授权任务互换根/子引用均被拒绝。
- NAS 启停、权限变更、云盘重新授权会更新持久访问 generation；普通 OAuth token 刷新不改变 generation。恢复原布尔权限也不会复活旧 grant。
- 读取前后复核连接权限与目的地；资源正文不进入普通 operation 缓存。文件名和正文作为不可信数据返回，不承担权限或控制指令。未知格式、超限、非法编码、越界和撤权均拒绝。
- focused 测试使用真实 NasPlugin、真实加密与 Agent 工具层，加上受控连接器；未读取用户文件，尚未宣称真实 NAS/云盘端到端或物理双节点数据隔离通过。

## 7. T06 第二批：可继续的有界读取

目录工具现每次最多 10 项，返回 `next_cursor`，调用方必须携带同一 `resource_ref` 继续，直到 cursor 为空。目录为实时 provider 分页，并非全目录原子快照；同一 provider 页内续读重新查询并校验完整页指纹，发生变化拒绝，避免偏移默默漏项。Provider 分页 token 被封装在加密 cursor 内，长度超出安全上限明确拒绝，不输出原始 token。

文本仍最多读取 256 KiB，每次输出 2000 字符、字符偏移、全文字符数、内容 SHA256 与下一 cursor。后续分片重新执行授权读取、核对全文 hash，文件变化时拒绝继续。Cursor 绑定 task_id、scope_revision、grant_id、connection generation 和原资源引用指纹；模型不能任填 offset 或另一个路径，scope 改变、跨任务或换 ref 后不能复用。所有分片继续标记不可信来源。

此批保持既有 canonical scope/grant 结构，不改变 runtime 的 scope 持久格式。目录/正文分页只解决可继续读取；Google/OneDrive 祖先核验、有界 PDF 提取及真实资料端到端仍待后续批次。

## 8. T06 第三批与出站代际补强

Google/OneDrive 已加入只读祖先检查。每个 ID 路径段须由当前 provider metadata 证明直接父对象和相同 drive；读取前后重新校验，Google shortcut、OneDrive remoteItem/deleted 及跨 drive 均拒绝。首批只支持账户自己的根盘，Google shared drives 不冒充支持。OneDrive 下一页 URL 还限定为同一个目录的 children endpoint。依据官方 [Google File 字段](https://developers.google.com/workspace/drive/api/reference/rest/v3/files) 与 [Microsoft itemReference](https://learn.microsoft.com/en-us/graph/api/resources/itemreference?view=graph-rest-1.0)。

资料正文及列表结果带可信 origin。列表额外保存可见子项 ID/版本的加密 `listing_snapshot`，防止“只列出、尚未读取的 child 移出授权树，但父目录版本没变”通过旧历史继续出站。Runtime 私有 message.resource_origin 及跨摘要保留的当前 epoch origins 在每次模型/摘要请求前复核；取消回调使用 cheap_only 检查本地权限/代际，避免高频远端请求。已发出的数据无法追撤，跨多次 provider 请求的网络级 ABA 不宣称强事务保证。

PDF 复用现有隔离子进程提取器：输入上限 8 MiB、提取上限 100000 字符、10 秒截止时间，按 2000 字符分片；临时文件只含本次授权字节并在完成/失败后清理。权限在提取返回后再次检查。无 OCR/Office 支持，扫描 PDF 或无法提取的布局不宣称已完整理解；Windows 不能保证原提取器的硬内存上限，仍受输入、输出和时间限制。`document_byte_limit` 是新增 canonical grant 字段，旧 grant 不自动扩大到 PDF。普通 UTF-8 仍限 256 KiB。

模型目的地新增非秘密 `destination_revision`。SourceStore 在 endpoint、credential、模型/参数或启停改变时持久生成新随机代际，分享和状态探测不更换代际；A→B→A 不复活旧授权。Bridge 接收 `expected_destination`，在真实发送前把 current record、代际和所选 immutable provider adapter 的来源快照绑定，避免仅较早 catalog 检查的竞态。

目前只有 SourceStore 管理的 API/本地兼容来源具备此绑定。Native/CLI/peer 普通推理保留，但资料出站暂拒绝；这是安全过渡，**不是 T09/T15 完成**。跨节点还需提供节点签名/认证的 source/model 配置代际和 peer 权限 revision，以及撤权不复活、物理双节点隔离验收。上述云盘与 PDF 已通过受控 transport/真实本地服务 focused 回归，尚未宣称真实云盘账户与真实模型联合验收完成。

## 9. T06 首批新建与持久业务回执

`ryn_create_text_file(directory_ref, name, content)` 只新建 UTF-8 `.txt`/`.md`（最多 16 KiB），`ryn_create_directory(directory_ref, name)` 只新建一个目录。两者只支持 SMB/WebDAV，要求既有 NAS `allow_ai` 和 `writable`，以及 owner task scope 显式授予 `create_file`/`create_directory`。未授予的动作不会随工具上线自动扩大。目录由已授权 opaque reference 定位，name 只允许单个路径段，不支持覆盖、删除、重命名、云盘写入或任意主机文件操作。

模型提议必须经过 runtime 写入权限与审批；预览含确切父引用、名称、内容、字节数、hash、scope/connection 代际。执行前重新核验。源配置锁与既有 source gate 保持代际直到一次有超时限制的业务请求结束，因此权限修改可能等候该次请求；已发送操作不能因随后暂停/撤权而追撤。SMB 沿用独占临时文件加拒绝覆盖的 rename，WebDAV 沿用条件 PUT `If-None-Match: *` 和 MKCOL，不改变手动 NAS 行为。

业务 operation ledger 位于节点 `agent/business-operations.json`，复用节点 `messaging.x25519` 加密，单独于 Agent 会话事务。它按精确 task/owner/node/goal/scope/arguments SHA256 与 operation ID 绑定，写入前持久 intent/dispatched，收到明确成功确认后先持久 confirmed receipt，再返回 runtime。Agent 回执事务前中断，可重启查询原业务回执；不会为了恢复发第二次写入。上限 5000 项、明文编码 16 MiB，超限在新派发前拒绝；归档策略另属 T10。

每个未结束操作还预留 4096 字节成功回执容量；所有操作的事务均计入全部预留，不能由后来的 intent 耗尽先前写入的回执空间。新建结果在派发前限制为 3072 字节，另留状态/时间字段余量。磁盘 I/O 本身失败仍可能发生，按 unknown 保守处理。

远端提交后断连、或业务确认后 ledger 未能成功落盘，仍保持 unknown；相同文件名、hash、当前存在状态以及测试 fixture 的审计日志都不能代替原操作回执。不同 task/goal/scope/参数不能查询为本次成功。精确原操作的确定拒绝也有 not_executed 回执。分享工具目前仍仅有进程内 operation cache，不能据此宣称分享的跨重启业务查询闭环。

注册机械核验对新文件进行有界真实字节回读并与批准内容比较，对目录重新列父目录确认目标。当前状态匹配与原操作归因分开报告；完整目标仍需独立覆盖评估。已通过隔离 loopback WebDAV 的真实 HTTP 创建、同名冲突、回读、权限/预览变化、精确查询、重启，以及提交后断连/回执落盘故障测试。尚未据此宣称真实外部 SMB/WebDAV 服务、真实模型资料到产物或完整 R03/30 项验收全部通过。

## 10. 只读事实核验的范围

六个只读业务工具注册 `effect=read` 的事实核验，仅从 confirmed 的真实执行回执建立判据。目录/正文以 `resource_ref` 和 `cursor` 区分各页；其他工具核验最后一次同类查询。核验重新经过当前权限执行真实读取，绕开普通操作缓存，返回 `checked_facts`、`coverage`、`uncertainties`。目录和服务目录检查配置/显式授权，不冒充已测模型推理、吞吐、远端主机或端到端访问。

文本只证明原片段的版本、范围与正文仍匹配，不证明完整文档或语义已覆盖；提取截断和未读后续片段仍明确。目录只核对本页已经返回的条目仍存在且元数据匹配，允许合法新增产物，但明确不能据此证明当前目录全部内容、递归搜索或原子快照。设备 presence 未知但稳定重读一致时，只验证“缺少连接证据”这一分类事实，不能证明连通或离线；self 的进程存活与真正 HTTP/LAN 探测分开，未执行探测不能变成可达证据。任务完整目标仍需要覆盖这些明确边界的独立语义评估，不能用一个无关事实或写入成功代替剩余目标。
