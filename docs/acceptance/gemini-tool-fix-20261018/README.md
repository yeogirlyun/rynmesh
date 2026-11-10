# Gemini 工具调用修复验收记录

本目录收录 2026-10-18 的 Gemini 工具协议、安装保全及真实办公任务独立验收，2026-10-19 整理为公开交付副本。
报告中的 hash、安装身份及运行计数绑定当轮快照，不自动代表后续源码或本次上传重新验收。

## 实际改动

- Antigravity 工具对话将既有 schema 传入推理，通过独立 `--json-schema` 参数交给 provider；普通文本对话保持原路径。
- 优先解析 `structured_output`，无效结构化输出不能回退到显示文本绕过校验；工具名称、数量、字段及参数仍受原合同约束。
- 工具提案保持由 Ryn 授权和执行；provider 原生工具未因此取得业务执行权限。
- 校验失败映射为固定分类反馈，参数值、未知名称及原始拒绝正文不进入反馈，支持受控纠错后继续。

协议实现和测试范围见 [独立协议报告](independent-001/protocol/protocol-review.md)。

## 最终验收与历史失败

[独立总报告](INDEPENDENT-REVIEW-001.md) 结论 PASS：实现修复、普通 Windows 安装身份、监测范围内的原数据/设置/授权保全，
以及同一 Gemini 完成新的 Word、PDF 均通过约定范围验收。
独立离线重跑 54 项全部通过且网络尝试为零；原 JUnit 合计 233 个不重复用例，不能再把重跑 54 项追加计数。

两项真实任务合计 12 次模型调用、41 对提案/结果、39 次 host handler 派发及 9 次 workspace 自动授权；
没有额外人工业务提示或显式审批，模型回复拒绝为零。配对结果、handler 派发和成功写入是不同口径，费用 unknown 不等于零。
Word 与本轮 PDF 输入逐字节对应；数学、全文和最终各一页可读性检查通过。
详见 [业务事件报告](independent-001/business-events/REVIEW.md)。

历史三次 invalid 缺少原回复，无法恢复精确类别或证明唯一根因；无 schema 的 baseline probe 也曾成功，不能宣称缺 schema 必然失败。
早期 pytest 临时目录清理 exit 1、安装前预检失败、安装后短超时造成的 missing 误报，以及首次 Codex 上下文 native 验证拒绝均保留。
后续只读恢复证据、普通进程 QA 和正式测试分别解释这些结果，没有回写原失败状态或放宽校验。
安装证据与保全范围见 [独立安装报告](independent-001/installation/installation-review.md)。

## 范围边界与公开内容

- 两个真实成功任务不等于所有模型/所有任务可靠性证明；unknown 32768 fallback 不是实测模型窗口。
- Word 在同 WordCraft 引擎的独立普通进程验证；标准渲染缺少 `soffice.exe` 的失败保留，未证明 Microsoft Word/LibreOffice 跨引擎兼容。
- 文件与权限状态保全不等于逐外部服务重新登录；未重新解密全部旧历史，也未把安装版本标签代替 hash 身份验证。
- 本目录 `.gitignore` 仅公开报告及说明；机器专用 harness、raw 状态、日志、归档与私有备份在本机留存。
- 公开报告保留原始证据名称及 hash；完整可维护自动化以仓库根 `tests/` 为准，后续改动需按风险重新验证。
