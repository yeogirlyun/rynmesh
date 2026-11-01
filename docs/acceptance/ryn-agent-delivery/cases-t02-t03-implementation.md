# T02/T03 两批实现边界与 T05 接口准备

2026-10-09。状态：G1 第三批真实复测期间的实施准备；本文件未修改运行代码。已完成的 T01 有界模型重试保持冻结。本文承接 `cases-runtime-contract.md`，不提前实现 T04–T12。

## 批次 A：持久目标、状态迁移、模型提议和分层上下文（T02）

文件所有权：runtime 负责人新增 `state.py`、`prompt.py`，修改 `runtime.py` / `store.py` / `routes.py` 及专属测试。业务工具仍归 integrations，前端按稳定公共字段另行接入。

实现顺序与审阅切面：

1. `state.py` 定义 v2 字段初始化、v1 内存迁移、公共投影、run_state/outcome 区分和纯状态校验；`store.py` 在原加密事务中保存升级结果。保留旧版本 key/加密边界及 `status/revision` 兼容字段。没有真实验收器时只允许 unverified，不通过模型回复制造 verified。
2. 创建任务保留原文及稳定 message ID，建立 goal revision 1、用户原始目标的 required semantic 条件、空 plan、空 pending_input、control_revision/event_cursor。用户目标的原话和后续修订完整保留，不通过模型提炼替换原文。
3. `prompt.py` 输出版本化分层提示：产品规则 → 服务端当前目标/条件与权限/预算/模型去向 → 技能/偏好接口（本批为空）→ 当前证据/合法工具与最近历史。当前 context compaction 仍复用，只把目标/required 条件置于不可被摘要替代的权威层；不在本批重写大结果存储或归档。
4. 在正常主控工具协议中加入两个内部提议工具：`ryn_update_plan`、`ryn_request_input`；另可由 plan 参数携带 criteria_proposals。它们由运行时处理，不经 provider 执行，不进入业务工具的写批准 allowlist。简单问答不必调用任何内部工具。
5. 内部提议也须严格 schema、限长/限数量、有 tool_call_id 配对结果，并计入现有迭代预算。无权限扩大语义。格式错误返回可纠正的 not_accepted 结果，不能吞掉工具调用配对。

最小提议形状：

```json
{
  "tool": "ryn_update_plan",
  "arguments": {
    "goal_revision": 1,
    "steps": [{"id": "s1", "text": "检查来源", "criterion_ids": ["goal-coverage"], "depends_on": []}],
    "criteria_proposals": [{"description": "结果文件可回读", "kind": "mechanical", "evidence_hint": "file readback"}]
  }
}
```

`source`、`required`、`verified`、条件 `status`、权限/范围版本均不属于模型参数 schema，额外字段直接拒绝。`criteria_proposals` 作为 `proposal_origin=model,accepted=false` 的不可信候选保存，**不直接写入权威 criteria**。模型不能通过附带某条用户消息 ID 自封 user 来源。权威 source 只能由实际处理入口生成：本地 owner 的目标/条件操作 -> user；固定后端规则 -> system；已校验业务回执 -> tool。后两者不能代替用户授予权限。将来 T04 采用候选条件，仍需明确的后端 verifier 映射与原目标依据，或用户专门确认，不能只相信模型的 source 声明。

请求输入参数固定 `{goal_revision,prompt,kind,fields}`，kind 为 information/choice/credential_ref；敏感值字段只允许 credential reference，不接收明文 key。等待问题后本轮停止新工具派发。`pending_input` 的控制/目标/范围绑定由后端生成，问题文本不能授权工具。回答入口在批次 B 一并实现版本与幂等语义。

批次 A 验收：加密 v1 三类迁移（completed/unknown-write/旧审批）；新任务字段与旧 UI 字段同时可读；旧 completed 不变 verified；简单问答无额外规划调用；模型伪造 source/required/status 被拒绝；目标/required 原文经过现有压缩仍在权威层；计划/请求输入工具合法配对；模型共享提供端完全不进入这些内部动作。

## 批次 B：控制版本、问题回答、中途更正与安全检查点（T03）

1. 拆分事件与控制版本：每条 event 增 cursor 和旧 revision，只有权威控制变化增 control_revision；正文 delta、工具进度、模型开始不是控制变化。新增 pending_input/approval、消费问题/批准、目标/权限/模型变化、pause/resume/cancel 才更新控制版本。
2. action 入口支持 `client_request_id` 幂等及 `expected_control_revision`。approve/deny/resume/answer/steer 必须严格版本；旧客户端继续以旧 expected_revision 校验这些严格动作。pause/cancel 无论新旧字段是否过期均可靠且幂等，返回最新状态；客户端仍不能提交工具结果/参数替换/已验证状态。
3. `POST messages` 支持 `intent=steer|information`，steer 原子保存原文、goal revision 和控制版本并使旧排队动作/旧审批失效。后端接收更正不等待模型“同意”；已发生副作用不被文字撤销。新原文先入持久 pending_user_messages，在旧工具历史配对闭合后进入 provider history。
4. `POST actions` 的 `action=answer` 携带 pending_input_id、goal/control 版本和 answers；匹配后原子消费问题并持久用户答案，不能因答案增加授权。修改范围须另走用户专用 scope 控制。
5. 每次运行 claim 保存操作身份和 goal/control/scope/业务版本。模型完成、工具准入、业务写锁、回执保存均检查控制意图；旧模型响应不排队工具。steer/pause/cancel 的异常收尾由执行器按当前控制状态处理，不能误标普通失败或自动复活。
6. 尚未执行的旧 tool_calls 逐个形成 not_executed 结果后才接入用户更正；已派发工具保留真实/未知回执。等待副作用结果期间不启动下一轮，晚到写入证据即使 cancelled 也要保存。unknown write 沿用既有 interrupted 且不可 resume，恢复业务查询留给 T04。
7. 每个公共任务响应派生 allowed_actions，并给出 reason/requires_control_revision；不把该投影当作授权。UI 能据此区分等待问题、审批、暂停、未知写入和结束但未验收。

最小请求：

```json
{"message":"不要分享，只检查状态。","intent":"steer","expected_goal_revision":1,"expected_control_revision":4,"client_request_id":"owner-request-uuid"}
```

```json
{"action":"answer","pending_input_id":"input-id","expected_goal_revision":2,"expected_control_revision":6,"answers":{"target":"resource-id"},"client_request_id":"owner-answer-uuid"}
```

严格冲突 409 保留服务端当前控制快照；不能自动将旧批准/答案重定向到新对象。pause/cancel 在同操作重复请求、文本 cursor 高频改变和晚到模型回复三种情况下都返回一致可解释状态。

批次 B 验收：流式 delta 中旧 pause/cancel 可靠；模型飞行中 steer 后旧响应零派发；工具已提议/批准未派发时 steer 导致旧批准失效与 tool pair 闭合；写锁前撤权无副作用；写已成功再 cancel 保留回执；重复 answer 幂等、旧 question/control/goal 版本 409；信息回答不增加权限；restart 保留有效 pending_input/审批和控制计数；T01 一次模型重试仍计 steps/elapsed，cancel/steer 不能复活旧重试。

## T05 的最小对齐边界（已与 integrations 对齐）

统一服务端 `ExecutionContext`：

```text
task_id, operation_id, owner_id, consumer_node_id,
goal_revision, control_revision, scope_revision,
model_destination {model_id,source_id,provider_node_id?},
resource_grants [{connection_id,connection_access_generation,canonical_root_locator,
                  actions,recursive,formats,max_bytes,resource_version?}],
egress_grants [{origin_ids,destination_id}],
configuration_grants [{action,target_id,expected_revision?}]
```

上述上下文由运行时从已持久的用户授权与后端策略构建，模型参数只有实际业务目标/操作参数。禁止模型填 owner/consumer/scope_revision/egress allow。无授权 scope 默认不包含资料读取/出站、新建、删除、覆写或来源配置权限；当前已有 Agent 的 inspect/share 行为先映射为原有限定的管理权限，不凭空获得泛化文件权限。

metadata 增加 `scope_context_supported` 与明确 action 类型，只有已实现上下文验证的工具才能暴露新的资料/配置能力；缺 context 必须拒绝。旧工具签名暂兼容，后续 scope 工具 `execute(...,execution_context=...)` 与 preview 同用受信上下文，在最接近业务锁的位置复核最新范围版本。运行时 cancelled/check_authority callback 仍作为撤权即时检查，不能拿传入的旧 snapshot 替代当前授权。

resource actions 使用 `list/read_ai/create_file/create_dir`；普通读取权限不自动意味着允许发送到 AI。NAS 的 allow_ai 是独立边界。Google/OneDrive 的资源标识可能是 item ID，canonical_root_locator 必须通过后端资源引用/提供方 ancestry 复核，不能对任意字符串做前缀判断。connection_access_generation 只随 enabled/root/account/allow_ai/writable/reauth 等授权要素变化，不能直接使用会因 token 刷新变化的整份密文 hash。

每次读取返回及每次模型出站前复查最新 scope/generation/egress origin 集合。撤权不能撤回已经发给提供方的内容，但后续请求不得从缓存或持久工具历史再次发送已撤权资料；压缩摘要也需携带输入 origin 集合，不能洗掉来源绕过出站检查。无法证明 origin 的历史不能在新限制下默认安全。范围修改走专用本机 owner endpoint，同时绑定 expected_control_revision 与 expected_scope_revision；普通消息、信息答案和内部提议不授予范围。

业务层输出普通事实/回执，含 operation_id/目标 ID/版本/实际结果；T04 verifier 以后消费该证据，但 tools 不能写 task.outcome。T06/T07 的资源与 credential ref 细节由 integrations 给出，再收敛字段名。上下文中的 destination 使用 catalog 稳定身份，不采用模型提供的 URL；模型切换/重定向需新出站校验。

后续回执接口预留 `tools.reconcile(operation_id,context)->{outcome:executed|not_executed|unknown,evidence,artifact}` 与 `verify(receipt,criteria,context)`。先持久 intent 后 dispatch；未知只查询原操作，不重放。receipt 至少包含 origin/canonical target ID、预期内容 hash/size、provider resource/upload/operation ID、实际版本/hash。文件名/内容吻合不足以确认写入归属。Google 当前 list+create 不能保证跨客户端同名原子唯一，不把本机锁包装成幂等承诺。

T07 credential_ref 由本机安全表单生成，绑定 source/draft/task/expiry；模型上下文只见 has_credential，引用本身由后端保管并传业务层。draft_version + expected_source_revision 控制编辑与保存；测试证据绑定精确配置及凭据引用 fingerprint，保存锁内 CAS 复核。测试后的编辑/并发配置改变使测试失效；保存后复测失败保留真实当前态，不能自动覆盖别人更新的配置。

批次 A/B 不实现：文件连接器、来源创建事务、自动回滚/未知写查询、通用结果验收器、长期记忆/技能管理、任务归档/删除、完整预算预留结算、UI 重做。已有机制保持回归；这些分别仍属于 T04–T14，不能因 v2 字段存在就标完成。
