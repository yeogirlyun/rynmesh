# Issue #23 测试文档：stream-v1 后端与 Webapp

## 自动化范围

主测试文件：`tests/test_llm_streaming.py`。

覆盖项：

- NDJSON 在任意网络分片和 Unicode 边界下正确重组。
- 非法 UTF-8、非对象 JSON、单事件/总量超限失败关闭。
- Transport 增量 POST 保留 network-key header，并限制总响应。
- delta 的签名、recipient、task、service、expiry、sequence 和大小校验。
- duplicate/gap/wrong signer/wrong task/expired/oversized/cumulative overflow 注入。
- broker 的 task/event 上限与 reconnect replay。
- OpenAI SSE delta、`[DONE]` 和最终 usage。
- 运行时返回普通 JSON 时不启动第二次推理。
- cancel 会关闭活动 response，错误文本不泄露 frame。
- Provider 首个 delta 在生成完成前可被迭代器取得，证明不是缓冲后的动画。
- Provider 终态重放不重跑推理；两次 settlement 只产生一次 earning。
- Consumer 直连流、验签解密、本机 SSE、最终 settlement 和磁盘隐私。
- 原 Transport、完整 LLM、hardening 回归。
- 显式 Relay/严格 P2P 即使 Provider 声明 streaming，也保持完整响应协议。

## 执行命令

```powershell
D:\code\rynmesh\.venv\Scripts\python.exe -m ruff check `
  rynmesh/transport.py rynmesh/peer_http.py `
  rynmesh/llm_package/adapters.py rynmesh/llm_package/routes.py `
  rynmesh/llm_package/stream_protocol.py tests/test_llm_streaming.py

D:\code\rynmesh\.venv\Scripts\python.exe -m pytest `
  tests/test_llm_streaming.py tests/test_transport.py `
  tests/test_llm_package.py tests/test_llm_hardening.py -q

D:\code\rynmesh\.venv\Scripts\python.exe -m pytest -q
```

## 隐私检查

自动化使用唯一 prompt/output marker，读取 Consumer/Provider task JSON，确认：

- prompt 不存在；
- delta 字段和部分输出不存在；
- 完整 output 只存在于加密 `ciphertext`，磁盘无明文；
- 错误码不包含模拟的私密 frame 文本。

## Webapp 自动化

- 首 delta 替换 thinking，连续 delta 追加且重复 sequence 被忽略；
- terminal 前加密会话没有 assistant partial，terminal 后只有一个 assistant；
- Stop 保留已显示片段并标为 incomplete；
- 断线显示 recovering，以最后 sequence 重连并应用累计 snapshot；
- 第二次断线或 sequence gap 只轮询同一 task 的终态，不创建第二个订单；
- 无 delta 的完整响应 fallback 从同一本机 SSE 表面完成；
- cancel terminal 前不保存 partial assistant，terminal 后只保存一个 cancelled/incomplete assistant；
- EventSource URL 只在 `/api/local`，task ID 编码、凭证、after-sequence 和 close 均测试。

Webapp 聚焦测试 `2 files / 9 tests`、全量 `10 files / 45 tests`、TypeScript lint 和生产
build 均通过。真实双节点/路由时间戳和 exact-commit 跨平台 CI 仍是整项验收门槛。

## 2026-09-02 后端执行结果

- Ruff（本切片全部 Python 文件）：通过。
- 最终聚焦与相关回归：`84 passed, 1 warning`。
- 平台无关全量回归（排除 5 个已确认的 Windows/POSIX 环境项）：
  `535 passed, 3 skipped, 5 deselected`；其后新增的 128 KiB 终态 wire-boundary 测试在
  最终聚焦回归中通过。
- 初次原始全量诊断：`540 passed, 3 skipped, 14 failed`（之后新增的单项测试另在聚焦与
  排除项回归中通过）。失败均不在本切片修改文件：
  Windows 默认 GBK 读取 UTF-8 部署模板、POSIX executable/0600 mode 断言、不可用 WSL bash、
  Windows `select()` 不支持 pipe，以及一个 Windows 文件 replace 并发用例；最后一项单独重跑通过。

本分支未修改这些跨平台基线问题，也没有把带排除项的结果写成“全量通过”。

## 2026-09-03 Webapp 执行结果

- `npm test -- --run src/screens/PrivateAIChat.test.tsx src/domain/liveNodeClient.streaming.test.ts`：
  `2 files / 9 tests` 通过；
- `npm test`：`10 files / 45 tests` 通过；
- `npm run lint`：TypeScript 通过；
- `npm run build`：1739 modules，Vite production build 通过；
- `npm ci` audit：0 vulnerabilities。

## 2026-09-03 完成审计补强

- 后端聚焦/相关回归及 E2E verifier 单测：`99 passed, 1 warning`；新增决策测试证明
  显式 Relay/P2P 不选择 direct stream。
- Webapp 新增第二次断线终态轮询、sequence gap 终态轮询，以及 cancel 前后加密会话
  持久化边界测试；聚焦 `9`、全量 `45` 均通过。
- Ruff、TypeScript lint、生产 build（1,739 modules）通过。

## 可重复双节点证据命令

`scripts/llm_e2e.py` 现在统一从 async order + Consumer 本机 SSE 收集事件，并输出不含
delta 正文的 JSON 证据：submit 返回耗时、first-delta、terminal/total 时间、事件序列、
transport evidence、Consumer settlement 和 Provider earning 的同 task 唯一性。

```bash
# Direct stream-v1：必须有 delta，且 first_delta < terminal
python scripts/llm_e2e.py stream-run

# 请求 stream-v1 但指定 strict P2P / Relay：必须没有 delta，只有完整终态
python scripts/llm_e2e.py run
python scripts/llm_e2e.py relay-run

python scripts/llm_e2e.py down
```

`stream-run` 使用会真实分三段发送 SSE 并在分段间等待的 deterministic adapter，因此
它能区分在线增量与 terminal 后回放；CI 已新增该命令。当前 Windows 主机没有运行中的
Docker Desktop Linux engine，故这里只完成 `99` 项单元/相关回归，尚不能把 Docker
双节点结果登记为已通过。

## Docker-free 本地验收（2026-09-03）

以下命令不要求 Docker，使用当前 Python 环境启动四个真实 HTTP 进程，并在 Windows 本机
实际通过：

```powershell
D:\code\rynmesh\.venv\Scripts\python.exe scripts/llm_e2e.py local-run
```

也可单独复现：

```powershell
D:\code\rynmesh\.venv\Scripts\python.exe scripts/llm_e2e.py local-stream-run
D:\code\rynmesh\.venv\Scripts\python.exe scripts/llm_e2e.py local-fallback-run
```

验收结果分别写入 gitignored 的
`deploy/llm-e2e/results/local-stream-result.json` 和
`deploy/llm-e2e/results/local-fallback-result.json`。证据包括 exact commit、运行平台、临时端口、
submit/first-delta/terminal/total 单调时间、事件类型/sequence/delta 字节数、协商协议、实际
transport、duplicate submission 是否复用 task、三种 exactly-once 账本断言，以及四个进程日志
的 SHA-256/字节数/body-marker-absent 结果；不包含 prompt、delta 或最终 output 正文。
同时递归扫描临时 Registry/Provider/Consumer 持久文件并记录文件数与 body-free 布尔值。

本次实际结果：stream 场景产生 3 个 delta，`first_delta_before_terminal=true`，首段比终态
提前约 0.7 秒；fallback
场景发现记录仅含 `complete-v1`、产生 0 个 delta；两个场景均为 `peer_http_direct` 且同 task
hold/settlement/earning 各一次。首次开发中曾观察到 80 ms adapter 间隔可能与 200 ms broker
轮询相位重合；测试 adapter 间隔改为 250 ms 后，本地 verifier 能稳定区分首 delta 与终态。
真实运行还识别出 Transport `read(max_chunk_bytes)` 会让小型 NDJSON 等到 EOF；改用 `read1`
并增加真实 HTTP 延迟终态测试后，Provider 到 Consumer 的首段在生成完成前立即交付。

CI 的 `llm-e2e` job 也在任何 Docker Compose 步骤前执行 `local-run`。Relay/strict P2P 仍需
对应 transport 环境；本地 direct 结果没有被冒充为这两种路由的通过证据。

新增 harness、真实 HTTP 增量读取和相关 LLM/Transport 回归共 `104 passed, 1 warning`；
本切片修改的 Python 文件 Ruff 全通过，CI YAML 可解析。仓库级 `ruff check .` 仍报告
`sim/scale_sim.py` 与 `sim/sweep_b_c.py` 共 7 个既有问题，本分支未修改或冒充修复这些文件。
