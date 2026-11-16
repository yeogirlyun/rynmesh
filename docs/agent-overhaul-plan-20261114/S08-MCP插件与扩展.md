# S08 MCP插件与扩展：改动项

共 70 项。行号按基线 develop a8ec8d81。


基线：`C:\Users\sguser\ryn-worktrees\agent-overhaul-plan`（a8ec8d81）。行号均为基线代码中打开核对过的位置。

## S08-I1 MCP 服务器发来的请求不再断开会话
- 问题：两个读循环把任何"带 id 且 id 不等于本次请求"的消息当协议错误。服务器发 ping、roots/list、sampling、elicitation 时，整次调用报 agent_mcp_protocol_invalid 且 dispatched=True，会话被关，写工具变"结果未知"。Codex 交给 handler 处理（`rmcp-client/src/elicitation_client_service.rs:216-240`）。
- 涉及行：A11-N2, A12-N4, A19-N1, 28-M19, 24-M10.49
- 要改：
  - `rynmesh/agent_runtime/extensions/mcp_transport.py:442-448` StdioMcpSession._exchange —— 先判断消息含 method 且含 id：ping 回 `{"result":{}}`；roots/list 回空 roots（工作区根在 S08-I2 之后再给）；elicitation/create 在 S08-I2 落地前回 `{"action":"decline"}`；其余回 error -32601。用 `_send` 写回后 `continue`，不再抛错。
  - `rynmesh/agent_runtime/extensions/mcp_transport.py:839-840` StreamableHttpMcpSession._extract_sse_message —— 同样分派。HTTP 侧的应答要另发一个 POST（带 Mcp-Session-Id 与 MCP-Protocol-Version，复用 `_headers()` `:751-769`），新增 `_post_response(message)`；发送失败只记日志，继续等原响应。
  - 两处共用一个新函数 `_server_request_reply(message) -> dict`，放在 `:183-207` 附近。
- 新增测试：`tests/test_agent_mcp_transport.py`（或现有 MCP 传输测试文件）：stdio 假服务器在回应前先发 ping、roots/list、sampling/createMessage，调用仍成功，收到对应回包；HTTP SSE 流里夹一个 ping，客户端发回应 POST 且原调用成功。
- 依赖：无
- 兼容和风险：不改存储。旧行为是失败，新行为更宽松；要防止恶意服务器无限发请求——每次请求内服务器请求计数上限 64，超限仍按原错误处理。
- 工作量：小
- 影响：中

## S08-I2 MCP elicitation：表单转主人填写，应答回传
- 问题：initialize 声明 `capabilities: {}`（stdio `mcp_transport.py:518`，HTTP `:1057`），stdio 报 2025-03-26（`:53`，elicitation 是 2025-06-18 才有）。需要表单的 MCP 工具只能失败。Codex 经 handler 转用户（`rmcp-client/src/logging_client_handler.rs:41-50`）。
- 涉及行：02-M14, 03-M166, 06-M5, 06-M44, 06-M45, 06-M46, 21-M51, 26-M73, 26-M119, 26-M121, 26-M122, 26-M123, 26-M125, 26-M126, 27-M37, 27-M75, 27-M78, 28-M86, 42-M27, 46-M50, 47-M75, 26-M84, 27-M17, 27-M77
- 要改：
  - `mcp_transport.py:53` PROTOCOL_VERSION —— stdio 改报 2025-06-18（与 `:54` 一致）；`:518`、`:1057` capabilities 改 `{"elicitation":{}}`，roots 声明 `{"roots":{"listChanged":false}}`，roots/list 回任务工作区根。
  - `mcp_transport.py:442-448`、`:839-840`（S08-I1 的分派点）—— elicitation/create 交给会话上的 `on_elicitation(request_id, params)` 回调；回调阻塞等应答，等待期间把 `_exchange` 的 deadline 顺延（`:396` deadline 改为可延长的对象），不计工具耗时。请求 `_meta` 原样带进表单记录并回填。
  - `mcp_transport.py:1360-1373` McpSessionManager._create_session —— 注入 on_elicitation，按 session_scope/task 找到当前任务。决策链：子代理或 read_only 档回 decline；automatic 档且 requestedSchema 无字段（纯确认）直接 accept `{}`；其余转主人表单。
  - `rynmesh/agent_runtime/runtime.py:2305-2334` answer 分支 —— 新增 `pending["kind"] == "mcp_elicitation"`：不追加用户消息，把 answers 交给 manager 的等待表回 `{action:"accept",content}`；新增 decline/cancel 动作；找不到等待项回 cancel。requestedSchema 映射成 fields（string/number/boolean/enum→choice），映射失败直接回 cancel。
  - `mcp_transport.py:1387-1399` forget_task、`:610-621` close —— 有挂起表单时回 `{action:"cancel"}` 并清掉 pending_input；`:1219-1252` refresh_tool_catalogs 不关有挂起表单的会话。
  - 等表单期间的任务状态与计时：现有 ryn_request_input 在工具结果写回后才置 waiting_input（`rynmesh/agent_runtime/loop.py:2184-2198`，`:2197` 扣除 wait_elapsed）。elicitation 是工具在途时等待，须在 on_elicitation 回调里（持任务存储事务）写 pending_input、置 waiting_input、发 input_requested，并记等待起点；应答或取消后恢复 running，等待时长计入 `wait_elapsed` 不计 elapsed_seconds（S02 文件，需 S02 确认）。
- 新增测试：`tests/test_agent_mcp_elicitation.py`：假服务器在 tools/call 中发 elicitation/create → 任务进 waiting_input、kind=mcp_elicitation；answer 后服务器收到 accept+content，原调用返回；automatic 下空 schema 自动 accept；子代理收到 decline；任务取消时回 cancel；等待 2 倍 tool_timeout 不超时。
- 依赖：S08-I1；S02 主循环（任务状态 waiting_input 与工具线程并存，需 S02 确认）
- 兼容和风险：pending_input 新增 kind，旧任务无此 kind 不受影响；界面需认 kind=mcp_elicitation（复用 ryn_request_input 表单）。工具线程阻塞期间任务不能被重新调度，需 S02 保证。
- 工作量：大
- 影响：中

## S08-I3 读循环容错：坏消息跳过，通知按单次请求计数
- 问题：stdio `_read_loop` 遇一行坏 JSON 即置 protocol_invalid 断会话（`mcp_transport.py:313-319`）；SSE 坏 data 直接抛错（`:825-830`）。通知计数 `self.notifications` 只在建会话时清零（`:230`、`:671`），会话复用后第 65 条 progress/日志通知让当次调用失败（`:433-440`、`:835-837`），写操作变"结果未知"。Codex 坏行只记日志（`rmcp-client/src/bounded_stdio_transport.rs:99-111`），progress 只记日志（`rmcp-client/src/logging_client_handler.rs:63-72`）。
- 涉及行：14-M28, A08-N4, 28-M20, A12-N3, A17-N1, A19-N2
- 二轮漏项：G21-4
- 要改：
  - `mcp_transport.py:300-324` StdioMcpSession._read_loop —— json.loads 单独 try；ValueError、非 dict、jsonrpc≠2.0 时计数后 continue，连续坏行超 64 才置 agent_mcp_protocol_invalid。
  - `mcp_transport.py:822-830` _extract_sse_message —— 解析失败跳过该事件并计数，同样上限。
  - `mcp_transport.py:409`（stdio 发请求前）与 `:857`（HTTP）—— 每次 `_exchange` 开头 `self.notifications = 0`。
  - `:429-440`、`:831-838` —— notifications/progress、notifications/message 只记 diagnostic_log 不计数；其他通知超 64 条后丢弃，不抛 agent_mcp_notification_limit。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_transport.py:237,317-319` StdioMcpSession._read_loop —— 队列满不再判 agent_mcp_protocol_invalid：无 id 的通知在读线程里就处理（progress/message 记 diagnostic_log，其余计数后丢弃），不入队；队列只放响应与服务器请求，入队不设 0.2 s 超时（同时只有一个在途请求，不会积压）。
- 新增测试：MCP 传输测试：stdio 服务器先输出一行非 JSON 再正常回包，调用成功；同一会话先后两次调用各收 50 条 progress，均成功；SSE 夹坏事件仍成功。；二轮：空闲会话期间服务器连发 200 条日志通知，下一次调用成功
- 依赖：无
- 兼容和风险：无存储变化。错误码 agent_mcp_notification_limit 保留但几乎不再出现。
- 工作量：小
- 影响：中

## S08-I4 stdio 会话寿命改空闲超时，单行上限 8 MiB
- 问题：SESSION_LIFETIME_SECONDS=300（`mcp_transport.py:65`）既是会话 deadline（`:227`）又是 process_guard 的硬超时（`:268`），`_watch_loop` 到点直接 terminate（`:326-335`），在途调用被杀。`output_bytes` 会话累计超 4 MiB 即断（`:306-309`）。Codex 单行 8 MiB（`rmcp-client/src/bounded_stdio_transport.rs:24`）。
- 涉及行：A13-N1, 27-M74
- 要改：
  - `mcp_transport.py:65,227` —— 拆成 `SESSION_IDLE_SECONDS=1800` 与 `SESSION_MAX_SECONDS=86400`；deadline 在每次 `_exchange` 成功返回后续期为 now+idle；有在途请求时 `_watch_loop`（`:326-335`）不按 idle 到期。
  - `mcp_transport.py:264-269` —— spawn 的 timeout 传 SESSION_MAX_SECONDS（Unix 下 process_guard 包装进程按它硬杀，`rynmesh/llm_package/process_guard.py:12-37`）。
  - `mcp_transport.py:62,303-309` —— 新增 MAX_LINE_BYTES=8 MiB，readline 按行上限；删掉 output_bytes 会话累计判断。
  - `mcp_transport.py:343-349` is_usable —— 用新 deadline。
  - 本轮补：单行上限跟随方法的响应上限（tools/call 40 MiB，见 S08-I14；其他方法 8 MiB），超限只断该请求。
- 新增测试：stdio 会话 deadline 设短（monkeypatch idle=0.5 s），连续调用间隔 0.3 s 不重启；在途调用超过 idle 仍完成；单行 5 MiB 结果可读，9 MiB 行断开。
- 依赖：无（process_guard 只是传参，不改）
- 兼容和风险：常驻进程更久，内存占用增加；空闲 30 分钟仍会回收。
- 工作量：小
- 影响：高
- 本轮修订：E104-72 要求单文件 25 MiB，base64 后约 34 MiB，原定 8 MiB 单行上限会挡住它；改为按方法取上限（tools/call 40 MiB，其他 8 MiB）。

## S08-I5 MCP 超时默认值、上限与超时/错误文案
- 问题：调用超时默认 30 s、钳在 1-120 s，启动默认 10 s、钳在 1-60 s（`mcp_transport.py:223-224,667-668`；`store.py:455-456,1005-1006,2315-2316,2362-2367,2965-2966`；`imports.py:1038-1045,1089-1090`；界面 `AgentExtensions.tsx:3354-3355,4336,4350`、`agentExtensions.ts:785-786`）。慢工具必超时。JSON-RPC error 的 message 被丢掉（`mcp_transport.py:449-473,1006-1034`），超时错误不带阶段和秒数。Codex 默认启动 30 s、调用 300 s（`codex-mcp/src/rmcp_client.rs:122-123`），并取服务器与请求的较小值（`codex-mcp/src/binding.rs:390-404`）。HTTP 非 2xx 只给状态码，不读响应体（`mcp_transport.py:899-906`）。
- 涉及行：05-M42, 26-M62, 26-M81, 27-M1, 27-M2, A13-N2, B06-1, B05-26, 28-M87, 28-M109
- 本轮漏项：E105-8
- 二轮漏项：G21-21, G22-20
- 要改：
  - 新增常量 `DEFAULT_STARTUP_TIMEOUT_MS=30000`、`MAX_STARTUP_TIMEOUT_MS=300000`、`DEFAULT_TOOL_TIMEOUT_MS=300000`、`MAX_TOOL_TIMEOUT_MS=3600000`，放 `store.py` 顶部；`store.py:455-456,1005-1006,2315-2316,2364,2367,2965-2966`、`imports.py:1040,1044,1089-1090`、`mcp_transport.py:223-224,667-668` 全部改用常量。
  - `mcp_transport.py:1811-1826` McpSessionManager.call_tool —— 新增 `timeout_ms` 参数，取 min(服务器, 请求)；adapter.execute 暂不传（为以后工具自报超时留口）。
  - `mcp_transport.py:449-473,1006-1034` —— error.message 清洗（去控制字符）截 500 字放进 McpTransportError.details；`adapter.py:1303-1314` 转换处把 details 放进 AgentToolRejected/AgentError 的 details，模型可见（`loop.py:1859-1862` 已带 details）。
  - `mcp_transport.py:361,712,977-985,1107-1109` 超时抛错处带 details={phase, timeout_seconds, method}。
  - `webapp/src/screens/AgentExtensions.tsx:3196,3199,3354-3355,4336,4350` 与 `webapp/src/domain/agentExtensions.ts:785-786` 默认值和上限同步。
  - `mcp_transport.py:899-906` _exchange —— 非 2xx（401/403 除外）在 `_watch_response` 的截止时间内读响应体前 2 KB，去控制字符、遮蔽 Bearer/长 base64 串，截 1000 字放 details.http_body，与状态码一起进入工具结果。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_transport.py:555-575`（stdio）与 `:1120-1145`（HTTP）list_all_tools —— 每页超时沿用 startup_timeout（本项已把默认提到 30 s），另加整段上限 10×startup_timeout（Codex 分页用整段超时，`codex-mcp/src/pagination.rs:13`）；列工具超时或失败时若已有 discovered_tools，走 S08-I10 的 Degraded 保留旧目录，不写 Error。
  - 二轮补：`rynmesh/agent_runtime/extensions/adapter.py:1303-1314` 读路径与 `:1561-1566` 写路径未派发分支 —— 白名单外错误码仍归一为 agent_mcp_transport_error / agent_mcp_operation_not_executed，但把原始码（仅 `[a-z0-9_]{1,80}` 形式，否则记 other）与 McpTransportError 的 phase、http_status 放进 details.cause，台账 error_result 同样记录；不含参数与凭据。
- 新增测试：store 默认值与钳制；imports 导入 tool_timeout_sec=900 得 900000；HTTP 假服务器 rpc error message 进入工具结果 details；超时结果带 timeout_seconds。前端 vitest：表单默认 30000/300000。；HTTP 500 带文字错误体，工具结果 details 含状态码与截断后的正文、不含 Authorization 值；二轮：两页 tools/list 各耗时 20 s、启动超时 30 s 时发现成功；已有目录的服务器列工具超时后工具仍可用（Degraded）；白名单外码 agent_mcp_foo 的工具结果 details.cause=agent_mcp_foo
- 依赖：无
- 兼容和风险：已存的服务器记录保留原值（30000 仍有效），只影响新建/导入；可在迁移里把"等于旧默认 30000 且未被主人改过"的记录升到 300000——无法区分是否改过，故不迁移。HTTP 客户端超时随 tool_timeout 变长（`:689`）。
- 工作量：小
- 影响：高

## S08-I6 失败原因给模型和主人：认证重连提示、stderr 尾部、诊断接口
- 问题：stdio 服务器 stderr 接到 DEVNULL（`rynmesh/llm_package/process_guard.py:13-14`），启动失败只有错误码（`mcp_transport.py:1479-1503`）；auth_required 给模型只是码，无"请主人重连"文字（`adapter.py:1303-1314`）；界面只能看 status_message 一句话。
- 涉及行：28-M88, 26-M103, 28-M113, B05-27
- 二轮漏项：G21-25
- 涉及漏项：G21-28
- 要改：
  - `rynmesh/llm_package/process_guard.py:12-14` spawn —— 新增 `stderr` 参数（默认 DEVNULL，CLI 模型路径不变）。
  - `mcp_transport.py:264-269` —— MCP stdio 传 PIPE，新增有界读线程写 8 KiB 环形缓冲（`self.stderr_tail`），按行写 diagnostic_log，不给模型。
  - `mcp_transport.py:1479-1503` test_server —— 失败时把 stderr 尾部（脱敏、截 2 KB）与按码的恢复提示（timeout→调大启动超时；auth→重新登录；executable_not_found→检查命令）存入服务器记录新字段 `last_error_detail`。
  - `adapter.py:1303-1314` —— agent_mcp_auth_required 抛带 correction 的 AgentToolRejected："该 MCP 服务器需要重新登录，请让主人到扩展页重连"。
  - `rynmesh/agent_runtime/extensions/routes.py` 新增 GET `/api/local/agent/extensions/mcp/servers/{id}/diagnostics` 返回 last_error_detail 与 stderr 尾部；`AgentExtensions.tsx` 服务器详情加"诊断"折叠区。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_transport.py:1504-1526` test_server 的 except Exception —— 异常类名写 diagnostic_log；status_message 按类别细分：ValueError/TypeError（配置值类型错）→ agent_mcp_config_invalid，OSError → agent_mcp_process_start_failed，其余仍 agent_mcp_transport_error；last_error_detail 带类名。配置值的类型校验前移到保存时（`rynmesh/agent_runtime/extensions/store.py:2362-2367`，与 S08-I20 的超时解析同处）。
  - 转入补：`webapp/src/screens/AgentExtensions.tsx:446-452` isErrorStatus 分支 —— 不再原样显示 rawMessage：新增 `describeStatusCode(code, tx)`（与 `:121-159` describeError 共用同一码表，S08-I39 已把码表移到 i18n），把 agent_mcp_process_disconnected、agent_mcp_timeout、agent_mcp_process_start_failed、agent_mcp_executable_not_found、agent_mcp_transport_error 等转白话加"下一步"（如"检查命令是否存在、调大启动超时"）；不认识的码显示"连接失败（<码>）"，码放在 title 里。`rynmesh/agent_runtime/extensions/mcp_transport.py:1482-1503` 仍存码（机器可读），白话只在界面层。
- 新增测试：假 stdio 服务器写 stderr 后退出，test_server 结果 last_error_detail 含尾部；auth_required 调用结果带 correction；diagnostics 路由返回内容且不含密文头。；二轮：配置值类型错引发 ValueError 时状态为 agent_mcp_config_invalid 且诊断含类名；转入：vitest：status_message=agent_mcp_timeout 的 Error 状态显示含"超时"的白话与重试按钮；未知码显示通用文案并带码
- 依赖：无（process_guard 属 S01 文件，仅加可选参数）
- 兼容和风险：新增存储字段，旧记录缺省为空。stderr 可能含密钥，存前按已知 secret_env 值脱敏。
- 工作量：中
- 影响：低

## S08-I7 HTTP 重试与会话过期恢复
- 问题：`_exchange` 对所有 ≥300 状态记 dispatched=True（`mcp_transport.py:899-906`），4xx 未执行的写也变"结果未知"；无 408/429/5xx 重试；带 session_id 收到 404 不重建会话；401 刷新只覆盖 initialize/tools/list（`:870,959-967`）。Codex 重试间隔 [250,1000] ms，仅 HTTP（`rmcp-client/src/streamable_http_retry.rs:24-40`）。另外，任何已派发调用的异常都会关会话：stdio `_exchange` 的 except 分支无条件 `self.close()`（`mcp_transport.py:483-498`），服务器正常回的 JSON-RPC 业务错误（`:449-473`，phase=rpc_error）也会杀进程；manager 对 dispatched 异常一律关会话（`:1927-1935`）。
- 涉及行：26-M90, 27-M107, 27-M108, 28-M93, 28-M94, 28-M106, 28-M108, A13-N3, 28-M12
- 本轮漏项：E105-5
- 二轮漏项：G21-19
- 要改：
  - `mcp_transport.py:899-906` —— 4xx（非 408/429）记 dispatched=False；404 且带 session_id 且方法不是 initialize 时抛新码 agent_mcp_session_expired（dispatched=False）。
  - 新增 `_is_retryable(exc)`：http_status ∈ {408,429,500,502,503,504} 或连接断开且 dispatched=False。
  - `mcp_transport.py:1047-1116` initialize、`:1118-1145` list_all_tools —— 包重试，延迟 0.25 s、1 s，共 3 次，共用 startup_timeout 的 deadline，尊重 Retry-After；auth_required 不重试；tools/call 不重试。
  - `mcp_transport.py:1892-1936` McpSessionManager.call_tool —— 捕获 agent_mcp_session_expired：丢旧会话、重建并重试一次原调用（dispatched=False 才重试）。
  - `mcp_transport.py:870,959-960` —— 401 刷新重试扩到 tools/call（401 说明未执行）；403 含 insufficient_scope 不重试，记 challenge（`:771-783` 已记录）。
  - `mcp_transport.py:483-498` StdioMcpSession._exchange —— phase 为 rpc_error（服务器已正常回包）时只抛错不 close；只有进程退出、写失败、协议损坏、超时才 close。
  - `mcp_transport.py:1927-1935` McpSessionManager.call_tool —— 关会话条件改为：会话已不可用，或异常 phase 不在 {rpc_error, http_status}；业务错误后同一会话继续复用。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_transport.py:897-898` _exchange —— 只有 401，或 403 且 WWW-Authenticate 为 Bearer error="insufficient_scope" 时才走 `_auth_error`（`:771-783`，会 record_challenge 写 error=agent_mcp_auth_required）；Codex 也只对 insufficient_scope 做授权升级（`rmcp-client/src/www_authenticate.rs:67-127`）。普通 403 抛 agent_mcp_http_forbidden（tools/call 记 dispatched=False），不调 record_challenge，不污染凭据行（`rynmesh/agent_runtime/extensions/mcp_oauth.py:874`）。
- 新增测试：HTTP 假服务器：initialize 先 503 后 200 成功；tools/list 429 带 Retry-After；tools/call 404 带会话后重建成功；tools/call 400 结果标 not_executed；tools/call 401 刷新 token 后成功。；stdio 假服务器对第一次调用回 -32000 业务错误，第二次调用复用同一 pid 成功；二轮：tools/call 回无挑战头的 403 → 工具错误 agent_mcp_http_forbidden，之后同服务器其他工具不报需要登录
- 依赖：无
- 兼容和风险：重试会延长失败返回时间，受启动超时约束。dispatched 语义变化影响台账"结果未知"判断，需对照 S13 ledger 测试。
- 工作量：中
- 影响：高

## S08-I8 HTTP 重定向与请求头约束
- 问题：httpx 固定 follow_redirects=False（`mcp_transport.py:689`），307/308 直接 http_error；配置头在基础头之后合并（`:682-688`），可覆盖 Accept、Content-Type、User-Agent、Mcp-Session-Id；导入允许任意 http:// 地址（`imports.py:974-975`）。Codex 同源最多 10 跳（`rmcp-client/src/http_client_redirect.rs:24`），丢弃 16 个客户端自管头（`codex-mcp/src/agent_plugin_config.rs:19-36`）。301/302/303 同样当错误；手动添加接口把 sse 静默改成 streamable_http（`store.py:2104-2105`），而导入路径明确拒绝 sse（`imports.py:942-949`），界面添加的旧 SSE 服务器运行时才失败。
- 涉及行：27-M109, 27-M23, 28-M1, 28-M2, 26-M52, A12-N5
- 本轮漏项：E105-11, E105-18
- 二轮漏项：G20-69
- 要改：
  - `mcp_transport.py:889-906` _exchange —— 307/308 且 Location 与配置 URL 同源、请求不带 Authorization/配置头时手动重发（≤10 跳，复用 `_deadline` 剩余时间）；跨源或带凭据仍报 agent_mcp_http_error。
  - `mcp_transport.py:682-688` —— 合并 connector_headers 时跳过 accept、content-type、content-length、host、mcp-session-id、mcp-protocol-version、user-agent、connection、transfer-encoding 等（不分大小写；authorization 保留，因静态 bearer 依赖它）。
  - `imports.py:974-975` 与 `mcp_transport.py:664-666` —— 非回环地址必须 https、URL 不得含用户信息。
  - `mcp_transport.py:889-906` —— 301/302/307/308 同源时原样重发 POST 与请求体（JSON-RPC 只能 POST）；同主机 http→https 升级视为同源；303 与跨源仍报 agent_mcp_http_error，details 写 Location 主机，提示主人改地址。
  - `store.py:2104-2105` upsert_mcp_server —— transport=sse 时抛 agent_mcp_transport_sse_unsupported（与导入一致），界面提示"旧 SSE 传输不支持，请改用 streamable HTTP 地址"。
  - 二轮补：`rynmesh/agent_runtime/extensions/store.py:2132,2179` upsert_mcp_server —— args、env_vars 超过 64 项报 agent_mcp_invalid_args / agent_mcp_invalid_env_key，不再 `[:64]` 静默截断（与 S08-I20 的导入路径一致）。
- 新增测试：同源 307 跟随成功；跨源 307 报错；带 Authorization 不跟随；配置头 Content-Type 被丢弃；导入 http://example.com 被拒，http://127.0.0.1 允许。；同源 301（补尾斜杠）重发成功；http→https 同主机升级成功；303 报错；手动添加 sse 返回 agent_mcp_transport_sse_unsupported；二轮：界面手动添加 65 个参数被拒并提示上限
- 依赖：无
- 兼容和风险：已存的非回环 http 服务器在下次编辑时会被拒；运行时先只告警不拒绝，避免老配置突然失效。
- 工作量：小
- 影响：中

## S08-I9 取消发 notifications/cancelled，关闭先宽限再杀
- 问题：任务取消时 `_watch_loop` 直接 terminate stdio 进程（`mcp_transport.py:326-335`），HTTP 侧 watchdog 关响应（`:733-740`），服务器不知道请求被取消；forget_task 直接 close（`:1387-1399`）。close 立即杀进程组（`:610-621` → `process_guard.py:44-61` SIGKILL）。Codex 调用方丢弃时取消请求（`rmcp-client/src/request_cancellation.rs:83-100`）。
- 涉及行：27-M76, 28-M78, 28-M79, 28-M90, 28-M18, 28-M112
- 要改：
  - `mcp_transport.py:326-335` _watch_loop —— cancelled 分支先 `_send({"method":"notifications/cancelled","params":{"requestId":当前 id}})`，等 2 s 无响应再 terminate；超时分支同样。
  - `mcp_transport.py:733-740` _watch_response —— 取消时另发 POST notifications/cancelled（2 s 超时）再关响应。
  - `mcp_transport.py:1387-1399` forget_task —— 先对在途请求发取消并等写完，再关会话。
  - `mcp_transport.py:610-621` close —— 先关 stdin 等 2 s 再 terminate。
  - `rynmesh/llm_package/process_guard.py:44-61` terminate —— 新增 `grace=0.0` 参数：Unix 先 SIGTERM 进程组等 grace 再 SIGKILL；Windows 先等 grace 再关 Job；CLI 模型调用保持 grace=0。
- 新增测试：stdio 假服务器记录收到的 notifications/cancelled；取消后进程在 2 s 内退出；close 时服务器能在 stdin EOF 后自行退出。
- 依赖：S08-I4（会话不再因寿命被杀）
- 兼容和风险：取消返回延迟最多 2 s；process_guard 是 S01 文件，只加可选参数。
- 工作量：中
- 影响：中

## S08-I10 目录可用性：失败保留缓存工具，后台重试 Error 与 stdio 服务器
- 问题：后台刷新只覆盖 ready 的 HTTP 服务器（`mcp_transport.py:1231-1235`）；turn 前重连只对 streamable_http 且 NeedsTest（`adapter.py:658-662`）；test_server 失败写 status=Error（`mcp_transport.py:1479-1503`），adapter 只认 ready（`adapter.py:522-524`），已发现的工具全部从菜单消失，要主人手动点测试。改配置后 stdio 不自动重测。独立 MCP（非插件）失败不进不可用清单（`adapter.py:429-434` 只遍历插件）。Codex 失败启动持续重连、保留缓存目录（`codex-mcp/src/connection_manager/tool_catalog.rs:386-400`）。
- 涉及行：26-M107, A12-N6, 27-M30, 43-M29, 26-M113
- 二轮漏项：G22-15, G27-53
- 要改：
  - `mcp_transport.py:1219-1252` refresh_tool_catalogs —— 也收 status=Error 与 stdio 服务器，按服务器指数退避（30 s 起，上限 10 分钟）后台重试，成功写 ready。
  - `mcp_transport.py:1479-1503` test_server 失败分支 —— 若原有 discovered_tools 且错误为传输类（timeout/process_disconnected/transport_error），状态写 `Degraded`，保留工具并记 last_error。
  - `adapter.py:522-524` _active_mcp_tools —— 接受 Degraded；`adapter.py:436-446` plugin_catalog 的 state 映射加 Degraded→service_unreachable 提示但仍可用。
  - `adapter.py:658-662` refresh_catalogs_for_turn —— 放宽到 stdio 与 Error。
  - `store.py:2046` upsert_mcp_server 改配置后，由 routes 触发后台 test_server（HTTP 已有，stdio 补上），成功前保留旧工具表。
  - `adapter.py:686-722` plugin_overview_for_model —— 追加独立 MCP 服务器（plugin_id 为空）的非 ready 项。
  - 二轮补：`rynmesh/agent_runtime/extensions/adapter.py:676-677,684-685` refresh_catalogs_for_turn —— except 分支写 diagnostic_log（只记 server_id 与错误码），不再静默 pass。
  - 二轮补：`rynmesh/agent_runtime/extensions/adapter.py:658-662` —— 原计划"放宽到 stdio 与 Error"改为：回合起点只等"已有工具表、正在刷新"的服务器，总等待仍是现有的 TURN_CATALOG_WAIT_SECONDS=3.0（`rynmesh/agent_runtime/extensions/adapter.py:625,651`）；stdio 重连与 Error 重试只由后台按上面的退避进行，回合起点不在调用线程里起进程、不等它们。
  - 二轮补：3 s 后仍在刷新的服务器，在本回合的工具菜单状态里标 `catalog_stale`（`rynmesh/agent_runtime/loop.py:216` `_menu` 返回的 menu_state，S06 字段），工具沿用上次的表。
- 新增测试：服务器先 ready 后 test 失败，工具仍在 specs 且标 Degraded；Error 服务器 30 s 后后台重试成功恢复 ready；独立服务器 Error 出现在 unavailable_plugins。；二轮：一个 stdio 服务器启动要 20 s，新回合首个模型请求在 3.5 s 内发出且该服务器工具标 catalog_stale；刷新抛异常时 diagnostic_log 有记录
- 依赖：S08-I11（避免后台刷新使在途调用过期）
- 兼容和风险：新状态值 Degraded，界面 `agentExtensions.ts` 需认；后台重试增加少量网络/进程开销。
- 工作量：中
- 影响：中
- 本轮修订：盲查 G27-53 指出原方案把回合前重连放宽到 stdio 与 Error 会拖慢每个新回合的首个请求。核对 `rynmesh/agent_runtime/extensions/adapter.py:625,651` 已有 3 s 总等待，风险在于放宽后要等进程启动；改为 stdio/Error 只走后台退避，回合起点不等。

## S08-I11 过期调用只按本工具定义判定
- 问题：任何发现都会分配新 revision（`store.py:2483`），而绑定比较含服务器整体 revision（`adapter.py:864-878`；`mcp_transport.py:1612-1625,1680-1686`）。后台刷新或另一工具变化后，已批准/已排队的调用报 agent_tool_definition_changed。另外，调用期间看门线程每 50 ms 调一次取消检查（stdio `mcp_transport.py:327`，HTTP `:734`），manager 的 `_call_cancelled`（`:1867-1890`）每次都走 `_revalidate_tool_call` → `store.get_mcp_server_internal`，后者在 file_transaction 里读整份扩展存储（`store.py:1379-1380`），每个在途调用持续抢文件锁。
- 涉及行：26-M63, 26-M77, 27-M33
- 涉及漏项：J05-12
- 本轮漏项：E105-15
- 二轮漏项：G21-14, G21-24, G20-40
- 要改：
  - `adapter.py:864-878` verify_binding —— 比较键去掉 revision，保留 server_id、original_tool_name、instance_id、config_generation、schema_sha256、effective_effect、plugin_id、plugin_revision、computer_access。
  - `mcp_transport.py:1601-1625` _revalidate_tool_call、`:1830-1837,1854-1863,1875-1888` —— 不再用 expected_revision 调 get_mcp_server_internal，只比 instance_id、config_generation 与工具 schema hash；session 复用判定（`:1680-1686,1765-1771`）去掉 revision。
  - `adapter.py:1289-1300` 及写路径同处 —— 继续传 expected_revision 但只作日志。
  - `mcp_transport.py:1867-1890` _call_cancelled —— 先比扩展存储的变更戳（文件 mtime/size 与内存缓存 id，同 `adapter.py:475-511` _mcp_tools_cache_key 的做法），没变就返回上次结论，变了才 `_revalidate_tool_call`；`:1580-1597` _make_session_lifetime_cancelled 同样处理。看门间隔不变，开销降为内存比较。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_transport.py:1595-1596` _is_session_revoked 与 `:1889-1890` _call_cancelled 的 `except BaseException: return True` —— 只有确定的配置变化（服务器删除/禁用、agent_stale_revision、instance/generation 不符等 AgentError 码）才当已取消；文件锁超时、agent_state_unreadable 等读取异常沿用上次结论，连续失败超 30 s 才结束调用，码为 agent_mcp_state_unreadable（不是 agent_cancelled），写 diagnostic_log。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_transport.py:1829-1842` call_tool 调用前校验失败 —— 只有 instance_id/config_generation 变化、服务器被删或禁用时才 `invalidate_server`；工具被禁用、schema hash 变化、revision 冲突只拒本次调用，不关其他任务的会话。
  - 二轮补：`rynmesh/agent_runtime/extensions/views.py:242-253,289-300` 插件视图与 tool-query 的 cancelled() —— 与 _call_cancelled 同法：先比扩展存储与 OAuth 文件的变更戳，没变返回上次结论；before_dispatch 与调用后的 recheck 仍完整执行。
  - `rynmesh/agent_runtime/extensions/mcp_transport.py:283-291` Stdio `_is_cancelled` 与 HTTP 的 `_is_cancelled`（`:694-698`，看门狗 `:735` 调用）（J05-12）—— `except Exception: return True`：调用方传入的 `cancelled()`（`runtime.py:3306` 起，每次 `store.read_control`）抛读取异常（Windows 文件锁约 10 秒拿不到，`file_transactions.py:37`）时不再当已取消。沿用上次结论，连续 30 秒读失败才结束调用，码 `agent_mcp_state_unreadable`（同上一条二轮补）。
  - 同两处 —— 看门狗线程（`:325-333`）和等待线程（`:709,735`）各按 50 ms 查一次，3 分钟导出要做上万次加锁读。`_is_cancelled` 加 0.25 秒结果缓存（`time.monotonic()`），两个线程共用一份结论；取消延迟上限 0.25 秒。
  - `rynmesh/agent_runtime/runtime.py:3306-3318` 调用方 `cancelled()` 的 `store.read_control` —— 套用本项变更戳办法：先比任务存储文件的 mtime/size，没变返回上次结论，变了才读并解密深拷贝。
- 新增测试：工具 A 已排队，服务器后台刷新只改工具 B，A 仍执行；A 的 schema 变化则拒绝。；一次 5 s 调用期间配置不变时 get_mcp_server_internal 调用次数 ≤2；中途禁用服务器仍在 0.1 s 内取消；二轮：_call_cancelled 中 get_mcp_server_internal 抛一次锁超时，调用不被取消；禁用工具 A 后另一任务对同服务器工具 B 的会话 pid 不变；插件视图 5 s 查询期间 store 读取 ≤2 次；F3 J05-12：一次 5 秒的调用期间 `store.read_control` 调用数 ≤ 25；`cancelled()` 抛一次 OSError，调用不被取消；连续失败超 30 秒才以 agent_mcp_state_unreadable 结束；任务被取消仍在 0.3 秒内生效。
- 依赖：无
- 兼容和风险：台账里 extension_binding 仍含 revision，旧记录对账不受影响。放宽后安全性依赖 schema hash 与 config_generation，已足够。
- 工作量：小
- 影响：中
- 本轮修订（F3 J05-12）：补该漏项，见上面带 J05-12 的要改条目。

## S08-I12 MCP 输入 schema 规范化与压缩
- 问题：发现时 schema 原样存（`store.py:2442-2445`，非 dict 才换 `{"type":"object"}`），adapter.specs 原样发给模型（`adapter.py:583-590`）。请求前逐个 `validate_tool_schema`（`rynmesh/llm_package/chat.py:65-83,242`），一个第三方工具的 schema 不合规（布尔 schema、坏 $defs、未知 type、外部 $ref）会让整次模型请求失败。大 schema 不压缩，吃上下文。Codex 先清洗、剪未引用定义、超 5000 字节压缩（`tools/src/json_schema.rs:37-63`，四级压缩 `tools/src/json_schema/compaction.rs:22-40`）。
- 涉及行：11-M20, 11-M21, 11-M22, 11-M23, 11-M24, 11-M25, 11-M26, 11-M28, 11-M31, 11-M32, 11-M34
- 本轮漏项：E104-75
- 要改：
  - 新增 `rynmesh/agent_runtime/extensions/schema_normalize.py`：`sanitize(schema)`（布尔 schema→对象，const→enum，按 properties/items/enum 线索补 type，只认 7 种 type 名，未知结构→`{}`，object 缺 properties 补 `{}`，array 缺 items 补 `{"type":"string"}`，$defs/definitions 非对象整表删除）；`prune_unreferenced_defs`；`compact(schema, max_bytes=5000)` 四级（删描述→本地 $ref 改 `{}` 并删定义→深度≥3 复杂节点改 `{}`→删 anyOf/oneOf/allOf）；顶层 type 不是 object 时返回 None（工具不可用）。
  - `store.py:2442-2445` record_mcp_discovery —— 存原 schema 为 `input_schema`（参数校验用），另存 `model_schema`（规范化+压缩后），`schema_sha256` 仍按原 schema；顶层不合规的工具记 `schema_invalid=True`；保留 `outputSchema` 为 `output_schema`。
  - `adapter.py:575-603` specs —— 发 `model_schema`；对它先跑 `validate_tool_schema`，不合格跳过该工具。`adapter.py:530-569` _active_mcp_tools 跳过 schema_invalid 的工具。
  - `adapter.py:812-828` validate —— 仍按原 schema 校验参数（压缩后的 `{}` 不约束）。
  - 插件页（`AgentExtensions.tsx` 工具列表）对 schema_invalid 显示"schema 不合规，未提供给模型"。
  - `adapter.py:827-828` validate —— 去掉重复的 `jsonschema_validate`，只保留 `validate_tool_arguments`（Draft 2020-12，`rynmesh/llm_package/chat.py:86-90`）。
- 新增测试：`tests/test_agent_mcp_schema_normalize.py`：布尔 schema、const、缺 type、坏 $defs、未知 type、20 KB schema 压到 ≤5000 字节、未引用定义被剪；集成：一个坏 schema 工具不影响其他工具进菜单，整次请求不失败。；参数校验只执行一次（mock 计数），错误码不变
- 依赖：无
- 兼容和风险：新增存储字段；旧记录缺 model_schema 时 specs 现场计算一次。schema_sha256 不变，旧绑定仍有效。code mode 若读 output_schema 属 S11。
- 工作量：中
- 影响：高

## S08-I13 工具描述截断放宽，服务器 instructions 交给模型
- 问题：发现时描述截到 1000 字符（`store.py:2451`，签名同截 `mcp_transport.py:50`），发给模型时再与前缀合并截 600（`adapter.py:591-598`），服务器写在描述后半段的用法规则到不了模型；initialize 返回的 instructions 被丢弃。Codex 普通 MCP 不截，插件 MCP 截 1000 字节（`tools/src/mcp_tool.rs:9-36`）。
- 涉及行：09-M1, 09-M2, 10-M73, 11-M33, A12-N1, 27-M12
- 要改：
  - `store.py:2451` 与 `mcp_transport.py:50` —— 存储上限放到 16 KB（同时改签名），超出加"…（描述已截断）"。
  - `adapter.py:591-598` specs —— 插件服务器（plugin_id 非空）描述按字符边界截 1000 字节，主人自配服务器截 4000 字节；`[服务器名] ` 前缀单独拼接不占额度；描述为空时用"Tools from <服务器名>."。
  - `mcp_transport.py:1477-1478,1528-1540` test_server 与 `:1254-1273` _refresh_catalog —— 把 `session.server_info.get("instructions")`（截 4 KB）传给 record_mcp_discovery 存为 `server_instructions`。
  - `adapter.py:724-761` plugin_context_note 不放 instructions（保持前缀稳定）；ryn_discover_tools 返回某服务器工具时附一次 server_instructions（`loop.py:2036-2042` 结果拼装处，S06 相关，按 server 去重）。
- 新增测试：3000 字节描述完整进入主人自配服务器的 spec；插件服务器截 1000 字节；instructions 存储与 discover 结果附带。
- 依赖：S06（discover 结果格式与菜单容量）
- 兼容和风险：菜单字节增加，需在 tool_menu 容量计算里核对；已存描述只在下次发现时变长。
- 工作量：小
- 影响：中

## S08-I14 MCP 结果规范化：截断有标记，坏块不整次失败
- 问题：`_normalize_mcp_result` 只看前 64 块（`adapter.py:1028`），每块截 32000 字符（`:109,1033,1144`），结果无截断标记；未知块类型（audio、resource_link）静默丢弃；只读路径一张坏图片让整次失败（`:1105-1107`，`:1321` allow_image_rejection=True）；resource blob 非白名单 MIME 或多个 blob 抛 agent_mcp_artifact_invalid（`:1121-1131`）。单响应上限 4 MiB（`mcp_transport.py:62`）。results.save 超 2 MiB 报错（`rynmesh/agent_runtime/results.py:14,143-146`）。Codex 逐项转 JSON，失败项换占位（`codex-mcp/src/binding.rs:471-486`）。单个 blob 上限 2 MiB（`adapter.py:1124,1130`）。
- 涉及行：26-M64, 27-M72, A12-N2, 27-M105, 25-M30, 25-M32
- 本轮漏项：E104-72
- 二轮漏项：G22-1, G22-2
- 要改：
  - `adapter.py:1028-1037` —— 截断时 normalized 加 `truncated: {blocks_total, chars_original}`；超长文本整段交 results 分页（ryn_read_result），不丢。
  - `adapter.py:1028-1147` —— 未知 b_type 转 `{"type":"text","text":"[<type> 内容未展示]"}`；resource_link 显示 uri 与 name。
  - `adapter.py:1105-1107,1321` —— 只读路径 allow_image_rejection 改 False（坏图换占位块，文本照常返回）。
  - `adapter.py:1119-1131` —— 非白名单 MIME 存为 `.bin` 通用文件；多个 blob 逐个存（上限 8 个）；不再整次失败。
  - `mcp_transport.py:62` —— 单响应上限 8 MiB。
  - 服务器记录新增 `max_output_chars`（默认 32000，`store.py:2361` _apply_mcp_policy_fields 可配），替代 MAX_TEXT_BLOCK_CHARS 常量（`adapter.py:109`）。
  - `results.py:143-146` save（S02 文件）—— 超限时存前 2 MiB 并标 truncated，不报错。
  - `adapter.py:1124,1130` —— 单文件解码后上限放到 25 MiB，扩展名表加 docx、pptx、zip、png、jpeg；超限只跳过该文件并给占位文本，不整次失败。为容纳 base64 后约 34 MiB，tools/call 单响应上限定 40 MiB（`mcp_transport.py:62` 拆成按方法的上限，其他方法 8 MiB；S08-I4 的行上限随之）。
  - 二轮补：`rynmesh/agent_runtime/extensions/adapter.py:157-158,196-200` _strip_raw_images —— 列表上限从 200 放到 5000，超出时末尾追加 `{"_truncated": true, "omitted_items": n}`；深度超 12 的节点换成字符串标记 "[深度超限已省略]"，不再变 None；函数返回截断计数。
  - 二轮补：`rynmesh/agent_runtime/extensions/adapter.py:187-189` —— data/base64 类键的长字符串只有 `_looks_like_image_base64`（`rynmesh/agent_runtime/extensions/adapter.py:128`）判为图片或整串是合法 base64 时才置空（计入 omitted_binary）；普通长文本（CSV、报表正文）按 max_output_chars 截断并加 "...[已截断 N 字符]"。
  - 二轮补：`rynmesh/agent_runtime/extensions/adapter.py:1154-1160` —— structuredContent 有任何截断或省略时不丢文本块（两者都给模型），normalized 加 `structured_truncated: {omitted_items, depth_cut, omitted_strings}`；没有截断时维持只给一份。
- 新增测试：70 块结果带 truncated 标记；10 万字符文本可经 ryn_read_result 翻页读全；audio 块变占位；只读工具一张坏 PNG + 文本，文本返回；未知 MIME blob 存为 .bin。；20 MiB docx blob 存盘成功；30 MiB blob 被跳过并有占位，其余内容照常返回；二轮：structuredContent 含 500 行列表 → 模型拿到全部 500 行，6000 行时有 omitted_items；data 键下 40000 字 CSV 保留前段并有截断标记；结构化被截断时文本块保留
- 依赖：S02（results.save 截断存储）
- 兼容和风险：结果结构多字段，界面忽略未知字段即可。写工具路径的图片校验保持严格。
- 工作量：中
- 影响：高

## S08-I15 工具目录细节：可见性、命名、重名、分页上限
- 问题：`_meta.ui.visibility` 不含 model 的工具仍给模型（发现时 `store.py:2446-2461` 不存 _meta）；工具名固定切 18/24 字符（`store.py:221-232`），短服务器名时工具名被无谓截断；重名工具不跳过；分页上限 16 页、256 工具（`mcp_transport.py:63-64`），游标重复静默截停（`:572-573,1141-1142`）。Codex 100 页、2048 项（`codex-mcp/src/pagination.rs:9-13`），visibility 规则见 `codex-mcp/src/connection_manager/tool_catalog.rs:41-65`。
- 涉及行：26-M105, 26-M145, 27-M63, 26-M155, 27-M9
- 二轮漏项：G21-20
- 要改：
  - `store.py:2446-2461` —— 保存 `visibility`（来自 `_meta.ui.visibility`）；`adapter.py:530-541` _active_mcp_tools 跳过 visibility 存在且不含 "model" 的工具。
  - `store.py:221-232` compute_model_tool_name —— s_slug/t_slug 按 64 字符剩余预算分配，哈希后缀不变；只用于新发现的工具，已有工具沿用存储里的 `model_tool_name`（`store.py:2459`）。
  - `store.py:2438-2462` —— 同名工具只保留第一个并记 warning。
  - `mcp_transport.py:63-64` —— MAX_TOOL_PAGES=100、MAX_DISCOVERED_TOOLS=2048；`:572-573,1141-1142` 游标重复改为报 agent_mcp_protocol_invalid。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_transport.py:567-569,1138-1140` —— 超过 MAX_DISCOVERED_TOOLS 时保留前 2048 个，不再整台报 agent_mcp_tools_limit；discovery 结果带 `catalog_truncated: {kept, reason: "tools_limit"}`，页数用尽记 reason="pages_limit"；record_mcp_discovery 存入服务器记录，界面服务器详情显示"工具过多，只加载前 N 个"。Codex 超限是报错（`codex-mcp/src/pagination.rs:9-13`），Ryn 改截断以免整台不可用。
- 新增测试：visibility=["app"] 的工具不进 specs；短服务器名 + 40 字符工具名完整保留；重名工具第二个被跳过；300 个工具分 3 页全部发现。；二轮：2100 个工具的服务器 → 2048 个可用且带 catalog_truncated
- 依赖：无
- 兼容和风险：`adapter.py:542-544` 与 `store.py:919` 有两处现场重算名字，须统一读存储值，否则新旧名不一致。
- 工作量：小
- 影响：低

## S08-I16 MCP 资源工具（列资源、列模板、读资源）
- 问题：两类会话只调 tools/list（`mcp_transport.py:541-576,1118-1145`），没有 resources/list、resources/templates/list、resources/read。提供文档/数据资源的服务器在 Ryn 里用不上。Codex 有三件套且可并行（`core/src/tools/handlers/mcp_resource/list_mcp_resources.rs:25-35`）。
- 涉及行：09-M14, 09-M15, 09-M16, 09-M17, 09-M18, 09-M19, 09-M20, 09-M21, 09-M22, 09-M23, 09-M24, 10-M76, 26-M66, 26-M99, 26-M159, 27-M39
- 要改：
  - `mcp_transport.py` StdioMcpSession / StreamableHttpMcpSession —— 在 list_all_tools 旁加 `list_resources(cursor)`、`list_resource_templates(cursor)`、`read_resource(uri)`，分页同 tools/list；McpSessionManager 加 `list_all_resources(server_id|None)`，跨服务器汇总，单服务器失败只告警；结果按服务器 revision 缓存，收到 notifications/resources/list_changed（`:429-440` 分派处）或配置变化时失效。
  - `store.py:2392` record_mcp_discovery —— 记录 initialize 返回的 `capabilities.resources` 为 `supports_resources`。
  - `adapter.py:575-603` specs —— 有 supports_resources 的就绪服务器时加 `ryn_mcp_list_resources`、`ryn_mcp_list_resource_templates`、`ryn_mcp_read_resource`；`adapter.py:772-810` metadata 给 effect=read、supports_parallel；`adapter.py:1192` execute 新分支：server/cursor 去空白、空串当缺省；无 server 却给 cursor 回 correction；read 缺 server/uri 回 "<field> must be provided"；未知服务器回 agent_mcp_server_unknown 并列出可用名；不给 server 时聚合、每项带 server、按名排序、不分页；结果走 `_normalize_mcp_result` 与 results 分页；blob 存工作区文件；按 `_check_data_policy`（`:605-618`）校验。
  - 工具描述写明：服务器提供的文档/数据先读资源再考虑网页搜索；server、uri 必须与 list 返回一致；模板是参数化地址。
- 新增测试：`tests/test_agent_mcp_resources.py`：假服务器 3 个资源 2 页；不带 server 聚合；带 cursor 不带 server 回 correction；read text 与 blob；list_changed 使缓存失效；不支持资源的服务器不出现工具。
- 依赖：S06（内置工具进菜单与并行调度）
- 兼容和风险：新增三个工具，只在有资源能力的服务器时出现。
- 工作量：大
- 影响：中

## S08-I17 每服务器/每工具审批模式（非自动档）
- 问题：MCP 工具只有"主人标只读 / 否则写"二值（`adapter.py:545-547`；`store.py:899-932`），workspace、confirm_changes 档下所有非只读 MCP 工具每次都问（`rynmesh/agent_runtime/execution_policy.py:239-275`）。destructiveHint/openWorldHint 存了但不参与判定。没有"始终批准"或"每次都问"。Codex 有 Auto/Prompt/Writes/Approve（`core/src/mcp_tool_call.rs:2595-2626`）。插件页面查询页的"确认只读页面工具"按钮把服务器全部只读候选一次性设为只读（`webapp/src/screens/PluginViews.tsx:106-111` 提交 `hintCandidates.map(...)`），主人点一次就放开了没看过的工具。
- 涉及行：05-M28, 05-M35, 25-M31, 27-M55, 29-M6, B06-3, B07-30
- 本轮漏项：E111-13
- 要改：
  - `store.py:455-460` 默认值、`store.py:2361-2391` _apply_mcp_policy_fields —— 服务器加 `default_tools_approval`（ask/approve，默认 ask）与 `tool_approvals: {name: ask|approve|prompt}`；导入配置里的值仍忽略（保持 `imports.py:1063-1067` 的原则：只有本机主人可设）。
  - `store.py:899-932` _public_mcp_server —— 工具视图带 effective approval。
  - `adapter.py:545-568` _active_mcp_tools 绑定加 `approval_mode`；`adapter.py:799-810` metadata 输出 `approval_mode`。effect 仍按已确认只读决定台账（approve 的工具仍是 write，留预览和台账）。
  - `execution_policy.py:239-275` decide（S04 文件）—— workspace/confirm_changes 档：approval_mode=="approve" 时返回 execute（reason="owner_approved_tool"）；"prompt" 时"本任务/永久"批准降为单次。automatic 档不变；read_only 档仍拒绝。
  - `AgentExtensions.tsx:3058-3139` 工具行加审批下拉；`webapp/src/screens/RynAgentReadOnlyHints.tsx:44-63` 横幅展开为工具清单逐个勾选，dismiss 状态改存后端（服务器记录 `hint_dismissed_revision`）。
  - `PluginViews.tsx:106-111` handleConfirmReadOnly —— 只提交 `view.tool` 对应的那一项候选；其余候选仍在连接详情里逐个确认。
  - 服务器自报只读的工具在自动处理档的并行与出错处理见 S08-I42（本项只管非自动档的审批模式）。
- 新增测试：execution_policy：confirm_changes + approve 工具直接执行；prompt 工具"永久批准"降为单次；automatic 不受影响；导入配置声明 approve 被忽略。前端：下拉保存。；页面确认只读后，read_only_tools 只多出该页面的工具
- 依赖：S04（decide 新分支）
- 兼容和风险：新增字段，旧记录默认 ask，行为不变。界面需说明 approve 等于不再确认。
- 工作量：中
- 影响：中

## S08-I18 插件重导入继承设置，清理旧版本
- 问题：commit_imported_items 每次把 MCP 服务器重置为 enabled=False、read_only_tools=[]（`store.py:2949-2985`），只对三个公司插件 id 的"完全相同包"恢复启用（`:3027-3045`）；插件升级后主人要重新启用、重标只读。旧 package_dir 不删，新包已没有的技能/服务器残留。插件记录不记来源。Codex 保留插件 MCP 用户策略（`core-plugins/src/loader.rs:1001-1022`）。
- 涉及行：29-M74, A13-N5, 31-M6, 30-M104, 30-M46
- 要改：
  - `store.py:2949-2985` —— 同 id、同 plugin_id 的旧记录存在时继承 enabled、explicitly_disabled、read_only_tools/read_only_sources、tool_timeout_ms、tool_approvals（S08-I17）；只读继承的工具在下次发现时按 schema 比对（`store.py:2464-2476` 的校验扩到 owner 来源：schema 变了就撤销）。
  - `store.py:2987-3045` 插件循环 —— 来源相同时继承插件 enabled（不限公司 id）；删除旧记录里 plugin_id 相同、新包已没有的技能和服务器；提交后 `_safe_rmtree`（`:3093-3099`）旧 package_dir（仍被引用时跳过）。
  - 插件记录加 `source`（type、path 或 url、ref、revision），由 `imports.py:633` commit 从 preview payload 传入。
  - 本轮补：提交新版本时不删旧 package_dir，而是记为插件的 `previous_package_dir` 与 `previous_version`（只留一代），再旧的才 `_safe_rmtree`，供 S08-I23 回滚。
- 新增测试：导入插件 → 启用、标只读 → 重导入新版本（schema 不变）→ 仍启用、只读保留；schema 变化的工具只读被撤；新包删掉的技能被移除；旧目录被删。；连续导入 v1→v2→v3 后磁盘只留 v2、v3 两个包目录
- 依赖：无
- 兼容和风险：改变"重导入一律先停用"的默认——只在来源相同时继承，来源不同仍停用；提交预览写明"将保留原设置"。
- 工作量：中
- 影响：中
- 本轮修订：原写"提交后删旧 package_dir"，会让回滚（E105-42）无从做起；改为保留上一代。

## S08-I19 保留公司插件 id，第三方包不能覆盖
- 问题：preview 只拒绝 computer-use id（`imports.py:510-523`），第三方 zip 声明 company-workspace / company-dashboard / company-affiliate 也能预览并提交，覆盖公司插件记录（`store.py:2987-3045`）；OAuth 对这三个 id 有特殊处理（`mcp_oauth.py:185,648-654` 设备标识）。Codex 保留名不能从其他来源加载（`core-plugins/src/marketplace_policy.rs:107-157`）。
- 涉及行：A14-N3, 30-M26
- 要改：
  - `imports.py:510-523` preview —— 插件 id 在公司保留名中且来源不是签名公司包时 can_import=False 并给警告；`company-` 开头的技能和服务器 id 同样处理。
  - `store.py:2824` commit_imported_items —— 加 `trusted_company=False` 参数，非受信写这些 id 时抛 agent_plugin_reserved_id；公司插件自动更新路径传 True。
  - 本轮补：保留 id 名单不再在核心写死，改由 S08-I50 的签名包 `authProvider.reserved_ids` 声明；过渡期保留现有三个 id 作兼容默认。
- 新增测试：zip 声明 company-dashboard 预览 can_import=False；受信路径照常写入。
- 依赖：无
- 兼容和风险：主人手动导入公司插件 zip 的旧做法会被拒，需走签名包；提示里写明。
- 工作量：小
- 影响：低
- 本轮修订：E105-24 指出公司细节写死在核心；本项原计划在核心再加一层写死的保留名，改为读插件声明（S08-I50）。

## S08-I20 插件包导入兼容与容错
- 问题：只认 .codex-plugin 与 .claude-plugin（`imports.py:1226-1227`）；mcpServers 一条出错整个插件失败（`:898-899` 非 dict 抛错，`:956-957` 未知 transport 抛错）；`$schema` 只判非空（`:332-334`）；env 值 `${X}` 原样当字面值（`:980-987`）；claude 格式技能只看直接子目录（`_collect_skills_from_path` `:395-441`）；不同插件同名技能 id 互相覆盖（`:1391-1418` 只在本包内去重）；interface.defaultPrompt 不读。导入配置输入校验不严：`startup_timeout_sec`/`tool_timeout_sec` 为小数或字符串时 `int()` 抛未捕获 ValueError（`imports.py:1038-1045`，同类 `store.py:2363,2366`）；配置与 SKILL.md 用严格 utf-8 读取（`imports.py:818,826,1136,1156,1394,1469`），带 BOM 的文件 JSON 解析失败；args 与 env_vars 超 64 项静默截断（`:966,994`）。
- 涉及行：16-M72, 26-M49, 26-M156, 29-M33, 49-M127, 29-M37, 29-M73, 16-M68, 29-M111
- 本轮漏项：E105-45
- 二轮漏项：G21-47
- 要改：
  - `imports.py:1226-1227` —— 加 `.cursor-plugin/plugin.json` 作第三候选。
  - `imports.py:897-1096` _parse_mcp_config_text —— 每条服务器单独 try：AgentError 记 warning 并跳过，不让整包失败。
  - `imports.py:324-337` _is_recognized_portable_manifest —— 按 $schema 前缀分流：已知版本放行，同前缀其他版本 warning "不支持的版本"，无 $schema 保持兼容。
  - `imports.py:980-987` —— env 值为 `${X}` 时把 X 记入 env_vars 透传并删除字面值。
  - `imports.py:1359-1364` —— claude_plugin 格式改为递归收集（深度≤4），Ryn 原生格式保持直接子目录。
  - `imports.py:1391-1418` 与 `store.py:2852-2856` —— 插件技能 id 与另一 plugin_id 的已装技能冲突时自动改名为 `<plugin_id>-<skill>` 并给 warning，不再覆盖。
  - `imports.py:1290-1320` —— 读 interface.defaultPrompt（字符串或数组，≤3 条，每条≤128 字符）存入插件条目，界面在插件卡片展示。
  - `imports.py:1038-1045` 与 `store.py:2362-2367` —— 超时值用 try 解析，接受整数、小数（取整）和数字字符串，其余抛 agent_invalid_mcp_config 并指出字段。
  - `imports.py:818,826,1136,1156,1394,1469` —— 改用 utf-8-sig 读取；解码失败抛 agent_import_encoding_invalid（提示另存为 UTF-8）。
  - `imports.py:966,994` —— 超过 64 项时报 agent_mcp_invalid_args / agent_mcp_invalid_env_key，不截断。
  - 二轮补：`rynmesh/agent_runtime/extensions/imports.py:847` 与 `:1500-1501` —— SSE 条目只跳过并警告（`:942-949` 已有），不再让整个配置或插件 can_import=False：`:847` 改为 `len(servers) > 0`，`:1500-1501` 删除 sse 判定；unsupported 仍列 sse，预览里该服务器标"未导入"。
- 新增测试：`tests/test_agent_extension_imports.py`：.cursor-plugin 包；一条坏服务器 + 一条好服务器只导入好的；$schema 版本不符警告；`${TOKEN}` 透传；嵌套技能被发现；两个插件同名技能不覆盖；defaultPrompt 截断。；tool_timeout_sec="1.5" 得 1500 ms；"abc" 得 agent_invalid_mcp_config；带 BOM 的 .mcp.json 可导入；GBK 文件得 agent_import_encoding_invalid；65 个 args 报错；二轮：配置含一台 sse 与一台 stdio 服务器 → can_import=True，只导入 stdio
- 依赖：无
- 兼容和风险：改名的技能 id 只影响新导入。
- 工作量：中
- 影响：中

## S08-I21 stdio 启动环境：环境变量白名单、插件根变量、默认工作目录
- 问题：环境白名单只有 PATH、SYSTEMROOT 等 10 个（`mcp_transport.py:128-139`），缺 USERNAME、TZ、ProgramFiles、CA 证书变量，依赖它们的服务器启动失败；插件 stdio 服务器 cwd 缺省时落在公共 `mcp_runtime` 目录（`mcp_transport.py:242-244,1214`），不是插件根；只认 `${CLAUDE_PLUGIN_ROOT}`/`${CODEX_PLUGIN_ROOT}`（`imports.py:270-287`）。Codex 默认环境+CA 变量转绝对路径（`rmcp-client/src/utils.rs:16-40`），注入 PLUGIN_ROOT/PLUGIN_DATA（`codex-mcp/src/agent_plugin_config.rs:17-18`）。私有 HOME/APPDATA/LOCALAPPDATA/TEMP 是每次会话新建的一次性临时目录（`mcp_transport.py:141-151,248-250`），代理变量（HTTP_PROXY 等）、NODE_PATH、npm/uv 缓存都不透传；npx、uvx 类服务器每次启动都重新下载，需要代理才能出网的环境里直接失败。
- 涉及行：26-M51, 28-M119
- 本轮漏项：E105-6
- 要改：
  - `mcp_transport.py:128-139` _stdio_environment —— keep 加 USERNAME、USER、LOGNAME、SHELL、TERM、TZ、PROGRAMFILES、PROGRAMFILES(X86)、PROGRAMDATA、ALLUSERSPROFILE、NUMBER_OF_PROCESSORS、PROCESSOR_ARCHITECTURE；SSL_CERT_FILE、SSL_CERT_DIR、REQUESTS_CA_BUNDLE、NODE_EXTRA_CA_CERTS 转绝对路径后透传；HOME 等仍指私有目录。
  - `imports.py:270-287` _substitute_plugin_root_token —— 增认 `${PLUGIN_ROOT}`；`${PLUGIN_DATA}` 换成 store 下该插件的数据目录。
  - `imports.py:965-971` —— 插件 stdio 服务器 cwd 缺省时设为插件包根。
  - `mcp_transport.py:248-255` —— 私有临时目录改建在 `store.root/mcp_runtime`（避免写进插件包破坏哈希）；插件服务器注入 PLUGIN_ROOT、PLUGIN_DATA，secret_env 不得覆盖这两个名字。
  - `mcp_transport.py:128-139` —— keep 再加 HTTP_PROXY、HTTPS_PROXY、NO_PROXY、ALL_PROXY（大小写两种）、NODE_PATH；`npm_config_cache`、`UV_CACHE_DIR`、`PIP_CACHE_DIR` 指向每服务器持久缓存 `store.root/mcp_cache/<server_id>`。
  - `mcp_transport.py:141-151,248-250` —— HOME、USERPROFILE、APPDATA、LOCALAPPDATA 改为每服务器持久私有目录 `store.root/mcp_home/<server_id>`（跨会话保留服务器自己的配置与登录缓存，仍与主人真实 HOME 隔离）；TMP/TEMP 仍是每会话临时目录。服务器配置可选 `inherit_user_home`（仅主人可设，界面写明风险）时才用真实 HOME。
- 新增测试：环境里有 TZ、相对路径 SSL_CERT_FILE 时子进程收到绝对路径；`${PLUGIN_ROOT}/bin/x` 解析到包根；插件服务器默认 cwd 为包根，包哈希不变。；设 HTTPS_PROXY 时子进程收到；两次会话间 npm 缓存目录保留；inherit_user_home 未设时 HOME 不是真实主目录
- 依赖：无
- 兼容和风险：多透传的变量都不含凭据；CA 变量是公开路径。
- 工作量：小
- 影响：高
- 本轮修订：原方案保留一次性私有 HOME，并把私有目录挪到 `store.root/mcp_runtime`；盲查指出这让 npx/uvx 每次重下、代理丢失。改为每服务器持久私有 HOME + 持久缓存，TMP 仍每会话一次性。

## S08-I22 插件 commands 迁移成技能
- 问题：插件的 commands/ 目录整体忽略，只给警告（`imports.py:62,75`；目录检查 `:1346-1349`）。Claude/Codex 插件常把工作流写成斜杠命令，导入 Ryn 后能力丢失。Codex 迁成 `source-command-*` 技能（`core-plugins/src/command_migration.rs:14-15`）。
- 涉及行：29-M44, 29-M45, 29-M46, 29-M47, 29-M48, 29-M49, 29-M50, 29-M114, 31-M7
- 要改：
  - `imports.py` 新增 `_migrate_commands(pkg_root, plugin_id, commands_path)`：路径取清单 `commands` 字段，缺省 `commands/`；递归收集 .md 并排序（同 `_collect_skills_from_path` 的符号链接与越界检查）；名为 `source-command-<相对路径 slug>`，>64 字符、README、重名跳过；描述取 frontmatter description，没有则跳过；含 `$ARGUMENTS`、`$1`、成对 `{{ }}`、shell 内联、`@文件` 的命令跳过并给 warning；正文为标题 + 一句使用说明 + "Command Template" 小节；CLAUDE.md 改 AGENTS.md，产品名按词边界换成 Ryn；渲染后 >4000 字节丢弃；每次 preview 现生成，不落缓存。
  - `imports.py:1346-1349` —— commands 目录不再算 unsupported，改调 `_migrate_commands` 并入 skills（归属 plugin_id，默认 allow_implicit_invocation=False）。
  - `imports.py:62,75` —— 删 commands 的"不支持"文案。
- 新增测试：commands/review.md（有 description）→ 技能 source-command-review；含 $ARGUMENTS 的跳过；README.md 跳过；commands/git/commit.md → source-command-git-commit；>4000 字节丢弃。
- 依赖：S09（技能注入与隐式调用规则）
- 兼容和风险：只影响新导入；迁移技能默认不隐式注入。
- 工作量：中
- 影响：中

## S08-I23 插件 git 来源导入与检查更新
- 问题：preview 只接受 path、config_text、skill_text、zip_base64（`imports.py:480`），GitHub 上的插件要主人先下载打包。Codex 支持 git clone（可 sparse、指定 ref，`core-plugins/src/marketplace_add/install.rs:12-30`）。
- 涉及行：29-M61, 29-M62, 29-M81, 29-M119, 30-M8, 30-M9, 30-M16, 30-M40, 30-M48, 30-M41, 30-M42
- 本轮漏项：E105-42
- 要改：
  - `imports.py:478-486` preview —— 新来源 `git_url`（可带 `ref`、`subdir`），仅 kind=plugin/skill。
  - `imports.py` 新增 `_parse_git_source()`：认 https://、ssh://、git@host:、owner/repo 简写（转 GitHub https），file:// 拒绝，subdir 不得越界。
  - 新增 `_run_git(args, cwd, timeout)`：用系统 git，GIT_TERMINAL_PROMPT=0、GIT_OPTIONAL_LOCKS=0，清除 GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE 等仓库环境变量，`-c safe.bareRepository=explicit`；clone 60 s、fetch 30 s；失败把 stderr 前 2 KB 放进错误。
  - `imports.py:1196-1222` _preview_plugin —— git 来源：`clone --depth 1`（有 subdir 时 sparse）到 staging_dir，ref 为 40 位十六进制直接 checkout，否则 clone 后 `rev-parse HEAD` 记 revision；之后走目录分支（`copy_directory_safely` 与哈希校验不变）。
  - 插件记录 source 带 url、ref、revision（见 S08-I18）；`routes.py` 新增 POST `/import/check-updates`：对记录了 git 来源的插件逐个重新 preview 同一来源，revision 变了才提示，错误按插件收集不中断。
  - `AgentExtensions.tsx` 导入弹窗加"Git 地址"来源，插件详情加"检查更新"。
  - 回滚：`routes.py` 新增 POST `/api/local/agent/extensions/plugins/{id}/rollback`，用 S08-I18 保留的 previous_package_dir 走一次 commit（同样继承设置）；检查更新结果列出当前版本、新版本号与 revision，主人确认后才提交。
- 新增测试：本地裸仓库作 file 源被拒；用本地 git daemon 或 http 假仓库 clone 成功；subdir 越界被拒；无 git 可执行时报 agent_git_unavailable；检查更新一个失败一个成功。；v2 回滚到 v1 后工具表与设置恢复；无上一版时回滚返回 agent_plugin_no_previous_version
- 依赖：S08-I18（source 字段）
- 兼容和风险：依赖系统 git；clone 内容仍走现有大小与符号链接检查。网络受限时（国内）GitHub 慢，超时文案要明确。
- 工作量：大
- 影响：中

## S08-I24 插件包限额放宽与 npm 来源
- 问题：插件 zip 与技能共用 6 MiB 上限（`imports.py:48,191,1215`），带前端资源或二进制的插件导入不了；没有 npm 来源。Codex npm 包 tgz 50 MiB、解压 250 MiB（`core-plugins/src/npm_source.rs:11-12`）。
- 涉及行：30-M53, 30-M54, 30-M55, 29-M120
- 要改：
  - `imports.py:48` —— 新增 MAX_PLUGIN_ARCHIVE_BYTES=50 MiB、MAX_PLUGIN_TOTAL_BYTES=250 MiB、文件数 4096、单文件 32 MiB；`extract_zip_safely`（`:189-248`）与 `copy_directory_safely`（`:250-267`）加 `limits` 参数，插件用插件限额，技能维持现值；`:1215` 路径分支同改。
  - 前端 zip 走 base64（`AgentExtensions.tsx:4494-4504`），50 MiB 会过大：插件包大于 6 MiB 时改用 path 来源（桌面版选文件拿路径）。
  - `imports.py:478-486` preview —— 新来源 `npm_package`（+version、registry 须 https）：`npm pack <name>@<version> --ignore-scripts --pack-destination staging_dir`（Windows 用 npm.cmd，超时 120 s）；校验恰好一个 .tgz、解开后有 package/、package.json 的 name 等于请求名；解 tgz 用与 zip 同样的越界、符号链接检查（新增 `extract_tgz_safely`）。
- 新增测试：40 MiB 插件 zip 可导入、技能 zip 仍限 6 MiB；tgz 含 `../` 被拒；name 不符被拒；无 npm 时报 agent_npm_unavailable。
- 依赖：无
- 兼容和风险：store 加密存储上限 12 MiB（`store.py:35-36`）只存元数据，不受包大小影响；包目录占磁盘增大。
- 工作量：中
- 影响：中

## S08-I25 导入 staging 遗留目录清理
- 问题：staging 目录只在内存预览过期时删（`imports.py:454-469`）；进程重启后 `_previews` 为空，磁盘上的旧 staging 永远留着（`:447-452`）。预览对已装同名插件只给一句"will be updated/replaced"（`imports.py:515-536`），不展示工具、权限、网络地址的变化。
- 涉及行：30-M4, 30-M38
- 本轮漏项：E105-44
- 要改：
  - `imports.py:447-452` ExtensionImportManager.__init__ —— mkdir 后清扫 staging_root 下全部子目录（启动时都是遗弃的）。
  - `imports.py:454-469` _cleanup_expired —— 另扫磁盘上无对应预览且 mtime 超 PREVIEW_TTL_SECONDS 的目录。
  - `imports.py:515-536` preview —— 同名插件/服务器已存在时生成结构化 diff：新增/删除的技能、MCP 服务器与工具，web_app.permissions 变化，服务器 url 主机与 stdio 命令变化；新增服务器或权限扩大标为高风险。
  - `AgentExtensions.tsx` 导入确认页 —— 展示 diff，高风险项需单独勾选后才可提交。
- 新增测试：预先放一个 staging 子目录，新建 manager 后被删；运行中孤儿目录超时后被删、在用预览不删。；重导入新增一个服务器的包，preview 返回 diff 且 high_risk=true；未勾选时界面提交按钮禁用
- 依赖：无
- 兼容和风险：多进程共用同一 store 时（不支持）会误删；Ryn 节点单进程。
- 工作量：中
- 影响：低

## S08-I26 OAuth 发现与 scope：issuer 可异源、scope 优先级、invalid_scope 重试
- 问题：`_allowed_url` 要求 issuer、授权端点、令牌端点都与 MCP 资源同源（`mcp_oauth.py:494-498`，在 `:528,547` 使用），授权服务器在独立域名的正规服务连不上。scope 取并集（`:634-635`），不按优先级；元数据给的 scope 出错不会去掉重试。安装插件后不会自动发起登录。Codex scope 按显式>配置>发现取一个（`codex-mcp/src/mcp/auth.rs:161-180`）。
- 涉及行：26-M141, 28-M41, 26-M143, 27-M70, 44-M78
- 要改：
  - `mcp_oauth.py:494-498` _allowed_url —— 拆成两个检查：PRM 的 authorization_servers 里列出的 issuer 可异源但须 https、无用户信息；authorization/token/registration/revocation 端点须与 issuer 或 token 端点同源（`:547`）。trusted_origin 例外保留。
  - `mcp_oauth.py:634-635` start_login —— scope 改为：主人配置 > 质询 scope > 元数据 scopes_supported，取第一个非空的，不并集；已授予的 scope 只在增量授权时加入。
  - `mcp_oauth.py:728` complete_login —— 授权回 invalid_scope 且 scope 来自元数据时，记标志，下一次 start_login 不带 scope（界面提示"重试"）。
  - `routes.py:657` import/commit 之后 —— 对新启用的 OAuth 型插件服务器返回 `needs_login` 列表，界面提示一键登录（不自动打开浏览器）。
- 新增测试：mcp_oauth 测试：issuer 在另一 https 域名、端点与 issuer 同源 → 发现成功；端点在第三域名 → 拒绝；scope 只取配置；invalid_scope 后重试不带 scope。
- 依赖：无
- 兼容和风险：放宽同源是安全边界变化——仍要求 issuer 出现在资源服务器自己的 PRM 里，并保留 S256、iss 校验（`:539`、`:751`）。
- 工作量：中
- 影响：中

## S08-I27 OAuth 令牌保留：刷新失败不过早丢 access token
- 问题：access_token 在到期前 60 s 就刷新（`mcp_oauth.py:893`）；refresh 回 invalid_grant 直接删整套 tokens（`:915-917`）；刷新暂时失败直接报 refresh_unavailable（`:918`），即使 access token 还没真正过期。无 OAuth 元数据时（`:551-559` discovery_failed）存储的 token 也不用。令牌响应校验过严：expires_in 必须是 int、返回 scope 须是请求 scope 子集，否则 fail（`mcp_oauth.py:715-726`），登录完成后被判失败并吊销已拿到的令牌（`:813-814`）；刷新瞬时失败不重试（`:918`）；insufficient_scope 直接 auth_required（`:884-885`），不告诉主人缺什么 scope。
- 涉及行：28-M33, 28-M46, 28-M99
- 本轮漏项：E105-21, E105-23
- 要改：
  - `mcp_oauth.py:915-917` —— invalid_grant 只删 refresh_token，access token 未到期时保留并返回；到期后再要求登录。
  - `mcp_oauth.py:918` —— 非 invalid_grant 失败：先重读存储（别的进程可能已刷新），若 `expires_at > clock()` 返回旧 access token，否则 fail。
  - `mcp_oauth.py:876-893` —— 元数据缺失（row 无 metadata）但有未过期 access token 时直接返回，不刷新。
  - `mcp_oauth.py:715-726` _tokens —— expires_in 接受数字字符串与小数（取整，仍限 1..31536000），缺省 300 不变；token_type 缺省按 Bearer；返回 scope 超出请求时以实际授予为准记录并写 warning，不 fail（resource 不符仍 fail）。
  - `mcp_oauth.py:906-918` —— 刷新遇 5xx 或网络错误时在刷新锁内按 0.25 s、1 s 重试两次，再走上面的"旧令牌未过期仍用"。
  - `mcp_oauth.py:884-885` 与 login_status（`:827`）—— insufficient_scope 时返回所需 scope；`McpAuthSettings.tsx` 显示"需要追加授权：<scope>"与重新登录按钮。
- 新增测试：mcp_oauth 测试：refresh 回 invalid_grant 且 access 还剩 30 s → 返回旧 token；刷新 503 且 access 未过期 → 返回旧 token；已过期 → auth_required。；expires_in="3600" 登录成功；返回多一个 scope 登录成功并有 warning；刷新先 503 后 200 成功；insufficient_scope 时 login_status 带 scope
- 依赖：无
- 兼容和风险：存储格式不变。
- 工作量：小
- 影响：中

## S08-I28 OAuth client_secret 配置入口
- 问题：OAuth 层已能注入 client_secret（`mcp_oauth.py:686,700-713`），但 normalize_mcp_auth 不接受该字段（`store.py:332-352`），导入时显式拒绝（`imports.py:912-913`），预注册机密客户端的服务器无法配置。
- 涉及行：28-M57, A13-N4
- 二轮漏项：G21-33
- 要改：
  - `store.py:332-352` normalize_mcp_auth —— 公开配置仍不含 secret；新增 `secret_client_secret` 走 secret 字段（同 secret_headers 的加密存放与界面掩码），要求 client_id 存在。
  - `store.py` upsert_mcp_server 处理 `client_secret` 写入 secret 字段；`mcp_oauth.py:686` 从 secret 字段读取。
  - `imports.py:912-913` —— 改为接受并存入 secret 字段，给 warning"已导入机密客户端密钥"。
  - `McpAuthSettings.tsx` 登录配置加"客户端密钥"输入（掩码）。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_oauth.py:694-698` 动态注册 —— 服务器回了 client_secret，或把 token_endpoint_auth_method 改为 client_secret_basic/client_secret_post 时接受：secret 存进本项的加密 secret 字段，记录 auth_method，令牌请求（`rynmesh/agent_runtime/extensions/mcp_oauth.py:700-713`）按它注入；redirect_uris 不符仍失败；private_key_jwt 等其他方式报 agent_mcp_oauth_client_auth_unsupported。
- 新增测试：设 client_id+client_secret 后 token 请求带 Basic 认证；overview 不回显 secret；只有 secret 无 client_id 被拒。；二轮：DCR 返回 secret 与 client_secret_basic → 登录成功，令牌请求带 Basic 认证，overview 不回显 secret
- 依赖：无
- 兼容和风险：新增加密字段；导出/诊断须确认不含该值。
- 工作量：小
- 影响：低

## S08-I29 OAuth 登录无浏览器备用与失败原因
- 问题：只有回环回调（`mcp_oauth.py:245-302`）；系统浏览器打不开时 tauri 命令只回 browser_open_failed（`webapp/src-tauri/src/mcp_auth_browser.rs:41-46`），界面没有"复制链接"；回调主机只放行 localhost/127.0.0.1（`:20-29`）；失败一律显示"操作未完成"（`webapp/src/screens/McpAuthSettings.tsx:58,110`），scopes 与回调端口不能改（`:115-125`）。公司账号状态每 1 s 轮询一次且无退避（`McpAuthSettings.tsx:36`）。
- 涉及行：28-M55, 28-M56, B09-11, B06-16
- 本轮漏项：E111-11
- 二轮漏项：G21-35, G21-31
- 涉及漏项：G18-31, G18-34, G21-37
- 要改：
  - `mcp_oauth.py` 新增 `submit_pasted_redirect(auth_id, url)`：URL ≤64 KiB、无 fragment、state 非空，解析 query 后交 `complete_login`（`:728`）现有 state/iss 检查；`routes.py:303-318` 旁加 POST `.../oauth/login/{auth_id}/redirect`。
  - `mcp_auth_browser.rs:20-29,41-46` —— 放行 `[::1]`；打开失败时把 URL 原样回给前端；`webapp/src/domain/mcpAuthBrowser.ts:29` 失败时显示链接与"复制"按钮和"粘贴回调地址"输入框。
  - `McpAuthSettings.tsx:58-59,110` —— 按 login_status 的错误码映射短句（过期、取消、被拒、端口占用、issuer 变化）；`:115-125` 登录配置加 scopes 与固定回调端口输入。
  - `McpAuthSettings.tsx:28-40` —— 公司账号轮询改指数退避：1 s 起翻倍到 30 s；账号版本变化、页面重新可见或主人点登录时重置为 1 s。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_oauth.py:272` 回调 Host 校验与 `:298-301` —— 回调主机可配置为 127.0.0.1 / localhost / [::1]（服务器记录 auth.callback_host，默认 127.0.0.1）；Host 头按所选主机校验，redirect_uri 用同一主机，路径仍 /callback；`McpAuthSettings.tsx` 登录配置加该选项。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_oauth.py:179-182` request_json 与 `:507-509` _call —— 按异常类型分码：超时 → agent_mcp_oauth_timeout，连接/DNS/代理错误 → agent_mcp_oauth_network_error，TLS 错误 → agent_mcp_oauth_tls_error；HTTP 非 2xx 仍为 OAuthHttpError(status)。这三个新码不写凭据行 error，不把服务器标为需要登录。
  - 二轮补：`webapp/src/screens/PluginViews.tsx:148` —— 只对 agent_mcp_auth_required 提示"登录已失效"；agent_mcp_oauth_request_failed 与上面三个网络码提示"连不上登录服务器，请检查网络或代理后重试"；`McpAuthSettings.tsx` 的失败码映射同步加这三个码。
  - 转入补：`webapp/src/screens/ExtensionWebApp.tsx:76-83` 更新状态轮询 —— 只在 `visible` 为真（页面可见且在前台）时每 2 s 查一次；隐藏的保活页不轮询；`serviceUpdates` 的 SERVICE_UPDATE_CHANGED_EVENT 事件（`:78-81` 已监听）为主，兜底轮询放宽到 30 s；页面重新可见时立即查一次。`:204-206` 的健康检查 PLUGIN_HEALTH_POLL_MS 循环同样在 `visible` 为假时跳过本轮（保留定时器，不累计失败次数）。保活数 3（`KeptPluginPages.tsx:11`）不改。
  - 转入补：`webapp/src/screens/McpAuthSettings.tsx:35` 公司账号轮询的 catch —— 单次失败不再 `setAccount(null)`（会把状态翻成"未登录"），保留上次 account 并设 statusUnknown，界面显示"状态未知，正在重试"；连续失败沿用上面的指数退避。`:57` 授权进度轮询的 catch —— 网络错误重试 3 次（间隔 1.5 s、3 s、6 s）后才判失败，期间 progress 保持 waiting；后端明确返回 failed/expired 才立即失败。
  - 转入补：`rynmesh/agent_runtime/extensions/mcp_oauth.py:1054-1059` logout 与 `:1085-1090` forget —— 非公司插件也用后台线程调 `_revoke`（与 `:1056-1058` 公司分支同一写法，线程名 mcp-remote-revoke），本地令牌与凭据行先清；吊销失败只写 diagnostic_log，不影响返回。
- 新增测试：mcp_oauth：粘贴正确回调完成登录，state 错误被拒，带 fragment 被拒；rust 单测：`[::1]` 放行；前端 vitest：失败码映射。；vitest：连续 6 次无变化后间隔到 30 s，版本变化后回到 1 s；二轮：发现请求超时得 agent_mcp_oauth_timeout，插件视图显示网络提示而不是重新登录；callback_host=localhost 时 Host=localhost:端口 被接受；转入：vitest：隐藏的保活页（visible=false）不发 status 请求，变为可见立即发一次；公司账号查询失败一次后仍显示上次账号并标状态未知；授权进度前两次网络错误后第三次成功，不判失败。Python：revoke 慢 5 s（假 HTTP）时 logout 在 1 s 内返回且本地令牌已清
- 依赖：无
- 兼容和风险：粘贴入口不改变 PKCE 与 state 校验，安全性不降。
- 工作量：中
- 影响：中

## S08-I30 HTTP MCP 服务器并行调用
- 问题：StreamableHttpMcpSession.call_tool 整个请求期间持会话锁（`mcp_transport.py:1163-1171`），同一服务器的并行只读调用被串行；adapter 只给只读工具 supports_parallel（`adapter.py:805`），服务器无配置项。Codex 由服务器配置 supports_parallel_tool_calls 决定。stdio 会话同样整段持锁（`mcp_transport.py:592-608`），同一 stdio 服务器的调用全部串行。
- 涉及行：26-M108, 28-M61, 09-M3
- 本轮漏项：E105-9
- 要改：
  - 调度闸门不改：`rynmesh/agent_runtime/tool_dispatch.py:21-26` supports_parallel 只认宿主元数据（只读来自主人确认的 read_only_tools，`adapter.py:529,545-547`），继续不认服务器自报 readOnlyHint。
  - `store.py:2361` _apply_mcp_policy_fields —— 加 `supports_parallel_tool_calls`（默认 false，仅主人可设）。
  - `mcp_transport.py:1163-1171` —— HTTP 会话对主人确认只读的工具调用一律只在取序号与读取 session_id 时持锁，请求期间放锁（httpx.Client 线程安全）；写工具只在服务器开启 supports_parallel_tool_calls 时放锁。stdio 会话（`:592`）保持整段持锁（单管道串行，按 id 并发分发不在本项）。
  - `adapter.py:805` metadata —— supports_parallel 还要求服务器开启该项或工具只读。
  - `mcp_transport.py:300-324,388-498,592-608` stdio —— `_read_loop` 按响应 id 分发到每个请求自己的队列（dict id→Queue，通知与服务器请求另走分派）；`_send` 只持写锁；`call_tool` 等待响应时不持会话锁。仅对主人确认只读的工具或开启 supports_parallel_tool_calls 的服务器并发，其余仍串行。
- 新增测试：HTTP 假服务器每次 sleep 0.5 s，开启后两次并行调用总时长 <0.9 s；未开启时串行。；stdio 假服务器乱序回包，两次并行只读调用各拿到自己的结果、总时长约等于单次
- 依赖：S06（并行调度）
- 兼容和风险：会话 404 重建（S08-I7）在并发下要串行化，需加重建锁。
- 工作量：中
- 影响：中
- 本轮修订：原写"stdio 会话保持整段持锁"；盲查（E105-9）指出本地 stdio 服务器因此永远串行。改为按 id 分发，受同一并行开关约束。

## S08-I31 tools/call 带 _meta 上下文
- 问题：tools/call 只在受信写时带 `_meta.ryn_operation`（`mcp_transport.py:1166-1167`；stdio `:604-605` 从不带），服务器拿不到任务、调用 id，无法做幂等或日志关联。
- 涉及行：05-M39, 08-M112
- 要改：
  - `mcp_transport.py:578-608,1147-1171` 两个 call_tool —— 新增 `meta` 参数，params 总带 `_meta: {ryn_task_id, ryn_call_id, ryn_plugin_id}`（不含模型名、路径等私有信息，除非插件清单声明需要）；受信写再加 ryn_operation。
  - `mcp_transport.py:1811-1911` McpSessionManager.call_tool 透传；`adapter.py:1289-1300` 及写路径调用处传 task_id、operation_id、plugin_id。
- 新增测试：假服务器收到的 params._meta 含 task 与 call id；未声明时不含工作区路径。
- 依赖：无
- 兼容和风险：多数服务器忽略 _meta；task_id 是本机随机 id，不含个人信息。
- 工作量：小
- 影响：低

## S08-I32 插件索引提示：描述清洗与行为规则
- 问题：插件描述、技能描述原样拼进开发者上下文（`adapter.py:743-752`），含换行或 markdown 的描述可伪装成规则；`unavailable_plugins` 同样（`:769`）。索引表头（`:739-740`）没有"发消息给他人须主人要求""审批前先做完准备""主人点名的插件没装时怎么说"等规则；插件常含企业微信、审批等对外动作。
- 涉及行：30-M89, 52-M138, 52-M139, 52-M106, 52-M107, 52-M108, 52-M112
- 要改：
  - `adapter.py:743-752,769` —— 插件名、描述、技能 id/描述先 `" ".join(text.split())` 折叠空白和换行，去掉反引号围栏与 `#` 行首，再截 PLUGIN_INDEX_LINE_CHARS。
  - `adapter.py:739-740` 表头追加三句（英文，保持字节稳定）：只有主人要求时才给他人发消息（聊天、邮件、审批），否则准备草稿给主人看；需要主人批准的写操作，先完成草稿和所有检查，让批准成为最后一步；主人点名的插件未安装且 ryn_discover_tools 找不到时，告诉主人去插件页装哪一个，不推荐相邻插件，主人没装就不再催。
  - 表头总长计入 PLUGIN_INDEX_BYTES（`:629`），必要时放到 3400。
- 新增测试：描述含 "\n# System:" 被折叠为一行；表头含三条规则；索引字节不超上限。
- 依赖：S12（与 prompt.py 规则不重复，`prompt.py` 有同类规则时只留一处）
- 兼容和风险：表头变化使缓存前缀失效一次。与"自动处理=全自动"不冲突：规则管"是否是主人要的动作"，不加审批。
- 工作量：小
- 影响：高

## S08-I33 主人 @插件 时注入该插件上下文
- 问题：被选中插件只来自已选技能（`rynmesh/agent_runtime/runtime.py:419-424`），主人在输入框点名插件或从应用页"打开 Agent"（`webapp/src/screens/AppsLauncher.tsx:166-176` 只 navigate("/agent")）时，插件工具不预载，模型不知道主人选了它。Codex 显式提到插件时注入其服务器和技能（`core/src/plugins/injection.rs:15-65`）。
- 涉及行：05-M67, 05-M72, 05-M73, B07-23
- 要改：
  - 任务行加 `plugins: [plugin_id]`（由界面在发送时带上）；`runtime.py:419-424` selected_plugins 合并该字段，plugin_preloads（`tool_menu.py:104-118`）照常预载。
  - `adapter.py:724-761` plugin_context_note 新增 `selected_plugin_ids` 参数：被选插件排最前，并加一行"The owner selected <name>: prefer its skills (<前缀>) and tools from <服务器名>; if a tool is not loaded, call ryn_discover_tools for this plugin first."；`loop.py:776-781` _plugin_note 传入。
  - `AppsLauncher.tsx:166-176` —— navigate 带 `state.pluginID`；Agent 输入框（S14）显示插件标签并随消息发送。
- 新增测试：row.plugins=[x] 时 preloads 含 x 的工具、上下文含选择句；未选择时上下文字节不变。
- 依赖：S14（输入框插件标签）、S06（预载）
- 兼容和风险：旧任务无 plugins 字段按空处理。
- 工作量：中
- 影响：低

## S08-I34 可安装插件提示与 ryn_list_plugins 分页
- 问题：ryn_list_plugins 只列已启用插件，一次返回全部插件和 MCP 连接，无分页、无查询（`rynmesh/node_apps/plugin_agent.py:84-117`）；主人问的功能属于"Ryn 有但未安装"的应用时，模型只能说不支持。ryn_discover_tools 无命中时不给推荐技能（`loop.py:2036-2042`）。`next(...)` 取插件行无默认值（`plugin_agent.py:128,146`），插件在 lease 后被卸载抛 StopIteration，只捕获 CraftError/AppError（`:158`），变成内部错误。
- 涉及行：08-M118, 31-M16, 05-M66, 24-M10.25, 07-M59, B02-14
- 本轮漏项：E110-22
- 要改：
  - `plugin_agent.py:27-53` ALL_LIST 参数加可选 `query`、`limit`（默认 20）、`offset`，返回 `next_offset`；默认只给名称、简介、工具数，细节走 ryn_plugin_info。
  - `plugin_agent.py:84-117` —— 增加 `installable` 段：Craft 目录里未安装/未启用的应用（`self.manager.catalog()` 的 enabled=False 行）与 `store.list_recommended_skills`（`store.py:1885`）未装项，名、id、描述截 240，标注"由主人在应用页安装"；不提供安装工具。
  - `loop.py:2036-2042`（S06 文件）—— discover 无命中时附 recommended（按 query 匹配推荐技能，最多 5 条）。
  - `plugin_agent.py:128,146` —— `next(..., None)`，None 时抛 AgentToolRejected("plugin_not_installed")；`:158` except 增加 KeyError（缺 app 参数转 agent_tool_arguments_invalid）。
- 新增测试：plugin_agent 测试：limit/offset 分页；installable 列出未装 Craft 应用；discover 无命中时带 recommended。；lease 后插件被卸载返回 plugin_not_installed 拒绝而非内部错误
- 依赖：S06（discover 结果）
- 兼容和风险：结果字段增加；默认结果变短，依赖完整字段的旧提示需看 ryn_plugin_info。
- 工作量：小
- 影响：低

## S08-I35 Craft MCP 会话协议容错
- 问题：craft_native.McpProcess._exchange 通知计数按会话累计超 64 报错并关会话（`rynmesh/node_apps/craft_native.py:543-547`）；服务器发来的请求（ping、roots/list）当 id 不匹配直接报错杀会话（`:548-549`）；RPC error 只报 craft_mcp_rpc_error，丢 error.message，且任何异常都关会话（`:550-551,555-557`）。`tools_list` 只发一次 tools/list（`craft_native.py:586-587`），`craft_agent.py:166` 只取 tools，不处理 nextCursor。
- 涉及行：B01-1, B01-2, B01-3
- 本轮漏项：E110-33
- 二轮漏项：G20-15, G20-18
- 要改：
  - `craft_native.py:531` —— 每次 `_exchange` 开头 notifications=0；`:543-547` progress/message 通知只记日志不计数。
  - `craft_native.py:548-549` —— 先判 message 含 method 且含 id：ping 回空结果，其余回 -32601，继续等。
  - `craft_native.py:550-551` —— RPC error 时 CraftError 带 details={code, message 截 500}，不关会话；`:555-557` 只有协议/进程类错误才 close()。
  - `rynmesh/node_apps/craft_agent.py:446` execute 把 details 放进 AgentToolRejected.details。
  - `craft_native.py:586-587` tools_list —— 循环读 nextCursor（≤16 页、≤256 工具、共用一个超时，游标重复报错）；`craft_agent.py:166` 用合并后的列表。
  - 二轮补：`rynmesh/node_apps/craft_native.py:445-448` _read —— output_bytes 不再按会话累计：每行单独限 MAX_OUTPUT（`:38`，8 MiB），删除会话累计判断（同 S08-I4 对 MCP 的做法）。
  - 二轮补：`rynmesh/node_apps/craft_native.py:418,453-455` —— 无 id 的通知在读线程里直接计数或丢弃，不入队；队列只放响应，put 不设 0.1 s 超时，空闲时连发的日志通知不再让会话判坏。
- 新增测试：假 craft MCP 进程发 70 条 progress 后回包成功；发 ping 后回包成功；RPC error 后同会话下一次调用成功，模型结果含 message。；假 craft 进程分两页返回工具，全部被发现；二轮：同一会话连续 3 次各读 4 MiB 结果都成功；空闲时假进程连发 100 条日志通知，下一次调用成功
- 依赖：无
- 兼容和风险：与 S08-I1/I3 逻辑相同，可抽公共函数，但 craft_native 是独立实现，先各自改。
- 工作量：小
- 影响：中

## S08-I36 Craft 文档会话按空闲续期
- 问题：Craft 文档会话按创建时间 240 s 过期（`rynmesh/node_apps/craft_agent.py:38,133-145`），McpProcess 寿命 300 s（`craft_native.py:399`，上限 600 s `:406`，并作为 process_guard 硬超时 `:430`）；长文档编辑必被打断，未保存的编辑丢失。
- 涉及行：B02-12
- 二轮漏项：G20-23, G20-24
- 要改：
  - `craft_agent.py:38` —— 改 `SESSION_IDLE_SECONDS=1200`；`:133-139` 判定改用 `session["last_used"]`，每次成功 read/action 后刷新。
  - `craft_native.py:398-406` —— lifetime 上限放到 4 小时，McpProcess 加 `touch()` 续期 deadline；process_guard 硬超时传上限值。
  - 会话到期前若有未保存编辑（revision>0 且无导出），`craft_agent.py:140-145` 的纠正提示带最后一次自动导出的 `.ryn-autosave` 路径（新增：每次 action 成功后后台导出一份到工作区）。
  - 二轮补：`rynmesh/node_apps/craft_agent.py:133-149` _session —— 活会话满 16 个时先关闭最久未用、无未保存编辑的会话（所属任务已结束的优先），再新建；全部都有未保存编辑才报 agent_craft_session_limit，correction 说明可先导出。
  - 二轮补：`rynmesh/node_apps/craft_agent.py:154-161` session_cancelled —— 结论缓存 1 s（看门线程仍 50 ms 调一次，`craft_native.py:462-468`，1 s 内复用）；read_control 或 manager.generation 抛异常时沿用上次结论，连续 30 s 读不到才取消，码 craft_state_unreadable。
- 新增测试：间隔 200 s 的两次调用（时钟 monkeypatch）不过期；空闲 1200 s 后过期；过期提示含 autosave 路径。；二轮：16 个会话满时第 17 个回收最久空闲会话后成功；控制存储读取抛一次异常会话不被杀；1 s 内 read_control 调用 ≤2 次
- 依赖：无
- 兼容和风险：常驻进程更久；16 会话上限不变（`:148`）。autosave 文件放工作区，需标为 Ryn 生成。
- 工作量：中
- 影响：中
- 本轮修订：原写"16 会话上限不变"，满了直接拒绝（G20-23）；上限保留，改为满时先回收最久空闲会话。

## S08-I37 插件服务重启不打断在途调用、启动等待不占全局锁
- 问题：`ensure` 发现配置变了立即重启旧进程（`webapp/src-tauri/src/plugin_services.rs:116-118,183-196`），不管 agent 是否有在途 MCP 调用；整个启动等待（最长 30 s）持有 children 互斥锁（`:180,206-220`），监督线程和其他服务的拉起被卡住。另外，自有进程崩溃后要等看门下一次 30 s 周期才 ensure（`plugin_services.rs:310-316`），Agent 调用期间持续失败，也没有"服务重启中"的提示。
- 涉及行：B09-9, B09-10
- 本轮漏项：E110-16
- 二轮漏项：G19-10
- 涉及漏项：G19-35
- 要改：
  - `plugin_services.rs:179-221` ensure —— 拆成"锁内判断 + 放占位 Starting"与"锁外 spawn、健康等待"，完成后再短暂持锁 insert；重复 ensure 看到 Starting 等待或返回 plugin_service_starting。
  - 重启前向节点查询在途调用：新增节点路由 GET `/api/local/agent/extensions/local-service/inflight?service_id=`（`routes.py`，由 McpSessionManager 统计该服务 server_ids 的在途 call_tool 数）；非零时延后，最多 60 s。
  - `plugin_services.rs:293-322` watch —— 每 2 s 循环里检查自有子进程 `try_wait()`，已退出就立即 ensure，不等 30 s（"存活但不健康"仍按 `recovery_due` 的 90 s 宽限，`:271-273`）。
  - `adapter.py:1303-1314` 与写路径 —— 本机插件服务（127.0.0.1 + X-Ryn-Service-Token，判定同 `mcp_transport.py:1897`）出现未派发的 agent_mcp_process_disconnected 时，拒绝带 correction"插件服务正在重启，请稍后重试同一调用"。
  - 二轮补：`webapp/src-tauri/src/plugin_services.rs:277-291` plugin_service_instance —— 持 children 锁只做 spec 比较、try_wait 与取 pid，释放锁后再 `healthy(service)`（`:120-129`，最长约 2.7 s）；OwnedService 加 `last_healthy: Option<Instant>`（watch 或 ensure 健康时写），3 s 内的结果直接用，不再每次连端口。
  - 转入补：`webapp/src-tauri/src/plugin_services.rs:179-220` ensure 与 `:242,264,313` 三处 `log::warn!` —— PluginServices 增加 `last_error: Mutex<BTreeMap<String,(String,u64)>>`（服务 id → 错误码与 unix 秒），ensure 失败写入、成功清除；watch 循环（`:313`）的失败也写入。新增 Tauri 命令 `plugin_service_status`（`:278` plugin_service_instance 旁）返回 `[{service_id, plugin_ids, last_error, at, owned}]`，在 `lib.rs` 的 invoke_handler 注册。
  - 转入补：`webapp/src/screens/AgentExtensions.tsx:1596-1698` 插件卡片 —— 桌面端读 `plugin_service_status`，对有 last_error 的插件显示"本地服务启动失败：<白话原因>"（码表同 `ExtensionWebApp.tsx:296-298` 已有的 plugin_service_* 文案，抽到共用的 `webapp/src/domain/pluginServiceErrors.ts`）和"重启服务"按钮（调 `restart_plugin_service`，`plugin_services.rs:257-268`）；MCP 服务器详情的状态行对该插件的服务器同样显示。5 s 刷新一次，仅在存在 last_error 时。
- 新增测试：rust 单测：慢启动服务期间另一服务的 ensure 不被阻塞；有在途计数时重启被延后。Python：inflight 路由计数。；rust：杀掉自有子进程后 3 s 内被重启；Python：本机服务断开时工具结果带重启 correction；二轮：rust：健康检查慢（2 s）时两个并发 plugin_service_instance 不互相排队，ensure 不被它卡住；转入：rust 单测：ensure 因端口占用失败后 plugin_service_status 带 plugin_service_port_in_use，下一次成功后清空；vitest：有 last_error 的插件卡片显示原因与重启按钮，点击调用 restart_plugin_service
- 依赖：无
- 兼容和风险：Starting 占位要在 spawn 失败时清除，否则服务永远起不来。
- 工作量：中
- 影响：中

## S08-I38 扩展页信息展示：风险来源、隐式调用开关、状态实时、跳转到服务器
- 问题：界面不显示 effective_risk、read_only_sources、install_generation（类型有，`webapp/src/domain/agentExtensions.ts:69-84`，`AgentExtensions.tsx` 无引用）；技能 allow_implicit_invocation 只在数据层（`agentExtensions.ts:188-190`），后端 PATCH 只收 enabled（`routes.py:622-636`）；扩展页只在挂载时加载（`AgentExtensions.tsx:794-796`），启动中/失败不刷新；插件页链接 `?server=ID`（`PluginWorkspace.tsx:103`）但扩展页只读 plugin/view 参数（`AgentExtensions.tsx:658-672`）；应用页插件卡片只有 OAuth 徽标（`AppsLauncher.tsx:102-104`）。徽章在 status=ready 且测试过时一律"已连接"（`AgentExtensions.tsx:377-382`），服务器之后崩溃、令牌过期、插件服务退出都不变，也没有最近一次实际调用的错误；"需重启"提示靠 React 状态 `mcpModifiedSinceRestart`（`:690`，置位处 `:943,1308,2257`，显示 `:2084`），刷新页面即丢，切换工具开关或只读标记不会置位。
- 涉及行：B05-22, B05-23, B06-9, 42-M18, B07-24, B06-4
- 本轮漏项：E110-39, E110-40
- 要改：
  - `AgentExtensions.tsx:3058-3139` 工具行 —— 显示"只读（来源：主人/服务器声明/安装器）"或"写入"标签。
  - `AgentExtensions.tsx` 技能详情 —— 加"允许自动使用"开关；`routes.py:622-636` update_skill 接受 `allow_implicit_invocation`，`store.py:1717` set_skill_enabled 旁新增 set_skill_policy。
  - `AgentExtensions.tsx:794-796` —— 存在 NeedsTest/启动中/Degraded 服务器时每 5 s 带 revision 重新拉 overview，全部稳定后停。
  - `AgentExtensions.tsx:664-672` —— 读 `server` 参数：切到 MCP 标签并选中该服务器。
  - `AppsLauncher.tsx:102-146` —— 卡片加 MCP 状态点（已连接/失败/启动中）与工具数，数据来自 extensionOverview.mcp_servers。
  - `mcp_transport.py:1912-1936` McpSessionManager.call_tool —— 每服务器在内存记 `live_state`（connected/failed/auth_required，按最近一次调用或探测结果）与 `last_call_error`（错误码、时间）；`adapter.py:365-383` overview 把这两项并进每个服务器。
  - `AgentExtensions.tsx:377-382` 徽章 —— 优先用 live_state 与 last_call_error（"最近调用失败：<码> · <时间>"），不再只看 last_tested_at；页面可见时每 15 s 静默刷新 overview（不稳定状态仍 5 s）。
  - `adapter.py:365-383` overview —— 每服务器加 `pending_restart`：manager 里已有会话的 config_generation/revision 与存储不同即为 true；`AgentExtensions.tsx:2084` 的"重启"提示改由后端字段决定，并说明"新任务生效，运行中的任务保持旧连接"；删掉 `mcpModifiedSinceRestart` 状态。
- 新增测试：vitest：`?server=x` 打开详情；只读来源标签；有启动中服务器时轮询、稳定后停止；Python：PATCH allow_implicit_invocation 生效。；ready 服务器后续调用失败后徽章显示最近错误；改工具开关后 pending_restart=true，刷新页面仍显示提示，重启后消失
- 依赖：S09（allow_implicit_invocation 的注入语义）
- 兼容和风险：轮询增加本地请求，只在不稳定状态下进行。
- 工作量：中
- 影响：中

## S08-I39 扩展请求与导入界面健壮性
- 问题：extensionRequest 无默认超时，后端无响应时一直转圈；FastAPI 校验错误 detail 是数组时变成 extension_request_failed，网络中断同码（`webapp/src/domain/agentExtensions.ts:1049-1071`）。插件视图超过 8 个整体丢弃、单个字段超限静默丢弃（`:1004-1019`）。导入 ZIP 整包读进内存无大小检查（`AgentExtensions.tsx:4494-4504`）；"清除所有应用数据"无二次确认（`:2140-2151`）。"清除所有应用数据"实际只写一个无人读取的时间戳 `mcp_app_data_cleared_at`（`store.py:2656-2672`），manager 只关会话（`mcp_transport.py:1445-1448`）。页面查询（`PluginViews.tsx:127-153`）只在重新查询/卸载时中止，传了 signal 所以不受默认超时约束，服务无响应时一直转圈。describeError 只翻译 10 个错误码（`AgentExtensions.tsx:121-159`），其余显示"操作未完成 (code)"；测试、重启时界面一直"正在测试…"，测试中按钮禁用，没有取消（`:1086-1093` 只按 runId 复位）。
- 涉及行：B05-25, B05-24, B06-17
- 本轮漏项：E105-26, E111-14, E110-41, E110-42
- 二轮漏项：G19-22
- 涉及漏项：G19-20
- 要改：
  - `agentExtensions.ts:1049-1071` —— 未传 signal 时用 30 s 超时（AbortSignal.timeout）；detail 为数组时拼前两条 msg；fetch 抛错时给 extension_network_error，超时给 extension_timeout。
  - `agentExtensions.ts:1004-1019` —— 超过 8 个只取前 8 个；被丢弃的视图和原因记入插件 warnings，插件详情列出。
  - `AgentExtensions.tsx:4494-4504` —— 选择文件时检查 file.size（插件 50 MiB、技能 6 MiB，与 S08-I24 一致），超限提示。
  - `AgentExtensions.tsx:2140-2151` —— 清除前弹确认对话框。
  - `store.py:2656-2672` clear_mcp_app_data —— 真正清除：关全部会话后删除 S08-I21 的 `store.root/mcp_home/*` 与 `mcp_cache/*`（不动 OAuth 令牌与服务器配置），删掉无用的时间戳字段；返回删除的字节数。
  - `PluginViews.tsx:127-153` query —— 用 AbortSignal.any 合并 60 s 超时，busy 时显示"取消"按钮。
  - `AgentExtensions.tsx:121-159` describeError —— 码表移到 i18n 资源（与其他界面共用），覆盖 agent_mcp_*、plugin_*、craft_*、本项新增的超时/网络码；未知码显示通用文案加"复制诊断信息"按钮（复制码、服务器 id、时间）。
  - `AgentExtensions.tsx:2858-2870,1086-1093` 测试/重启 —— 用 AbortController，60 s 界面超时，测试中显示"取消"按钮；`routes.py:423-436` 旁新增 POST `.../mcp/servers/{id}/test/cancel`，`mcp_transport.py:1450` test_server 把探测会话登记在 manager，取消时关闭该会话（stdio 先发 notifications/cancelled，见 S08-I9）。
  - 二轮补：`webapp/src/domain/agentExtensions.ts:604-630` parseMcpArgsInput —— 非 JSON 输入按行切分后去掉每行首尾空白并丢弃空行；只有一行且含空格时返回 warning "args_contains_space"，表单在参数框下预览实际参数列表，并提示"每行一个参数，`-y @pkg/server` 要写成两行"（不自动按 shell 拆分，避免引号歧义）。
  - 转入补：`webapp/src/domain/agentExtensions.ts:1069-1071` extensionRequest 成功分支 —— `response.json()` 前先判 `response.status === 204` 或 content-length 为 0，返回 `undefined as T`；json 解析失败（成功状态却非 JSON）抛 AgentExtensionError('extension_bad_response', status)，不再冒出 SyntaxError；`AgentExtensions.tsx:154-158` describeError 为该码给"后端返回了无法识别的内容，请重试或升级应用"，不再提示"请检查节点连接"。`:1058-1068` 错误分支的非 JSON 5xx 原归 extension_request_failed，改为带状态码：`extension_http_<status>`（仅 5xx）。
- 新增测试：vitest：请求超时得 extension_timeout；数组 detail 拼接；9 个视图保留 8 个并有警告；大文件提示；清除需确认。；清除后私有目录为空、令牌仍在；视图查询 60 s 无响应提示超时并可重查；未知码显示复制诊断按钮；测试中点取消，界面复位且后端探测会话被关闭；二轮：vitest："a\nb\n" 得 ["a","b"]；单行 "-y @x" 给 warning 且预览显示 1 个参数；转入：vitest：204 成功响应不抛错；200 非 JSON 得 extension_bad_response 且提示不含"节点连接"；502 HTML 响应得 extension_http_502
- 依赖：S08-I24（大小上限）
- 兼容和风险：长时间操作（导入预览、测试服务器）须显式传更长超时，否则 30 s 被截断——这几个调用传 120 s。
- 工作量：小
- 影响：中
- 本轮修订：原只给"清除应用数据"加二次确认，但该按钮实际不清除任何东西；改为真正清除服务器私有目录与缓存，二次确认保留。

## S08-I40 发布声明合并记录跳过原因
- 问题：configure_local_mcp_services 与 refresh_released_skill_packages 多处静默 continue（`rynmesh/node_apps/release_declarations.py:35-41,46-50,129-140`），升级后插件/技能没更新也查不到原因。
- 涉及行：B01-24
- 二轮漏项：G19-13, G19-17
- 要改：
  - `release_declarations.py:23-56,115-148` —— 每处 continue 前把 (id, reason) 记入 `skipped` 列表；`apply_release_declarations`（`:171`）把 skipped 一并返回并写进安装记录与 diagnostic_log。
  - 二轮补：`rynmesh/node_apps/release_declarations.py:171-188` apply_release_declarations —— refresh/bind/oauth 迁移/本地服务配置/信任五步各自 try：失败把 (step, 错误码) 记入 `failed_steps` 继续后续步骤；`migrate_release_defaults` 失败仍整体失败（它改存储默认值，失败应走更新回滚）。`rynmesh/node_apps/service_updates.py:647-659` _declarations 的调用方：failed_steps 非空时更新记为 applied_with_warnings（不转 service_update_apply_failed），失败项写进安装记录给界面；这些步骤幂等，下次启动或下次更新重跑。
  - 二轮补：`rynmesh/node_apps/release_declarations.py:77-82` migrate_declared_oauth_clients —— 只比较声明 auth 里出现的键（client_id 除外），存储侧补的默认字段不影响；不匹配时把 (sid, "auth_customized") 记入 skipped。
- 新增测试：声明里一个服务器被主人改过 URL → skipped 含 (id, "customized_url")；技能文件被改 → skipped 含 (id, "owner_edited")。；二轮：upsert 冲突让 configure_local_mcp_services 抛错时 oauth 迁移与信任仍执行，结果 failed_steps 含该步、更新为 applied_with_warnings；存储 auth 多一个默认字段时迁移仍发生
- 依赖：无
- 兼容和风险：返回值多字段，调用方（安装器）需兼容旧返回。
- 工作量：小
- 影响：中

## S08-I41 扩展页文案与文档链接改正
- 问题：扩展页页头写"所有工具调用均遵循当前任务的权限与确认设置"（`webapp/src/screens/AgentExtensions.tsx:1435-1440`），电脑控制小节写"遵循任务确认模式"（`:2544`），而自动处理模式是全权限直接执行，文案与行为不符。MCP 与技能的"文档/了解更多"链接指向 developers.openai.com（`AgentExtensions.tsx:3742-3750`，`webapp/src/screens/CodexSkillsView.tsx:567-573`），是另一个产品的文档，国内网络常打不开。
- 涉及行：B06-5, B06-18
- 要改：
  - `AgentExtensions.tsx:1437-1438` —— 改为"确认模式下需要你确认；自动处理模式下直接执行"（中英两份）。
  - `AgentExtensions.tsx:2544` —— 改为"可操作内容（确认模式下需确认，自动处理直接执行）："。
  - `AgentExtensions.tsx:3742-3750`、`CodexSkillsView.tsx:567-573` —— 链接改为 Ryn 自带说明：新增本地帮助内容（如 `webapp/src/help/mcp.md`、`skills.md`，以弹窗展示），不再外链 OpenAI 文档。
- 新增测试：vitest：页头文案不含"均遵循"；两处链接 href 不含 developers.openai.com，点击打开本地说明弹窗。
- 依赖：无
- 兼容和风险：只改界面文案与链接，无存储变化。
- 工作量：小
- 影响：低

## S08-I42 服务器声明只读的 MCP 工具：自动档按读处理，传输错误回给模型不中断任务
- 问题：只有主人手工标只读的工具才是 read（`adapter.py:529,545-547`），服务器给的 readOnlyHint 完全不看；绝大多数查询型 MCP 工具按写处理：不能并行（`adapter.py:805`，`rynmesh/agent_runtime/tool_dispatch.py:21-26`），派发后遇断网/超时抛 agent_write_outcome_unknown（`adapter.py:1627-1632`），主循环把整个任务置 interrupted 等主人确认（`rynmesh/agent_runtime/runtime.py:2802-2813`）。一次网络抖动就能让查询中断任务。Codex 按注解判定（`core/src/mcp_tool_call.rs:2595-2626`）。
- 涉及行：E104-66, E104-67
- 二轮漏项：G18-35
- 涉及漏项：G18-36
- 要改：
  - `adapter.py:545-568` _active_mcp_tools —— 绑定新增 `server_hint_read = annotations.readOnlyHint is True and destructiveHint is not True`（主人标为写的 owner_write_tools 除外）；effect 仍按主人确认（台账与非自动档审批不变，保持主人确认才算只读的安全边界）。
  - `adapter.py:799-810` metadata —— 输出 `server_hint_read`。
  - `tool_dispatch.py:21-26` supports_parallel（S06 文件）—— 任务为 automatic 档时，server_hint_read 的工具也可并行（需 S06 把任务档位传入）。
  - `adapter.py:1627-1632` 写路径 —— binding.server_hint_read 为真时不抛 agent_write_outcome_unknown，改抛新码 agent_mcp_outcome_unknown_reported：台账保持 dispatched（不改成 not_executed），工具结果回给模型 `{outcome:"unknown", hint:"先用只读工具核对再决定是否重试"}`。
  - `runtime.py:2802-2813`（S02 文件）—— flight 带 server_hint_read 且错误为上述码时不按 ambiguous_write 置 interrupted，把结果交回模型继续（与 S02-I9 在自动档的做法合并，扩到所有档）。
  - 二轮补：`webapp/src/screens/McpDataPolicy.tsx:58,80` —— 文案按审批模式说明："确认模式下，确认后这些工具不再每次询问；自动处理模式本来就不询问，确认只读只影响并行和出错时是否中断任务"（中英两份）；确认只读不改变自动档的审批（与本项一致）。
  - 转入补：`webapp/src/screens/McpDataPolicy.tsx:8` McpDataPolicySummary —— 去掉 `policy || { models: any, memory: allow }` 的默认值：policy 缺失时显示"数据策略未知，请刷新"（role=status），不渲染"不限制/允许"两行；`:12,18-20` 只在 policy 存在时按其内容渲染。
- 新增测试：adapter 测试：readOnlyHint=true 的未标记工具，HTTP 超时后结果 outcome=unknown、任务不 interrupted、台账为 dispatched；destructiveHint=true 的工具仍走原中断流程；automatic 档两个 hint 只读调用并行；confirm_changes 档仍需审批。；二轮：vitest：只读提示与确认对话框文案含"自动处理"说明；转入：vitest：policy 缺失时显示"策略未知"且不含"不限制"
- 依赖：S02-I9（结果未知回给模型）、S06（并行闸门）、S08-I17
- 兼容和风险：恶意服务器谎报 readOnlyHint 只能换来并行与"不中断"，拿不到免审批（非自动档仍问，自动档本来就不问），台账仍记 dispatched 供对账。
- 工作量：中
- 影响：高

## S08-I43 OAuth 与公司授权请求的代理策略与 MCP 连接一致
- 问题：OAuth 发现/令牌请求（`mcp_oauth.py:163` request_json）与远程公司授权校验（`mcp_oauth.py:964` workspace_member）用 `trust_env=False`，不读系统代理；MCP 主连接 `httpx.Client`（`mcp_transport.py:689`）默认读代理。需要代理出网的环境里 MCP 能连、登录和刷新却失败。
- 涉及行：E105-22
- 要改：
  - 新增 `extensions/net.py`：`client_kwargs(url)` —— 回环地址（127.0.0.1/localhost/[::1]）返回 trust_env=False（本机服务不能走代理）；其余返回 trust_env=True，或用节点设置里可选的显式代理 `network.proxy`。
  - `mcp_oauth.py:163`、`mcp_oauth.py:964`、`mcp_transport.py:689` —— 统一改用 `client_kwargs`。`routes.py:334` 公司账号读取的目标是本机服务（`company_authorization.py:344` 返回 `http://127.0.0.1:<port>`），保持 trust_env=False。`company_authorization.py:310` 目标是 `http://127.0.0.1`（`:330`），保持 trust_env=False 不改。
- 新增测试：设 HTTPS_PROXY 指向本地假代理，OAuth 发现请求经过代理；127.0.0.1 的 MCP 与本机服务请求不经过代理；显式代理设置优先。
- 依赖：无
- 兼容和风险：原先刻意不走代理可能是怕代理截获令牌；系统代理是主人自己配置的，回环仍直连。界面在高级设置说明。
- 工作量：小
- 影响：中
- 本轮修订：原把 `routes.py:334` 当远程请求列入；核对 `company_authorization.py:344` 后它是本机地址，已移出，routes.py 不因本项改动。

## S08-I44 插件网页 tool-query 要求主人确认只读，并支持多服务器插件
- 问题：插件网页经 tool-query 调工具时，`get_plugin_tool_internal` 只要求服务器自称 readOnlyHint（`store.py:1361-1362`），不查 read_only_tools；插件视图路径同时要求两者（`store.py:1326-1329`）。写错或谎报注解的服务器可让网页内容调其任意工具。另外只取插件第一个服务器（`store.py:1345-1350`），多服务器插件其他服务器的工具调不到。
- 涉及行：E105-27, E105-28
- 涉及漏项：G20-60
- 要改：
  - `store.py:1361-1362` get_plugin_tool_internal —— 加与 `:1328-1329` 相同的检查：工具须在 read_only_tools（owner 或 installer 来源），否则 agent_plugin_view_read_only_confirmation_required。
  - `store.py:1345-1350` —— 按工具名在插件全部 mcp_server_ids 里查找；多于一个匹配时要求 payload 指定 server，否则 agent_plugin_tool_ambiguous。
  - `views.py:268` query_plugin_tool —— payload 接受可选 `server`，传给 get_plugin_tool_internal。
  - 转入补：`rynmesh/agent_runtime/extensions/store.py:1629-1631`（set_plugin_enabled）与 `:2343`（upsert_mcp_server）—— `_clear_server_hints`（`:2574`）只在配置指纹变化（`new_fp != old_fp`）时调用；纯启停（lifecycle_changed）不再清 server_hint/installer 来源的只读确认（owner 来源本来就保留），仍保留 `discovery_revision = 0` 以触发重新发现；工具 schema 哈希变化时的清除由 record_mcp_discovery 路径负责（`:2696,2807` 已有）。
- 新增测试：readOnlyHint=true 但未确认的工具经 tool-query 被拒；确认后可调；两服务器插件按工具名找到第二个服务器；重名时不指定 server 被拒。；转入：Python：停用再启用插件后，installer 来源的 read_only_tools 与插件视图查询仍可用；改命令行（指纹变化）后确认被清除
- 依赖：S08-I17（逐工具确认入口）
- 兼容和风险：现有依赖"只凭注解"调用的公司插件网页会被拒——公司插件服务器是安装器信任的（installer 来源，`store.py:2508-2524`），已在 read_only_tools 里，不受影响；第三方插件需主人确认一次。
- 工作量：小
- 影响：中

## S08-I45 MCP 服务器 required 标志与导入未识别字段告警
- 问题：没有"必需服务器"概念：服务器起不来时任务照常跑，模型只会说功能不存在或绕路；导入配置里的 required、supports_parallel_tool_calls、审批模式、输出上限等键被静默忽略，没有任何警告（只有 read_only_tools 有警告 `imports.py:1063-1066`）。界面输入类型也没有这些字段（`webapp/src/domain/agentExtensions.ts:341-389`）。
- 涉及行：E105-46, E110-37
- 要改：
  - `store.py:2295-2335` upsert 记录与 `store.py:2361` _apply_mcp_policy_fields —— 加 `required`（bool，默认 false，主人可设）。
  - `imports.py:897-1096` _parse_mcp_config_text —— 读取 required；其他已知但 Ryn 不从导入采纳的键（审批模式、并行、输出上限，原则同 S08-I17）与未知键汇总成一条 warning"已忽略：<键名>"。
  - `adapter.py:642-684` refresh_catalogs_for_turn —— 等待结束后返回仍未就绪的 required 服务器列表；主循环回合开始处（S02，`loop.py:776-781` 附近调用点）若非空，任务进 failed（error=agent_mcp_required_unavailable，details 带服务器名与恢复建议），不发模型请求。
  - `agentExtensions.ts:341-389` 与 `AgentExtensions.tsx` 服务器表单高级设置 —— 加"必需（连不上就停止任务）"勾选。
- 新增测试：required 服务器 Error 时新回合直接失败并给恢复建议；非 required 照常；导入含 required 与 unknownKey 的配置，required 生效、unknownKey 出现在 warnings。
- 依赖：S02（回合开始的失败分支）、S08-I10（就绪判定含 Degraded）
- 兼容和风险：默认 false，旧配置行为不变。
- 工作量：中
- 影响：中

## S08-I46 插件网页加载状态与错误提示通用化
- 问题：加载中/就绪/出错/超时生命周期只对写死的三个公司插件 id 生效（`webapp/src/screens/ExtensionWebApp.tsx:26,45`，提示与"重新加载"按钮 `:309-314`），其他插件页白屏无提示；非回环插件页不做可达性检查直接挂 iframe（`:166-169`），iframe onError 对 5xx/拒连基本不触发（`:323`）；页面桥每条 ryn.plugin.request 都重新拉完整 overview、无缓存无超时（`:227`），所有失败统一回 plugin_capability_unavailable（`:272-274`）；overview 拉取失败或 10 s 超时都显示"请检查插件是否已启用"（`:155-160,300`），把瞬时故障误报成未启用。
- 涉及行：E111-5, E111-6, E111-7, E111-8
- 要改：
  - `rynmesh/agent_runtime/extensions/web_app.py:7` validate_web_app —— 接受可选 `reports_lifecycle: bool`。
  - `ExtensionWebApp.tsx:45` —— `reportsLifecycle = COMPANY_PAGES.has(pluginId) || plugin.web_app.reports_lifecycle`；不报生命周期的插件也有 20 s 加载超时提示与"重新加载"按钮（onLoad 未触发时）。
  - `ExtensionWebApp.tsx:166-169` —— 远程插件先 `fetch(url, {mode:"no-cors"})` 带 10 s 超时探测，网络错误进入与本机插件相同的错误卡片。
  - `ExtensionWebApp.tsx:227` —— overview 缓存 5 s、请求 10 s 超时、单插件并发 ≤4；`:272-274` 错误分类回 plugin_not_enabled / permission_denied / invalid_arguments / host_busy。
  - `ExtensionWebApp.tsx:155-160,300` —— 拉取失败设 'overview_failed'，文案"读取插件信息失败，可重试"；仅在拿到清单且未启用/无页面时显示"未启用"。
- 新增测试：vitest：第三方插件声明 reports_lifecycle 后显示加载状态；远程地址不可达显示错误卡片；overview 失败与未启用两种文案；桥请求未授权方法返回 permission_denied。
- 依赖：无
- 兼容和风险：公司插件行为不变；no-cors 探测只能判断网络可达，不能判断 5xx，5xx 仍靠加载超时提示。
- 工作量：中
- 影响：中

## S08-I47 节点应用写后验证不因后续正当修改变成 unknown
- 问题：verify 只在"回执存在且当前工作区修订号等于回执时修订号"时通过（`rynmesh/node_apps/agent.py:207-226`，`:216`）；之后主人或别的任务的任何正当修改都会让验证变 unknown，Agent 分不清"被改过"和"没写成功"。回执本身已校验 consumer、provider、app、action、参数哈希与请求哈希（`:138-166`）。
- 涉及行：E110-21
- 要改：
  - `agent.py:207-226` verify —— 通过条件改为 `_receipt` 返回有效回执（证明该操作已按这些参数执行）；修订号相同记 evidence.revision_unchanged=true，不同记 evidence.changed_after_write=true 与当前修订号，状态仍为 passed；回执缺失或不符才是 unknown；服务调用异常仍 unknown。
- 新增测试：`tests/test_node_apps_agent.py`：写入后另一次修改使修订号变化，verify 仍 passed 且 changed_after_write=true；回执参数哈希不符返回 unknown。
- 依赖：S10（验证结果的使用方，字段向后兼容）
- 兼容和风险：passed 的含义从"未被改动"变为"确已执行"；evidence 字段名保留 original_receipt_and_revision_match 以兼容旧读取方。
- 工作量：小
- 影响：低

## S08-I48 Craft 写操作：按工具给时限，发布后可对账
- 问题：McpProcess 单次调用默认 30 s（`rynmesh/node_apps/craft_native.py:399`，上限 300 s `:527`），`craft_agent.py` 调 tools_call 从不传 timeout（`:425,486`）；大表格、大 PDF、批量转换超时后整个会话被关（`craft_agent.py:521-529`），动作被映射成 agent_write_outcome_unknown，模型被告知不得重放。输出文件发布后，哈希不符（`:437-438`）或后续任一步（重开验证 `_reopen` 只捕获 CraftError，`:320-340`）抛异常也统一报 unknown（`:527-529`）；reconcile 只认 confirmed 台账（`:536-542`），文件已在、校验和已知也无法恢复。Codex 默认工具超时 300 s（`codex-mcp/src/rmcp_client.rs:123`）。
- 涉及行：E110-26, E110-30
- 二轮漏项：G20-25
- 要改：
  - `craft_agent.py:425,486` —— 按工具类别传 timeout：读/检查 60 s，保存/导出/转换 300 s（新增 `TOOL_TIMEOUTS` 表，按 OUTPUT_TOOLS 判定）。
  - `craft_native.py:398-406` —— 单次上限与会话寿命（S08-I36）一致放宽，超时只结束本次请求：发 notifications/cancelled 后丢弃迟到的该 id 回包（`:525-557` `_exchange` 记已取消 id 集合，S08-I35 的 id 处理同处）；进程仍存活则会话保留。
  - `craft_agent.py:430-444` —— 发布前在台账记 `staged_outputs`（目标路径与 staged 校验和），台账状态 `published`；`_reopen`（`:320-340`）捕获所有异常，返回 validation=failed，不向上抛。
  - `craft_agent.py:536-542` reconcile —— 对 published 记录逐个核对目标文件哈希，全部一致返回 executed（带文件清单），否则 unknown。
  - `craft_agent.py:521-529` —— 超时且有 staged 输出完整写出时走 reconcile 的同一核对；只有核对不了才报 agent_write_outcome_unknown。
  - 二轮补：`rynmesh/node_apps/craft_agent.py:506-529` 写路径 —— 用局部标志 `engine_dispatched`，在调用 `_invoke_action` 前才置真；此前 `_session`、`_arguments`、baseline 抛出的 AgentToolRejected（参数无效、会话过期、新路径已存在等确定未执行）单独捕获：台账记 not_executed（error=原码），原样抛出，不关会话、不报 agent_write_outcome_unknown；置真之后的异常才走 unknown/对账路径。
- 新增测试：`tests/test_craft_agent.py`：导出工具 120 s 内完成不超时；读工具超时后同一会话下一次调用成功；发布后 _reopen 抛 OSError，结果仍 ok 且 validation=failed；台账为 published 时 reconcile 返回 executed。；二轮：参数无效的写调用 → 结果为原码、台账 not_executed、会话保留；同一 operation_id 修正参数后可重新执行
- 依赖：S08-I35、S08-I36；S13（台账新增 published 状态）
- 兼容和风险：台账新状态需 ledger 与对账界面认识；旧记录无 staged_outputs 按原流程。
- 工作量：中
- 影响：高

## S08-I49 非 Windows 平台的 Craft 文档能力：说明原因并提供只读后备
- 问题：文档引擎只在 Windows x64 提供（`rynmesh/node_apps/craft_native.py:32,47-48`）；不支持时 Craft 的 Agent 工具整体不出现（`craft_agent.py:88-98` supported 为空即返回 []），ryn_list_plugins 也不说原因，macOS 上 Agent 完全不能读 Office/PDF，模型只会说"不支持"。
- 涉及行：E110-28
- 要改：
  - `rynmesh/node_apps/plugin_agent.py:84-117` —— Craft 应用在不支持的平台上列出并带 `unavailable_reason: "platform_unsupported"` 与平台名。
  - `craft_agent.py:88-98` specs —— 不支持平台时仍提供只读工具 `ryn_craft_read` 的后备实现：PDF 用已有依赖 pypdf（`pyproject.toml:41`，`craft_agent.py:322-325` 已在用）抽文本与页数；docx/xlsx/pptx 用标准库 zipfile + xml 抽正文、表格单元格与幻灯片文本（不新增依赖）；返回 `engine: "fallback_readonly"`。
  - 写入与转换在非 Windows 仍不可用，结果给明确原因。
- 新增测试：模拟 platform_supported=False：ryn_list_plugins 带 unavailable_reason；后备读取样例 docx/xlsx/pptx/pdf 返回文本；写工具不出现。
- 依赖：S08-I34（ryn_list_plugins 结构）
- 兼容和风险：非 Windows 写入能力是否要另带上游引擎（包体积、授权）需老板决定，本项不做写入。
- 工作量：中
- 影响：中

## S08-I50 把公司插件的认证细节从核心挪到插件清单声明
- 问题：通用 OAuth 管理器与扩展存储写死了公司产品细节：三个公司插件 id（`mcp_oauth.py:185`，`company_authorization.py:23`）、服务器 id `company-workspace-mcp`/`company-dashboard-dashboard` 与登录动作（`mcp_oauth.py:473-478`）、`company-workspace` 判定（`mcp_oauth.py:950`，`company_authorization.py:166`）、生产地址 `https://dashboard-hk-api.gshopper.com.cn/ryn/workspace/authorize`（`mcp_oauth.py:965`），以及重导入免停用名单（`store.py:3045-3054`）。第三方插件不能复用，核心升级与公司插件耦合；rynmesh 是公开仓库，生产地址写在核心里也不合适。
- 涉及行：E105-24
- 二轮漏项：G18-32, G19-24, G26-13
- 要改：
  - 插件清单 `ryn.authProvider` 新字段（`imports.py:1323-1329` 读 ryn 段处校验）：`kind:"company_account"`、`workspace_authorize_url`（须 https）、`account_servers`（触发账号登录动作的服务器 key）、`reserved_ids`；只接受签名公司包（S08-I19 的受信路径）。
  - `mcp_oauth.py:185,473-478,950,965` —— 公司插件 id、服务器 id、授权地址都从已安装插件的 authProvider 声明读取；`company_authorization.py:23,166,257` 同样改读声明。
  - `store.py:3045-3054` —— 免停用判定改为"插件声明了 authProvider 且来自受信路径"，不再写死 id（与 S08-I18 的来源继承合并）。
  - 公司插件仓库（D:/code/chat-backend/integrations/company-app）清单补上这些声明，两仓分别集成、记录版本（AGENTS.md 要求）。
  - 二轮补：`webapp/src/screens/ExtensionWebApp.tsx:26,45,231-233` —— COMPANY_PAGES 改为读 overview 里插件的 authProvider 声明（kind=company_account 的插件才允许 company.session.bootstrap）；生命周期上报改由 S08-I46 的 reports_lifecycle 决定。
  - 二轮补：`webapp/src/screens/McpAuthSettings.tsx:17,87-89` —— 公司判定与账号登录服务器（company-workspace-mcp / company-dashboard-dashboard）改读 authProvider.account_servers。
  - 二轮补：`webapp/src/domain/agentExtensions.ts:151-155` —— 所有 OAuth 登录成功都广播 `ryn:mcp-authorization-changed`（detail 带 server_id、plugin_id）；公司插件页订阅通用事件，`ryn:company-authorization-changed` 再保留一个版本作兼容。
  - 二轮补：`webapp/src-tauri/src/lib.rs:47-79` 写死 wework.gshopper.cn 的发票 PDF、公司详情、VAT 页面规则与 `:141-170` on_new_window —— 改由插件清单声明 `authProvider.document_windows`（host、path 或前缀、允许的查询键，须 https），外壳启动和插件变化时经节点接口读取，只做通用校验；过渡期保留现有规则作兼容默认，公司插件发布声明后删除。
  - 二轮补：`webapp/src/screens/KeptPluginPages.tsx:11` 保活数 3 是通用 LRU 上限，与插件 id 无关，不改。
- 新增测试：mcp_oauth 测试用一个假 authProvider 插件跑通账号登录动作与授权校验；核心源码 grep 不再出现 gshopper 域名与 company-* 字面 id（测试里断言）；旧公司插件包（无声明）升级前保持原行为的兼容分支。；二轮：vitest：假 authProvider 插件跑通 bootstrap 与账号登录；非公司插件登录成功也广播通用事件；rust：清单声明的文档窗口放行，未声明主机拒绝；前端与外壳源码 grep 不再出现 gshopper 域名与 company-* 字面 id（兼容分支除外）
- 依赖：S08-I18、S08-I19；公司插件仓库同步改清单
- 兼容和风险：过渡期需兼容未声明的旧公司包（按旧 id 识别，带弃用日志），公司插件发布新版后再删兼容分支。动的是登录与授权路径，需在宿主机做公司账号一次扫码的回归。
- 工作量：大
- 影响：中

## S08-I51 扩展存储读路径减少整库深拷贝与持锁哈希
- 问题：非 borrow 的 `_read` 每次整库 deepcopy（`store.py:524-525`）；`list_enabled_mcp_servers_internal` 先整库拷贝再逐个拷贝服务器（`:1438-1462`）；`select_skills` 在 file_transaction 内对每个技能目录算 `compute_directory_manifest`（`:1123-1143`）。这些在任务启动和每轮工具目录构建时反复调用。
- 涉及行：E105-48
- 二轮漏项：G21-39
- 要改：
  - `store.py:1438-1462` —— 改 `_read(borrow=True)`，只 deepcopy 要返回的服务器条目；`list_mcp_servers_internal`（`:1414`）、`get_mcp_server_internal`（`:1369-1412`）同样处理。
  - `store.py:1123-1143` select_skills —— 技能目录 manifest 结果按（package_dir、目录 mtime、文件数）缓存在内存，命中时不重算；在锁外算，锁内只比对缓存键与 manifest_sha256。
  - 二轮补：`rynmesh/agent_runtime/extensions/mcp_oauth.py:1092-1093` public_status —— 新增可选参数 `server=`，调用方已有记录时直接用，不再遍历 list_mcp_servers_internal；`rynmesh/agent_runtime/extensions/mcp_transport.py:1236-1240` refresh_tool_catalogs 循环传入已取到的 server；无参数时用 instance_id→server_id 的内存映射（随存储变更戳失效）再 get_mcp_server_internal。
- 新增测试：store 测试：连续 100 次 list_enabled_mcp_servers_internal 期间 deepcopy 调用数与服务器数成正比；返回值被调用方修改不影响缓存；技能文件被改后 select_skills 仍报 agent_skill_integrity_failed。；二轮：refresh 10 台服务器时 list_mcp_servers_internal 只调 1 次
- 依赖：无
- 兼容和风险：borrow 读取的对象绝不能被返回或修改，代码评审要逐处确认。
- 工作量：小
- 影响：低

## S08-I52 OAuth 凭据文件读不出时自愈，不拖垮全部 MCP
- 问题：McpOAuthStore._read 对解密失败、格式不符一律抛 agent_mcp_oauth_storage_unavailable，没有恢复（`rynmesh/agent_runtime/extensions/mcp_oauth.py:195-213`）；McpOAuthManager 构造时就读这个文件（`rynmesh/agent_runtime/extensions/mcp_oauth.py:321`），McpSessionManager 在构造里建 McpOAuthManager（`rynmesh/agent_runtime/extensions/mcp_transport.py:1205`），adapter 建会话管理器不捕获（`rynmesh/agent_runtime/extensions/adapter.py:312,353`）。文件损坏或节点密钥变化后，连不需要登录的 stdio MCP 也全部不可用。Codex 解析失败同样只返回错误（`rmcp-client/src/oauth.rs:1118-1123`），这是 Ryn 自身问题。
- 涉及行：（二轮漏项）G21-34
- 要改：
  - `rynmesh/agent_runtime/extensions/mcp_oauth.py:195-213` McpOAuthStore._read —— 区分"读文件 IO 错误"（瞬时，仍抛 storage_unavailable）与"解密失败/版本或结构不符"（损坏）：损坏时在文件锁内把文件改名为 `mcp_oauth.json.corrupt-<UTC 时间>`（最多留 3 份），返回空状态并置 `recovered_at`，写 diagnostic_log（不含内容）。
  - `rynmesh/agent_runtime/extensions/mcp_oauth.py:316-323` McpOAuthManager.__init__ —— 公司授权 sync_bindings/start 包 try，失败只记日志，不让构造失败。
  - `rynmesh/agent_runtime/extensions/mcp_oauth.py:1092-1100` public_status —— 有 recovered_at 时，原本需登录的服务器返回 state=logged_out、error=agent_mcp_oauth_credentials_reset；`webapp/src/screens/McpAuthSettings.tsx` 与扩展页显示"登录信息已损坏并重置，请重新登录"。
- 新增测试：`tests/test_agent_mcp_oauth.py`：写入乱码 mcp_oauth.json → 新建 McpSessionManager 成功，stdio 服务器调用成功，OAuth 服务器为 logged_out 且带 credentials_reset，目录里有 .corrupt- 备份；读 IO 错误时仍报 storage_unavailable 且不改名。
- 依赖：无
- 兼容和风险：坏文件改名不删除，可人工恢复；公司账号需要重新扫码一次。
- 工作量：小
- 影响：高

## S08-I53 插件桌面路由：非 Windows 进程名可匹配，自动处理档只提醒不拒绝
- 问题：published_routes 只接受 `.exe` 结尾的进程名（`rynmesh/agent_runtime/desktop_routes.py:58-61`），check 按去路径的小写名比较（`rynmesh/agent_runtime/desktop_routes.py:11-12`），macOS/Linux 上插件路由永不匹配，"插件拒绝后禁止 UI 绕过"也失效。同一目标第一次桌面写必抛 agent_plugin_route_preferred/conflict（`rynmesh/agent_runtime/desktop_routes.py:31-49`），不分权限档，"自动处理"下也先拒一次；同一目标里该插件任一 PERMISSION_DENIED 让该插件全部进程的 UI 操作被拒（`rynmesh/agent_runtime/desktop_routes.py:19-28`），不看是不是同一业务工具。
- 涉及行：（二轮漏项）G27-7, G27-8
- 要改：
  - `rynmesh/agent_runtime/desktop_routes.py:58-61,67` published_routes —— 进程名正则放宽为 `[A-Za-z0-9_. -]{1,80}`（可带 .exe/.app）；存归一化名：casefold 后去掉 `.exe`、`.app` 后缀。
  - `rynmesh/agent_runtime/desktop_routes.py:11-12` check —— 观察到的进程名同样归一化后比较。
  - `rynmesh/agent_runtime/desktop_routes.py:19-28` —— PERMISSION_DENIED 回执只在其工具（binding.original_tool_name）属于匹配路由的 `tools` 时拦截；结果未知的重放拦截不变。
  - `rynmesh/agent_runtime/desktop_routes.py:31-49` —— 从已读的任务行与全局设置取 permission_mode（同 `rynmesh/agent_runtime/execution_policy.py:240`，两者都是 automatic 才算自动档）；自动档不抛 route_preferred/conflict，返回提醒文字；`rynmesh/agent_runtime/native_computer.py:1972-1974` 把返回值放进 plan_out 的 `route_notice`，随工具结果给模型。其他档保持首次拒绝。PERMISSION_DENIED 与结果未知的拦截在所有档都保留（是业务权限，不是确认）。
- 新增测试：`tests/test_desktop_routes.py`：声明 "WeChat.app" 时观察到的 "WeChat" 匹配；自动档首次桌面写不被拒且结果带 route_notice；confirm_changes 档仍拒一次；插件对工具 A 的 PERMISSION_DENIED 不拦截只含工具 B 的路由。
- 依赖：S11（native_computer 结果字段）
- 兼容和风险：Windows 的 .exe 声明照旧匹配；公司插件声明不用改。
- 工作量：小
- 影响：中

## S08-I54 MCP 图片与导出文件：无工作区也能保存，任务遗忘时清理
- 问题：图片和导出文件固定写工作区 `.ryn/mcp/<task>/<op>/`（`rynmesh/agent_runtime/extensions/adapter.py:1075-1077,1132-1134`，经 `self.local.path`）；无工作区时 `rynmesh/agent_runtime/local_tools.py:530-533` 抛 agent_workspace_required，图片分支把它改名为 agent_mcp_image_invalid（`rynmesh/agent_runtime/extensions/adapter.py:1105-1107`），导出分支整个结果失败。只用公司插件、不选项目文件夹时，返回图表或 xlsx 的工具全部失败。这些文件只增不减（全仓只有 `rynmesh/agent_runtime/extensions/adapter.py:1075,1132` 与 `rynmesh/agent_runtime/code_mode.py:1801` 写，无清理），还可能被 git 带上。
- 涉及行：（二轮漏项）G22-7, G22-8
- 要改：
  - `rynmesh/agent_runtime/extensions/adapter.py` 新增 `_mcp_output_target(rel_path, context)`：有工作区同现在；无工作区落到 agent 私有目录 `<agent 数据目录>/task_files/<safe_task>/mcp/<safe_op>/`，返回 (target_path, display_path)，display_path 用 `ryn-task:mcp/<safe_op>/<文件名>`；`:1075-1079`、`:1132-1135` 改用它。
  - `rynmesh/agent_runtime/extensions/adapter.py:1105-1107` —— agent_workspace_required 等非图片问题不再改名为 agent_mcp_image_invalid，按原码处理。
  - `rynmesh/agent_runtime/image_capabilities.py:1053-1070` verify_and_load_mcp_image_artifact —— 接受私有目录根（同样的无符号链接、字节数与 SHA-256 校验）；`rynmesh/agent_runtime/code_mode.py:1801` 同一写法改用 `_mcp_output_target`（S11 文件，需 S11 确认）。
  - 在工作区建 `.ryn/` 时若无 `.ryn/.gitignore` 则写一份 `*`（只在 Ryn 自建目录）。
  - `rynmesh/agent_runtime/extensions/adapter.py:393-401` forget_task —— 删除该任务私有 `task_files/<safe_task>/mcp`；工作区 `.ryn/mcp/<safe_task>` 只删台账记录哈希与磁盘一致的文件（主人改过的保留）。
- 新增测试：无工作区调用返回 PNG 与 xlsx 的工具，结果带私有路径且 ryn_image_inspect 可读；workspace_required 不再报 agent_mcp_image_invalid；forget_task 后私有目录被删、改过的工作区文件保留；`.ryn/.gitignore` 存在。
- 依赖：S11（图片校验与 code mode 同路径）、S08-I14
- 兼容和风险：旧任务的工作区文件不动；私有目录计入 agent 数据大小。
- 工作量：中
- 影响：中

## S08-I55 带服务预览的写工具：只 prepare 一次，大参数预览给摘要，操作凭证临发时生成
- 问题：带 X-Ryn-Service-Token 或 x-ryn-operation=1 的写工具，preview 向服务器 prepare 一次（`rynmesh/agent_runtime/extensions/adapter.py:942-957`）；execute 又调 preview 再 prepare 一次，并要求与 loop 传入的预览完全相等（`rynmesh/agent_runtime/extensions/adapter.py:1360-1365`；`rynmesh/agent_runtime/loop.py:1525,1662-1663`）。服务器 prepare 不稳定或第二次失败，写操作被拒为 agent_approval_preview_changed/unavailable，"自动处理"档也一样。preview 原样带完整参数（`rynmesh/agent_runtime/extensions/adapter.py:978`），loop 按 max(max_output_chars, 参数上限) 检查预览（`rynmesh/agent_runtime/loop.py:1526-1527`），大参数写工具报 agent_output_limit。trusted_operation 的 expires_at=now+60 在 call_tool 之前生成（`rynmesh/agent_runtime/extensions/adapter.py:1529-1539`），建连或初始化慢时服务端收到可能已过期。
- 涉及行：（二轮漏项）G22-12, G22-13, G22-17
- 要改：
  - `rynmesh/agent_runtime/extensions/adapter.py:942-957` preview —— 新增参数 `reuse_service_preview`；`:1360-1365` execute 有 expected_preview 时，本地部分（标题、绑定、参数、artifact）照常重算比较，服务端部分直接复用 expected_preview 的 service_preview，不再二次 prepare。其 digest 在 `:1536` 作为 preview_digest 发给服务器，由服务器执行时核对，安全性不降。
  - 自动档不跳过 prepare（preview_digest 是服务端的执行凭证，不是确认对话框）；prepare 失败给 correction"插件服务暂时无法生成操作预览，请稍后重试同一调用"。
  - `rynmesh/agent_runtime/extensions/adapter.py:978` —— operation 编码超过 16 KiB 时预览改放摘要：字段名、各字段类型与长度、前 2000 字、整体 sha256；完整参数仍由台账记录（`:1519` ledger 与 args_sha）。
  - `rynmesh/agent_runtime/extensions/adapter.py:1509` _before_write_dispatch —— 在派发回调里重写 trusted_operation 的 expires_at=now+60（同一字典引用，`rynmesh/agent_runtime/extensions/mcp_transport.py:1165-1167` 放进 params 后由 _exchange 发送时序列化，实现时核对顺序）。
- 新增测试：每次 prepare 返回不同 fields 的假服务：一次写调用只收到 1 次 tools/prepare 且执行成功；自动档执行成功；30 KB 参数的写工具预览为摘要且执行成功；initialize 人为延迟 70 s 时服务器收到的 expires_at 仍在未来。
- 依赖：无（公司插件服务端是否要求执行前重新 prepare 需在插件仓库核对）
- 兼容和风险：非自动档审批框里大参数只显示摘要，需写明"完整参数已记录"。
- 工作量：中
- 影响：中

## S08-I56 插件网页桥接方法收紧
- 问题：插件页面经 ryn.plugin.request 调宿主方法时只按权限名放行，参数只查总长 64 KB（`webapp/src/screens/ExtensionWebApp.tsx:231`）。有 models 权限的页面可直接 models.share 调 setCLISharing 改模型共享（`webapp/src/screens/ExtensionWebApp.tsx:250`），与紧邻注释"共享在模型池管理，插件只能跳转"相反；有 messages 权限可 messages.send 以主人身份发消息（`:254`）；peer.publish 直接发布（`:262`）。
- 涉及行：（二轮漏项）G18-29
- 要改：
  - `webapp/src/screens/ExtensionWebApp.tsx:250` —— 删除 models.share，返回 permission_denied，提示改用 models.manage 跳转。
  - `webapp/src/screens/ExtensionWebApp.tsx:254` messages.send 与 `:262` peer.publish —— 宿主弹确认框（显示收件人与正文前 500 字，或要发布的模型列表），主人确认才执行；同一页面会话可选"本次不再询问"。
  - `methods` 表旁新增 `validators`：每个方法校验参数结构（类型、长度、id 格式），不合格回 invalid_arguments（错误分类与 S08-I46 一致）。
- 新增测试：vitest：页面调 models.share 得 permission_denied；messages.send 弹确认，取消时不调 sendMessage；参数类型错得 invalid_arguments。
- 依赖：S08-I46（错误分类）
- 兼容和风险：公司插件页面若在用 models.share/messages.send，需在插件仓库同步改（两仓分别集成、记录版本）。
- 工作量：小
- 影响：中

## S08-I57 导入预览展示实际要执行的内容与未导入组件
- 问题：导入确认页对 MCP 服务器只显示 command 或 url（`webapp/src/screens/AgentExtensions.tsx:4843-4848`），技能只显示标题（`webapp/src/screens/AgentExtensions.tsx:4828-4831`）；后端预览已返回技能文件、env 键名、header 键名、是否含密钥（`rynmesh/agent_runtime/extensions/imports.py:574,595-599`），类型里也有（`webapp/src/domain/agentExtensions.ts:417-442`），界面没用，主人确认前看不到完整命令行和附带脚本。agents/ 子 agent 定义被忽略（`rynmesh/agent_runtime/extensions/imports.py:63,76`），只在页尾一行小字列出（`webapp/src/screens/AgentExtensions.tsx:4855-4866`），主人以为插件已完整安装。Codex core-plugins 也没有插件 agents 的加载（只发现 hooks，`core-plugins/src/loader.rs:1221-1240`），这里只补展示。
- 涉及行：（二轮漏项）G19-44, G21-48
- 要改：
  - `webapp/src/screens/AgentExtensions.tsx:4843-4848` —— stdio 服务器显示完整命令（command 与逐个参数，不拼 shell 字符串）、cwd、env 键名（不显示值）、是否含密钥；HTTP 服务器显示 url、header 键名、认证方式。
  - `webapp/src/screens/AgentExtensions.tsx:4828-4831` —— 技能可展开文件清单（路径、大小；.sh/.ps1/.py/.js 等标"可执行脚本"）。
  - `webapp/src/screens/AgentExtensions.tsx:4855-4866` —— 未导入组件改为提交按钮上方的警示块，逐项说明"导入后不会生效"及原因；hooks 在 S08H-I2、commands 在 S08-I22 落地后从列表去掉。
  - `rynmesh/agent_runtime/extensions/imports.py:63,76` —— agents 提示文案改为"Ryn 不导入子 agent 定义，可把其中的提示改写成技能"。
- 新增测试：vitest：预览含 args 与 env_keys 时逐项显示且不显示值；技能文件清单可展开；含 agents/ 的插件预览出现警示块。
- 依赖：S08-I25（同一确认页的 diff 展示）
- 兼容和风险：只改界面与文案，无存储变化。
- 工作量：小
- 影响：中

## S08-I58 插件本地服务守护：启动时限可配、失败退避且可见、更新失败后恢复守护
- 问题：启动健康等待硬 30 s（`webapp/src-tauri/src/plugin_services.rs:206-220`），超时 owned 被丢弃（Drop 杀进程，`:34-44`），watch 每 30 s 再试（`:310`），无退避，界面不知原因；被 stop 打断也报 plugin_service_start_timeout（`:209,220`）。更新安装失败时 `webapp/src-tauri/src/updates.rs:313-320` 先 stop 再 resume，但 watch 见 stopping 就退出线程（`plugin_services.rs:299,318`），resume（`:229`）只清标志，之后再无守护。services.json 全有或全无（`:56-74`，超 64 KB 拒绝 `:86-88`），watch 里 `if let Ok` 静默忽略错误（`:302`）。plugin_matches 要求插件声明 web_app 且网址正是服务端口（`:93-98`），纯 MCP 本地服务不会被拉起。每 2 s 拉完整扩展概览（上限 16 MiB，`:100-103`）只为判断启用。prepare 之后 spawn 失败、健康超时、提前退出都直接返回（`:202-220`），不作废这次凭据；节点侧待激活 60 s 过期（`rynmesh/agent_runtime/extensions/local_services.py:48,56-60`），冷启动超 60 s 时 activate 报 agent_local_service_stale，而 prepare 时已向公司授权通道登记（`rynmesh/agent_runtime/extensions/local_services.py:50-53`，登记逻辑 `rynmesh/agent_runtime/extensions/company_authorization.py:114-138`）。
- 涉及行：（二轮漏项）G19-2, G19-3, G19-7, G19-8, G19-41, G19-42, G19-43, G21-41
- 要改：
  - `webapp/src-tauri/src/plugin_services.rs:8-22` Service —— 加可选 `start_timeout_secs`（默认 90，上限 600，serde default）；`:206` 用它。
  - `webapp/src-tauri/src/plugin_services.rs:209-220` —— 循环结束先判 stopping，返回 plugin_service_stopping；失败写入新的 `PluginServices.last_errors`（id → 码、时间、连续次数）。
  - `webapp/src-tauri/src/plugin_services.rs:302-314` watch —— 失败按服务指数退避（30 s 起翻倍到 10 分钟），成功清零；新增 tauri 命令 `plugin_service_status` 返回 last_errors 与下次重试时间，插件页错误卡片（S08-I46）显示原因。
  - `webapp/src-tauri/src/plugin_services.rs:299,318` —— stopping 时不退出线程，只暂停等待，resume 后继续守护（不靠重新调 watch，避免重复线程）。
  - `webapp/src-tauri/src/plugin_services.rs:56-74` parse —— 版本与总数仍整体校验；单个服务字段不合规时跳过该服务并 warn（id 与原因），其余照常；`:302` installed() 出错写 warn 并记入 last_errors 的全局项。
  - `webapp/src-tauri/src/plugin_services.rs:93-98` plugin_matches —— 插件启用且在服务 plugins 内即匹配；只有插件声明了 web_app 时才要求其 url 指向 `http://127.0.0.1:<端口>`。
  - `webapp/src-tauri/src/plugin_services.rs:100-103` plugins() —— 改请求新增轻量接口 GET `/api/local/agent/extensions/plugins/states`（`rynmesh/agent_runtime/extensions/routes.py` 新增，只回 id、enabled、web_app.url、revision，不带工具表）。
  - `webapp/src-tauri/src/plugin_services.rs:202-220` —— prepare 后任何失败路径调新增 POST `/api/local/agent/extensions/local-service/discard`（`rynmesh/agent_runtime/extensions/routes.py:104` 旁）；`rynmesh/agent_runtime/extensions/local_services.py` 新增 discard：删 pending，若公司授权通道登记的 generation 等于这次的则清掉该凭据；`rynmesh/agent_runtime/extensions/local_services.py:48` 待激活过期放到 600 s（覆盖启动时限上限）。
- 新增测试：rust：启动 40 s 的假服务在默认时限内成功；连续失败的重试间隔翻倍；stop 后 resume，watch 仍会拉起崩溃的服务；services.json 一坏一好时好的启动；无 web_app 的纯 MCP 服务被拉起。Python：discard 后 activate 报 stale 且公司授权通道无该凭据；prepare 后 300 s 内 activate 成功。
- 依赖：S08-I37（ensure 锁外启动）
- 兼容和风险：Service 有 deny_unknown_fields，旧 Ryn 读到带 start_timeout_secs 的 services.json 会整体拒绝——公司插件包须在新 Ryn 发布后才加该字段；轻量接口为新增。
- 工作量：中
- 影响：中

## S08-I59 Unix 插件服务进程树：宽限后整组结束，崩溃残留可识别回收
- 问题：Unix 的 Drop 对进程组 SIGTERM 后立即 child.kill() 只杀直接子进程、wait 只等直接子进程（`webapp/src-tauri/src/plugin_services.rs:39-42`），孙进程无宽限也不保证结束；Windows 靠作业对象在桌面崩溃时连带结束整棵树（`:36-38,146-150`），macOS/Linux 没有。桌面异常退出后服务进程留着占端口，下次 ensure 判外部占用永远返回 plugin_service_port_in_use（`:200-201`）。
- 涉及行：（二轮漏项）G19-5, G19-6
- 要改：
  - `webapp/src-tauri/src/plugin_services.rs:34-44` Drop（unix）—— SIGTERM 进程组后最多等 2 s（轮询 try_wait），仍在则 `kill(-pgid, SIGKILL)` 整组，再 wait。
  - `webapp/src-tauri/src/plugin_services.rs:130-156` spawn —— 成功后写 `<log_dir>/plugin-<id>.pid`（pid、进程组、program 绝对路径），Drop 正常结束时删除；Linux 在 pre_exec 里设 `PR_SET_PDEATHSIG(SIGTERM)`。
  - `webapp/src-tauri/src/plugin_services.rs:200-201` ensure —— 端口被占时先读 pid 文件：进程仍在且可执行路径（Linux `/proc/<pid>/exe`，macOS `proc_pidpath`）等于本服务 program，才按上面的宽限流程结束整组后继续；否则仍报 port_in_use，错误详情带端口号供界面提示。
- 新增测试：rust（unix）：服务派生孙进程，Drop 后 3 s 内整组退出；模拟残留进程加 pid 文件，ensure 回收后启动成功；pid 文件指向别的程序时仍报 port_in_use。
- 依赖：无
- 兼容和风险：只影响 Unix；pid 复用由可执行路径核对防误杀。
- 工作量：中
- 影响：中

## S08-I60 公司授权后台同步：按需唤醒、临时停用不撤销、打开页面时短重试
- 问题：后台线程每 1 s 无条件 sync_bindings（列出并解密全部 MCP 服务器、开凭据文件事务）、flush、续期（`rynmesh/agent_runtime/extensions/company_authorization.py:84-94,96-112`），空闲也一直抢文件锁。绑定带 effective_enabled（`:103`），插件停用再启用就记一次 configuration_changed 撤销（`:107-111`），而 OAuth 行按 `_same_authorization` 忽略 enabled 保留令牌（`rynmesh/agent_runtime/extensions/mcp_oauth.py:337-343`），结果浏览器侧授权被撤要重新扫码。bootstrap 要本地服务 2 s 内 ACK（`rynmesh/agent_runtime/extensions/company_authorization.py:308-313`），否则直接 agent_local_service_unavailable（`:415-416`），服务启动中打开公司页面失败。
- 涉及行：（二轮漏项）G21-56, G21-58, G21-59
- 要改：
  - `rynmesh/agent_runtime/extensions/company_authorization.py:84-94` _worker —— 只在 `_wake` 置位、有未 ACK 序号或续期到点时运行，空闲等待 30 s；sync_bindings 先比扩展存储变更戳（同 S08-I11），没变跳过列服务器与开事务。配置变化处已调 changed_server 唤醒（`:366-371`）。
  - `rynmesh/agent_runtime/extensions/company_authorization.py:99-112` sync_bindings —— 比较绑定时去掉 enabled：enabled 变化记 `suspended`（新状态字段，flush 时通知本地服务暂停对这些插件下发会话，恢复启用后清除），不调 record_revocation；instance_id、config_generation、resource、auth 变化仍撤销。与 OAuth 行规则一致。
  - `rynmesh/agent_runtime/extensions/company_authorization.py:372-418` bootstrap —— flush 未 ACK 时在 5 s 内按 0.5 s、1 s、2 s 重试；仍失败且插件服务在启动中返回 agent_local_service_starting，否则 agent_local_service_unavailable；`webapp/src/screens/ExtensionWebApp.tsx:232-237` 显示"公司服务启动中，请稍后重试"与重试按钮。
- 新增测试：空闲 60 s 内服务器列表读取 ≤2 次；停用再启用公司插件后 revocations 无新增；本地服务首次 ACK 延迟 1.5 s 时 bootstrap 成功。
- 依赖：S08-I11（变更戳）、S08-I50；公司插件仓库本地服务需认 suspended 通知（两仓分别集成）
- 兼容和风险：停用期间浏览器会话暂停而不撤销，是安全语义变化，需公司插件服务端配合；变更戳漏判时最迟 30 s 兜底同步。
- 工作量：中
- 影响：中

## S08-I61 Craft 插件管理器：状态文件容错、启停不占全局锁、清理与退出不中断
- 问题：craft-plugins.json 超 64 KiB 或损坏时 _read 抛 plugin_state_invalid（`rynmesh/node_apps/plugins.py:228-242`），目录、启用、lease、agent 工具菜单全部失败，没有备份；configure 偏好只合并不删、写同一文件（`:381-400`），会把文件撑过上限。launch 持 self.lock 绑定端口、起 uvicorn、sleep 轮询最多 5 s（`:555-586`），_stop 在锁内 join 5 s（`:458-468`），期间 catalog/lease/generation 全卡住。_reconcile_cache_locations 在构造时同步 copytree，整段 except: pass（`:108-149`）。uninstall 只删当前版本目录（`:500-505`），旧版本与迁移副本只增不减。close() 某个 _stop 超时抛 plugin_busy 后其余不再停（`:590-595`）。ticket 满 64 报 plugin_busy（`:543-547`），与"插件正在操作"同码。
- 涉及行：（二轮漏项）G20-1, G20-2, G20-4, G20-6, G20-7, G20-8
- 要改：
  - `rynmesh/node_apps/plugins.py:228-242` _read 与 `:241-242` _write —— 写前把旧文件复制为 `.bak`；读坏或超限时先试 .bak；都坏时坏文件改名 `.corrupt-<ts>`，以空状态继续并在 catalog 带 `state_recovered: true`；超限单独报 plugin_state_too_large 并给上限。
  - `rynmesh/node_apps/plugins.py:381-400` configure —— 每插件偏好序列化后 ≤8 KiB，超了报 plugin_preferences_too_large。
  - `rynmesh/node_apps/plugins.py:555-586` launch —— 锁内只校验 ticket 并放 Starting 占位；绑定端口、起线程、等 started 在锁外，完成后短持锁写入；`:458-468` _stop 锁内取出条目，锁外 join。
  - `rynmesh/node_apps/plugins.py:108-149` _reconcile_cache_locations —— 放后台线程执行一次，异常写日志并在 catalog 带 `migration_error`；复制并核对后删除旧位置副本。
  - `rynmesh/node_apps/plugins.py:500-505` uninstall —— 校验 parent 在 cache 内后删整个插件目录；更新成功后清掉非当前、非上一版本的目录（留一代可回滚）。
  - `rynmesh/node_apps/plugins.py:590-595` close —— 逐个 try，收集失败继续停其他，最后记日志。
  - `rynmesh/node_apps/plugins.py:543-547` —— ticket 满报 plugin_launch_ticket_limit（文案"打开请求过多，请稍后再试"）。
- 新增测试：`tests/test_node_apps_plugins.py`：状态文件写坏后 catalog 可用且 state_recovered=true，.bak 可恢复；慢启动编辑器期间另一线程 catalog() 不等待；close 时一个线程卡住其余照常停；卸载后插件目录不存在；ticket 满时码为 plugin_launch_ticket_limit。
- 依赖：S06-I17、S06-I18（同文件 catalog 与 lease 改动，需合并）
- 兼容和风险：状态文件格式不变；锁外启动用 Starting 占位防同一插件并发 launch。
- 工作量：中
- 影响：中

## S08-I62 Craft 文档组件下载：可配置下载源与可读失败原因
- 问题：文档引擎与网页组件固定从 github.com releases 下载（`rynmesh/node_apps/craft_native.py:322`；`rynmesh/node_apps/craft_host.py:235`），download_to 超时 60 s（`rynmesh/node_apps/craft_host.py:95-102`），重定向只放行 GitHub 主机（`rynmesh/node_apps/craft_host.py:34-38`），没有镜像；国内网络常失败，只得到通用下载错误。S02-I25 的镜像只覆盖桌面更新。
- 涉及行：（二轮漏项）G20-20
- 要改：
  - `rynmesh/node_apps/craft_host.py:95-102` download_to —— 接受下载源列表：节点设置 `craft.download_mirrors`（须 https）在前，GitHub 在后，逐个尝试，每个源单独超时（连接 15 s、读 60 s）；内容仍按现有 sha256 校验，来源变化不降安全；`:34-38` 重定向白名单加入已配置镜像主机。
  - `rynmesh/node_apps/craft_native.py:322` 与 `rynmesh/node_apps/craft_host.py:235` —— 按源列表构造地址（镜像路径约定 `<mirror>/<slug>/<tag>/<asset>`）。
  - 失败分码：craft_download_timeout、craft_download_network、craft_download_http_<状态>、craft_download_checksum；进度事件带当前源主机；应用页安装失败提示显示原因与"可在设置里配置下载源"。
- 新增测试：第一个源超时、第二个源成功；sha256 不符报 checksum 且不安装；全部失败时码为 network 或 timeout。
- 依赖：无（镜像由谁提供——公司服务器或第三方代理——需老板定，代码只提供配置项）
- 兼容和风险：只加源不改校验；镜像内容须与 GitHub 发布物逐字节一致。
- 工作量：小
- 影响：中

## S08-I63 Craft 工具加固（低）：错误码、私有目录、环境变量、读分页缓存、回读复用、来源链接
- 问题：参数超 64000 字节与"工具不支持"同报 agent_craft_tool_not_supported（`rynmesh/node_apps/craft_agent.py:244-250`）；输入文件缺失与超 64 MiB 合报且不说上限（`rynmesh/node_apps/craft_agent.py:50-52`）；读工具每次带 offset 都重新 tools_call 再切片（`rynmesh/node_apps/craft_agent.py:486-488`，`:343-362`），两次之间文档变了会错位；缺 inputSchema 时 KeyError（`rynmesh/node_apps/craft_agent.py:172,251`）；每次保存另起一个 McpProcess 回读（`rynmesh/node_apps/craft_agent.py:320-340`）；私有 HOME/TEMP 建在用户工作区 `.ryn-craft-*`（`rynmesh/node_apps/craft_native.py:421-422`），输出暂存 `.ryn-craft-output-*` 也在工作区（`rynmesh/node_apps/craft_agent.py:398`）；环境白名单缺 Windows 系统变量（`rynmesh/node_apps/craft_native.py:385-392`）；ryn_craft_editor_info 的 source_url 写死（`rynmesh/node_apps/plugin_agent.py:150`），目录行已用 update_source.url（`rynmesh/node_apps/plugins.py:293`）。
- 涉及行：（二轮漏项）G20-12, G20-19, G20-27, G20-28, G20-30, G20-31, G20-32, G20-70
- 要改：
  - `rynmesh/node_apps/craft_agent.py:244-250` _arguments —— 超长参数单独报 agent_craft_arguments_too_large，details 带上限 64000。
  - `rynmesh/node_apps/craft_agent.py:50-52` file_hash —— 分为 agent_craft_file_missing 与 agent_craft_file_too_large（details 带上限与实际大小）。
  - `rynmesh/node_apps/craft_agent.py:486-488` —— 首次读结果按 (session id, session_revision, 工具, 参数 sha256) 缓存在会话里（最多 4 条、合计 8 MiB），带 offset 时读缓存；revision 变化即失效，缓存缺失时重算。
  - `rynmesh/node_apps/craft_agent.py:169-176,251` —— 工具缺 inputSchema 或非对象时补 `{"type":"object","properties":{}}`。
  - `rynmesh/node_apps/craft_agent.py:320-340` _reopen —— 每任务每应用复用一个回读进程（懒建，随 close_task 关闭），不再每次保存起新进程（S06-I18 去掉全局锁后合并）。
  - `rynmesh/node_apps/craft_native.py:421-422` —— 私有 HOME/TEMP 改建在 Ryn 数据目录（`manager.cache` 同级 `craft_runtime/<会话>`）；`rynmesh/node_apps/craft_agent.py:398` 输出暂存受引擎写根限制（`rynmesh/node_apps/craft_native.py:424-427` 只放行工作区）仍在工作区，但收进 `.ryn/craft-output/`（带 `.gitignore`），会话启动时清扫超过 1 小时的遗留。
  - `rynmesh/node_apps/craft_native.py:385-392` _environment —— 白名单加 SYSTEMDRIVE、PROGRAMFILES、PROGRAMFILES(X86)、PROGRAMDATA、NUMBER_OF_PROCESSORS、PROCESSOR_ARCHITECTURE、OS、TZ（不含凭据类变量）。
  - `rynmesh/node_apps/plugin_agent.py:150` —— source_url 取 catalog 行的 source_url。
- 新增测试：`tests/test_craft_agent.py`：超长参数得 arguments_too_large；缺失与超限两个码；翻页时 tools_call 只调 1 次、revision 变化后重算；缺 inputSchema 的工具可列出；两次保存只起 1 个回读进程；工作区里不再出现 .ryn-craft- 目录；editor_info 的 source_url 与目录一致。
- 依赖：S08-I35、S08-I48；S06-I18
- 兼容和风险：私有目录挪位置后旧的 .ryn-craft- 遗留需一次性清扫；引擎写根限制以 CLI 实际参数为准，实现时核对。
- 工作量：中
- 影响：低

## S08-I64 扩展核心加固（低）：缓存键、恢复诊断、判空、参数校验、删除顺序、迁移事务、死代码
- 问题：工具缓存键含当前秒数与 id(对象)（`rynmesh/agent_runtime/extensions/adapter.py:497-505`），有 OAuth 时每秒失效，id 复用可能命中旧缓存；写后结果恢复吞掉所有异常返回 None（`rynmesh/agent_runtime/extensions/adapter.py:1684-1719`），看不出为何没恢复；download_authority 直接用 `self.manager.oauth.access_token`（`rynmesh/agent_runtime/extensions/adapter.py:995`），别处都先判空（`:368`）；tool-query 非 dict 参数静默换 {}（`rynmesh/agent_runtime/extensions/views.py:274`），schema 本身不合法也报 agent_invalid_request（`rynmesh/agent_runtime/extensions/views.py:174-183`）；删服务器先通知授权通道再删（`rynmesh/agent_runtime/extensions/routes.py:283-291`），删失败时状态已变；activate 后的 rediscover 只捕获 AgentError（`rynmesh/agent_runtime/extensions/routes.py:110-118`），其他异常让后面的服务器不再探测；migrate_release_defaults 在锁外读 baseline、新增项另开事务（`rynmesh/agent_runtime/extensions/store.py:2730-2739`）；preview_extension_import 无调用方（`rynmesh/agent_runtime/extensions/imports.py:1549-1550`）。
- 涉及行：（二轮漏项）G22-16, G22-18, G22-19, G20-41, G20-44, G20-46, G20-68, G21-51
- 要改：
  - `rynmesh/agent_runtime/extensions/adapter.py:497-505` _mcp_tools_cache_key —— 去掉 int(clock()) 与各 id()，改用 ExtensionStore.revision()（`rynmesh/agent_runtime/extensions/store.py:634`）、OAuth 文件戳与"最早令牌到期时间是否已过"的布尔值。
  - `rynmesh/agent_runtime/extensions/adapter.py:1684-1719` _recover_declared_result —— 每个 return None 带原因码（no_declaration、status_tool_not_read、status_is_error、exception:<类名>），调用处写进台账 error_result.recovery。
  - `rynmesh/agent_runtime/extensions/adapter.py:995` —— `getattr(self.manager, "oauth", None)` 判空，没有时不加 Authorization。
  - `rynmesh/agent_runtime/extensions/views.py:274` —— arguments 存在但不是对象时报 agent_invalid_request；`:174-183` check_schema 失败单独报 agent_plugin_tool_schema_invalid。
  - `rynmesh/agent_runtime/extensions/routes.py:283-291` —— delete_mcp_server 成功后再调 channel.changed_server(previous, {})。
  - `rynmesh/agent_runtime/extensions/routes.py:110-118` —— rediscover 逐个捕获所有异常并写日志，继续下一台。
  - `rynmesh/agent_runtime/extensions/store.py:2730-2739` migrate_release_defaults —— baseline 读取与新增项提交放进同一个 file_transaction（commit_imported_items 拆出持锁内部版本复用）。
  - `rynmesh/agent_runtime/extensions/imports.py:1549-1550` —— 删除 preview_extension_import（`rynmesh/agent_runtime/extensions/routes.py:650` 是同名局部路由函数，不受影响）。
- 新增测试：有 OAuth 时连续两秒取工具表只构建一次；恢复失败台账带原因；manager 无 oauth 时 download_authority 不抛；tool-query 传数组参数被拒、坏 schema 得 schema_invalid；删除版本冲突时授权通道状态不变；rediscover 中一台抛 ValueError 后下一台仍被探测；迁移期间并发写不丢新增项。
- 依赖：S08-I11（变更戳）、S08-I51
- 兼容和风险：均为局部修正，无存储格式变化。
- 工作量：中
- 影响：低

## S08-I65 应用页插件安装与扩展状态刷新（低）
- 问题：批量安装时单项失败即 throw，后面选中的不装，也无失败清单（`webapp/src/screens/CraftApps.tsx:121-145`）；安装进度每 180 ms 轮询（`webapp/src/screens/CraftApps.tsx:114`）；扩展概览加载失败被静默吞掉（`webapp/src/screens/CraftApps.tsx:67-74`）；AppsLauncher 声明 onRefreshExtensions（`webapp/src/screens/AppsLauncher.tsx:20`）却不解构（`:33`），CraftApps 传了（`webapp/src/screens/CraftApps.tsx:296`）也从不调用；Shell 每 10 s 和每次聚焦用 Promise.all 同时拉插件目录与扩展概览，任一失败两者都不更新（`webapp/src/personal/Shell.tsx:45-62`）。
- 涉及行：（二轮漏项）G02-32, G02-33, G02-37, G02-45
- 要改：
  - `webapp/src/screens/CraftApps.tsx:121-145` install —— 逐个安装，单项失败记入 failed 列表继续下一个；结束后列出失败项与"重试失败项"按钮。
  - `webapp/src/screens/CraftApps.tsx:114` —— 进度轮询改 750 ms。
  - `webapp/src/screens/CraftApps.tsx:67-74` loadExtensions —— 失败设 extensionsUnavailable，AppsLauncher 显示"扩展状态暂不可用"与重试。
  - `webapp/src/screens/AppsLauncher.tsx:33` —— 解构 onRefreshExtensions，接到上面的重试按钮。
  - `webapp/src/personal/Shell.tsx:45-62` —— 两个请求用 Promise.allSettled 各自更新（失败的保留上次结果）；定时兜底放到 60 s；扩展插件启用或导入成功后也派发 `ryn-installed-apps-changed`（现只在 `webapp/src/domain/craftPlugins.ts:67` 派发）。
- 新增测试：vitest：三个插件中第二个安装失败，第三个仍安装并列出失败项；概览失败时显示暂不可用并可重试；Shell 中扩展概览失败时插件目录仍更新。
- 依赖：S08-I38（AppsLauncher 卡片状态同文件）
- 兼容和风险：只改界面。
- 工作量：小
- 影响：低

## S08-I66 插件页面面板：错误分类、重挂载保留结果、复杂值展示（低）
- 问题：`webapp/src/screens/PluginViews.tsx:143-152` 结构不合规（抛的 `invalid_view_result` 无错误码）与未知码都显示"请检查日期、筛选条件和账号权限"，真实原因不显示；后端把所有失败压成 agent_plugin_view_query_failed（`rynmesh/agent_runtime/extensions/views.py:186-191,263-265,310-312`）。面板 key 是含 revision、oauth.state、discovery_revision 的整串（`PluginViews.tsx:58-60,72`），后台刷新一次就重挂面板，已查结果与填的条件全丢。formatViewValue 对对象、数组、超 4096 字符一律 "—"（`:19-23`），详情面板（`:214`）同样。
- 涉及行：（F2 转入）G18-33, G18-39, G18-40, G20-37
- 要改：
  - `rynmesh/agent_runtime/extensions/views.py:186-191,263-265,310-312` —— `_result` 与两处 except：MCP isError 时取结果里首个 text 内容，脱敏后截 300 字符放进 AgentError 的 details（随 S08-I70 的响应格式带回前端）；只有 text、无 structuredContent 时尝试把 text 当 JSON 解析为 dict 再走原校验，失败码改 `agent_plugin_view_bad_result`；服务器异常保持 agent_plugin_view_query_failed 但带异常类别（timeout/transport）。
  - `PluginViews.tsx:138-143` —— 结构校验失败抛带码的 `AgentExtensionError("agent_plugin_view_bad_result")`；`:148-152` 为该码显示"插件返回格式不正确，请联系插件作者"，其他未知码显示"查询未完成（<错误码>）"，原"请检查日期、筛选条件和账号权限"只留给确定属于输入问题的码。
  - `PluginViews.tsx:58-60,72` —— `binding` 只放影响查询合法性的字段（plugin.id、plugin.instance_id、view.id、server.instance_id、server.config_generation、server.enabled、tool 的 owner_read_only）；revision、oauth.state、discovery_revision 的变化不再重挂面板，改为在面板内标"配置已更新，结果可能过期"并给"重新查询"按钮。
  - `PluginViews.tsx:19-23` formatViewValue —— 对象、数组返回 JSON 字符串（深度 3、2048 字符截断）；列表单元格显示截断文本，详情面板（`:214`）用 `<details>` 折叠显示完整 JSON；字符串超 4096 时截断并加"…（展开）"，不再整体变 "—"；money/number 的格式化路径不变。
- 新增测试：`webapp/src/screens/PluginViews.test.tsx`：结构不合规时显示格式不正确提示；未知码显示码；后台 revision 变化后输入与结果仍在并显示"可能过期"；对象值在详情面板折叠展示。Python views 测试：isError 带文本时错误详情含截断文本；只有文本的 JSON 结果被接受。
- 依赖：S08-I70（错误细节随接口返回）
- 兼容和风险：返回的错误文本来自第三方服务器，必须脱敏并截断；结果展示只改格式，不改查询参数。
- 工作量：小
- 影响：中

## S08-I67 插件工作台与插件库：错误类型、缓存失效、独立窗口、按能力显示（低）
- 问题：插件工作台读清单 10 秒超时，失败只说"无法读取插件"（`webapp/src/screens/PluginWorkspace.tsx:40-46,60-65`）；模块级 `lastOverview`（`:14`）缓存不随卸载失效，回访先用旧清单渲染。"独立窗口"是 `<a target="_blank">`（`:91-92`），Tauri 壳里新窗口行为取决于 `on_new_window`（`webapp/src-tauri/src/lib.rs:147-170` 只放行少数文档窗口），未验证。插件库顶部扩展条每个插件都写"在应用中打开报表"，无网页的链到空的 /apps，不显示启停与错误状态，搜索不过滤（`webapp/src/screens/CraftPluginLibrary.tsx:43-61`）。
- 涉及行：（F2 转入）G18-37, G18-38, G18-41
- 要改：
  - `PluginWorkspace.tsx:40-46` reload —— catch 里区分 AbortError（超时）与 AgentExtensionError（带码）；`:60-65` 失败提示改"读取插件超时，请重试"/"后端返回错误（<码>）"。
  - `PluginWorkspace.tsx:14` lastOverview —— 缓存加时间戳，超过 60 s 不用于首屏渲染；`webapp/src/domain/agentExtensions.ts:1136` deletePlugin 与插件启停成功后调用新导出的 `clearOverviewCache()`（清缓存并派发 `ryn-extension-overview-changed`，工作台订阅后重载）。
  - `PluginWorkspace.tsx:91-92` —— 桌面（Tauri 环境）时按钮改 onClick 调新增 Tauri 命令 `open_plugin_window(plugin_id)`：用 `WebviewWindowBuilder` 以 `WebviewUrl::App` 打开应用内窗口（模式同 `webapp/src-tauri/src/system_apps/mod.rs:259`），label 按插件 id 复用；浏览器环境保留 `<a target=_blank>`。
  - `CraftPluginLibrary.tsx:43-61` —— "打开"链接只在 `plugin.web_app` 为真时显示，文案用插件自己的 title（"打开 <title>"），不再写死"报表"；每行加状态（已启用/已停用/出错，取 `plugin.enabled` 与其服务器状态）；`:22-24` extPlugins 同时按搜索框的关键字过滤（title、description）。
- 新增测试：vitest：超时与后端错误显示不同提示；卸载插件后回访工作台不再先渲染旧清单；无 web_app 的插件无"打开"链接；搜索关键字过滤扩展条。rust 单测：open_plugin_window 对非法插件 id 拒绝。
- 依赖：无
- 兼容和风险：新增 Tauri 命令要在 `lib.rs` 注册；独立窗口走应用内路由，不带外部 URL，登录态与主窗口共享同一 WebView 存储。
- 工作量：小
- 影响：低

## S08-I68 技能页：错误提示、文件切换竞态、平台文案、打开失败回退（低）
- 问题：技能页首次加载失败直接忽略（`webapp/src/screens/CodexSkillsView.tsx:302-304` 注释写测试环境），刷新失败先回退到旧概览，只有两次都失败才提示（`:336-346`），"打开 SKILL.md/显示文件"失败也被忽略（`:1156-1163`）。快速点两个文件，先发后到的响应覆盖后选的（`:1125-1143` handleSelectFile 无序号）。"在文件资源管理器中显示"写死 Windows 用语（`:1262`）。后端 open 在 Windows 用 os.startfile，没有 .md 关联时抛 OSError，只回 agent_skill_open_failed；Linux 只靠 xdg-open（`rynmesh/agent_runtime/extensions/codex_skills.py:788-806`）。
- 涉及行：（F2 转入）G18-44, G18-47, G18-48, G21-66
- 要改：
  - `CodexSkillsView.tsx:302-304` —— catch 里 setNotice({tone:"error", text:"推荐技能加载失败", retry}) 并给"重试"按钮；`:339-343` 回退成功时也提示"推荐列表未刷新，显示的是上次结果"；`:1159-1163` 失败时 setNotice 显示后端错误码对应的白话与"复制路径"。
  - `CodexSkillsView.tsx:1125-1143` handleSelectFile —— 加 `fileRequestSeq` ref，每次选择自增，响应到达时序号不等则丢弃；`setLoadingFile(false)` 同样按序号。
  - `CodexSkillsView.tsx:1256-1264` —— 按 `navigator.platform` 判断：macOS "在访达中显示"/"Reveal in Finder"，Windows 保持"在文件资源管理器中显示"，其他"在文件管理器中显示"。
  - `rynmesh/agent_runtime/extensions/codex_skills.py:788-806` —— `os.startfile` 抛 OSError（winerror 1155 无关联程序）时回退 `subprocess.Popen(["notepad.exe", path])`，仍失败则返回错误码 `agent_skill_open_no_handler` 且响应带 path；其余失败保持 agent_skill_open_failed；Linux 在 `shutil.which("xdg-open")` 为空时直接返回 agent_skill_open_no_handler。界面对 no_handler 显示路径与"复制"。
- 新增测试：vitest：首次加载失败显示提示条与重试；连点两个文件，慢响应不覆盖后选内容；macOS 平台显示"访达"。Python codex_skills：startfile 抛 OSError 时回退 notepad，二者都失败得 agent_skill_open_no_handler（monkeypatch os.startfile 与 subprocess）。
- 依赖：S09-I32（同文件同函数的错误态，先后顺序不限，合并时注意冲突）
- 兼容和风险：只增错误码，旧前端把未知码当通用失败。
- 工作量：小
- 影响：低

## S08-I69 扩展页：revision 回退、同服务器修改串行、表单脏检查、编辑模式传输类型（低）
- 问题：mergeReconciledOverview 在新概览 revision 小于旧值时整体丢弃（`webapp/src/screens/AgentExtensions.tsx:552-559`），存储被恢复或重建后页面一直显示旧状态。同一服务器快速切换多个工具开关，各请求带同一旧 revision，第二个必然 409，却提示"其他视图已更新"（`:1096-1151` handleToggleServerTool）。服务器编辑弹窗点背景或 Esc 直接关闭，已填内容全丢（`:239-273` AccessibleModal 只在 busy 时拦截，`:3642-3657` 表单）。编辑模式提示"请先卸载再切换类型"，传输类型按钮却可点（`:3766-3817`）。
- 涉及行：（F2 转入）G19-26, G19-28, G19-38, G19-39
- 要改：
  - `AgentExtensions.tsx:552-559` mergeReconciledOverview —— `next.revision < prev.revision` 时，若 `next.store_instance_id` 与 prev 的不同就接受 next；新增参数 `force` 供手动"刷新"按钮使用，绕过比较。后端 `rynmesh/agent_runtime/extensions/adapter.py:365-383` overview 返回 `store_instance_id`（扩展存储文件创建时生成的 uuid，旧存储首次读取时补写）。
  - `AgentExtensions.tsx:1096-1151` handleToggleServerTool —— 每服务器一条 Promise 链（`serverQueueRef: Map<string, Promise>`），修改排队串行，每次排队执行时重新读最新 `server.revision`；本地排队引起的冲突不再显示"其他视图"，只有重新读取后仍 409 才显示。
  - `AgentExtensions.tsx:239-273` AccessibleModal —— 新增 `dirty?: boolean` 属性；dirty 为真时背景点击与 Esc 先弹"放弃修改？"确认；`:3642-3657` 的服务器表单把"当前值与 baselineServer 不同"传给 dirty，保存成功后清除。
  - `AgentExtensions.tsx:3766-3817` —— `mode === "edit"` 时两个传输类型按钮 `disabled`，保留现有提示文案。
- 新增测试：vitest：新概览 revision 更低但 store_instance_id 不同则接受；同一服务器连点三个工具开关，请求依次用递增 revision 且无冲突提示；表单有修改时按 Esc 弹确认，无修改直接关；编辑模式传输按钮 disabled。Python：overview 返回稳定的 store_instance_id。
- 依赖：无
- 兼容和风险：store_instance_id 为新增只读字段，旧前端忽略；串行队列只影响同一服务器的连续修改。
- 工作量：小
- 影响：中

## S08-I70 扩展接口：超时与不可用的状态码和可重试标记（低）
- 问题：`rynmesh/agent_runtime/extensions/routes.py:20-34` _error_status 把其余码都归 400（agent_mcp_timeout 也是 400），`:77-79` 非 AgentError 异常一律 503 agent_unavailable，界面分不清"稍后重试"与"输入错"。
- 涉及行：（F2 转入）G20-43
- 要改：
  - `routes.py:20-34` _error_status —— 新增：`agent_mcp_timeout`、`agent_mcp_oauth_timeout` → 504；`agent_mcp_transport_error`、`agent_mcp_process_disconnected`、`agent_mcp_oauth_network_error` → 503。
  - `routes.py:67-79` —— HTTPException 的 detail 保持字符串（前端 `webapp/src/domain/agentExtensions.ts:1058-1068` 只读字符串），另在响应头加 `Retry-After: 2`（仅 503/504），并可选带 `X-Ryn-Error-Detail`（S08-I66 的截断错误文本，已脱敏）；前端 `AgentExtensionError` 增 `retryable`（status 为 503/504）供 describeError 显示"稍后重试"。
- 新增测试：Python routes 测试：agent_mcp_timeout 得 504 且带 Retry-After，agent_invalid_request 仍 400；vitest：503/504 的错误提示含"稍后重试"。
- 依赖：无
- 兼容和风险：前端按状态码分支的旧代码只看 409/404（isRevisionConflictError），改状态码不影响；接口仅本机界面使用。
- 工作量：小
- 影响：低
