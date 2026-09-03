# Issue #23 验收文档：后端/传输切片

验收结论：**后端、传输、Webapp 及 Docker-free 本地独立多进程验收通过；专用 Relay/公网 P2P 路由证据未运行。**

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
- [x] 本地独立 Registry/adapter/Provider/Consumer 进程记录 submit / first delta /
  terminal / total 时间戳，且 direct `first_delta < terminal`。
- [x] Provider 不声明 `stream-v1` 时，通过真实发现与 HTTP 路径完成 complete fallback。
- [ ] 专用 Relay 与不同公网出口 strict P2P 的实际路由报告齐全（本机未配置，不伪造）。

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

## 本地多进程验收证据

执行：

```powershell
D:\code\rynmesh\.venv\Scripts\python.exe scripts/llm_e2e.py local-run
```

- `local-stream-result.json`：四进程、真实 Registry 发布/发现、`stream-v1`、3 个有序 delta、
  `first_delta_before_terminal=true`、最终 `peer_http_direct`、重复提交复用同 task。
- `local-fallback-result.json`：Provider 仅发布 `complete-v1`，Consumer 请求 `stream-v1` 后
  明确采用 complete 响应，delta 数为 0，最终 `peer_http_direct`。
- 两份结果都要求 Consumer hold/settlement 和 Provider earning 各恰好一次；任何一项不满足，
  命令非零退出。
- 证据只保存 output SHA-256、delta 字节数和日志摘要；四个进程日志必须通过 prompt/output
  body marker 缺失检查，Registry/peer 临时持久文件也逐个扫描。节点数据与原始日志使用
  临时目录，进程退出后自动删除。

这个命令满足单机可完成的功能验收，并已在 Windows/Python 3.12 实际运行通过。它不会声称
loopback direct 是 Relay 或 ICE P2P；后两者是发布路由矩阵的独立待办。

## 尚需外部环境的精确证据

无需 Docker 的 direct/fallback 基线先在 exact-commit runner 执行：

```bash
python scripts/llm_e2e.py local-run
```

如需补齐显式路由矩阵，再在装有 Docker Linux engine 的 runner 执行；脚本会把 submit、
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
本机 Docker 探测的精确阻塞是 Linux engine named pipe 不存在：
`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`。

发布级路由验收可在两个独立节点/主机检出同一候选 `<sha>`，Provider 使用真实支持 OpenAI
SSE 的模型，补充显式 Relay、严格 P2P 和真实网络扰动。严格 P2P 需要两条不同公网出口；
Relay 需要独立密文 relay。保存 exact commit、OS、
Python/Node/Rust 版本、网络拓扑、task ID、脚本 JSON、body-free 双端日志、账本事件，以及
Registry/日志 prompt/output marker 扫描结果。

## 发布门槛

本提交已达到本地功能验收门槛，可作为 #23 的 backend/transport 基础提交供 #24/#25 UI
分支集成。若仓库关闭策略把真实 Relay/P2P 路由报告列为强制发布门槛，则需待上方最后一项
打勾后关闭；否则可将其作为发布前网络矩阵跟踪项，不能把未运行写成通过。
