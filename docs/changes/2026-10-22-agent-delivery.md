# 2026-10-22 Agent 交付

目标仓库：`yeogirlyun/rynmesh`，目标分支：`feature/ryn-integrated-workspace`。

## 变更范围

- 自动任务增加当前对话 heartbeat、分钟周期、相对提醒和通知策略，更新任务管理界面。
- 整合 Chrome 控制运行时与 CodeMode，改进桌面窗口恢复、操作浮层和计算机工具调用。
- 调整记忆运行时、项目上下文、工作区产物与附件展示，更新 Agent 输入和编辑界面。
- 更新模型上下文、输出参数及来源元数据处理。
- 补齐 Chrome 运行时的固定版本 npm 依赖、适配器和 Python 包资源配置；桌面构建前自动安装依赖。

## 本轮整理

修正测试导入排序、文件末尾空行，以及项目上下文格式更新后遗留的旧文本断言。
本机临时账单图片、PDF 检查脚本、测试 sandbox、缓存与 node_modules 不纳入 Git。

## 验证范围

- 自动任务、桌面恢复、Chrome、记忆、项目、产物与后台任务：242 项后端回归通过。

- Agent 主界面和产物界面：2 个前端测试文件，96 项通过。
- 模型来源、CLI 模型和 JS 资源：4 个后端测试文件，32 项通过。
- `npm run build` 通过，包括 TypeScript 检查和 Vite 生产构建；保留已有分块大小及动态导入提示。
- 相关 Python 目录及回归测试 Ruff 检查通过；Git whitespace 检查通过。
- Chrome 依赖 `npm ci --omit=dev` 与 ClassicLevel 适配器实际加载通过。
- Python wheel 构建通过，压缩包内 9 项关键 Chrome 资源（含原生依赖）存在性检查通过。
- `npm run test:native`：5 项通过。

首次 pytest 使用系统临时目录时，在清理旧目录符号链接阶段遇到 Windows 权限错误，最终验证改用仓库 `.codex-tmp` 下的独立目录。
本轮不代表全仓测试、完整桌面安装包或跨平台原生程序验收。
