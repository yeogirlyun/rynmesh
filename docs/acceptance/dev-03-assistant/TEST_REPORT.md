# DEV-03 测试报告

日期：2026-09-04

平台：Windows x86_64，本地隔离 worktree

## 质量门禁

| 命令 | 结果 |
|---|---|
| `.venv\\Scripts\\python.exe -m ruff check rynmesh tests/test_assistant_context.py tests/test_assistant_runs.py` | 通过 |
| `.venv\\Scripts\\python.exe -m pytest tests/test_assistant_context.py tests/test_assistant_runs.py -q` | 12 passed |
| `.venv\\Scripts\\python.exe -m pytest -q` | 618 passed, 1 skipped |
| `npm test -- --run` | 13 files, 51 tests passed |
| `npm run lint` | 通过 |
| `npm run build` | 通过；Vite production bundle 成功 |
| Windows 元数据并发读取重复测试 | 20/20 通过 |

唯一 skip 为仓库原有平台/环境条件 skip，不属于 DEV-03 失败。pytest 输出中的 Starlette/FastAPI deprecation warning 为既有依赖警告。

## 重点测试

- context：文本、Markdown、PDF、4 MB gate、CJK/Unicode 安全截断、owner/expiry/missing。
- run：幂等、持久化、local facade、friend route、cancel/late result、restart、引用校验。
- 隐私：导出不含 prompt/context/output；assistant scope 清除 run/context；元数据不落正文。
- Web：空态、真实本机对话、文件上下文、无模型引导、跨边界确认、共享草稿、会话 CRUD。
- 存储：v1→v2 迁移、旧记录保留、AES-GCM store 兼容。
- 容量：500 条会话输入只渲染 50 条首屏，逐页增加 50 条；查询在分页前过滤。

## 故障修复记录

全量测试首次捕获 Windows `Path.replace` 极短分享锁窗口，导致轮询读出现一次 `PermissionError`。实现增加有上限的 5 次短退避读取；随后目标用例连续 20 次及全量 618 个后端测试均通过。
