# Issue #23 开发文档：stream-v1 后端与传输切片

状态：后端、传输和 Webapp 集成完成；Docker-free 本地四进程验收已具备并通过。

## 协议

请求仍为现有 `llm_request` 密文，新增可选字段：

```json
{
  "response_mode": "stream-v1",
  "stream_event_max_bytes": 262144
}
```

发现记录新增：

```json
{
  "adapter_capabilities": {
    "chat_completions": true,
    "streaming": true,
    "cancel": "best_effort"
  },
  "delivery_protocols": ["complete-v1", "stream-v1"]
}
```

Provider 的 `POST /api/peer/llm/tasks/stream` 返回 NDJSON。每行都是一个完整
`SignedPayload`：

- 增量：外层 `kind=llm_stream_delta`；密文体含
  `stream_version/task_id/service_id/sequence/delta`。
- 终态：复用原 `kind=llm_response`，仍包含完整 output、usage、duration、amount 和 state。

sequence 从 0 开始严格连续。重复、缺口、乱序、错误 task/service/signer/recipient、过期、
超限和非法 UTF-8/JSON 均失败关闭。

## 代码结构

- `rynmesh/transport.py`
  - `Transport.iter_post_bytes` 增量 seam。
  - `StdlibHttpsTransport`、`FrontedHttpsTransport` 使用 `read1` 交付当前可用字节，避免
    小型 NDJSON 等满读取上限/EOF，并保持有界读取与 finally 关闭。
  - 旧插件未实现该方法时明确返回 unsupported，不隐式旁路。
- `rynmesh/peer_http.py`
  - `HttpPeerClient.iter_post_ndjson` 负责 UTF-8 增量解码、换行 framing、对象校验、
    单事件/总量限制和稳定错误码。
- `rynmesh/llm_package/adapters.py`
  - `infer_stream` 发送 OpenAI `stream: true`，解析 SSE、`[DONE]` 和 final usage。
  - 普通 JSON 响应安全地作为单个 delta，不发第二次推理。
  - cancellation 关闭活动 response；capability 探测缓存 5 分钟。
- `rynmesh/llm_package/stream_protocol.py`
  - delta 密封、严格序列验证、终态验证和有界进程内 `StreamEventBroker`；订阅者落后于
    ring 时返回最多 128 KiB 的累计 snapshot delta，而不是静默缺段。
- `rynmesh/llm_package/routes.py`
  - Provider 有界队列/worker/NDJSON 端点。
  - Consumer 直连增量接收、验证、内存 broker 和本机 SSE。
  - 原完整响应、Relay、P2P 和结算路径保持兼容。

## Provider 状态机

1. 解密并校验请求，按原 idempotency binding claim task。
2. `accepted -> running`，获取并发 slot。
3. adapter worker 逐段调用 `on_delta`；每段立即密封后进入最多 32 项的队列。
4. HTTP iterator 从队列逐项发出。客户端断开时设置 disconnect flag 并调用 cancel。
5. 完成后只持久化最终 `llm_response` 密文及 body-free usage/amount/sequence count。
6. 相同 task + 相同 fingerprint 只重放最终密文，不重新推理。

## Consumer 状态机

1. 仅当调用方请求 `stream-v1` 且发现记录声明支持时启用直连流式。
   显式 `relay`/`p2p` 请求由 `_direct_stream_enabled` 固定为完整响应；只有 `auto`/`direct`
   可以选择 direct stream。
2. 使用 Transport seam POST，逐行验证 SignedPayload。
3. `StreamSequenceVerifier` 验证后才把 delta 放入内存 broker。
4. 收到并验证终态后执行原 hold settle/release 流程。
5. 直连中断时进入 `recovering`；auto 模式可使用同一密文请求/task ID 经完整响应路径
   取得 Provider 已保存的终态，避免重复推理和重复扣款。

## 资源限制

- wire event 默认最大 256 KiB；Provider delta 明文最多 128 KiB，以容纳密文/base64 开销。
- adapter 原始防护上限为 4 MiB；stream-v1 Provider/Consumer 输出上限为 128 KiB，保证
  带完整 output 的最终密文行仍落在 256 KiB wire event 上限内。
- peer NDJSON 总响应最大 16 MiB。
- Provider 队列最多 32 个密文事件；Consumer broker 32 tasks × 256 events。
- socket/response 在迭代结束、异常和取消时关闭。

## 兼容性

- 不带 `response_mode` 的所有现有调用保持 `complete-v1`。
- Relay、P2P 和无 streaming capability 的服务不使用流式端点。
- 最终信封、Task Balance ledger、settlement ID 和 Provider earning ID 均未改版。
- UI 接入只需扩展 NodeClient 订阅本机 SSE，无需接触 Provider 或密钥。

## Webapp 集成

`NodeClient.subscribeLLMOrder()` 将浏览器限制在
`/api/local/llm/orders/{task_id}/events`，携带同源凭证并支持 `after_sequence`。组件请求
`stream-v1`，但能力协商仍由 Consumer 决定，旧 Provider/Relay/P2P 可继续完整响应。

`PrivateAIChat` 把 partial assistant 保存在独立 React state，不调用会话存储。连续 delta
追加，重复 sequence 忽略，broker snapshot 原子替换。首次 SSE 断线以最后 sequence 续订；
再次断线或 sequence gap 改为轮询同一 task 的终态，不创建第二次推理或结算。terminal
后构造一个 assistant message 并走原加密持久化一次。取消会保存已显示片段并明确标为
incomplete，而不是成功回答。

## Docker-free 本地多进程验收

`python scripts/llm_e2e.py local-run` 连续运行两个完全隔离的场景。每个场景都从 OS 分配
四个非冲突 loopback 端口，在临时目录分别启动 Registry、adapter、Provider、Consumer，
逐个等待 `/health` 后才提交任务。两个 peer 使用不同 `RYNMESH_HOME` 和
`RYNMESH_NETWORK_DIR`，通过真实 Registry 发布/发现和真实 HTTP/NDJSON/SSE 通信，不使用
FastAPI TestClient，也不复用调用进程内状态。

- `local-stream`：adapter 真实发送三段 OpenAI SSE；Provider 发布
  `delivery_protocols=[complete-v1, stream-v1]`，Consumer 走 `peer_http_direct`。
- `local-fallback`：同一 adapter 进程配置为 complete-only；Provider 只发布
  `complete-v1`。Consumer 虽请求 `stream-v1`，仍通过真实 direct complete 端点得到终态，
  不产生伪 delta，也不触发第二次推理。

verifier 在收到 terminal 后以完全相同的 task/order 再提交一次，再读取双方账本与订单状态，
证明 duplicate submission 复用原 task，Consumer hold/settlement 与 Provider earning 各一条。
四个子进程随后被 harness 回收；日志只做 prompt/output marker 扫描，证据 JSON 不写正文。
delta 与 terminal SSE 事件带 Consumer 进程产生的 `emitted_monotonic_ns`；验收器用它计算
first-delta/terminal，而不是用可能被 HTTP 缓冲合并的客户端读取时刻。
退出前还会递归扫描 Registry/Provider/Consumer 临时持久文件，确认 prompt marker 和唯一
output digest marker 均不存在；原始日志和节点状态在证据摘要写出后随临时目录删除。

显式 Relay 与 strict P2P 命令仍保留在 Docker/真实网络 harness 中。loopback direct 不是这两种
传输，因此本地命令不会把 direct 改名为 Relay/P2P 来制造通过记录。
