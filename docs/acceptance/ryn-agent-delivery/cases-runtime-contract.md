# T02/T03 统一任务契约 v2（实现前冻结）

状态：设计契约，尚未写入 runtime/store/routes。T01 的真实模型 G1 通过后才进入核心实现。本文件是验收包的接口依据，不代表功能已经完成。

## 1. 持久任务字段

| 字段 | 形状及权威来源 |
| --- | --- |
| `schema_version` | 整数 `2`；加密存储格式版本，不是控制修订号 |
| `goal` | `{revision, kind, original_message_id, text, constraints[]}`；`kind=question/action/mixed/unclassified`；原始用户消息不可变，修订追加用户原文，当前目标由服务端提交 |
| `goal.constraints[]` | `{id,text,source,message_id,required}`；`source=user/system/tool` 表示事实来源，不代表工具具有授权能力；删除用户必需约束只能由用户明确修改目标 |
| `criteria[]` | `{id,goal_revision,source,source_ref,required,kind,description,verifier,status,evidence_refs[]}`；`kind=mechanical/semantic`；`status=pending/passed/failed/unknown`；仅验收器写状态 |
| `plan` | `{revision,goal_revision,steps[]}`；步骤 `{id,text,criterion_ids[],depends_on[],status}`；`status=pending/running/blocked/done`；简单问答允许空 steps，不强制额外规划模型调用 |
| `control_revision` | 服务端单调递增的控制状态版本；持久化目标/范围/模型/权限改变、输入或审批的创建与消费、pause/resume/cancel 等控制变化时增长 |
| `event_cursor` | 持久事件序号；每条正文 delta、工具进度、日志及控制事件均增长；正文和无权威变化的进度**只增 cursor**，不增 control_revision |
| `run_state` | `queued/running/pausing/paused/waiting_input/waiting_approval/reconciling/ended`；描述执行器状态，和目标达成分开 |
| `pending_input` | `null` 或 `{id,kind,prompt,fields,goal_revision,control_revision,scope_revision,business_revision,created_at}`；`kind=information/choice/credential_ref`；答案是用户数据，不等价于新授权 |
| `pending_approval` | 既有精确操作/预览加 `{goal_revision,control_revision,scope_revision,business_revision}`；参数 hash、工具名、目标、预览 hash、操作 ID 仍须同时绑定 |
| `outcome` | `{status,goal_revision,criteria_results[],summary,evidence_refs[],uncertainties[]}`；`status=unverified/verified/partial/not_achieved/unknown`；只有服务端验收器能写 `verified` |
| `allowed_actions` | 公共投影 `{action:{enabled,reason,requires_control_revision}}`；每次响应按当前状态/待核对写入/权限派生，不持久化为权限，不接受客户端提交 |

`goal.revision` 与 `plan.revision` 独立。文本修正产生新 goal revision，保留旧版本；纯步骤重排可以只改 plan revision，但不能借计划修改条件、权限或目标。`scope_revision` 复用 T05 的授权快照版本；`business_revision` 来自对应业务服务，可能是单值或来源 ID 到版本的映射。它们不能用模型生成的字符串替代。

模型只能提交目标理解、验收条件和计划**提议**。提议不含 `verified`、`status=passed` 或授权字段；结构出现此类字段直接拒绝。模型可提议条件，服务端仅按用户原文、系统规则或已验证工具事实采用，记录 `source`、`source_ref`、`goal_revision`；模型无权删除/弱化已有 required 条件。`tool` 来源只能补充可核验事实，不能获得 user 来源的授权强度。

机械条件明确 verifier（如文件 readback hash、目标资源与数量、业务版本/接收范围、操作回执）；语义条件由单独内容检查给出证据和不确定性。流畅的最终回答、模型自称完成、工具成功的数量都不是机械验证。文件存在不能证明未知写由该操作完成。全部必需条件通过、没有未知副作用或未满足的安全条件，才能 verified；主观条件不确定时保持 partial/unverified。Q 类简单回答也核对其依赖事实，无须强制生成计划。

## 2. 控制请求及并发语义

统一请求使用 `client_request_id`（幂等）和严格动作的 `expected_control_revision`。目标修改还要求 `expected_goal_revision`；问题回答绑定 `pending_input.id` 和其 goal/control 版本；审批绑定 `pending_approval.id`、精确参数和业务/范围版本。版本不匹配返回 `409`，附当前控制快照和 allowed_actions，不能自动把旧答案/批准套到新目标。

`pause` / `cancel` 是可靠的幂等控制，不因正文事件或旧 revision 拒绝。`pause` 对已暂停返回当前状态；`cancel` 对已取消/已结束返回当前状态，无二次派发。重复 `client_request_id` 返回已有控制操作；cancel 不承诺回滚。`resume`、审批、回答问题、目标/范围修改需要严格乐观锁，是否可用由 allowed_actions 指明。旧客户端 `expected_revision` 仅在需要严格锁的动作继续校验旧全局版本，返回明确过期冲突；对 pause/cancel 忽略该旧字段。新客户端必须使用 control_revision；过渡期不把旧 revision 数值冒充 control_revision。

中途更正接口最小为 `POST tasks/{id}/messages` 增加 `intent=steer`，内容与 `expected_goal_revision`、`expected_control_revision` 一起原子落盘。服务端立即增长目标/控制版本、撤销旧排队写入与旧批准，然后要求当前模型停止/到安全检查点，不等待模型自行“理解后生效”。普通补充事实可用 `intent=information`；它不能隐式扩大权限。

安全点至少覆盖模型请求前、接收模型结果后、工具准入前、持有业务写锁的最终权限检查、收到工具回执后。每个 in-flight 操作记录 `{operation_id,goal_revision,control_revision,scope_revision,business_revision,authorization_kind,dispatch_phase}`。过期模型回复不能重新排队旧目标工具；已派发写入的晚到结果仍挂回原操作，追加真实证据，不能因 cancel/steer 丢掉回执。

steer 废止尚未执行的队列时，为旧 assistant `tool_calls` 的每个未执行调用写入对应 `tool_call_id` 的明确 `not_executed` 结果，再追加用户更正。保留完整配对，避免下轮提供方拒绝未闭合工具历史；这些结果由执行器生成，不能接受客户端伪造。已派发调用先保存真实回执或 unknown 标记，再以合法配对历史继续新目标，不能捏造执行结果闭合历史。

问题回答和审批消费都在同一持久事务内完成版本检查、消费标记和后继状态切换。工具执行器在真实写锁下复查当前权限和业务版本，不能仅凭运行时之前检查通过。持久化和副作用之间的间隙仍按原 operation_id 恢复核对，禁止盲目重放。

## 3. 最小完整流程

1. 入口完成 owner/local-control、模型能力、预算、当前资料/出站范围预检，持久用户原文和版本；事实缺失建立 pending_input，不擅自猜凭据。
2. 服务端装入当前 goal/required criteria/权限/预算等不可被摘要覆盖的权威层，再装入节点偏好、选中技能与允许工具和带来源的资料。技能与工具结果是数据，不能覆盖上层授权。T11 未接入时技能/偏好为空并明确可用范围。
3. 模型提议计划或工具。执行器做结构校验、准入、审批、预算预留和安全点检查后执行；工具回执持久化并返回模型，模型继续选择下一步。
4. 无工具的模型回复进入验收候选，不直接等同 verified。机械核验与语义检查分别输出 criterion result；不足时有界补读/纠正，信息缺失转 waiting_input，超预算/持续无进展明确停止。
5. restart 先恢复控制/目标和未完成工具配对，查原 operation receipt；确定未执行才可重新提议，经最新权限确认后新建操作；未知写入保持 reconciling/unknown，拒绝重复副作用。
6. 输出事实/产物引用、未完成条件和终态；结束运行不等同目标已达成。后续用户更新形成新目标版本，旧结果留在历史。

## 4. v1 迁移策略与实现依赖

T02/T03 增量迁移，保留旧字段与公共 `status/revision` 兼容投影。新加密 payload 内写 schema_version=2；沿用现有 key 与原子写，不写明文备份。先纯内存构建并验证迁移结果，再在原存储事务写回；失败保持旧文件可读，不启动任何工具。

- 旧用户首消息建立 goal revision 1，kind=unclassified，原文保留；不能凭摘要制造已接受的条件。系统最低安全核验可新增 source=system 的 pending 条件。
- 旧 completed -> run_state=ended / outcome=unverified，保留旧最终回复；绝不自动迁成 verified。旧 failed/cancelled 同样记录已有事实与不确定性。
- 旧审批缺新版本绑定时标失效并要求针对原精确操作重新确认，不能自动消费。T02 新格式任务的重启保持相同有效审批 ID（R02）；这是迁移和普通重启的不同路径。
- 旧 in-flight/unknown-write marker 原样保留，优先 reconciliation，不因 schema 升级重新提交。旧任务无证据不能把文件“看起来存在”当成操作回执。
- 初始 event_cursor 可取既有最大事件序号；control_revision 独立从 1 开始，legacy revision 继续兼容增长，不能把两者混用。
- 不回写或猜测老任务的 scope/业务版本、预算使用和 token 费用；缺失保留 unknown。allowed_actions 因未知写入禁用 resume/approve 等动作，允许可靠 cancel 与核对查询。

T02 交付持久字段/提议校验/提示词分层/目标权威；T03 交付控制事务/版本绑定/检查点/中途更正/allowed_actions。T04 实现验收器与未知结果查询；在它接入前允许 outcome=unverified，禁止用“模型停止”替代完成。T05 的范围字段先存接口占位，仅已有权限可授权；T06/T07 的文件/来源 verifier 按相同 criterion/evidence 接口补齐。T10 压缩必须保留最新目标与必需条件，T12 预算预留独立于计划状态。

最低回归：正文高频 delta 下旧 pause/cancel 请求可靠；并发旧目标批准/答案均 409；批准消费后权限撤回在写锁内阻止副作用；steer 在模型/工具两种飞行阶段都不派发旧动作；cancel 后晚到写回执保留；简单问答空计划可结束但未核验不 verified；模型不能删除 required 条件或提交 verified；旧 completed/unknown-write/approval 三类迁移分别符合上述语义。
