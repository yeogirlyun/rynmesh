# DEV-02 测试报告

日期：2026-09-04  
代码提交：`052d0c3`

## 自动化结果

| 范围 | 命令 | 结果 |
|---|---|---|
| DEV-02 focused backend | `.venv/Scripts/python.exe -m pytest tests/test_local_ai_setup_recovery.py tests/test_llm_package.py tests/test_llm_hardening.py -q` | 60 passed，2 warnings |
| 全量 backend | `.venv/Scripts/python.exe -m pytest -q` | 606 passed，1 skipped，6 warnings，83.39s |
| Python lint | `.venv/Scripts/ruff.exe check ...` | All checks passed |
| Webapp unit/component | `npm test -- --run` | 11 files，42 tests passed |
| Webapp typecheck | `npm run lint` | passed |
| Webapp production build | `npm run build` | passed，1742 modules transformed |

Warnings 来自既有 FastAPI/Starlette deprecated API，不是本次失败，也不包含敏感正文。

## 新增边界覆盖

- 设备建议协议版本、推荐档位与未支持平台 fail closed；
- 有效 `206 + Content-Range` 才允许续传；服务器忽略 Range 时重新下载；
- 下载体积上限、磁盘余量、SHA-256 与 HTTPS 限制；
- runtime zip 路径逃逸/软链接拒绝；
- 安装任务 v2 的字节进度、安全错误、attempt 与重试；
- setup/readiness 响应与 retry 文件的 prompt/path marker 负面检查；
- 原有取消、GGUF 只读导入、managed update/uninstall 和 Provider API 回归。
- 受管/已配置 Provider 直接驱动 Assistant Run；同一 manifest 在 node 重启后恢复为
  device target；managed runtime startup/shutdown hooks 经过故障隔离测试。

## 物理 Windows E2E

测试机为 Windows 11 x86_64、Intel i7-9700K、8 logical CPUs。测试从空的隔离目录
开始，下载 491,400,032-byte Qwen2.5 0.5B GGUF 与 18,506,923-byte llama.cpp
runtime archive；两个摘要均与固定 manifest 一致。

原生 runtime 在 `127.0.0.1:18091` 健康启动，真实 self-test 返回非空固定短答，
输入 40 tokens、输出 8 tokens、推理耗时 202ms。随后在一个 Python 进程启动 runtime，
在另一个 Python 进程执行 status/stop；停止后健康检查失败且测试目录内残留进程数为 0。

物理模型与 runtime 只保存在系统临时验收目录，不进入 Git。

## UI 验收

在本地 fixture node 上使用真实页面 `/local-ai?client=fixture` 完成浏览器操作：

1. 推荐屏：显示检测到的内存、磁盘、runtime 能力和唯一推荐档位；
2. 进度屏：显示后端阶段、百分比、字节数、取消与“离开页面不会中止”；
3. 完成屏：显示 ready 和第一次回答；
4. 带标注截图逐项指出用户决策点与状态反馈。
