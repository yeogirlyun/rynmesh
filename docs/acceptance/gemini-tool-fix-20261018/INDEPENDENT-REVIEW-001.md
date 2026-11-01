> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`cf5db52fd54a0adbdb5d63b431dc06dbe1de9c34659773c14b678ea1104fa435`。

# Gemini 4 工具调用修复及本机真实任务独立验收

**结论：PASS。没有发现阻断本轮目标的缺陷。** 本轮两个可证明的实现问题已修复；固定代码进入普通 Windows 用户实际运行的安装版；留存证据支持原数据、设置和授权状态保全；原 Gemini 模型完成了本轮新 Word 和 PDF，文件、数学及逐页可读性检查通过。旧三次 invalid 的确切类别仍无法恢复，本结论不把实现缺陷等同于已证明的唯一历史根因。

日期：2026-10-18，Asia/Hong_Kong。独立审查从原始源码、before 副本、部署材料、完整本轮任务快照及实际文件开始，未把主流程的 passed 字段作为验收依据。协议、安装保全、业务事件由三个独立子审查并行检查，主审查另外复算数学、比对全文、重渲染 PDF、从普通 Windows 进程重开 Word，并查看本轮每页。

## 范围和工作区

- 仓库：`<repo>`；HEAD：`0146c0255fb056dcbb1b4939e27cd81c05073ef7`。
- `W`：`<user-home>/Documents/Codex/2026-10-18/new-chat-4/work/ryn-gemini-fix-20261018`。
- `P`：`<repo>/.codex-tmp/installed-gemini-fix-20261018`。
- `E`：`<local-research>/research/ryn-gemini-fix-20261018/outside-update-002`。
- `B`：`<user-home>/Documents/Codex/2026-10-18/new-chat-4/outputs/Ryn-Gemini工具链验收-20261018/gemini-installed-01`。
- `Q`：`<repo>/docs/acceptance/gemini-tool-fix-20261018/independent-001`。

已有大量 dirty 文件保留；本验收仅新增自己的报告、离线测试和 QA 副本。未安装、未启动第二个 Ryn 节点、未发模型请求或用户业务任务、未改产品设置/权限/凭据/业务原件，未 commit、stage、reset 或 clean。没有复制私有备份，没有读取密钥或旧会话正文。额外启动的是仅处理 QA 副本的离线 WordCraft 进程。

| 验收目标 | 判断 | 直接依据 |
|---|---|---|
| schema 和安全纠错反馈实际接通 | PASS | 源码/delta/AST、独立 54 项离线重跑、原 233 项 JUnit、探针 |
| 本机实际安装并运行新代码 | PASS | frozen 295 模块/31 UI、857 源文件、完整 exe SHA、外部进程链和 socket |
| 原数据/设置/授权保全 | PASS | 安装后启动前 3099 文件逐 SHA 相等；28 旧任务语义及20授权字段保全 |
| 同一 Gemini 独立完成新 Word/PDF | PASS | 完整事件、HTTP-only 客户端、fresh 输入 SHA、真实 Craft 导出、数学/全文/视觉 QA |

## 代码与协议

主审查直接读取了 [生产源码](../../../rynmesh/llm_package/antigravity_session.py) 和 `W/product-delta.diff`。工具对话现将既有 `schema` 传入 `infer()`，其已有分支将 schema 编为独立 `--json-schema` argv；普通纯文本对话仍不传 schema。相对本轮 before，`infer()` 和 `prepare_workspace()` AST 均未变化。

解析优先使用 `structured_output`，无效结构化对象不能回退到显示文本绕过检查。提案数量、required、精确名称/字段、参数对象和原有 JSON Schema 参数校验仍执行；整批通过才返回工具调用，后一个无效提案不会先释放前一个有效提案。`tool_choice=none` 仍不能调用工具。provider 的空私有工作区、deny/空 allow、禁 slash、显式模型参数保持，Ryn functions 仍是文本协议中的提案，未注册成 provider 可执行工具。

新增反馈来自固定分类；validator 名仅在既有 `FEEDBACK` 白名单内才采用，否则归为固定 `arguments_schema`。既有 bridge/runtime 接收这些安全反馈。测试验证未知名称、参数值及原始拒绝正文不进入反馈，异常链被 suppressed，自主重试后副作用只执行一次。原有 `toolAction`/`toolSummary` 兼容根键不是本轮扩大范围。

独立重跑 **54 项全部通过**，网络尝试 0，ruff 通过。原两份 JUnit 独立解析为 **54 + 179 = 233 个不重复用例**，失败、错误、跳过均为 0。179 项为审阅既有运行证据，本审查未重跑；独立 54 项也不能再加到 233。HTTP wrapper 的9项离线 guards 单独通过，不计为真实业务或额外模型调用。

原流程更早一次12例断言已过，但pytest默认临时目录清理遇权限错误而exit1；正式专用basetemp运行及本次独立重跑exit0。该历史退出错误保留在修复说明中，未计作成功运行。

三次已有 live probe 均 accepted、各仅一次 Gemini 推理、工具执行为0。baseline 01/02 无 schema 也成功；fixed 01 与 baseline 01 的 body SHA 同为 `d499a9529f6c09344ba94f38d89d063f797bda11292a2272e02bddbf2f66a14d`，且 fixed 有有效 structured output。它们没有复现旧 invalid，也不是旧请求字节精确重放。旧任务三次仅保留通用拒绝，没有原回复，无法判断 JSON、结构、名称或参数哪一项失败。

详细代码行、测试及探针重建见 [独立协议报告](independent-001/protocol/protocol-review.md)。

## 安装身份与数据保全

独立离线核对了包内 **295 个 normalized 代码对象、31 项 UI** 与源码，857 项源文件前后 SHA 无漂移；对上轮 build 的生产 Python delta 只有 `llm_package/antigravity_session.py`。新 backend 完整 SHA 与构建、部署、任务身份记录相同。shell 仅在动态确定的唯一 Tauri 区域将 `UNK` 改为 `NSS`，恰3字节；据整个文件重算得到完整 expected installed SHA，未忽略任意区域。版本标签仍为 0.7.0，身份按 hash 区分。

`E/status.json` 确实记录唯一实际 NSIS 执行 exit **0**，新 shell/backend 启动；原 status 仍为 `failed / old_public_ids_missing_after_restart`。独立比较发现首个6秒 API 快照有6个端点 TimeoutError，比较器因缺响应误报32项 missing。后续45秒只读快照10个端点正常、28旧任务ID均在、设置一致，语义比较进一步确认消息和权限一致。resolution 绑定原失败 status 的 SHA，未重新安装或迁移，也没有改写失败原证据。

保留的早期 ACL SID 写法、health.status 预检及 launcher hash typo 失败均在停机或安装前退出；`outside-update-001` 不是另一次安装。它们不能被算成部署成功，也不影响 `outside-update-002` 的实际 NSIS 0 及后续恢复证据。

普通用户实际进程链为 **42424 → 48608 → 42636**。任务前后各自 keepalive 的反向 exact ESTABLISHED socket 都归 worker **42636**，并绑定新 backend 完整 SHA；独立当前 CIM/netstat 也仍见同一父链及8791连接。Codex 内直接读取 normal Local/Ryn 的 shell/backend 得到旧 shadow SHA，因此不据此反推普通安装未更新。

本轮备份记录为3103文件、4,374,391,587字节，未回写旧 profile。安装后启动前的数据核对覆盖 **3099 文件**：node home 347、plugins 539、Local profile 2211、Roaming settings 2，逐 SHA 零变化。启动语义证据覆盖28旧任务：全部 messages 相等、旧 events 为精确前缀、每任务20项权限/控制/pending approval 等字段相等，root settings SHA 相等。唯一旧 interrupted 任务新增重启事件，导致索引/一个加密 blob 名变化；不能据其密文字节变化宣称丢会话。本审查核查算法、输入输出绑定和安全汇总，未重新解密所有旧会话。凭据文件和权限保全不等于已逐外部服务重新登录。

安装完整方法与边界见 [独立安装报告](independent-001/installation/installation-review.md)，断言及当前只读证据见 checks.json（原始证据本机留存） 与 current-context.json（原始证据本机留存）。

## 同模型真实任务

两任务均冻结为 `local/antigravity-cli/model/gemini-4-argon-low`，adapter 为 `antigravity_cli`。检查完整事件序列、消息、wrapper 及锁定原 HTTP client：无 GLM 替换、无 settings PUT、无额外人工业务提示或客户端代写成品。每任务仅一个原始 user prompt；Word 原提示未预填计算合计。循环在已安装 worker 上执行，HTTP wrapper 未实例化 AgentRuntime。外部 job exit **0**、stderr 0字节。

| 实际指标 | Word | PDF | 合计 |
|---|---:|---:|---:|
| 模型调用 | 5 | 7 | 12 |
| 已接受提案/配对结果 | 15 | 26 | 41 |
| host handler 派发 | 14 | 25 | 39 |
| workspace 自动授权事件 | 4 | 5 | 9 |
| 模型回复拒绝 | 0 | 0 | 0 |
| not_executed / error-flag 结果 | 0 | 0 | 0 |
| 后续人工业务提示/显式审批 | 0 | 0 | 0 |
| 报告耗时（秒） | 147.578 | 198.234 | — |

Word任务：`agent_f7496d559ac74fe9998ff81fcb76ccb8`，事件1–67连续；PDF任务：`agent_86bbb6e76a1f4f92bb269031dd9f1d3a`，事件1–103连续。41个配对结果包含2个内部发现结果；39次派发包含读取和 schema 查询，二者均不能称为原生成功写入数。完整 receipt 数未知，cost 为 unknown/null，不代表费用为0。

最终采用 `B/sales_word/simulated-sales-report.docx` 和 `B/sales_pdf/simulated-sales-report.pdf`。PDF 输入副本逐字节等于本轮新 Word，服务器打开与授权事件的 SHA 也一致；未采用旧 GLM 文档或草稿 PDF。最终文件均有对应 call/operation ID 的服务器 Craft 导出及 artifacts API 记录。

详细逐事件、call ID配对及客户端检查见 [独立业务事件报告](independent-001/business-events/REVIEW.md)。

## 文件和逐页 QA

主审查重跑原解析器：Word17项、PDF16项及来源/输入绑定检查全部通过。另外从原件 XML 与 pypdf 独立逐行提取输入、重新计算，而非只搜索合计数字：

| 产品 | 数量 | 单价/单位成本 | 销售额 | 成本 | 毛利 |
|---|---:|---|---:|---:|---:|
| North | 12 | 150 / 90 | 1800 | 1080 | 720 |
| East | 8 | 240 / 150 | 1920 | 1200 | 720 |
| West | 5 | 360 / 220 | 1800 | 1100 | 700 |
| 合计 | 25 | — | 5520 | 3380 | 2140 |

费用500、利润1640、毛利率38.77%均正确。忽略逗号/空白和新增 `SIMULATED DATA` 后，PDF正文与全部12段 Word 文本完全相等，时期、CNY及模拟数据标识一致。

主审查独立以 bundled Poppler 120dpi 重渲染最终 PDF，并查看唯一第1页：全文可读，`SIMULATED DATA` 位于正文下方，无实质截断、覆盖或缺字。Word 额外由普通 Windows 下新离线 WordCraft 进程对 QA 字节副本重开、inspect、渲染；1页12段，全文等于原 XML。主审查看过该新第1页，内容和中文均可读，无实质排版缺陷；新图 SHA 与本轮已有图相同，未借用旧验收图片。

保留两类失败及边界：

1. 本次标准 `render_docx.py` 仍 exit1，日志明确缺少 `soffice.exe`。未安装新依赖。Word视觉证明来自同一 WordCraft 引擎的独立进程，未证明 Microsoft Word/LibreOffice 跨引擎兼容。
2. 主审查首次从 Codex 上下文运行 native helper，启动引擎前即报 `craft_agent_runtime_not_verified`。单独核对 receipt 与10个 payload 的 SHA 仍匹配固定 manifest，确切拒绝原因未诊断；没有放宽校验。随后普通 WMI 启动的 QA-only helper 通过同一严格校验并 exit0，CLI SHA 为 `74eff42097886095f106f662cd9a6eea77c10507be2b1698d80979c187368208`。首轮失败报告和日志原样保留，以独立 resolution 关联解释。

Word原件、PDF输入副本及最终PDF在全部QA前后SHA均相同。证据：独立文件QA最终判定（原始证据本机留存）、保留首轮失败（原始证据本机留存）、普通进程exit记录（原始证据本机留存）。

## 实际检查命令与 exit

以下路径缩写均定义于上文；完整 native/renderer argv另见 `Q/artifacts/commands.json` 与 `ordinary-native-exit.json`，测试完整 pytest argv见 `Q/protocol/focused-exit.json`。所有离线脚本只写自己的QA目录。

| 实际执行入口 | exit | 结果 |
|---|---:|---|
| `git rev-parse HEAD` / `git status --short` / `git diff --stat` | 0 | HEAD与既有dirty确认；无Git写操作 |
| `.venv/Scripts/python.exe -B Q/protocol/run_focused_offline.py` | 0 | 54 passed；network_attempts=0 |
| `.venv/Scripts/python.exe -B -X utf8 Q/protocol/audit_evidence.py` | 0 | delta、AST、233 JUnit及probe body SHA通过 |
| `.venv/Scripts/python.exe -B -m ruff check --no-cache rynmesh/llm_package/antigravity_session.py tests/test_antigravity_cli.py tests/test_antigravity_proposal_feedback.py` | 0 | All checks passed |
| `.codex-tmp/windows-package/venv/Scripts/python.exe -I Q/installation/verify_independent_installation.py` | 0 | 包、provenance、状态恢复和保全证据通过 |
| `.codex-tmp/windows-package/venv/Scripts/python.exe -I Q/installation/capture_readonly_context.py` | 0 | 当前CIM/netstat、exe视图、非敏感receipt/payload |
| `.codex-tmp/windows-package/venv/Scripts/python.exe -I Q/installation/word_runtime_preflight.py` | 1 | Codex库存验证拒绝；未启动Craft；保留边界 |
| `python -B -X utf8 Q/business-events/audit_business_events.py` | 0 | 完整事件、HTTP来源、9 guards通过；无live IO |
| bundled Python `-B -X utf8 W/business-harness/gemini_word_pdf_checks.py --root B --evidence W/gemini-installed-01 --qa Q/artifacts --poppler <bundled pdftoppm.exe>` | 0 | 17 Word +16 PDF、输入与来源检查通过 |
| bundled Python `-B -X utf8 Q/artifacts/verify_artifacts.py` | 1（PowerShell外层） | 数学/全文/原件hash通过；内层renderer和Codex native各exit1；保留失败 |
| `Invoke-CimMethod -ClassName Win32_Process -MethodName Create`，隐藏启动 `.venv/Scripts/python.exe -B -X utf8 Q/artifacts/native_ordinary.py` | 0；WMI ReturnValue=0 | 仅QA离线helper；其native_verify子进程exit0 |
| bundled Python `-B -X utf8 Q/artifacts/resolve_artifacts.py` | 0 | 普通native、原件不变、全文数学及视觉证据合并通过 |
| `view_image`：`Q/artifacts/ordinary-native/native-word/page.png`、`Q/artifacts/pdf-render/page-1.png` | 成功返回 | 各1页，均完整视觉检查 |

bundled Python 是 `<user-home>/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`；Poppler 是同runtime的 `dependencies/native/poppler/Library/bin/pdftoppm.exe`。本审查使用既有 repo虚拟环境仅运行产品离线测试/原生验证依赖，没有安装包。原始部署NSIS、HTTP业务job、普通native重开均以留存原exit记录0交叉核对，未在验收中重执行业务。

## 关键 SHA-256

| 文件/身份 | SHA-256 |
|---|---|
| 当前生产 antigravity_session.py | `491c4ebd6870bad1a5a8e64b842cbb55591cf30b4f16f8ccd1ef5538e19bdbd4` |
| before副本 | `378059fe8841abf340df74d72b790a0dffe1b04b7b1473f35289c8a1f1bc1876` |
| product-delta.diff | `feb8a24608024b6e74bf9ce3a9850624fd184da617e01e3c8497d4cacc43c296` |
| 本轮 installer（实际读取重算） | `b5c6b9595f67d557a29e04dee51f55a164f8a4e74389180db50bfc205e1ce972` |
| 普通安装 shell（整文件expected且外部运行证据匹配） | `436b13597ae1c206d3c8f58a9f48db8f46ac201a204b00de99b25cf8e7fca925` |
| 普通安装 backend（构建实算且外部运行证据匹配） | `8ad9968858ceeb1e5f43df2d993a211fc68c80389c3f9630574b23b6ac6f3a63` |
| W/installation-proof.json | `c27af528f37a0d10b0cd436e9ea63b30791f9adc4314a941d7bae1e9f84203dc` |
| E/status.json，保留failed原证据 | `ce6653c14ded46cbb53f5a6791523d554db4fca2d86e7c4244b485d4aa4c82fe` |
| W/startup-verification-resolution.json | `6e67584f37e604b91ffc0086957aa665fcc36d345e62d7df2fa4ba13f120249e` |
| W/full-userdata-byte-comparison-before-start.json | `671c9536ddb654f2ff56c04753d2ef810ce650790b63334db5447d882a88e8bd` |
| W/semantic-preservation-startup.json | `aa5f8fadb4980b6ff86041db9dbfe182bbbd3f45cf29da442169edb3305e3c9b` |
| HTTP wrapper | `0065fae24f4b7241362e5eb11c44853418def5478a630351a5245bdd935d716a` |
| 新Word原件及PDF输入副本，各7217B | `c64d87e5409745fd3a8baf70386d1017b2d406348d725918bfd412c1cff28605` |
| 最终PDF，46092B | `f5458049a705b917a3a96853cedfd199f8a0503618ad8fa2177b18bac6595e57` |
| 本审查普通native Word第1页PNG | `08fdab0a1e1e6a13d1ac00984fc7eacf35cbda3732b3e2e6d2422d058db09a25` |
| 本审查Poppler PDF第1页PNG | `e75f189772788244c17f2c91e88d8badbfea59d19446f1b78a8ebddb7a95a59a` |

其余输入、独立报告、脚本及主要QA输出的完整路径/大小/SHA在 review-manifest.json（原始证据本机留存）。本报告本身另有同目录 `.sha256` sidecar，避免自引用hash。

## 阻断项和明确边界

**阻断项：无。** PASS 只针对本轮明确目标和上述已核查证据。

- 无法恢复旧三次invalid的精确类别或证明唯一历史根因；无schema baseline也成功，不能宣称缺schema必然失败。
- 两个成功真实任务不等于所有任务/所有模型可靠性证明；没有逐provider请求独立原始抓包。`context_window=32768`为unknown fallback元数据，不是实测窗口；保留原有设置不代表测得新上限。
- Word已在同WordCraft引擎独立普通进程验证，Microsoft Word/LibreOffice跨引擎行为未验证；缺soffice及首轮Codex native拒绝日志均保留。
- 保全检查支持原数据、设置、权限/凭据文件未丢失；未逐外部服务重新登录，未再次解密全量旧历史。
- 41配对结果、39处理器派发、9自动授权是不同口径；完整receipt数和金额未知，未把unknown当0。

最终判定由源码与严格离线测试、实际部署身份/保全、完整同模型事件和本轮文件QA共同支持。未用旧GLM成功、单次probe或主流程自报completed替代本轮验收。
