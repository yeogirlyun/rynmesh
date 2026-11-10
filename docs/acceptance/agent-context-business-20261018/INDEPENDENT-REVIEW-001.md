> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`6e6bfa6b9ab09f2e3bcc6f59471f287115a6378aacb332d910045cbaef7796a8`。

# 上下文跟随模型与办公业务文件：独立验收 001

结论：**FAIL**。正常 Agent 自动/手动主路径及传输边界通过；完整合同存在已复现的 CLI 容量刷新、绑定及未知 usage 缺口。Word/PDF 通过有范围限制的业务验收；Excel 首轮与另开修复任务均失败。生产源码未由本验收聊天修改。

签署于 **2026-10-18 16:36（Asia/Hong_Kong）**，只覆盖本报告记录并归档的受核快照。679项生产源文件在独立测试前后和归档前均一致；本轮后续修复应另作新快照验收，本报告不追认其通过。

本审查独立读取当前 dirty 源码和本轮证据，没有采用主任务的通过判断。未读取真实认证文件、用户旧聊天，未修改真实 owner 设置或授权，未部署、提交或发起新的付费推理。所有新增模型边界测试使用隔离 fixture；真实 GLM/Craft 业务调用来自主任务已经运行的 Ryn 任务。

## 已确认的结果

| 要求 | 当前判断 | 独立证据与范围 |
| --- | --- | --- |
| Agent 新默认跟随模型；owner 手动 UTF-8 限额保留 | PASS | nullable auto、4000..2M 整数、旧 120000/600000 ambiguous 数值保留、旧权限/回执/重启接线通过 focused 和独占探针。 |
| 完整 messages/tools 序列化估计、完整输出预留 | PASS，限正常 Agent dispatch | `ceil(UTF8 messages + tools bytes / 3) + 512`；显式 heuristic 标记。`_model_probe` 仍是旧 bytes+512 预留，不宣称所有推理入口已统一。 |
| 切小/未知模型安全 | 部分 PASS，CLI 刷新 FAIL | 正常明确切换和未知 fallback 已测试；已选 CLI 子模型缓存缩容后仍沿用旧大容量，见 IR1-C03。 |
| 未知 usage 不冒称实际值 | FAIL | Codex 缺失 usage 被合成为 1137 并标为实际、`usage_complete=true`，见 IR1-B01。 |
| 来源 auto/manual、免费真实 listing、metadata 防伪 | 部分 PASS | caller metadata 被忽略、端点/凭证缓存隔离、override 优先和持久化接线通过；auto 来源输出配置仍受闲置 32768 字段限制，见 IR1-C01。 |
| CLI 具体模型容量及容量变化的 destination binding | FAIL | 静态新鲜容量正常；容量刷新不旋转绑定、selected 快照无限复用，见 IR1-C02/C03。 |
| transport 放宽且有限上界 | PASS | 认证 FastAPI 脚本 adapter、大 ASCII/中文 UTF-8 body、16MiB 拒绝、32MiB P2P/真实分片重组；不代表真实收费 1M-token 推理。 |
| UI auto/manual、旧值保留、无效值 | Agent PASS；来源无效值拦截 FAIL（若同样要求来源级） | 来源手动12/10000.5均触发PUT；12被后端拒绝，10000.5截断为10000。无授权绕过。 |
| Ryn 真实生成 XLSX/DOCX/PDF | 2 PASS / 2 FAIL（含单列修复任务） | Excel 两次都有错误公式和 step-limit；Word/PDF 内容、保存回执/hash和一页视觉检查通过。 |

## 未关闭源码问题

| ID | 严重度 | 触发、实测结果与位置 |
| --- | --- | --- |
| IR1-B01 | P2 | `codex_session.py:623–624` 在 usage={} 时造出数字，`bridge.py:50–53` 视作实际；真实隔离 runtime 最终 `total_tokens=1137, usage_complete=true`。应保留 unknown 和 labelled reservation。 |
| IR1-C01 | P2 | `sources.py:107–110`：已保存来源 metadata 已知 1048576，auto 编辑 128000 输出仍因旧 32768 数字抛范围异常。 |
| IR1-C02 | P2 | `cli_binding.py:43–44`：完整 catalog 刷新 400000→16000 后容量已变，destination revision/binding 不变。 |
| IR1-C03 | P1 | `models.py:185–187`：selected CLI 子模型 snapshot 无 TTL；缩容/全局缓存过期后仍返回 400000。真实 `AgentModelBridge.complete` 在原 expected destination 下把 60000 ASCII 内容字节发到 fixture adapter，未按 16000 窗口阻断。没有真实 provider 缩容或付费越界推理。 |
| IR1-C04 | P2 | `api.py:123,135,148,244–247`：兼容 API 的 CLI default 仍用 manifest 32768；已知 400000 的隔离 fixture 上，认证 150000 ASCII 内容字节请求被 HTTP400 拒绝。Agent 自身 ModelCatalog 路径不同。 |
| IR1-U01 | P2，来源级扩展要求 | `AISources.tsx:82–89,144,165` 仅min/max且保存onClick无显式integer/range校验；独立UI探针12/10000.5均发PUT。`sources.py:107–110` 对前者拒绝、后者 `int()` 截断。Agent新增invalid拦截本身通过。 |

上述 Codex usage、CLI binding/cache、API catalog 和来源无效值处理分支在本轮前源码已经存在；归为当前合同未关闭缺口，不声称全部是本轮新回归。来源 auto 输出校验是旧校验未适配本轮新增 auto/discovered 语义。精确 before 归因、行号及 SHA-256 见本目录 `independent-001/BUDGET-REVIEW.md`、`CAPACITY-REVIEW.md`、`BOUNDARY-REVIEW-001.md`。

直接复验：`independent-001/test_capacity_edges.py` 对应最终 `capacity-edges-003.junit.xml` 的5个失败；`test_budget_independent.py::test_missing_codex_usage_must_not_become_actual_runtime_tokens` 对应 `budget-independent.log`；来源UI最终证据为 `boundary-ui-independent-005.log` 和 `boundary-ui-probe.test.tsx`。早期UI探针配置错误保留但不作为生产失败。

## 业务首轮事实

独立复算：North 销售1800/成本1080/毛利720；East 1920/1200/720；West 1800/1100/700；合计销售5520、成本3380、毛利2140、毛利率2140/5520≈38.7681%、费用500、经营利润1640。

| 任务 | 状态 | 调用、文件和检查 |
| --- | --- | --- |
| sales_excel | FAIL | 12模型调用、12批准、`agent_step_limit`。4849B，SHA256 `9317309efad8b36c16df87e91be3c666262a7508b2306cf6e228404c34de333c`。产品在5–7行、公式错引4–6行，多处 `#VALUE!`；B11为220、B12为11.4583%，费用/利润被百分比格式显示。第12轮内存修改后未保存。错误文件与失败证据均保留。 |
| sales_word | PASS（已注明渲染限制） | 9模型调用、3批准；6999B，SHA256 `8ae130de8360ec528b6a891875252a76bce806ac6314ce60c75266f6b63ac650`。OOXML正文数字、模拟/期间/CNY/三产品/建议正确；保存回执哈希一致，native重开。 |
| sales_pdf | PASS | 9模型调用、5批准；42164B，SHA256 `8f91e0fae1e79c82cde20d13c8dae7cc58989e0bcae53779c4162239b2b485e7`。输入DOCX逐字节匹配 Word输出；WordCraft导出后 PdfCraft真实添加/保存注记。pypdf及独立PDF.js重开一页、中文和业务数字正确；全部页面视觉检查未见遮挡或缺字。元信息末尾“用”单字续行，为轻微排版问题。 |
| repair_excel | FAIL，1次人工纠正 | 另开Ryn任务10模型调用、11批准，`agent_step_limit`。4846B，SHA256 `c82816e5feb6aa1bd1f38886af64c3e297d3984784337c8c22a8bf0161036e17`。输入是首轮错误文件的逐字节复制，输入和原文件均保留；保存文件仍有12个错误单元格，B5:D7原始数值被错误写入公式覆盖，E5:G7仍是旧错行公式，费用/利润/毛利率为错误值。第9轮进一步编辑后第10轮保存提案未执行，不能把内存状态当已保存修复。 |

真实业务合计40次模型调用，4个任务共有58个confirmed工具回执、4个not_executed回执，31次精确批准；其中人工业务纠正1次。首轮Excel/Word/PDF的confirmed工具回执分别19/8/10，修复为21。`result.json` 的源配置、原Bridge调用封装、GLM响应、Craft提案、批准与执行回执一致；没有 Codex Agent 接管业务生成。三个native引擎工具数量Grid18/Word7/Pdf35，copied executable哈希均与preflight一致。

独立 PDF.js render 为 `independent-001/pdf-first/page-1.png`；独立 Artifact Tool 只读导入/渲染为 `xlsx-first/sheet.png` 和 `xlsx-repair/sheet.png`。另进程WordCraft重开副本并派生 `native-first/native-word/page.png`、QA PDF，全页已查看，正文和轻微换行情况与最终业务PDF相符；原DOCX/XLSX及核验副本均未保存改写。首份错误XLSX在副本内把North数量12改13后，预期总计未正确重算，`xlsx_recalculates=false`。没有由 Codex 或检查脚本代写、修复业务 XLSX/DOCX/PDF。

标准 `render_docx.py` 已独立运行并失败：`LibreOffice soffice.exe was not found on PATH`，exit1，日志 `independent-001/docx-standard-render.log`。不能把同一个 WordCraft 引擎另进程重开/派生PNG/PDF视为独立 MS Word/LibreOffice 兼容性验证。

## 测量和快照

独立测试合计 **176case：168pass / 8fail**：预算70（69/1）、容量46（41/5）、边界和UI58（56/2）、业务批准策略2（2/0）。8个失败断言归并为上表6项源码问题；来源级2项无效值探针是对扩展UI合同的失败。业务4个真实任务另外列示，不混入pytest/Vitest数量。失败探针不是xfail，探索重复不重复累计；Agent UI另有69项因定向筛选而skip，不称已测。未扩大全仓测试。

`independent-001/source-start.json` 对295个生产Python文件及384个webapp源文件共679项记录完整SHA256，manifest SHA256为 `f6831c34955ac7ceb008c2fc604123a41019cad0984cbe533e635da0b7cdc07f`；`source-end-before-fixes.json` 完全相同，`source-comparison.json` 的changed=[]。16:36归档前又完整核对679/679一致；归档 `reviewed-production-source.zip` 为2261840B，SHA256 `dc01ca1054f25f5885cbaf201bf42d552a2396e772e0f18d74c569f25ab4cc3b`。

业务首轮与修复snapshot各295/295生产Python与该受核快照一致，两个result均记录imported_source_unchanged=true、owner_source_unchanged=true。本审查未读取owner认证文件，后者依据本轮worker的前后hash守卫及harness代码。首轮snapshot SHA256 `483879a3407511460139f50036913ed65ad7b146b0b41435fdc469bb112847ac`；修复snapshot `a9abe31f406621b334165b017577ae31ea9a7d3eaa0950aa6caf154aad54c15e`。当前harness为加入明确修复分支后的版本；首轮harness旧hash由原snapshot保留，不用当前harness冒充首轮完全相同。

逐任务独立XML/公式/哈希核对为 `business-audit-business-final-01.json`、`business-audit-business-repair-01.json`；原始结果位于证据根 `work/ryn-context-business-20261018/{business-final-01,business-repair-01}`。四个result SHA256依次为：Excel `98d6abf61b38506c1c046e4cbcd9e77c5a560eb2c62c291c7cbb92cfca5a6bb0`、Word `62216817a9c7f73dcabbe2381a6c86b226060909f2631a3077b8fa4ad351331b`、PDF `9e4fc1c73c65a615f2a48c29105d735be15fd29515ba033558682241e4325a02`、修复 `4f88dcb39489a6eb2636c5a112f8205d612a99cc67f9748614ec7f2bf3872403`。

已有主任务兼容日志保留244pass/1旧诊断失败及before复现、UI整组78pass/1计时失败及单独复跑通过；这些不被改写为“全仓绿”。累计token限额按heuristic reservation准入，实际usage可以超过预留；未知窗口32768是标明的假设，均不是精确token/费用硬上界。
