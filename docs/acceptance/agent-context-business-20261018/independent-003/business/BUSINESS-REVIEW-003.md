> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`a1ea0ee6cdec329a78adcb9d07e412790b3ce1c7c049238665232279fbf895b4`。

# 真实办公业务成品独立复核 003

本片段结论：PASS，限本报告明确的业务来源、文件内容和既有视觉证据范围。三份最终成品保持原字节，来源可以追溯到真实 Ryn Agent → Bridge 模型响应 → 精确批准 → Craft 宿主执行及保存回执。此业务复核不能单独证明最新容量/usage/API 改动通过，完整结论须结合主验收的聚焦回归。

本轮先读 001、002 主报告、002 业务分报告、机器 JSON 和原只读脚本，再自行编写 `audit_business_003.py`。收到主审验前源码哈希完成后，以 Codex bundled Python 执行成功。没有运行真实模型、修改 XLSX/DOCX/PDF、调起 native 引擎、读取真实 owner 配置/认证/旧聊天或私有 credential cache。只读取 preflight 点名的公开 native executable，未读取其相邻私有状态。没有修改生产源码或原独立探针。证据为 `business-audit-003.json`、`audit-business-003.log`、`visual-review-003.json`。

## 本轮实测和沿用证据

本轮独立解析最终 XLSX 的 ZIP/全部 XML、15 个精确公式、9 个数值输入、费用 500、缓存计算结果及数字/百分比格式。独立由输入计算销售 5520、成本 3380、毛利 2140、费用 500、利润 1640、毛利率 2140/5520，结果一致且无错误单元格。DOCX 的 ZIP/全部 XML、12 段正文及期间/币种/模拟标记/产品与经营数字完整；PDF 用 pypdf 独立重开，未加密、一页、中文与业务数字及 SIMULATED DATA 标签完整。三原件解析前后 SHA-256 相同。

| 最终成品 | 字节 | SHA-256 |
| --- | ---: | --- |
| 销售费用公式表-模拟.xlsx | 4846 | bd907d8e0b58c0070a2599176f1e2e09624cc9a4b9b22e649ad87b4711824869 |
| 九月模拟销售经营报告.docx | 6999 | 8ae130de8360ec528b6a891875252a76bce806ac6314ce60c75266f6b63ac650 |
| 九月模拟销售经营报告.pdf | 42164 | 8f91e0fae1e79c82cde20d13c8dae7cc58989e0bcae53779c4162239b2b485e7 |

成品路径位于 `<user-home>\Documents\Codex\2026-10-18\new-chat-4\outputs\Ryn-业务文档验收-20261018\已验收成品`。最终副本哈希均匹配对应真实任务文件及 confirmed 保存回执，PDF 输入逐字节匹配 Word 输出，两次修复输入逐字节匹配首次错误 Excel。

002 真实 GridCraft 的副本内存扰动证据本轮只读复核，未再运行引擎：North 数量 12→13 后 North 销售/成本/毛利为 1950/1170/780，合计 5670/3470/2200、利润 1700、毛利率 2200/5670；副本未保存、原件不变。只读复核既有 JSON 不计为本轮再次实际重算。

最终文件哈希不变，本轮没有重复渲染。本审查重新打开并查看 002 已归档的全部三张图：XLSX `xlsx/sheet.png`、Word `native/native-word/page.png`、PDF `pdf/page-1.png`。表格可读，Word/PDF 元信息末尾的“用”字轻微续行仍在，无缺字、遮挡或截断。复看归档图不等于在本轮重新打开或渲染业务文件。标准 `render_docx.py` 缺 LibreOffice 的既有失败保留，本轮未安装或重试；WordCraft 另一进程同引擎渲染不能证明 MS Word/LibreOffice 跨引擎兼容。

## 真实来源复核及计数更正

本轮逐项重新读取 5 份原始 `result.json`，其文件 SHA-256 均与 002 机器证据一致。运行时提案与记录的 Bridge 响应一致，仅移除流式 index；提案 ID 与结果一一配对，参数 SHA-256 一致，需批准的回执均能匹配精确参数及 preview。已保存文件与 confirmed 导出回执匹配。读取 harness 证实它注册现有 Craft native 工具、经 Ryn 实际 dispatch，checker 不代写成品；owner 来源未变化只依据原 worker 守卫记录，本轮没有重读私有 owner 文件确认。

| 原运行 | 模型调用 | 提案/配对结果 | confirmed 宿主执行 | not_executed | 精确批准 | 原结果 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| business-final-01 / sales_excel | 12 | 21 | 19 | 2 | 12 | FAIL，agent_step_limit，错误公式和格式 |
| business-final-01 / sales_word | 9 | 9 | 8 | 1 | 3 | PASS |
| business-final-01 / sales_pdf | 9 | 10 | 10 | 0 | 5 | PASS |
| business-repair-01 / repair_excel | 10 | 22 | 21 | 1 | 11 | FAIL，agent_step_limit，输入被覆盖、保存文件仍错 |
| business-gemini-final-01 / repair_excel | 5 | 19 | 19 | 0 | 8 | PASS，第二次人工业务纠正后 |
| 合计 | 45 | 81 | 77 | 4 | 39 | 5 次尝试、2 次人工业务纠正、3 份最终成品 |

002 业务分报告的 Markdown 逐任务表将 Word 写为 8 提案/0 not_executed、GLM 修复写为 23 提案/2 not_executed。这是原叙述表的计数瑕疵；原始 result 与 002 机器 JSON 实际为上表的 9/1、22/1。合计 81 提案/77 confirmed/4 not_executed 未受影响。原报告和档案不改写，本轮明确更正。4 个 not_executed 不能算宿主执行。Gemini 成功不回写前两次 Excel 失败，也不称 GLM 首轮自主完成三份业务。

本轮是 **3 份成品解析审计和 5 份来源记录复核**，不是新增 5 次业务运行，也不混入 pytest/Vitest case 数。本轮新模型调用 0、native dispatch 0、业务文件写入 0、重新渲染 0。

## 源码和引擎边界

三个真实 run 各自 snapshot.json 的 295 个生产 Python 文件均逐项匹配各自 code 磁盘文件；三组生产映射相同，Gemini 复用了首批业务快照。其原始 snapshot SHA-256 依次为：首批 `483879a3407511460139f50036913ed65ad7b146b0b41435fdc469bb112847ac`、GLM 修复 `a9abe31f406621b334165b017577ae31ea9a7d3eaa0950aa6caf154aad54c15e`、Gemini 修复 `2346ee3a18910b89bb9f15548de1532c3ba54f20dd8a3824d30fba9faa658753`。本轮从当前项目文件重新取哈希比较，当前相对首批业务快照有 **13** 个生产 Python 差异：

- `rynmesh/agent_runtime/{bridge.py,models.py}`
- `rynmesh/llm_package/{adapters.py,api.py,api_formats.py,chat.py,cli_adapter.py,cli_binding.py,cli_models.py,codex_session.py,routes.py,sources.py,task_balance.py}`

18 个 `node_apps` 生产 Python 文件与业务快照全部相同。当前公开安装源及隔离复制的 GridCraft v0.6.1、WordCraft v0.6.1、PdfCraft v0.7.1 三个 executable 哈希均再次匹配 preflight，工具清单数量分别 18/7/35。本轮能具体证明的是上述源码和三 executable；没有历史完整 payload 目录 manifest，因而不扩大为目录内所有文件的历史不变性证明。

业务真实生成冻结在首批生产 Python 快照，**没有在当前最终源码哈希下重新付费跑完整业务生成**。本轮源码差异涉及 Agent Bridge、CLI 容量/绑定、适配器实际用量和 API/账务，应由主审本轮容量与 usage 聚焦回归桥接。旧实际业务本身不能作为新分支已实跑的证据。GLM 业务容量来自明确 model override 1000000、输出 4096；Gemini 为明确标记 unknown fallback 32768、输出 4096。两者都不构成 1M-token 真实推理或模型真实大窗口 metadata 声明的证据。

本片段仅签署本轮已哈希并复核的文件及公开源码范围，不追认未来修改。
