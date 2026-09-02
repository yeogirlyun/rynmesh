# Issue #23 产品文档：Private AI 流式响应

状态：**后端、传输和 Webapp 流式体验已实现；等待真实双节点/路由验收**
协议：`rynmesh.llm.stream.v1`（请求值 `stream-v1`）

## 用户问题

当前 Private AI 在 Provider 完整生成后才一次返回。长回答期间用户只能看到等待状态，
无法确认模型是否已开始工作，也不能看到逐步输出。

## 产品目标

- 在直连 `peer_http_direct` 且 Provider/模型支持流式输出时，让首个真实文本增量先于
  完整生成到达 Consumer。
- 浏览器只连接本机 Consumer 的 SSE，不直接连接 Provider。
- Relay、严格 P2P、旧 Provider、旧 Transport 和不支持流式的模型继续使用完整响应，
  不因本功能失效。
- 增量只用于展示；计费、成功状态和持久化以经过验证的最终响应为唯一事实来源。
- 断线、取消或非法事件不得把部分回答展示成成功结果。

## 用户流程

1. 客户端以 `response_mode: stream-v1` 提交任务。
2. Consumer 根据发现记录中的 `delivery_protocols` 决定是否启用直连流式路径。
3. Provider 逐段生成，每段独立签名、加密并发送。
4. Consumer 验证身份、任务、服务、时效、顺序和大小后，将增量放入进程内 broker。
5. 本机接口 `GET /api/local/llm/orders/{task_id}/events` 以 SSE 输出
   `state`、`delta`、`complete` 或 `error`。
6. 最终 `llm_response` 验证通过后才结算一次；失败则释放 hold。

## 降级与恢复

- 未声明 `stream-v1`：使用原 `complete-v1`。
- Transport 不支持增量：不走裸 `urllib`，由调用方选择完整响应路径。
- 直连中断：保留已到达的部分文本为“不完整”内存状态，并用同一 task ID 尝试取得
  Provider 已保存的最终密文；Provider 不重复推理。
- Consumer 重启：不承诺恢复增量，只通过原任务状态/终态恢复机制处理。
- Relay/P2P：本版本仍为完整响应，但本机 SSE 最终输出同一 `complete` 事件。

## 隐私与安全承诺

- 每个 delta 使用现有 X25519 密封、Ed25519 签名，绑定 task、service、sender、recipient、
  expiry 和连续 sequence。
- delta 明文只存在于 Provider 适配器内存、Consumer 有界内存 broker 和后续 UI 状态。
- `TaskOrderStore`、Registry、Relay、结算记录和日志不保存 delta 明文。
- Provider 队列、单事件、单任务总输出均有界；慢连接或断开会触发取消。
- 错误对外只暴露稳定错误码，不带请求/响应正文。

## 当前交付边界

已交付：能力协商、OpenAI-compatible SSE 解析、增量 Transport、NDJSON framing、Provider
端点、签名密文事件、Consumer 校验/broker、本机 SSE、终态恢复和 exactly-once 结算测试。

Webapp 已交付本机 EventSource 订阅、增量 assistant 预览、Stop、不完整状态、`aria-live`
播报、按 sequence 去重、snapshot 替换、一次断线续订和同 task 终态轮询恢复。增量文本只在
React 内存中；终态到达后才把 assistant 消息写入加密会话。完整响应降级也通过同一本机
事件表面完成。

未交付：真实双节点桌面时间戳、真实直连/Relay/严格 P2P 路由矩阵，以及 exact-commit
Linux/macOS CI。因此本分支仍不能宣称 GitHub Issue #23 整体验收通过。
