# DEV-02 一键启用本地 AI——验收记录

验收日期：2026-09-04  
实现提交：`052d0c3`  
正式支持平台：Windows x86_64（CPU）  
功能开关：`RYNMESH_LOCAL_AI_V1_ENABLED` / `VITE_RYN_LOCAL_AI_V1_ENABLED`

## 验收结论

DEV-02 在正式支持的平台上通过。普通用户可以在 Ryn 内完成设备检测、推荐确认、
许可证确认、可恢复下载、摘要校验、原生 runtime 启动和第一次真实回答，不需要
Ollama、Docker、Python、Node 或终端操作。

macOS runtime 摘要已经固定在代码中，但在获得对应实体设备验收证据前，硬件 API
不会把 macOS 标为一键安装支持平台；GPU 档位也不对用户宣称支持。

## 功能验收

- [x] Windows x86_64 无 Ollama/Docker 环境完成受管安装。
- [x] 已有 Ollama 与高级 Provider 路径保持兼容，不强制重复下载。
- [x] 默认仅突出一个 `light` 推荐档位，未发布档位可见但不可选。
- [x] 用户确认许可证与磁盘占用后即可开始。
- [x] 下载展示真实字节进度，支持取消、失败重试和经校验的 Range 续传。
- [x] App/node 中断后任务给出可执行的重试状态，并复用已验证 partial/checkpoint。
- [x] 安装完成条件包含 health check 和真实 local-node 问答自检。
- [x] 安装完成的 Provider 立即成为统一 Ask Ryn 的 device target；node 重启时受管 runtime
  自动恢复，退出时 best-effort 停止，避免安装自检与日常助手形成两套孤立入口。
- [x] 完成页展示第一次回答，并可进入 Ask Ryn。
- [x] 未安装/安装失败不会阻断 For You、Reader、Library 等非 AI 路由。

## 安全验收

- [x] 模型和 runtime 使用固定版本、HTTPS URL 和 SHA-256。
- [x] 非 HTTPS、oversize、摘要不符、恶意 Range 和压缩包路径逃逸 fail closed。
- [x] runtime 命令使用参数数组且固定监听 `127.0.0.1`，Web UI 被禁用。
- [x] setup job / retry 文件不保存 API key、prompt、模型输出或用户导入路径。
- [x] status API 只公开经过裁剪的 manifest 字段。
- [x] 受管进程写入所有权 PID 记录；跨 node 进程可以验证并停止。
- [x] 导入 GGUF 保持只读语义；卸载路径要求位于 managed root。

## 质量验收

- [x] focused pytest：62 passed。
- [x] 最终 full pytest：642 passed，1 skipped。
- [x] Ruff：通过。
- [x] 最终 Webapp tests：15 files / 60 tests passed。
- [x] Webapp TypeScript lint：通过。
- [x] Webapp production build：通过。
- [x] Windows 干净安装、首次回答、跨进程 start/status/stop：通过。
- [x] 三个独立干净目录均完成真实下载/安装/Ask Ryn 回答/重启恢复：通过。
- [x] UI 三态截图和带标注总览：已归档。

## UI 验收证据

- `screenshots/01-recommendation.png`：设备与推荐档位。
- `screenshots/02-progress.png`：真实阶段、进度、字节数和取消。
- `screenshots/03-first-answer.png`：ready 与第一次回答。
- `screenshots/dev02-ui-annotated.png`：带编号说明的验收总览。
- `ui-annotations.html`：标注图生成源。

## 关联材料

- [测试报告](TEST_REPORT.md)
- [物理 E2E 证据](physical-windows-e2e.json)
- [隐私负面验证](PRIVACY_NEGATIVE.md)
- [支持矩阵与许可证](SUPPORT_MATRIX_AND_LICENSES.md)
- [迁移与回滚](MIGRATION_ROLLBACK.md)
