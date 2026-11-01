> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`3417f8b939815334f3b8590624bd0fe5216e72065bb0b93b6c5e52a0cf957a40`。

# 独立协议验收：PASS

审查日期：2026-10-18（Asia/Hong_Kong）。范围为本轮 Antigravity / Gemini 工具提案协议的源码、离线测试和已留存探针；本报告不代替安装身份、数据保全或 Word/PDF 文件验收。仓库 HEAD 为 `0146c0255fb056dcbb1b4939e27cd81c05073ef7`，保留已有 dirty 状态。没有安装、启动节点、运行模型、修改产品设置/凭据/原产物，新增内容仅在本报告所在 QA 目录。

没有发现阻断本轮协议修复的缺陷。两个实现问题确有修复，校验和 provider 权限没有因本轮 delta 放松。旧三次回复的确切失败类别仍不可恢复。

## 独立发现

1. [antigravity_session.py:141](../../../../../rynmesh/llm_package/antigravity_session.py) 的工具对话现在向 `infer()` 传 `schema=schema`；[infer:55](../../../../../rynmesh/llm_package/antigravity_session.py) 将其编码为 `--json-schema` 的独立 argv 项。`infer()` 的 AST 与本轮 before 副本一致，证明 CLI 参数实现原来已存在，缺陷是调用点遗漏。纯文本路径 [110](../../../../../rynmesh/llm_package/antigravity_session.py) 不传 schema。独立重算的 unified diff 与 `product-delta.diff` 完全一致。

2. [本地解析:145](../../../../../rynmesh/llm_package/antigravity_session.py) 优先使用 `structured_output`；非空但无效的结构化对象不能回退到显示文本规避校验。[153–171](../../../../../rynmesh/llm_package/antigravity_session.py) 仍检查提案数量、required、整批精确工具名/字段、参数必须为对象，并执行原有 `validate_tool_arguments()`。所有提案校验后才返回；adapter 在返回后才发 buffered stream，后一个提案无效不会先输出前一个有效调用。`tool_choice=none` 的 allowed 集合为空，任何提案仍会被拒。

3. [反馈:143](../../../../../rynmesh/llm_package/antigravity_session.py) 使用源码固定分类；validator 名只在现有 `FEEDBACK` 白名单中才被采用，否则归并到固定 `arguments_schema`。错误不附带原始回复、未知工具名、参数值或 validator 文本，并以 `from None` 隐去异常链。现有 [bridge:261](../../../../../rynmesh/agent_runtime/bridge.py) 会把类别映射为固定反馈，再交给 runtime 自主纠错。离线 sentinel 测试和真实 adapter→bridge→runtime 的模拟重试均通过，副作用仅执行一次。

4. [prepare_workspace:17](../../../../../rynmesh/llm_package/antigravity_session.py) 和 `infer()` 对 before 与诊断副本的 AST 比较均相同。空私有 workspace、`copy_auth=False`、provider 权限 deny/空 allow、显式模型 argv 均保留。`functions` 只进入协议提示文本；本轮没有向 provider 注册可执行的 Ryn 工具。测试实际截获 spawn argv，确认 JSON schema、模型、输出格式和禁 slash 参数，并消费真实 infer 解码路径。

5. 本地 root shape 原有兼容键 `toolAction` / `toolSummary` 仍允许，provider schema 仍只允许 `content` / `tool_calls`；这点在 before 已存在，本轮没有扩大允许范围。未知工具及不合 schema 参数严格拒绝的结论不受影响。

## 验证及命令

工作目录均为 `<repo>`。独立 runner 禁止 socket connect，禁 bytecode/cache，将所有离线临时数据放在本 QA 目录。实际运行只产生一个无网络的 Python Windows console/Job Object 子进程，provider spawn 在测试中均被替换。

| 实际命令 | exit | 结果 |
|---|---:|---|
| `git rev-parse HEAD`；`git status --short` | 0 | 确认 HEAD 和既有大量 dirty，未 stage/reset/clean |
| `.venv/Scripts/python.exe -B docs/acceptance/gemini-tool-fix-20261018/independent-001/protocol/run_focused_offline.py` | 0 | **54 passed in 12.29s**；网络尝试 **0**；无 skip |
| `.venv/Scripts/python.exe -B -X utf8 docs/acceptance/gemini-tool-fix-20261018/independent-001/protocol/audit_evidence.py` | 0 | delta、AST、两组 JUnit、探针 body 重建 hash 断言全部通过 |
| `.venv/Scripts/python.exe -B -m ruff check --no-cache rynmesh/llm_package/antigravity_session.py tests/test_antigravity_cli.py tests/test_antigravity_proposal_feedback.py` | 0 | All checks passed |

独立 runner 实际执行四个原测试模块：`test_antigravity_cli`（17）、`test_antigravity_proposal_feedback`（27）、`test_antigravity_usage_accounting`（7）、`test_cli_background_windows`（3）。原两份 JUnit 解析得到 54 + 179 = **233 个不重复 classname/name 组合**，failures/errors/skipped 均为 0。179 项仅审阅留存 JUnit，未声称本审查重新运行它们。独立重跑的 54 项是同一组覆盖，不能再加到 233。HTTP wrapper 的 9 项 guards 不计入本报告的独立测试数量，也不计为真实业务。

输出为 focused.log（原始证据本机留存）、focused-exit.json（原始证据本机留存）、focused.junit.xml（原始证据本机留存） 和 evidence-audit.json（原始证据本机留存）。后者包含完整输入路径、逐文件 hash、测试类计数、旧事件的安全元数据和 body 重建结果。

## 探针和旧根因的明确边界

三份原始探针 JSON 均记录 `gemini-4-argon-low`、accepted、0 client tool dispatch。baseline 01/02 没有 schema 且没有 structured output，仍分别有 1/2 个合法提案；fixed 01 有 schema、存在 structured output 且接受 1 个提案。代码检查确认 probe live 分支只调用一次 `module.chat()`，没有工具 dispatcher；本审查没有运行该 live 分支。

独立调用 probe 的 `body_for()`（仅离线重建）得到 continuation body SHA `d499a9529f6c09344ba94f38d89d063f797bda11292a2272e02bddbf2f66a14d`，同时匹配 baseline 01 与 fixed 01；initial body SHA `2629852c6b9e67b61c0d0632abd7e83040dad2709f624309015a01876c3557cd` 匹配 baseline 02。三份都明确 `old_request_byte_exact=false`，不能算旧请求的逐字节重放。

旧任务 `agent_f79ae56fb2754bbabd6e72bd42df5c54` 的原 API 证据 SHA `b73c6fb044e9ad62c4094c40d46afaac3bd9570c48fa74d141ccd95cd8b3fe0b`。独立检查确有三次 `model_response_rejected`，全部是通用 `agent_invalid_model_response` / 通用 detail，finish_reason 都为 stop，实报 output 为 165 / 62 / 119 tokens；各轮 configured/effective cap 均为 4096。没有旧回复正文或具体分类，不能确定是 JSON、名称、结构或参数失败，也不能宣称确定存在或不存在所有截断问题。无 schema 探针成功还直接排除了“没有 schema 必然失败”的推论。本轮证明的是实现缺陷修复及当前路径可用，不能追认旧三次的唯一根因。

## SHA-256

| 文件 | SHA-256 |
|---|---|
| 当前 `antigravity_session.py` | `491c4ebd6870bad1a5a8e64b842cbb55591cf30b4f16f8ccd1ef5538e19bdbd4` |
| `antigravity_session.before.py` | `378059fe8841abf340df74d72b790a0dffe1b04b7b1473f35289c8a1f1bc1876` |
| `product-delta.diff` | `feb8a24608024b6e74bf9ce3a9850624fd184da617e01e3c8497d4cacc43c296` |
| 原 focused JUnit | `a71ed18f7969769d8cf56b2060ffc8134965aa932bd0b3bb0226a95918d0b638` |
| 原 regression JUnit | `f72755f456bc2c352255aab37f6da4ab72507bc3befa876f34560f74d283e236` |
| 本审查 focused JUnit | `b49d1b72194b5aaa4f0254f5da2b9139a2a679b9a8568dcf971d5680efd88561` |
| baseline-live-01.json | `bf3bfe369beab489ece18254c7f39dc8216e3d84ab986ce841e278584234b884` |
| baseline-live-02.json | `0947870b499ce614a5269ccb7ac3e402fa9c274a1e315dd6e2cd4f366539c600` |
| fixed-live-01.json | `d2bd24c3a9cf6e7dd9e70e1597e364240814a72dd8c69ce7092f0e4297ca0bda` |

**结论：协议部分 PASS；阻断项无。** 安装身份、数据保全、真实业务数学及逐页视觉结果由并行审查合并判断。
