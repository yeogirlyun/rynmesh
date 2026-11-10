# T09 本地逐模型规则契约

2026-10-09，实施中。来源管理的 API/本地 OpenAI-compatible 模型及 native GGUF 已有本地派发规则；各后端支持字段不同。CLI 仍待后续批次，也不代替两个物理节点验收。

## 配置与版本

`GET /api/local/llm/sources` 保留旧字段，新增 `model_settings[upstream_model_id]`：

- `overrides`：该模型显式覆盖。
- `effective`：合并来源默认值后的 context_window、max_output_tokens、request_defaults、max_concurrent、requests_per_day。
- `inherited_fields`：仍继承来源默认的字段；并发默认 1，日请求额度默认 null。
- `rule_revision`：服务端随机 CAS 版本，规则/分享变更时更新。
- `destination_revision`：服务端随机模型配置代际，endpoint/key/模型实际请求参数变更时更新。修改兄弟模型不更新本模型。A→B→A 不恢复旧代际。
- `evidence.connection/model_exists/tools`：各自 status、checked_at（未知 null）、已测试时 method。配置/实例健康不等于实际连通验证。

`PUT /api/local/llm/sources/{source_id}/models/config`：

```json
{"model_id":"upstream-id","expected_rule_revision":"opaque","configuration":{"max_output_tokens":2048,"max_concurrent":2,"requests_per_day":100}}
```

返回完整公开来源记录。版本冲突 409；参数错误 400。configuration 是覆盖补丁，字段 null 恢复继承，requests_per_day:null 表示不设日额度。上下文 256–2,000,000，输出 1–min(上下文,256,000)，并发 1–4，日额度 1–1,000,000 或 null。request_defaults 仅 temperature 0–2、top_p (0,1]、thinking {type:enabled|disabled}、reasoning_effort none/minimal/low/medium/high/xhigh/max。语法合法不证明供应商支持；以显式探测为准。

旧来源顶层参数编辑影响仍继承相应字段的模型；显式覆盖的兄弟保留其配置/代际。endpoint/key 变更影响来源下全部模型。新加模型默认私有，不继承已有 shared_models。旧来源记录在加载时补随机逐模型版本，保留凭据、source_revision 和历史供应商参数；其验证时间未知，不能补造。

隔离验收读取已有凭据时必须使用 `SourceStore(home, private_key, migrate=False)`：构造与 read 不做迁移写盘。产品默认 migrate=True；只读验收不能以读取为名改变真实 owner 配置。

## 探测证据

`POST /api/local/llm/sources/{source_id}/models/test`：`{model_id,probe?}`，probe 为 connection/model_exists/tools，缺省 tools 兼容原接口。

- connection：固定最小聊天，成功 status=passed，同时建立 model_exists=present。
- model_exists：读取模型清单，present/not_listed；not_listed 不等于模型确定不存在，也不证明聊天可用。
- tools：固定两轮 client tool 回路，passed/failed；成功同时建立聊天连通与存在证据。失败可能是瞬态网络原因，不能断言模型永远不支持工具。

每类分别记录时间。影响推理的配置变更清除相关模型证据和工具支持；并发/日额度/分享变更不伪造或删除不相关能力证据。主动测试可能产生供应商用量；不携带 Agent 历史或资料。

## 真正派发的规则

Provider 的 ModelDispatchAdapter 是实际请求入口，Agent、本地手动 orders、签名 peer 请求、chat/普通/流式推理共用。它重新核对模型 rule_revision 与启用状态，按模型计并发，并持久预留 UTC 日请求次数后才调用已有 adapter。规则重建不清除该进程的在途模型计数；旧实例不能发起新请求。已派发请求仍可能完成，不宣称修改规则已撤回在途请求。

计数在 `<home>/llm/model-usage.json`，跨进程文件锁保护原子计数，重启保留当日用量。失败/取消/未知响应不退款；同一 provider operation 的已完成重放仍走既有回执，不再调用 adapter 或重复扣次数。这是提供服务的请求配额，不是 token/费用估计。Agent 内主动能力探测另受 T12 模型预算约束。并发限额目前为本节点服务进程级；不声称跨多服务进程有分布式并发租约。

模型实例持有固定来源快照，只有实际模型配置/规则/能力变更才重建；修改兄弟不重建本模型。提供方仍仅处理本次调用内容，不读取本机 Agent 历史或执行消费者工具。

## 尚待后续批次

来源模型逐接收者授权矩阵和认证目录已接现有 ai_access，参见 [T15 目的地绑定](peer-model-binding-t15-contract.md)。逐模型 `share_scope` 为 personal_space/friends，默认 personal_space。native 仍仅个人空间分享，CLI 的逐模型规则与可验证目的地未完成。

T15 本地实现将 provider catalog 的 model generation、精确接收者 permission 代际封装到既有签名加密请求，并在实际 adapter 准入核对。消费者重新选择模型须 owner 确认新目的地并清旧 epoch。CLI 不开放宿主工具以绕过不兼容。本地双节点测试不能替代真实两机验收。

分享 UI 并发更新：`GET /api/local/llm/sources` 顶层返回 `source_revision_cas_supported:true`。`PUT /api/local/llm/sources/{id}` 可带 `expected_source_revision`，在来源锁内比较公开 source_revision；不符返回 409 `source_revision_conflict`。仅分享更新可用 `{shared_models,expected_source_revision}`。旧调用不带字段时保留兼容，但 UI 不得据此宣称并发保护。scope 配置、shared_models 开关和逐好友授权分别是独立 CAS 事务，UI 必须逐步回读/报告，不能称三者原子提交。

## Native GGUF 小批

`GET /api/local/llm/native` 新增 `model_settings`（未安装模型为 null），含 `configuration`、`rule_revision`、`model_binding`、`enforcement`。

当前还返回实际 `context_window`，供前端显示及验证输出上限。入口沿用模型池“配置”→本地模型→“更多模型设置”，参数编辑只提交改动字段并携带规则版本；冲突或保存结果不明必须显式重新读取，不重放旧编辑。输出上限、并发、UTC 日请求上限独立于启动、分享及助手默认选择。旧后端没有 model_settings 时显示不支持，不伪造可保存参数。

`PUT /api/local/llm/native/model-config`：

```json
{"expected_rule_revision":"32hex","configuration":{"max_output_tokens":512,"max_concurrent":1,"requests_per_day":100}}
```

configuration 仅以上三个字段的补丁；输出 1–min(模型上下文,256000)，并发 1–4，日请求 1–1000000/null。没有继承层，null 只允许日额度表示无限制。不支持的采样参数、任意路径/模型名或来源字段拒绝。冲突 409，无效参数 400。规则按实际 GGUF hash 派生的 service_id 隔离，存于 `<home>/llm/native/model-policies.json`，只含配置/代际/UTC请求次数，无凭据、任务正文或工具结果。

ManagedAdapter 在 native 锁内校验已捕获 binding，核对并发并持久消耗日额度后才捕获实际 upstream 对象；输出上限通过真实请求 max_tokens 限制。配置更新重建 adapter，仍共用 native.active 在途计数。失败/取消/未知请求不退款；重启保留次数。import/卸载和新 runtime 安装旋转配置代际，sharing 的关闭/重开旋转规则；旧请求不可借新适配器派发。既有请求准入后仍属在途，不能撤回。

`tests/test_native_model_policy.py` 验证 API CAS、实际请求参数、旧实例、派发前竞态、未知消费/重启额度与并发重建；`tests/test_agent_space_binding.py` 验证 native 经过认证空间目录与签名加密请求后的最终 gate。上游为本地替身，不代表真实 GGUF 引擎兼容验证。CLI 目前 max_tokens 只是提示语，不能宣称硬输出 token 上限，也不能套用此 native enforcement 声明。
