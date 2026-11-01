> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`1a296a7c99397bb08b34980193d546a657c75edf9a29fb65115065d8255ee635`。

# Ryn Agent mechanism comparison

本页是开发过程中的只读机制对照，不是最终独立验收。参照固定公开源码
`openai/codex@8c076942cb70fba8e5c4732c23f1faabc4aed484`；Ryn 对照的是当前工作树，
不是声称这份公开源码与已安装 Codex 二进制逐字对应。Ryn 实现并执行自己的 Agent，
模型来源仍是适配器；没有把 Codex 进程作为 Ryn 的执行内核。

## 已接线机制及来源

| 机制 | 固定公开 Codex 依据 | Ryn 当前实际路径 | 本轮变化与边界 |
| --- | --- | --- | --- |
| 基础指令与分层上下文 | [session/mod.rs:832–836](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/session/mod.rs#L832-L836) 选择配置、历史、模型基础；[base_instructions.rs:41–59](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/context/base_instructions.rs#L41-L59) 基础使用 developer；项目规则等上下文独立分组 | `prompt.instruction_layers` → `loop._context` → `request_context.build_context` → `provider_messages` → bridge/provider 投影 | **新增并已接线**：基础 developer → host 权限/scope developer → 可选应用 developer → 项目 contextual user → 历史。现有短 Ryn 基础、权限/资源/计划/项目信息继续使用；项目文档没有提升为 host 权限。 |
| 基础优先级 | 同上；显式空值与缺省不同 | `prompt.select_base_instructions`：session override > persisted base > model base > Ryn 默认；新任务在 `runtime.create` 固定 Ryn 默认基础，`loop._context` 使用 persisted base | **新增并接入默认路径**；没有为所有模型/会话 API 另造三种配置入口，也不宣称已固定每个供应商的独立模板。旧会话继续使用缺省回退，保持可读。 |
| 历史正规化与结果配对 | [history.rs:603–617、968–981](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/context_manager/history.rs#L603-L617)、[normalize.rs:21–100、171–211](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/context_manager/normalize.rs#L21-L100)：请求副本补缺失输出、清孤立输出 | `request_context.normalize_history`，由真实 `_context` 调用 | **新增并已接线**：保留 owner 原文/本地 metadata，按 call 顺序配对输出，缺失结果标明执行状态未知；重复、孤立和跨 epoch 输出不能绑定另一调用。只规范请求副本，不改旧加密归档或生成成功收据。当前内部合同主要是 function tool exchange，不是 Codex 所有 ResponseItem 的复制。 |
| 单次调用路由、错误与 schema | [tools/parallel.rs:100–112](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/parallel.rs#L100-L112) 区分可回写工具失败与致命错误 | `tool_router.normalize_tool_calls` → `loop._tool_rejection` / `_tool`；执行前 `assert_schema_unchanged` | **新增并已接线**：未知工具、坏 JSON、参数不符成为对应 call id 的错误结果，避免重试整个响应连带重做有效 sibling；请求时 schema 快照与执行时当前定义比较。非法/重复 call id、超预算仍是协议级失败。业务权限、审批和 operation ledger 是**原有能力保留**。 |
| 并行执行与有序回写 | [tools/parallel.rs:141、202–234](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/parallel.rs#L202-L234) 依据工具并行能力准入；[session/turn.rs:2568–2587](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/session/turn.rs#L2568-L2587) 有序收集并写历史 | 既有 `loop.PARALLEL_READS` / `_tool_batch`；新增 `StreamedReads` 的完成条目提前执行、durable read journal、按序 drain/publish | **旧批处理保留，新增流式路径已接线**。当前仅可信 `PARALLEL_READS` 可 early overlap；写操作等模型整条响应接受后，再走既有授权、审批和 ledger。这是具体实现边界，不是 Codex 所有工具的通用调度器。 |
| 回合继续/完成 | [session/turn.rs:592–604、3115–3119](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/session/turn.rs#L592-L604)：模型 follow-up、待处理输入、end_turn 共同决定 | `loop._model` → `turn_state.decide_turn` → 持久化 turn_decision；finish 前核对运行状态/control revision | **新增集中判定器并已接线**；原有长度截断续写保留。工具待处理先排空；纯文本 length、typed follow-up/end_turn=false 继续。中途 steer/cancel 通过 host token、revision 和 settle 保护，不需要复制 Rust mailbox 类型。 |
| 截断工具调用 | Codex 以完成的流事件和工具条目建调用，不把任意部分参数当成已完成工作 | `loop._model` 对 `finish_reason=length` 的调用生成 `agent_tool_call_incomplete`，交 `_tool_rejection` 返回模型 | **新增实质保护**：即使部分参数恰好是合法 JSON，也不会因此派发写操作。纯文本的可见答案续接、预算和大小限制是原有机制集中接线。 |
| 取消与迟到结果 | [tools/parallel.rs:145–147、260–317](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/parallel.rs#L260-L317) 对取消与某些必须完成的 runtime 分别处理 | bridge watcher 请求 adapter/remote transport 取消；调用前后检查 token；`loop.finish` 拒绝过期 control revision；工具收到协作取消函数 | **原有能力保留**。不能保证任意 Python 工具线程被强制终止，更不能声称取消会撤回已发生副作用。已有业务 ledger/unknown-write 恢复约束继续适用。 |
| 压缩与恢复 | [compact.rs:328–387、405–440](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/compact.rs#L328-L387)：只在完整压缩响应后安装替换历史，保留用户消息并恢复上下文基线 | 原有 `loop._compact` 选择闭合工具边界、无工具摘要、固定 owner/收据；新增 `build_context` 处理旧 checkpoint 切在 exchange 内的投影恢复 | 压缩主流程、加密归档和 scope 过滤是**原有能力**；本次新增正规化接线，并补了不完整摘要拒绝。Ryn 保留归档+checkpoint 投影，Codex 替换模型窗口并管理 worldstate baseline，持久化结构不同。 |

## 本轮对照发现并修补的入口

1. 审阅时发现真实 bridge 丢弃 `end_turn/needs_follow_up`，而 loop 已读取它们。
   当前 `bridge._response` 已按白名单校验并保留 bool；非法类型不能盲透传。
   这补齐了 Ryn bridge 到判定器的入口，仍需各实际 adapter 确实提供/正确映射这些协议事实。
2. 审阅时发现非空但被长度截断的摘要可能被安装为 checkpoint。
   当前 `_compact` 已拒绝 length/cancelled/content_filter、end_turn=false 或 follow-up 摘要，
   保留原历史/旧 checkpoint，产生 `agent_compaction_incomplete`，由有界恢复处理。
   修补是否覆盖真实模型长上下文任务，应由后续运行证据回答。

## 流式只读路径已接线，验证范围仍有边界

- 实际调用链已存在：`loop._model` 负责异常时 abort/join/recover；`_model_turn` 的 `on_event`
  交给 `StreamedReads`，模型响应返回后 `drain(final_calls)` 核对原始条目身份，接受 assistant 后才
  `publish` 并从普通队列移除已消费调用；`runtime.reconcile` 对 durable journal 收尾。
  对照时位置分别为 `loop.py:95、214、235、279、347`、`runtime.py:1406`。
- 提前执行只允许 `PARALLEL_READS` 内已完成的可信只读条目。准入和真正执行前均复查当前
  authority、完整任务 stamp、schema 与权限；intent/预算先落盘。结果完成先进入 journal/收据，
  最终响应接受后才按调用序进入模型历史。**写操作没有 early overlap**，仍在整条模型响应接受后
  走原授权、精确审批及 operation ledger；不会借流式路径获得额外写权限。
- 主任务的 `006` 运行日志记录 **56 passed in 19.70s**。本次只读确认了日志和接线，没有重新
  运行或将其冒充真实模型验收。日志位于研究 workspace 的 `work/rewrite-stream-006.log`，SHA256：
  `a914a736ef85aa4451837ceb4ef3f6a25d9b70c7b005343b8e9cfc91c173c99b`。
- 本次静态追查未发现可达的权限放大、取消/steer 后旧 stamp 结果进入新历史、或提前读变成重复写的
  具体路径。恢复仅收尾既有记录，不执行工具；未完成 intent 记为 interrupted，不能生成成功收据。
  这不是所有竞态不存在的证明。完成 read 后断流、final 不匹配、取消收据和 read→write 边界
  仍应由对应测试与后续真实任务分别提供证据。

### 预算边界的发现与修复

开发中发现：`streamed_reads._admit` 为每个 read 预留秒数，最初执行却只沿用整轮取消截止。
按 `budget.charged_seconds` 累加各 dispatch 的口径，最小触发为：
累计剩余 22 秒、单轮上限 10 秒，模型先预留 10 秒，两个 early read 分别预留 10 秒和 2 秒；
三者重叠各运行约 9 秒都未到整轮截止，最终结算可能为 27 秒。最后一个 read 实际超过自身 2 秒预留。
当前每个提前 read 已使用自己的 reservation 秒数收紧协作取消 deadline，并如实结算实际耗时，
不把超出的实际时间截账。另一次真实 GLM 任务发现 model 占满全部时间预留时 optional overlap
会误使整条响应失败；现在预算或存储不足只拒绝提前准入，模型结算后走正常队列。
`test_agent_stream_controls.py` 的 8 个实际 runtime 用例包括这两条预算回归、取消/steer 和加密
快照重启。工具执行器仍需协作取消，不能把任意线程保证成可被强制撤回的操作。

## 其他尚待验证范围

- 真实不同来源模型对角色投影、typed continuation、空/截断响应的映射仍需端到端验证。
  未知 legacy finish 标签目前保留旧纯文本完成 fallback，不能据纯函数测试推为所有 provider 语义等价。
- Ryn 的按需目录是既有 `ryn_discover_tools` full/directory 与 load/unload 投影；平铺 function、
  Ryn 应用工具合同仍是当前产品范围。Codex namespace、custom CodeMode、hosted tool_search、
  remote/async compaction、所有多 agent/host 分支没有因此全部复刻。

## 已有证据怎样使用

- 新 `test_agent_request_context_layers.py` 验证纯分层/历史副本机制；不是实际供应商请求证明。
- 新 `test_agent_rewrite_app_http.py` 使用真实 task HTTP、真实 Desk service/store 与应用 HTTP：
  一次现有授权、真实创建/回读、operation id 重试无重复、任务结果/事件及 settings/grants 保留。
  **仅模型边界脚本化**，不把它写成真实模型推理通过。
- 本轮选定 8 组兼容约束的 12 个参数化 case 曾干净退出通过，涵盖旧会话、设置、scope、
  模型切换与写回执恢复；后续生产文件继续变化后，要按最终快照运行所需回归。
- 真实 GLM API 和 Antigravity/Gemini 的三类固定任务均通过，另有 GLM→Desk HTTP 的第 4 类任务。
  全部失败和慢样本保留，查找另补一次成对重复；统计区分拒绝反馈与宿主执行，不作提速结论。
  第一个独立 `gpt-6.1-sol / ultra` 聊天已按七条通过；第二轮六条通过，第六条发现应用恢复问题，
  其失败报告原样保留。第三个全新聊天对最终快照独立复验后，七条全部通过，详见
  `INDEPENDENT-REVIEW-003.md`；不以旧快照的通过结论替代本轮故障恢复验证。
  本页不是独立验收报告，也不作“逐字一致”或“全 EXE 已恢复”的结论。

## 应用回执恢复的独立发现与修复

第二轮用真实加密好友传输故障确认：应用已提交后响应丢失或密文无效时，
`AppTools.execute` 原先可能误记为未执行；仅恢复 Agent 回执还会留下 consumer pending，
导致下一笔正常操作被阻塞。现将 `apps_response_unconfirmed`、`apps_response_invalid`、
`apps_store_unavailable` 都保留为 outcome unknown，并沿原 operation id 查询已存在的回执。
`AppTools.reconcile` 使用原 reviewed context/preview 重建原 command，验证回执后通过
`NodeApps.confirm_receipt` 结清原 consumer intent；这一步只改本地确认状态，
不调用业务 `invoke` 或 `resume`，不生成新 operation id。

确认必须匹配已存在的 intent、provider/consumer、app/action、参数摘要、request hash 和
原 preview。已确认的相同回执幂等；变化回执、拒绝状态、缺失或不匹配 intent 都拒绝。
新增实际 runtime 测试覆盖三种响应不确定错误、重启后重复确认、下一笔新操作可执行、
拒绝或伪造证据不能结清 pending，以及真正未执行的前置条件错误仍可修正后重试。
第三轮独立验收已复核原探针，并删去旧探针里对 `AppTools.reconcile` 的直接调用，
仅让真实 `runtime.run_once` 恢复。三类异常都只发原 `receipt` 查询、pending 清零，
原任务完成；新 Agent 任务经全新精确批准后也完成，原票恰好一张。
本轮独立关键回归 141 项、新增边界探针 24 项均通过，另有重叠 28 项不相加计覆盖。
295 个生产 Python 文件在验收前后字节一致，没有重新收费推理或替换安装版。

## 模型窗口与请求预算（后续上下文设置改进）

`max_context_chars=null` 现在表示自动跟随当前模型声明的 context window，数字仍表示
owner 手动选择的 UTF-8 请求字节上限。自动模式不再额外套用 600000 字节或手动设置的
2000000 字节范围；手动值仍会被小模型窗口夹限。完整配置的输出 token 预留和 512 token
framing 预留先从窗口扣除，剩余空间才投影为请求字节预算。未知窗口采用明确标为
`unknown_fallback` 的 32768 token 假设及保守字节上限，不声称该值是模型真实能力。

实际完整 messages（包含角色、指令、历史）与工具 schemas 的 JSON UTF-8 字节均进入
同一估计：`ceil(bytes / 3) + 512`。它是明确记录 `estimate_basis` 和
`token_count_is_estimate=true` 的启发式估计，**不是模型 tokenizer 的精确 token 数，也
不是所有 tokenizer 的硬上界**。累计 token 预算在派发前也沿用此估计预留，并保持既有
累计预算拒绝和输出限额执行；没有修改默认累计限额。供应商返回的 usage 才用于实际
结算；usage 缺失时累计状态保留 unknown，不把估计冒充实际消耗。独立验收需特别评估
高 token/字节比输入、供应商差异以及缺失 usage 的这条边界。

旧保存的 120000 或 600000 数值不能证明当时是默认还是 owner 手动选择；没有来源证据
时保留原数值，标为 `legacy_ambiguous_manual`。明确手动选择 600000 保持手动；有明确
自动来源或新设置 `null` 时才自动。现有任务的权限、owner 原文、回执和手动限额不由
新任务默认值更新改写。项目 guidance 与独立 checker 路径都已处理自动模式的 nullable
设置，并仍由真实 runtime 的模型预算完成最终准入。这批测试使用脚本化模型边界与
真实加密 store/loop，不构成付费模型准确计量或供应商硬窗口的证明。
