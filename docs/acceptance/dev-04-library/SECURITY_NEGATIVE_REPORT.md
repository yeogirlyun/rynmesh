# DEV-04 安全与隐私负面报告

标记词：`PRIVATE_LIBRARY_BODY`

| 攻击/泄漏面 | 结果 |
|---|---|
| `../../owner-notes.md` 路径逃逸 | 只保留清理后的 basename；通过 |
| `.pdf` 但无 `%PDF-` magic | `library_import_mime_mismatch`；fail closed |
| 文本后缀但 PDF magic | `library_import_mime_mismatch`；fail closed |
| 不支持扩展名 | `library_import_type_unsupported`；fail closed |
| 非 UTF-8 文本 | `library_import_not_utf8`；fail closed |
| 单文件 oversize/base64 oversize | request Content-Length 与 decode 前双 gate |
| PDF 超页数 | `library_pdf_page_limit` |
| PDF 解析超时 | 独立子进程 10 秒后 terminate，`library_pdf_timeout` |
| 提取文本膨胀 | 2 MiB hard gate |
| 中断临时目录 | 初始化/显式 cleanup 删除 `.imp_*.tmp` |
| 列表 API | marker、原始 bytes、extracted text、绝对路径均不返回 |
| 默认 privacy export | marker 与文件 bytes 不出现；仅 manifest/metadata |
| URL | 只保存 view；query/filename/body 不进入 URL |
| Ask Ryn handoff | URL 只有 opaque `import:imp_*` ID，node 再解析 |
| 删除 | 只删除 exact managed directory；原文件 bytes 未变化 |
| 远端发送 | 跨到 friend boundary 会创建空的新会话，不复制 Library contexts |

Library v1 没有 publish API；本地导入不会进入公开 recommendation、Registry 或 peer content listing。
