# T15：认证好友与个人空间模型目的地绑定

2026-10-09，实施中。仅本地代码与测试；未调用外部模型或远端机器，未通过真实两机验收。

## 范围和授权

来源模型新增 `model_settings.effective.share_scope`，取 `personal_space`（默认）或 `friends`。沿用 T09 单模型 CAS PUT 的 `configuration.share_scope` 设置，null 恢复默认。模型仍须在 shared_models 中显式启用；新模型不继承分享。friends 是 owner 明确选择的该模型访问策略，不能同时以个人空间成员身份绕过好友 grant。其模型名不进入公共注册广播。

原 owner API `PUT /api/local/ai-access/{service_id}/{relationship_id}` 的 `{allowed,expected_revision}` 现在可用于来源模型。身份既是 service_id，也是当前已验证好友关系的 peer_id；友情本身不授予任何模型访问权。撤销/重授沿用持久递增 revision，旧请求不复活。禁止外部模型或文档内容提交凭据替代 owner 授权。

FriendAICatalog 从单模型扩展至最多 100 个模型、64 KiB 加密响应，逐条仅返回该接收者实际获授模型。来源选为 friends 时使用逐关系权限；个人空间模型继续使用已有空间策略，不能经好友目录获得本批资料授权。

## 认证目录和请求字段

提供方已认证、加密的好友目录条目含：

```json
{"model_binding":{"version":"ryn.model-binding.v1","service_id":"src-model","model_generation":"32hex","rule_revision":"32hex"},"ai_permission":{"relationship_id":"32hex","revision":1}}
```

generation/revision 均为非秘密标识，不是 bearer 凭据。消费者只有在核对认证好友关系、加密响应、request_id/from/to、权限关系、字段格式后，才在本地条目上加入 `binding_verified=true` 和自己的 `consumer_peer_id`。公共发现记录不可授予这个标志，代码强制清除。

ModelCatalog peer 条目提供 `destination_binding_supported=true`、`consumer_peer_id`、`expected_model_binding`。四字段运行时 model_destination 保持不变；destination_revision 是 provider_peer_id + 精确 consumer_peer_id + model_binding + ai_permission 的规范 JSON SHA256，不包含秘密。

消费端传输在重新发现模型后比较准确 expected binding，不接受公共广告或新代际替代已批准旧代际。签名加密的 llm_request body 带：

```json
{"expected_model_binding":{"model_binding":{},"ai_permission":{},"consumer_peer_id":"receiver"},"ai_permission":{}}
```

真实结构如上，内部两个空对象位置必须为认证目录的完整对象；双方 permission 必须完全一致。binding 计入请求幂等指纹。消费节点身份不能由模型调用参数另选。

## 实际派发

ProviderService 先验签/解密并验证 sender、当前关系与授权；若有 expected binding，必须与实际固定 adapter 实例的模型/规则代际一致。ModelDispatchAdapter 在来源锁内再次检查当前模型配置 fingerprint、模型代际和已验签 sender 的权限，再持久消耗模型额度；chat、普通文本、流式均受同一入口约束。外层校验至 adapter 准入期间撤权会拒绝，不发送上游。

授权准入之后的请求属于在途请求；撤权不能撤回已发送数据，后台权限复查和 cancel 属于尽力中止。规则重建不能使旧 adapter 获得新请求能力。客户端工具描述/调用/回执仅作为当前推理请求数据传递；提供方不执行消费者工具，不读取本机 Agent 历史、偏好或技能。reported_usage 在签名响应中保留真实缺失信息。

## 验证和未完成部分

`tests/test_agent_peer_binding.py` 使用真实本地好友配对、加密目录、owner 权限 API、consumer bridge/execute_order、签名请求、ProviderService、SourceStore 与派发准入。仅网络交换与上游模型为内存替身。覆盖双接收者/双模型矩阵、接收者绑定、撤销重授、外层授权后撤权、旧模型代际、两个请求历史隔离以及公开标志不可代替认证。

运行时消费端另行核验可信条目标志、精确接收者和复算摘要后才允许 peer 资料任务/模型切换；缺任何一环继续拒绝。CLI 的可信模型配置代际仍待后续批次。真实物理两机、断连重连与实际模型业务验证仍未通过。

## 第二批：个人空间认证目录

`POST /api/peer/llm/space-catalog` 只接既有 `seal_task` 签名加密的 metadata challenge。请求 kind 为 `space_model_catalog_request`，body 仅 `{task_id,reply_messaging_pub}`；响应 kind 为 `space_model_catalog_response`，body 为 `{task_id,services,space_permission,provider_messaging_pub}`。提供端按验签 sender 验证当前有效空间成员与 AI space policy。消费者核对 provider identity、recipient、随机 task_id，只有成功响应才能建立可信目录。广播的 messaging key 仅用于发送无资料 challenge；后续推理密钥取已签名响应。

空间 expected binding 精确结构（与好友 ai_permission 互斥）：

```json
{"model_binding":{},"space_permission":{"space_id":"32hex","snapshot_revision":1,"access_generation":"32hex"},"consumer_peer_id":"receiver","consumer_access_generation":"64hex"}
```

`access_generation` 是提供端持久随机 policy epoch，local↔space、离开/恢复不能复用；snapshot_revision 来自已验证空间成员签名快照。`consumer_access_generation` 是消费者本机当前空间 permission 的摘要，只能由本机认证目录生成，同样进入 destination_revision。它防止消费端先关后开空间权限使旧 egress 恢复，不向提供端授予权限。提供端仍只按自己的有效授权裁决。成员快照变更后缓存失效，刷新也产生新目的地。

空间握手上限：入站请求 8 KiB；响应明文 64 KiB、密文 128 KiB、100 模型；同时入站和出站各 2；每 sender 每秒最多 1 次，最多保留 64 个 sender/peer；缓存最长 120 秒；网络请求最长 5 秒。后台每 15 秒最多刷新 1 个空间 peer；owner 可调用 `POST /api/local/llm/space-catalog/refresh`，body `{peer_id}`。不发送任何任务正文、工具历史或来源凭据。

派发时先取得空间权限锁再取得 source 锁，复核精确 space_permission 与固定模型绑定，消耗配额后释放锁再开始上游 I/O。既有无绑定手动空间推理仍沿用原成员策略；本批 epoch 只用于需绑定的资料目的地，不改变加入/退出流程。`tests/test_agent_space_binding.py` 通过真实本地签名空间快照、真实 HTTP route/TestClient、密文请求与真实 dispatch wrapper 测试，网络和模型仅本地替身，不等同物理两机验收。

## Native 绑定

native GGUF 的 `ManagedAdapter` 提供同一格式 model_binding。空间请求沿用上述认证目录与精确接收者结构，最终锁顺序为空间权限锁→native锁→policy文件锁；在锁内核对当前代际/权限并捕获固定 upstream 对象，随后释放锁发送。owner 本机 ModelCatalog 仅 runtime=native_llama_cpp 条目可提供 `local_model_binding`，目的地摘要为 `{provider_peer_id,model_binding}` 的规范 JSON SHA256。Bridge 在实际请求边界传 call_bound；仅早期目录核对不足以派发。运行时消费端还须严格检查 native 类型、service_id、自己节点身份、合法 binding 和复算摘要，不能放宽所有 local_service。未通过显式工具探测的 native 模型仍不能作为主控。

消费端以上严格检查已接入。`test_agent_native_scope.py` 通过实际 local catalog、精确 scope、真实 loopback DAV 读取和最终 native adapter 准入，确认仅授权正文进入该适配器；代际旋转后旧正文拒绝，类型/节点/服务/摘要任一不符拒绝。模型上游为替身，不能替代真实 GGUF 工具兼容性或两机验收。另测 4096 窗口下按模型窗口投影工具，原任务字节上限保持不变；窗口计算是沿用桥接估计的保守边界，不声称精确 tokenizer。
