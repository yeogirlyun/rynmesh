> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`156b90ab4ffbede658d315e9ff47189574c2efa73217907a81b79ca2326cdc85`。

# 真实办公文件独立核验 002

本片段结论：三份最终 XLSX/DOCX/PDF 通过有范围限制的业务文件核验。前两次 Excel 失败保留；最终 Excel 是第二次人工业务纠正后的 Gemini 修复结果，不算 GLM 首轮自主成功。

`audit_business_002.py` 只读原 result、snapshot、回执和成品。没有读取私有 home/auth/cache，没有发起模型调用，没有替 Agent 写入或修复业务文件。新增 QA 仅为导入、渲染及副本内存扰动。

| 原运行 | 模型调用 | 提案/配对结果 | confirmed 宿主执行 | not_executed | 精确批准 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| business-final-01 / sales_excel | 12 | 21 | 19 | 2 | 12 | FAIL，错误行引用/格式，保存文件有公式错误，step limit |
| business-final-01 / sales_word | 9 | 8 | 8 | 0 | 3 | PASS |
| business-final-01 / sales_pdf | 9 | 10 | 10 | 0 | 5 | PASS |
| business-repair-01 / repair_excel | 10 | 23 | 21 | 2 | 11 | FAIL，原始输入覆盖，已保存文件仍错，step limit |
| business-gemini-final-01 / repair_excel | 5 | 19 | 19 | 0 | 8 | PASS，累计2次人工业务纠正 |
| 合计 | 45 | 81 | 77 | 4 | 39 | 5次尝试，3份最终成品 |

所有 runtime 提案均与 Bridge 模型响应相同（仅规范化丢弃流式 index），tool_call_id 与 tool result 一一配对；回执参数哈希全部匹配，需批准的操作均能匹配精确参数和 preview。3份 accepted byte copy 的哈希与原宿主导出回执匹配。修复输入均逐字节匹配首轮错误 Excel，PDF 输入逐字节匹配 Word Agent 输出。只读核验前后业务原件不变。

| 最终文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| 销售费用公式表-模拟.xlsx | 4846 | bd907d8e0b58c0070a2599176f1e2e09624cc9a4b9b22e649ad87b4711824869 |
| 九月模拟销售经营报告.docx | 6999 | 8ae130de8360ec528b6a891875252a76bce806ac6314ce60c75266f6b63ac650 |
| 九月模拟销售经营报告.pdf | 42164 | 8f91e0fae1e79c82cde20d13c8dae7cc58989e0bcae53779c4162239b2b485e7 |

独立以输入复算销售5520、成本3380、毛利2140、费用500、利润1640、毛利率2140/5520。最终 Excel 的 B5:D7 数值输入、15个精确预期公式、缓存结果和数值/百分比格式均正确，无缓存公式错误。Bundled Artifact Tool 只读导入/渲染另行计算正确。另进程真实 GridCraft 重开副本，把 North 数量12改13后，North结果为1950/1170/780，合计5670/3470/2200、利润1700、毛利率2200/5670；副本未保存，原件哈希不变。

DOCX ZIP/OOXML全部XML可解析、正文和产品数字完整；真实 WordCraft 另进程重开并派生 QA PNG/PDF。PDF用独立 pypdf 提取、PDF.js另行渲染，一页中文与业务数字完整，保留底部 SIMULATED DATA。已人工查看全部3张最终渲染图：Excel清楚可读，Word/PDF有元信息末尾单个“用”字续行，无缺字/遮挡/截断。`render_docx.py` 本轮实跑 exit1：本机缺少 LibreOffice soffice.exe。因此同引擎另进程核验不等同 MS Word/LibreOffice 跨引擎兼容验证。

公开复制的native executable哈希仍与preflight一致，工具数量GridCraft18、WordCraft7、PdfCraft35。harness读取真实native payload注册到隔离Ryn，再由真实Agent→Bridge→模型→Ryn批准→Craft宿主执行；保存回执建立文件来源。Gemini helper的provider端工具关闭，工具提案由Ryn执行。没有checker代写成品。

## 快照界限

三个run的各295项生产Python均匹配各自 snapshot.json 和磁盘冻结 code。Gemini生产代码逐项匹配首轮业务快照；worker的 imported_source_unchanged/owner_source_unchanged 均为true。前者本轮独立逐项验证，后者仅为worker记录与harness前后hash守卫，不读取owner私有文件再验证。

当前受核源码相比业务快照有10项变化：`agent_runtime/models.py`，`llm_package/{api.py,api_formats.py,chat.py,cli_binding.py,cli_models.py,codex_session.py,routes.py,sources.py,task_balance.py}`。这些涉及CLI容量/绑定、来源校验和usage/账务，不改 `node_apps` 或办公native引擎。当前源码仅运行聚焦回归及真实引擎只读核验，没有重新付费跑完整业务生成。GLM原业务使用明确model override 1000000、输出4096；Gemini业务为unknown_fallback32768、输出4096，不把这组业务说成真实大窗口容量声明或1M-token推理验证。

当前Ryn应用/批准聚焦测试 `app-policy.junit.xml` 为5/5通过（业务批准策略2、Agent批准合同2、真实App HTTP接线1），独立于5次付费业务尝试。首次native核验误传相对QA目录导致找不到副本，已保留 `native-relative-path-diagnostic.json`；绝对路径重跑成功。Bundled Python缺certifi的启动诊断没有产生模型调用；Ryn引擎探针以repo虚拟环境运行，OOXML/PDF解析与Artifact/PDF.js使用Bundled运行时。均不把启动/配置错误归为业务成品失败。

详细事实在 `business-audit-002.json`、`native/native-independent-checks.json`、`xlsx/inspection.json`、`pdf/pdfjs-inspection.json`、3张PNG和原日志中。
