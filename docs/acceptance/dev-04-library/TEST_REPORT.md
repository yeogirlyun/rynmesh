# DEV-04 测试报告

日期：2026-09-04

平台：Windows x86_64，本地隔离 worktree

| 门禁 | 结果 |
|---|---|
| `python -m ruff check rynmesh tests` | 通过 |
| DEV-03/04 focused backend | 24 passed |
| backend full pytest | 630 passed, 1 skipped |
| frontend full Vitest | 14 files, 57 passed |
| TypeScript `npm run lint` | 通过 |
| Vite `npm run build` | 通过 |
| Browser functional inspection | 通过，4 个原始状态 + 1 个注释合成图 |

重点覆盖 cursor 一致性、批量 partial result、进度 restart、source/watcher pause、TXT/Markdown/PDF、MIME 欺骗、UTF-8、oversize、page bomb、临时上传清理、Ask Ryn 双 context race、privacy export/erase。

唯一 skip 为仓库原有环境条件 skip。Starlette/FastAPI deprecation warning 为既有依赖告警，不是 DEV-04 失败。

## 物理文件语义

Windows 临时目录中创建真实 Markdown 原文件，导入后删除 managed copy，再次读取原文件并比较 bytes；前后完全一致。所有删除均先验证 resolved parent 位于 `RYNMESH_HOME/library/imports`。
