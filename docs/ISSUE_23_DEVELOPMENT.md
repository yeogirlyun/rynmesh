# Issue #23 开发文档：stream-v1 后端与传输切片

状态：实现完成，等待 UI 集成分支消费本接口。

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
  - `StdlibHttpsTransport`、`FrontedHttpsTransport` 实现有界读取与 finally 关闭。
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
