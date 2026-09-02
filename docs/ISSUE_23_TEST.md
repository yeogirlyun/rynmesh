# Issue #23 测试文档：stream-v1 后端切片

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

## 尚待 UI 分支执行

- Webapp 首 delta 替换 thinking 状态的组件测试。
- 乱序/重复 SSE 不污染 React message 的测试。
- terminal 前不写 IndexedDB、terminal 后只写一次的测试。
- Stop、断线、不完整回答、重连快照和完整响应降级的可视状态测试。
- `npm test`、`typecheck`、`build` 与真实双节点浏览器/桌面时间戳证据。

以上未完成前，测试结论只能是“后端切片通过”，不能是 Issue #23 全量通过。

## 2026-09-02 执行结果

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
