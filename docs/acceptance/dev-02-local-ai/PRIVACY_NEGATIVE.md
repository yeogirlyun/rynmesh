# DEV-02 隐私与负面验证

日期：2026-09-04

## Marker 验证

测试把 `PRIVATE_PROMPT` 与 `/private/model/path` 注入内部异常，随后检查：

- `GET /api/local/llm/setup/status`；
- `GET /api/local/llm/readiness`；
- `llm/setup-retry.json`。

三处均未出现 marker。公开错误被映射为稳定的 `download_interrupted`，用户只看到可执行的
重试提示。固定 self-test 正文不会进入正式会话；持久化证据只保存输出 SHA-256、token 数、
耗时与短的产品固定预览。

## Fail-closed 验证

- 只允许 manifest 固定的 HTTPS 资源；
- Range 续传必须同时满足 206、正确起点与完整 Content-Range；
- Content-Length 超限或磁盘不足在写满磁盘前失败；
- checksum mismatch 删除 Ryn 自有 partial；
- runtime 压缩包的绝对路径、`..` 路径和链接条目被拒绝；
- runtime 只绑定 loopback，命令不经过 shell；
- PID 所有权检查要求 executable 位于该 manifest 的 runtime 目录。

## 数据删除边界

- imported GGUF 不属于 Ryn，卸载不删除原文件；
- managed uninstall 只操作经过 resolve/commonpath 校验的 managed root；
- 回滚 feature flag 不删除模型或用户现有 Ollama 数据。

