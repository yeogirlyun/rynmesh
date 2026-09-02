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

2026-09-03 Webapp 证据：聚焦 `2 files / 7 tests`，全量 `10 files / 43 tests`，TypeScript
lint、生产 build 和 0-vulnerability audit 全部通过。增量内存边界、Stop/incomplete、
sequence/snapshot/reconnect、完整响应 fallback 和 terminal 单次持久化均有确定性测试。

## 发布门槛

本提交可作为 #23 的 backend/transport 基础提交供 #24/#25 UI 分支集成；不得单独关闭
Issue #23。只有上方“整项仍未满足”全部打勾并附真实验收证据后才可关闭。
