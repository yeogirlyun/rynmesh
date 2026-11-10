> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`63bde6166ae5316f947146e530d8f0c841ae27b09f2d3e006e9ba469fb3220d2`。

# Ryn 本机安装版独立验收 001

审阅日期：2026-10-18，Asia/Hong_Kong。结论：**PASS**。本次安装身份、原数据/设置/授权文件保全，以及最终 Excel、Word、PDF 真实业务结果通过本次约定范围的验收，未发现阻断。**首次业务尝试有失败，最终结果经过两次人工业务干预才通过**，不能称为首轮全成功或全程无人介入。

本审阅从原始证据、实际文件、比较实现及调用事件独立作结论，没有沿用主线程或旧独立报告的 PASS。未切换审阅模型，未发新付费推理、启动第二节点、修改产品源码/运行任务/设置/授权/原业务文件，未提交、reset 或 clean 工作区。新增持久审阅文件为本报告；离线测试另产生独立 pytest 临时目录。本轮没有重新执行旧 430 项测试，也不把旧测试计入本次结果。未解密或输出密钥、Cookie 或旧聊天正文。

证据根：

- R = `<user-home>/Documents/Codex/2026-10-18/new-chat-4/work/ryn-installed-upgrade-20261018`
- B = `<user-home>/Documents/Codex/2026-10-18/new-chat-4/work/ryn-installed-office-20261018`
- P = `<repo>/.codex-tmp/installed-agent-20261018`

| 验收项 | 结论 | 独立核验所得 |
| --- | --- | --- |
| 已验收源码进入安装产物 | PASS | 当前构建源 857 项与构建前基线逐项相同；冻结 EXE 的 295 Python 模块规范化 code object 与 31 UI 文件 SHA 全部匹配。生产指纹为 `c3ef74102482c31689716b6c9e5073cf9d017c6f29344a2bf552a084ddeacc12`。Git HEAD 为 `0146c0255fb056dcbb1b4939e27cd81c05073ef7`，已有 dirty 内容保留。 |
| 普通 Windows 安装与实际 worker | PASS | 外部映像/hash 证据绑定有效；helper、shell、bootstrap、worker 均为 NO_PACKAGE，真实 Local/Ryn 路径。实时只读 CIM 仍确认 `44336 → 32868 → 35824`；HTTP 业务每轮前后自身 keepalive 反向 ESTABLISHED 元组均绑定 worker 35824、8791 端口。主数据根仍为 `<user-home>/.rynmesh`。 |
| 原数据、设置、待批与授权文件保全 | PASS，含明确边界 | 启动前监测范围零缺失/改动；24 旧任务、397 条消息、480 项权限/控制/待批字段保持；备份及迁移逐文件哈希证据有效。运行中 1 个 Cookie 锁定，未逐服务重新登录。 |
| 最终 Excel | PASS | 实际 updated 文件 24/24 静态断言通过，15 个公式与缓存值正确；独立进程 North 数量 12→13 的四组联动检查通过；全图可读。 |
| 最终 Word | PASS，限已验证引擎范围 | 实际文件 17/17 静态断言通过；9 段，独立 WordCraft 进程重开和一页渲染通过。Microsoft Word/LibreOffice 跨引擎未验证。 |
| 最终 PDF | PASS | 实际文件 16/16 断言通过，一页未加密，业务文字和 SIMULATED DATA 完整；本审阅在内存重新用 Poppler 渲染，与已审阅全页 PNG 哈希完全一致。 |

安装器实际 SHA 为 `e737093297be798ca9c3e8c783e8be04220741c3c062e4ec01a23da2063cf9b8`，backend SHA 为 `04caff554de1e169df30e0c0748f204284b9c8f91b518f70cf474b07116c5165`，安装 shell SHA 为 `aabd868f7aedb7e60c8069a5639744fe58d29568b8a1a2e728c74a43a268ee5d`。产品新旧版本标签均为 0.7.0，本结论按哈希识别。复算 P 的四份 provenance 与 R/build-identity.json 的绑定全部一致，核心摘要所列 20 项 evidence SHA 绑定也全部通过。

普通 Windows 与 Codex Package 的路径视图确实不同。不能用 Codex 内直接 GetFileHash 的旧 shadow 否定升级。本次复核了既有外部映像/hash 快照的绑定及实时 CIM 进程链，**没有重新采集一份实时外部映像/hash 快照**。业务期间另有逐轮 socket→worker→映像/hash 证据接续。

首次安装 guard 的失败必须保留：NSIS exit 为 0，但 installed executable identity guard 报 `installed_executable_identity_mismatch`，当时尚不能判定迁移完成。独立将 staged shell 的 offset 9825930 三字节 `UNK` 在内存改为 `NSS`，所得 SHA 正好等于已安装 shell，支持 `shell-byte-diff.json` 的解释。后续 resume 只恢复迁移，`installer_executed_by_resume:false`；脚本没有再次执行安装器、启动或终止进程。不能将此过程写成一次无异常的自动升级。

保全比较并未将不存在的文件算作成功。重新调用保存比较实现，离线前后 12 个已存在固定文件、9 个目录组及插件 manifest 对应条目一致。私有备份独立重哈希为 **3116 文件、4,462,339,692 字节**，全部匹配；15 个长路径用 Windows extended path 重试后通过，未输出其内容。初始运行中 inventory 的 3118 不是最终备份数量。迁移记录与清单筛选聚合一致：**539 插件 + 2192 有效 Local profile + 2 Roaming = 2733 文件**，复制先核临时文件再核最终目标 SHA，保留目标独有文件并防止旧主程序覆盖。备份与迁移不能扩大成所有私人内容已逐项作业务语义检查。

24 个旧任务的消息总数为 **397→397**，逐任务完整消息 JSON 规范化 SHA 相同。状态分布保持 **17 completed / 2 failed / 2 waiting_input / 1 interrupted / 1 paused / 1 waiting_approval**。24×20 项权限、控制、限制、待输入/待批及相关字段全部相同；唯一 waiting_approval 对象前后都存在且 SHA 一致，根 settings 语义 SHA 相同。旧事件是精确前缀，总数 **1481→1482**：原 interrupted 任务增加一次重启 interrupted 事件。23 个任务仅新增 context provenance/observed_files，另一个多出该重启事件，因此内容寻址文件名变化不能判作 24 任务丢失。语义探针会检查 v2 blob 长度、SHA 与 task ID 后再规范化比较；本审阅检查算法及四份输入/输出 SHA 绑定，未重新解密旧任务。

外部布局记录中 **16 快捷方式、11 文件关联**都指向新 normal_install，目标存在且 shell SHA 匹配，11 项 OpenWith 登记存在。16 个旧 Cookie 相关文件启动前逐文件匹配；运行时 15 SHA 相同，1 个主 Cookie 因锁定仅确认存在及 20,480 字节。常规快照跳过锁/临时/部分点文件等，零变化结论限定其监测范围；全量离线备份另有上述完整清单和重哈希。授权文件与权限状态保全成立，不能扩大为每个外部服务已经重新登录实测。

已有 agy.exe 原先仅在 Package 视图，导致 preflight01 在 0 付费调用时失败。备份后迁移同一既有二进制到真实 Local/agy/bin，大小 **200,970,904 字节**，SHA `4f6c7a807f3fae54bc61e74b8469ac79d07702ade5c62de62041bba44e2a7be0`；无设置、凭据或全局 PATH 改动。之后 Gemini 和既有 GLM preflight ready。缺失一个 CLI 令整个 catalog 被吞空的产品边界仍存在；当前环境依赖已恢复，**不能声称产品逻辑已修复**。

真实业务由 HTTP-only 客户端调用已安装 worker，客户端 SHA 为 `70b8508746d3300a15aed4a61e86c5609d8007625e52d189882b569f47a2cd69`，与两轮 launch 绑定一致。代码未 import/run AgentRuntime、Bridge 或 loop；导入旧 helper 只取 prompt/permitted/FILENAMES。业务产物由 Agent 的 Craft 工具导出，客户端只复制本轮 Word 输入及 QA 文件。安装身份采用自身连接的反向 ESTABLISHED 元组，实际证据没有依赖 listener fallback。

| 实际轮次 | 状态 | 模型调用 | host handler | paired results |
| --- | --- | ---: | ---: | ---: |
| Gemini Excel 初轮 | completed；初版后来被排版续做替代，原件保留 | 6 | 26 | 27 |
| Gemini Word 初轮 | **failed / agent_invalid_model_response**；未生成 Word | 4 | 1 | 2 |
| 初轮 PDF | Word 缺失，未创建任务 | 0 | 0 | 0 |
| 既有 GLM Word 重试 | completed | 12 | 11 | 13 |
| 既有 GLM PDF 重试 | completed | 14 | 15 | 16 |
| 原 Excel task 排版续做 | completed | 6 | 11 | 13 |
| 去重合计，4 task IDs / 5 执行轮次 | 保留失败成本 | **42** | **64** | **71** |

71 accepted proposals 与 71 paired results 不能称为 71 次 native 成功操作；handler 也不等于实际成功写入。共 4 个 `not_executed/no_mutation`：Word 参数错 1、PDF 参数错 2、Excel 旧路径覆盖被拒 1，之后均由模型自主纠正。23 次既有 workspace 策略自动授权，0 次精确审批；完整 receipt ledger 数未知。42 次调用包含失败 Word 的 4 次和续做的一次真实压缩调用，金额 cost 为 unknown，不能给出免费或已知费用结论。

**两次人工业务干预**分别为：换到既有 GLM 重试 Word/PDF并补充数值口径一次；对原 Excel task 追加排版要求一次。这不包括安装 marker 恢复、CLI 迁移、HTTP 客户端调试等技术操作，不能把全程人工介入总数写成 2。各 row 的 human_business_corrections=0 只描述 runner 内部，不代表整体验收无人纠正。首次 Gemini Word 的失败是公开反馈下的响应/提案验证失败，无法再细分为 JSON、schema 或工具名错误；finish_reason=stop、输出 165/62/119 tokens、有效 cap4096，无截断证据，不应归因为上下文截断。

Excel 续做第一次缺 expected_revision，HTTP 409 后客户端又出现 JSONDecodeError；原记录保留。成功请求带 expected_revision=103，唯一新增 message_added 为 seq104、goal2。revision 在消息/预算/状态变更前检查，因此拒绝未派发模型或文件操作；该判断来自源码及事件连续性，没有另存完整同期前后快照。续做 seq159/161 记录实际 context_compaction_started/context_compacted，带 source SHA，随后 seq162–168 继续并完成；6 次预算模型调用中含一次压缩，常规 model_started 仅5次，不能误报少一次。

最终文件核对如下；本审阅直接从这些原件重跑 24+17+16=**57/57** 断言，所有原件与 QA 副本 SHA 逐一匹配。

| 最终 Agent 原件 | 字节 | SHA256 |
| --- | ---: | --- |
| simulated-sales-updated.xlsx（原始证据本机留存） | 4873 | `678f83cfb137062b42c6d3d82d84145a28a1764e1236a0dacc02f71799c3fd8a` |
| simulated-sales-report.docx（原始证据本机留存） | 6823 | `3cdca6dea65c767a0e0af45d33bdcde5fb7da3830ceb4205572f4d898fee6eb7` |
| simulated-sales-report.pdf（原始证据本机留存） | 38260 | `ff63e4bcb450a70b6cb4e6d3dccb57043ed76728deaaf7a02bed71fe7e74a965` |

实际源值为 North 12/150/90、East 8/240/150、West 5/360/220；合计销量25、销售额5520、成本3380、毛利2140，营业费用500、经营利润1640、毛利率2140/5520≈38.77%。15 个公式逐格可追溯到输入，汇总及利润/毛利率均为公式，费用和利润未错误显示为百分比。旧 Excel SHA `ef16decb603d97e6af5394c7c5a66ab951873808f99531c03aa395b699ad6f60` 保持，本审阅另核最终与旧版全部15公式及缓存值完全一致。

已绑定的外部 native QA exit0，输入副本与最终原件逐字节一致。独立 GridCraft 新进程只在内存将 North12→13，行销售额/成本/毛利成为1950/1170/780，总额5670/3470/2200，利润1700，毛利率2200/5670，数量合计26，四组检查均真，副本未保存且原文件未变。本审阅复核了脚本和实际 changed_values，未再次启动 native 进程。独立 WordCraft 新进程重开9段、一页，并生成 QA PNG；它与业务使用同一引擎，不能作为 Microsoft Word 或 LibreOffice 兼容性证明。标准 renderer 原失败为 `LibreOffice soffice.exe was not found on PATH`，保留在 B/installed-http-retry-02/qa/standard-word-render/stderr.log。

已逐张检查 Excel 全表、Word 全页、PDF 全页图：中文与数值无缺字、遮挡或截短，Excel A2 期间/CNY/模拟标识完整可读。Word/PDF 有句尾“好。”独占短行，属于轻微版式问题，不阻断当前办公内容。最终 PDF 的当前轮 DOCX 输入与刚完成 Word 输出 SHA 相同，未借用旧轮 Word。Poppler 本审阅重新渲染 PNG SHA 为 `13403bc4908b2de829bad2f065e291db17bc24f5153f4b5e66f62c950a7d0071`，与已检视图一致。

存在两处需要准确表述的报告状态。R/installed-core-summary.json 与 installed-core-report-draft.html 仍是 draft，business/review 保留 PENDING，业务重试还写 in_progress；它们没有冒充最终全通过，但尚不能作为最终业务汇总。另 B/excel-layout-continuation/result.json 和 status.json 固定旧路径，仍哈希 ef16 旧 Excel，这是**真实摘要瑕疵**；应采用 seq154 实际新文件导出及最终索引中的678f文件。最终索引已明确披露此问题，本审阅不改写原记录。release-provenance 的 installed_or_launched:false 是打包阶段状态，由后续安装证据接续，不是实际未安装。

离线 guard 检查实际执行记录：

1. `.venv/Scripts/python.exe -m pytest -q docs/acceptance/agent-context-business-20261018/test_installed_http_business.py`：17项测试执行通过，但 pytest session finish 清理全局 `pytest-current` 时 PermissionError/WinError5，**exit1**。
2. 同命令增加 `--basetemp <repo>/.codex-tmp/independent-http-guards-20261018-business-trace`：**17 passed in 0.34s，exit0**。没有再增跑，也未将两次加总。

本审阅另一个只读辅助解析命令因 JSON UTF-8 BOM 首次 exit1，改用 utf-8-sig 后 exit0；不涉及产品、模型或业务文件写入。57 项静态检查与 PDF 内存重渲染一次通过。上述审阅工具异常与业务首轮失败分别记录。

现有设置 workspace / 64步 / 900秒 / 手动上下文限制384000 原值不变。Gemini 模型窗口仍是 unknown_fallback32768，GLM 1000000 来自 legacy_source_configuration，均不构成实际百万 token 调用量测。本次真实压缩后续做成功，可支持安装版这条实际执行链路，不能外推为所有任务或百万上下文规模保证。

以下为本审阅重新计算的关键证据文件 SHA；上述结论绑定本批文件，不追认未来变化。

| 文件 | SHA256 |
| --- | --- |
| R/installation-proof.json | `0e5cd693c8b88c4bb5cd19690ff87a3a4ad6bfa8021febc76abbffd9e4734b02` |
| R/outside-running-identity.json | `fe41d0d1a764c58e666a726e8849af9bc323eb73eb248481ffea66eef83d36a8` |
| R/migration-status.json | `9b6c17c81577c60218924f855c548491a0892971e925066909777db6aca6ee6e` |
| R/semantic-preservation-startup.json | `dfffd4fa7f6ec100dd8bfe7269cb5984e3821fea669135233101e6507da59eaa` |
| R/semantic-preservation-evidence-binding.json | `bd5adfa9868362a8614fe114179ccd196af012d78d8e44c72073775b1ac1c2e1` |
| R/installed-core-summary.json（草稿） | `59631fd964c16700057c631bd8a6bf0c72d81e88a6754ebf610604f4b91ee6ff` |
| R/installed-core-report-draft.html（草稿） | `aa01b6dcd2c2b6bec832e38e8f9fed5b173091f10a6c2cb4c1da180a43fdbab9` |
| B/installed-final-qa/final-acceptance.json | `6a7f86a061c8aaaf90d623e1c053ed3f01d7a2d5e4b437e5b7ba8050f731d202` |
| B/installed-final-qa/native/native-independent-checks.json | `dff2268bf181a91f4d92505f7a0336bbb0af383b541ffc0cc0603ec4cb8be287` |
| P/frozen-provenance.json | `028e0656f624fbd0666b6484ea71c1061bd2843ee715bc4881afbd9cbe43addc` |
| P/release-provenance.json | `189d0791fd0c154a5b851e7de5ef6a6141240b7c50e61bfebf2462aeb270df43` |

签署结论：本次用户要求的安装更新、保全及最终三个真实办公结果 **PASS**。首次 Word 失败、环境与客户端恢复、两次人工业务纠正、摘要瑕疵及上述能力/渲染边界均随结论保留。
