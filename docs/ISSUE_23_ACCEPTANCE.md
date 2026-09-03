# Issue #23 验收文档：后端/传输切片

验收结论：**后端、传输和 Webapp 切片通过；GitHub Issue #23 整项未通过（等待真实双节点/路由验收）**。

## 后端切片验收矩阵

- [x] 协议版本固定为 `rynmesh.llm.stream.v1`，发现记录可协商 `stream-v1`。
- [x] 首个 Provider delta 可在完整生成结束前到达 Consumer 迭代器。
- [x] 浏览器边界为 Consumer 本机 SSE；没有浏览器到 Provider 的新连接。
- [x] 每个 delta 独立签名、加密并绑定 task/service/signer/recipient/expiry/sequence。
- [x] duplicate、gap、reorder、wrong-task、wrong-signer、expired、malformed、oversized 均失败关闭。
- [x] Transport 实现增量 POST；不支持的插件明确 unsupported，无裸 urllib 旁路。
- [x] adapter cancellation 关闭活动 response；慢/断开 Consumer 不造成无界队列。
- [x] delta 明文不写 TaskOrderStore、Registry、Relay 或结算记录。
- [x] Provider 最终密文可按同一 task ID 重放，恢复不重跑推理。
- [x] 计费只使用最终 usage；settlement/earning 保持 exactly-once。
- [x] 非 streaming 调用、Relay、P2P 和旧完整响应路径保持兼容。
- [x] 聚焦测试、相关回归和 Ruff 通过。

## 整项仍未满足

- [x] `PrivateAIChat` 订阅本机 SSE 并逐段渲染一个 assistant message。
- [x] 首 delta、Stop、不完整回答、恢复和 fallback 具备明确可访问 UI 状态。
- [x] terminal 前不持久化 assistant 部分文本，terminal 后加密会话只保存一次。
- [x] Webapp tests、typecheck、build 全通过。
- [ ] 两台真实节点记录 submit / first delta / terminal / total generation 时间戳。
- [ ] 真实直连 streaming、完整响应 fallback、Relay、严格 P2P 验收报告齐全。

## 当前自动化证据

- `tests/test_llm_streaming.py`：协议、传输、adapter、Provider、Consumer、SSE、恢复、隐私、
  exactly-once 和“首 delta 先于完成”。
- 相关回归：`tests/test_transport.py`、`tests/test_llm_package.py`、
  `tests/test_llm_hardening.py`。
- 最终命令结果记录如下，并明确保留平台基线排除项。

2026-09-02 实际结果：

- Ruff：通过。
- 最终聚焦/相关回归：`84 passed`。
- 排除 5 个已确认 Windows/POSIX 基线项后的广泛回归：`535 passed, 3 skipped`；随后新增
  的终态 wire-boundary 测试已包含在最终 84 项聚焦回归中。
- 初次未排除全量诊断：`540 passed, 3 skipped, 14 failed`；失败属于部署脚本编码/可执行位、
  POSIX `0600`、Windows pipe `select()` 和既有 Windows 原子替换并发用例，并非本切片回归。

因此“后端切片通过”有证据；“跨平台全量通过”和“整 Issue 通过”仍不得勾选。

2026-09-03 Webapp 证据：聚焦 `2 files / 9 tests`，全量 `10 files / 45 tests`，TypeScript
lint、生产 build 和 0-vulnerability audit 全部通过。增量内存边界、Stop/incomplete、
sequence/snapshot/reconnect、完整响应 fallback 和 terminal 单次持久化均有确定性测试。

完成审计新增证据：后端聚焦/相关回归及 E2E verifier 单测 `99 passed, 1 warning`；显式 Relay/P2P 的
complete-response 选择、第二次断线与 sequence gap 的同 task 轮询、cancel terminal 前后
持久化边界均新增自动化覆盖。Ruff、Webapp lint 与 1,739-module build 通过。

## GitHub Issue 边界映射

| Issue #23 原始要求 | 当前证据 | 结论 |
|---|---|---|
| adapter → Provider → Consumer API → Web chat 的 direct streaming | adapter/Provider/Consumer/SSE/UI 自动化，首 delta 先于生成完成 | 切片通过；真实双节点时间戳待补 |
| Relay/P2P 保持完整响应并平滑回退 | 显式 transport 选择测试、完整路径既有回归、UI complete-event fallback | 自动化通过；真实路由矩阵待补 |
| Node 是唯一网关 | EventSource URL 仅 `/api/local`，浏览器无 Provider endpoint | 通过 |
| Registry/log 不含 prompt/output | 唯一 marker 的磁盘/错误文本检查，delta 仅有界内存 | 自动化通过；生产日志抽样待真实验收 |
| 最终计量只结算一次 | terminal-only settlement/earning idempotency tests | 通过 |

## 尚需外部环境的精确证据

先在装有 Docker Linux engine 的 exact-commit runner 执行自动矩阵；脚本会把 submit、
first-delta、terminal/total 单调时间、路由、事件摘要和 exactly-once 账本证据写入
`deploy/llm-e2e/results/*.json`：

```bash
python scripts/llm_e2e.py stream-run
python scripts/llm_e2e.py run
python scripts/llm_e2e.py relay-run
python scripts/llm_e2e.py down
```

deterministic direct 必须满足 `first_delta_before_terminal=true`；P2P/Relay 必须
`stream_event_count=0`；三者都必须满足 Consumer hold/settlement 与 Provider earning 各一次。
当前本机 Docker 探测的精确阻塞是 Linux engine named pipe 不存在：
`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`。

最终真实验收仍需在两个独立节点/主机检出同一候选 `<sha>`，Provider 使用真实支持 OpenAI
SSE 的模型。矩阵至少包含：direct streaming、Provider 不声明 streaming 的 complete
fallback、显式 Relay、严格 P2P、direct 中途断开后同 task 恢复、Consumer SSE 断开重连/
快照。严格 P2P 需要两条不同公网出口；Relay 需要独立密文 relay。保存 exact commit、OS、
Python/Node/Rust 版本、网络拓扑、task ID、脚本 JSON、body-free 双端日志、账本事件，以及
Registry/日志 prompt/output marker 扫描结果。

## 发布门槛

本提交可作为 #23 的 backend/transport 基础提交供 #24/#25 UI 分支集成；不得单独关闭
Issue #23。只有上方“整项仍未满足”全部打勾并附真实验收证据后才可关闭。
