> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`b90acd300177b764b0ff914facfc005da9b111ce8a1502c296a8fae6585853d4`。

# Ryn Agent 重写独立验收 002

**结论：FAIL，不能签署七项全部通过。** 第 1、2、3、4、5、7 条有充分证据；第 6 条的应用写入恢复存在实质缺口。常规关键回归 **240 passed，1 deselected，188.14 秒，退出 0**，但新增鉴别探针复现了常规测试未覆盖的问题。

验收日期：2026-10-18，Asia/Hong_Kong。最后失败探针时间：**15:42:45 +08:00**。这是本轮读取和运行所得的独立结论，没有继承其他验收聊天的结论。

本轮后半段，根任务在获知第一个缺陷后修改了 `node_apps/agent.py`。本报告区分初始冻结版、该局部修补版，不把旧失败冒充修补后仍存在，也不把局部复验冒充完整新快照验收。后续修复及新独立聊天的最终验收为 **PENDING**。

## 七项判定

| 条款 | 判定 | 独立核验所得证据与边界 |
| --- | --- | --- |
| 1. Ryn 自行运行分层提示词、工具调度、结果回写、继续/结束、压缩恢复 | **PASS** | 实际生产链为 `AgentRuntime` → `AgentLoop._model/_context/_compact` → 自有 request/context、router、turn-state、stream-read 模块。模型通过 bridge 返回响应；没有调用 Codex Agent 代替循环。真实接线与本轮实际 `run_once` 回归均成立。 |
| 2. 两个不同来源模型完成多轮工具任务 | **PASS** | 保存的真实 Bridge 调用、消息、工具结果及 receipts 支持 GLM `glm-5.3-flash` 的原 API 来源与 Antigravity `gemini-4-argon-low` 来源，均完成多轮任务；availability probes 未计入 Agent 成功。 |
| 3. 原 task API、工具注册、应用结果回传及已有应用端到端成功 | **PASS** | 原 routes/tools/NodeApps/store 边界保留；真实 GLM 经 task HTTP → 工具发现 → 精确批准 → NodeApps Desk 存储 → 模型最终答复完成。工单 ID 在应用 HTTP、confirmed receipt、task result 中一致。本轮另复跑脚本模型加真实 HTTP/Desk 存储测试。 |
| 4. 项目查找、文件修改验证、应用操作真实存在 | **PASS** | 真实搜索/读取文件与 marker；实际修改价格函数，shell 输出 `RYN_VERIFY_OK`、exit 0，磁盘 hash 与记录一致；真实 Desk 仅一张指定工单。不是只根据最终文字判成功。 |
| 5. 工具错误可纠正、中途追加/取消、普通长上下文继续 | **PASS** | 真实 Gemini 缺文件 → 错误回写 → 正确读取成功；240 项中包括 malformed history 压缩 5 项、流式控制 8 项、runtime control 14 项、完整压缩及恢复实际 IO 回归。应用提交异常的跨层恢复缺口单列第 6 条，不重复计分。有限窗口不能容纳全部固定数据的情形不承诺无限压缩。 |
| 6. 旧会话/设置授权保留，写入重试及恢复可靠 | **FAIL** | 旧加密会话迁移、设置/grants、同 operation 重试去重均有通过证据；但已提交应用写入曾被误报未执行。分类修补后，Agent 找回原收据仍不结算 consumer intent，后续应用写入持续被 pending gate 阻断。详见下文。未证实发生重复写入。 |
| 7. 同模型固定任务无明显完成/工具/人工退步 | **PASS，限描述性样本** | 三类固定任务最终均完成，未增加人工纠错或批准数量；搜索额外参数纠错和编辑额外读取均如实计数，时间增加也保留。成对搜索复测接近原耗时。证据不支持整体提速、统计显著性或所有任务均无退步的声明。 |

## 必须处理的实质缺陷

### F1：应用已提交，无效响应却被宣告为未执行

初始冻结源码 [node_apps/agent.py:128](../../../rynmesh/node_apps/agent.py) 仅将 `apps_response_unconfirmed` 转为 `AgentError(agent_write_outcome_unknown)`，却把 `apps_response_invalid` 和 `apps_store_unavailable` 包装为 `AgentToolRejected`。而 [service.py:337](../../../rynmesh/node_apps/service.py) 明确把这三个错误都视为应保留 pending 的不确定结果。

可达触发：使用自建双节点、真实加密 friend/app fixture，远端 Desk 实际提交工单并保存原收据，随后故意返回 malformed 密文。响应解密失败走 [service.py:244](../../../rynmesh/node_apps/service.py)。Agent 将该异常当成肯定未执行，在 [runtime.py:1810](../../../rynmesh/agent_runtime/runtime.py) 的分支写出 `not_executed=true/no_mutation=true`，清空 `in_flight`，丢失原 operation 的恢复入口。

实测同时存在：应用工单数量 **1**、provider 原收据存在；Agent receipt 却为 `not_executed`，`reconciliation=null`。`runtime.reconcile()` 不会修正该状态；直接查询原 operation 的 AppTools reconcile 能查得 `executed`。

原始失败证据：result.json（原始证据本机留存），SHA256 `019d11a0d86debb65b82f86a493659b42c24c9e12c33f5027081925e78319414`。

**本轮后续状态：此分类缺陷已由根任务局部修补。** 当前三个不确定错误统一保留 unknown。本审用同类真实提交后无效响应探针，确认可保留 `in_flight`、进入 reconciliation，并用原收据恢复为 confirmed。局部证据：result.json（原始证据本机留存）。该次曾手动重试原 operation 来验证去重，不能拿它证明 consumer pending 自动清理，因而继续运行了 F2 探针。

### F2：找回原收据后，consumer pending 未结算，应用不能继续

在 F1 分类修补版，仍可经原有 `apps_response_unconfirmed` 路径稳定触发。使用真实提交后丢失响应，不手工 `invoke/resume` 原 operation：Agent 进入 unknown，随后 `run_once` 查询原 provider 收据，Agent receipt 已变为 **confirmed**、`reconciliation.state=executed`、`in_flight=null`；但 consumer pending **仍为 1**，下一次应用操作返回 **`apps_pending_required`**。

精确原因：[AppTools.reconcile:164](../../../rynmesh/node_apps/agent.py) 只查询并验证 `_receipt`，随后返回 `executed`（本轮局部修补版约 179–194 行），没有结算 consumer 的同一 intent。[NodeApps.invoke:308](../../../rynmesh/node_apps/service.py) 会阻止同应用的新 pending 操作；当前只有成功 invoke 的 [service.py:350](../../../rynmesh/node_apps/service.py) 才写 `status=committed`。

主审亲自运行以下命令，**退出 1**，断言明确要求原收据恢复后自动清 pending 且后续应用操作可继续：

```powershell
.venv\Scripts\python.exe .codex-tmp\independent-final-002-apptools-probe.py --fault unconfirmed --skip-original-retries --expect fixed
```

脚本：independent-final-002-apptools-probe.py（原始证据本机留存），SHA256 `459a52a22169353240f09f53b2cf78beab20893e942519f8fec0639dfccf3e79`。

本次结果：result.json（原始证据本机留存），SHA256 `4ade78c2307c41911acf43b56e190b1046f666d51e5a56c062c83edb2aefc92c`。日志：required-contract.log（原始证据本机留存），SHA256 `f815aea12faa2a9d64c32b57b1f992baf3bd185b99cf7ea5f2729f211438f926`。

**最小完整修复：** 保留 F1 的三错误 unknown 分类；查询并验证原 provider receipt 后，以原 operation id、request hash、provider、app、action、arguments 匹配 consumer 的原 pending intent，原子结算该 intent。该路径只能结算原收据，不能重新派发业务写入。补测应包含“真实 commit 后异常 → 原收据恢复 → 不重执行 → pending 清零 → 后续应用操作可继续”。预览、授权、schema 及明确未执行的业务拒绝仍保持原合同。

这两处均在 dirty before 的原应用桥接中存在，**不能归因为本次 Agent 核心重写新增**；但它们直接影响写入事实、恢复和后续应用可用性，不能因是旧缺陷而将第 6 条判通过。底层去重没有失效：原 operation 重试两次仍只有一张工单，新 operation 在旧 pending 未结算时被挡住；报告没有声称已经观察到重复写入。

## 核心机制及修补核验

- [prompt.py:98](../../../rynmesh/agent_runtime/prompt.py) → [loop.py:414](../../../rynmesh/agent_runtime/loop.py) → [request_context.py](../../../rynmesh/agent_runtime/request_context.py)：基础与 host 权限分层，项目 AGENTS 为 user 语境；历史只在请求副本正规化，缺失工具输出明确标为 execution unknown，旧 checkpoint 不切断工具交换。
- [tool_router.py:94](../../../rynmesh/agent_runtime/tool_router.py) → [loop.py:288](../../../rynmesh/agent_runtime/loop.py)：未知工具/坏参数按 call 配对反馈，不重做成功 sibling；schema 执行前复验；截断提案不进入写执行。普通写仍经过现有权限、精确批准与 ledger。
- [streamed_reads.py](../../../rynmesh/agent_runtime/streamed_reads.py) 实际接入 `_model_turn`。仅可信 `PARALLEL_READS` 完成 item 可提前执行；最终响应接受后按序回写。预算不足会放弃 overlap 回普通队列；每条读取有 reservation deadline；`correction/details` 已保留。取消、steer、重启恢复不会重新执行历史读取。
- [turn_state.py:21](../../../rynmesh/agent_runtime/turn_state.py) 依据 pending tools、length、typed follow-up/end_turn 决定继续；最终提交核对 control revision。bridge 保留经校验的 bool 控制字段。
- [loop.py:441](../../../rynmesh/agent_runtime/loop.py) 选择闭合压缩边界，拒绝不完整摘要，在完整响应后原子安装 checkpoint，保留原档、owner 原文、权限、精确引用与写收据。坏 JSON 历史不再重复 parse 崩溃；只解协议 envelope，不递归业务文本；硬 reference 限制仍抛错。observer 的 `response_complete` 不再携带私有 `reasoning_content`。

固定公开 Codex 源目录 git HEAD 现场核实为 `8c076942cb70fba8e5c4732c23f1faabc4aed484`。上述关系与其 `context/base_instructions.rs`、`context_manager/normalize.rs`、`tools/parallel.rs`、`session/turn.rs`、`compact.rs` 的适用机制相符。Ryn 的归档加 checkpoint、固定 read 白名单、写等待整响应是实现差异；namespace、CodeMode、远程异步压缩及所有平台分支并未全部复制，本验收不添加该门槛，也不声称与闭源服务器逐字相同。

`MECHANISMS.md` 中后段“early read 没有单条 reservation deadline”的旧审阅段落已落后于当前代码，本报告按代码和实测判定。任意 Python 工具的硬终止、无限上下文也未被当成要求。

## 本轮运行及既有限制

本审使用 `.venv\Scripts\python.exe`，pytest 采用此前不存在的专属 `--basetemp <repo>\.codex-tmp\independent-final-002`，关闭 cacheprovider。20 组关键测试共 **240 passed / 1 deselected / 0 failure / 0 error**。包含 rewrite loop 14、坏历史压缩 5、应用 HTTP 1、stream controls 8、streamed reads 13、model events 25、router 28、turn state 15、Antigravity 错误反馈 12，以及真实控制、加密旧会话、进程恢复、权限、精确批准、上下文序列化与 checkpoint 测试。

原始 pytest.log（原始证据本机留存），SHA256 `13751973bcbe95b837df9720bb5c78337faeb20f3372f7b615fd0ed806a447d5`；JUnit（原始证据本机留存），SHA256 `3684e7f394dfbbde2df59aa52e616c4c7ce219d4c1508e8757dfed3e4df68f76`。每组数量见 results.json（原始证据本机留存）。自建探针使用新生成的隔离数据、密钥与真实 AppStore/加密传输，模型边界脚本化；没有收费推理。

明确未把 12 KB 的三个既有失败洗成绿色：本次 deselect 的 `checkpoint_refs::test_schema_cache_yields_to_exact_references_without_dropping_evidence`，以及未选入本次回归的 `long_resource_context::test_public_long_notes_all_checkpoints_to_approved_anonymous_report`、`compact_efficiency::test_real_resource_tools_reach_body_and_approved_artifact_under_pressure`。已读 before/current trace、JUnit 及 `before-compact-efficiency-013.log`；三者 before 也失败。第一项空摘要加 owner/权限/exact pins 已为 **12582 > 12000**，新版多 37 bytes；第二项后续 checkpoint 同样无法容纳固定数据。普通压缩、继续、重启、精确引用及拒绝不完整摘要均在本次通过范围。有限容量限制不否定这些机制，但不能声称全仓全绿或任何长上下文都能继续。

## 真实任务与固定比较

证据根：`<user-home>\Documents\Codex\2026-10-18\new-chat-4\work\ryn-live-acceptance`。核查了原始 result、tool messages、receipts、fixture 磁盘内容、harness 和来源/生成策略 hash，未读取用户旧对话或凭证明文。

| 固定任务 | before → after 秒 | Bridge 调用 | host 工具尝试 | 精确批准 |
| --- | ---: | ---: | ---: | ---: |
| GLM 项目查找，01/02 | 18.375 → 26.781 | 3 → 4 | 2 → 2 | 0 → 0 |
| GLM 项目查找，成对复测 04 | 18.391 → 17.453 | 3 → 3 | 2 → 2 | 0 → 0 |
| Gemini 错误路径恢复，01/03 | 52.297 → 58.500 | 3 → 3 | 3 → 3 | 0 → 0 |
| GLM 修改并 shell 验证，01/02 | 22.672 → 26.219 | 4 → 4 | 3 → 4 | 2 → 2 |

搜索 01/02 增加约 45.7% 时间，对应一次缺参数提案被拒绝后自纠；其 3 个 paired outputs 只有 2 次实际 host dispatch。编辑多一次 `verify.py` 读取；两代均两次固定批准。比较的 prompt、初始 fixture 和 4096 生成策略相同，before 源码对应原 dirty baseline，不是只取 HEAD。任务内人工纠错为 0，精确授权由 harness 显式作出，不能把“人工 0”解释为没有授权。

12 次 Agent 比较尝试 **8 pass / 4 fail 全部保留**：早期三个 after 预算失败、一次 Antigravity 无效 proposal 失败没有删除，三次独立 availability probes 没有计为 Agent 成功。修补后任务成功不能倒改旧记录，也不能把旧快照失败当作最终仍失败。Desk 另有一次 workspace 未建的前置失败、0 次模型调用，同样保留。

第 4 个真实任务 Desk：3 model / 2 tool / 23.281 秒；工单 `69f4844002894ae3ba06769d9e869d98`、标题 `RYN_LIVE_DESK_4b2a49d5aabb` 在应用 HTTP、confirmed receipt、task final 中一致。这里是 FastAPI TestClient 的真实 HTTP 路由及真实插件/存储，加真实 GLM 推理，不是部署后的浏览器 UI 测试。

真实任务跨越修补快照：Gemini batch03、GLM search batch04 的 295 个 Python 文件相对初始最终清单仅 `checkpoint_refs.py` 不同；edit batch02、search batch02、Desk 另有 `loop.py/model_events.py/streamed_reads.py/antigravity_session.py`，合计 5 处差异。本轮通过源码差分和 240 项复验承接这些修补，**没有声称所有真实任务都在本轮全部最终 hash 上重新推理**。新发现的 AppTools 缺陷则由单独实际提交故障探针证明，正常 Desk 成功不能覆盖它。

关键真实证据 SHA256：

| 文件（相对真实证据根） | SHA256 |
| --- | --- |
| `live-comparison-summary.json` | `174648aa5c43b7c680ca83739f1e09fbdaf3f76be24782c80ce44d83b96f5df8` |
| `comparison-20261018-02/edit_shell_validation-after/result.json` | `84de7cd8d6e153d6ff3be43764a5d89fff1930bd8ebc6bbc9d53a3bc5a75fb1b` |
| `comparison-20261018-03/wrong_path_recovery-after/result.json` | `f6994797deacdb4351317185510d9e5c6f2d4817d16aa9718aac22cba49c3810` |
| `comparison-20261018-04/project_search-after/result.json` | `7ce9006e926f25d190ddbd6529d5ba5c8c1791d00d881d1b69252fc6e3da9c22` |
| `desk-20261018-152219/result.json` | `ba549d96b8599fcfbc01a15f19791e92991079ec3b08a439953e3acc08e85f08` |

## 快照、归因与只读边界

初始现场 HEAD 为 `0146c0255fb056dcbb1b4939e27cd81c05073ef7`，branch 为 `feature/ryn-integrated-workspace`。34 个本次变更文件与 `rewrite-final-delta/manifest.json` 全部匹配；31 个原有 dirty/untracked 文件在原始 zip 中逐项 hash 全部匹配。没有把 `design-qa.md`、ServicesParts/ServicesView/services-refresh 的并行改动归入 Agent 重写。

本轮常规回归期间 295 个生产 Python 文件 hash 未变。F1 发现后根任务更新 `node_apps/agent.py`，故该文件的后续快照另记：

| 文件 | 240 项回归及初始冻结 hash | 最后失败探针 hash |
| --- | --- | --- |
| `node_apps/agent.py` | `ae88c7586e6338055e24e34e7d6f7b88ca91ab3320d147242a13e3e5b3804c0b` | `b60d675d3e6078561f8ac85d2fd2eb57e40707a5353c780ad9aa742c6f02f52e` |
| `node_apps/service.py` | `b876375fcfb42df7c46d624930fad06db7e2ba13670d2831c9bef08285049641` | 同左 |
| `agent_runtime/loop.py` | `3840654433ed1f8c83d91c21a737bcfc5463bb5d599520d195055f11fb0857e5` | 同左 |
| `agent_runtime/runtime.py` | `bf999bcf4fc60795efca45e776297064f7ccd4aeeebfb3ce9953ce9afc063112` | 同左 |
| `agent_runtime/checkpoint_refs.py` | `750dfdc2d3e5e884918e93ed01abd53279c60ef7eb75382bcc6582299ff15e92` | 同左 |

完整现场 hash：hashes.json（原始证据本机留存）。dirty baseline manifest SHA256 `f1f9dffaa701cd9e038b43ee1620149d1c08958f43449876170cf8fac9e25c88`；原 zip `32c50f712c584d665868ea225621b0cc9388e5b77f12fc3c991cc48f967f506f`；本轮开始时 delta manifest `43b9070a5a27854615256838602ee436a037607d5950e8126cca08a22bc673a5`、patch `88b9c807c52b247f578812cf408b399b8f1121f6186a215bb42d04ce1e7ecb6e`。

本验收及子审只新增报告、自建测试脚本、日志与隔离测试数据；没有编辑生产源码、部署、改用户模型/授权、重置/清理 Git、读取用户旧聊天或凭证明文，也未触发新收费推理。F1 后的生产修改来自根任务，已明确区分。F2 修复后应使用不手工重试原 operation 的同一探针，再由根任务按已约定的新独立聊天要求完成最终受影响条款及七项总判。
