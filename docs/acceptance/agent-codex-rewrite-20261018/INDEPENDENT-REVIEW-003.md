> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`7fb724a01119d3ff7fb21b59dcfdfbfdc4892372a6450ab45c60b18bbd19b13b`。

# Ryn Agent 重写独立验收 003

**结论：PASS。当前受核快照的七项均为 PASS，无未关闭的实质缺口或 PENDING 项。** 结论限于用户约定的“关键自动化＋3–5 个真实任务”范围。第二轮发现的应用不确定结果分类、找回收据后 consumer pending 未结算两项缺陷，在本轮实际故障复验中均已关闭。

验收完成：2026-10-18T15:56:06+08:00（Asia/Hong_Kong）。这是第三个全新独立验收聊天的结论；主审和三个并行子审读取当前代码、原始证据并实际复验，没有仅继承第一轮的通过判词。未触发新收费推理。

**七项判定**

| 条款 | 判定 | 独立依据与范围 |
| --- | --- | --- |
| 1. Ryn 自行装配提示词、调度工具、回写结果、继续/结束、压缩恢复 | **PASS** | 实际入口是 routes → Ryn AgentRuntime.run_once → AgentLoop；Bridge 仅提供模型响应。分层 request/context、call 配对 router、turn decision、流式读取和完整 checkpoint 安装都接入该循环。当前真实 host IO 集成回归通过，没有用 Codex Agent 代替 Ryn 执行内核。 |
| 2. 两个不同来源可用模型完成多轮工具 | **PASS** | 原始 GLM `glm-5.3-flash` API 与 Antigravity `gemini-4-argon-low` 记录均有多轮模型调用、工具交换与实际收据；后者原生文件/命令/MCP 工具被关闭，提案交 Ryn 执行。不要求两个模型逐字一致。 |
| 3. 保留 task API、工具注册、应用结果回传，一个已有应用闭环 | **PASS** | 真实 GLM 经 task HTTP → 工具发现 → 精确批准 → Desk 写入 → confirmed receipt → 最终答复及应用 HTTP 回读成立；当前 HTTP/真实 Desk 自动化及本轮新增故障恢复后的 Agent 闭环通过。 |
| 4. 项目查找、文件修改验证、应用操作有真实结果 | **PASS** | 查找命中实际 `src/catalog/sku.py` / `RYN_SKU_83`；编辑后的磁盘 hash、独立函数断言、shell `RYN_VERIFY_OK`/exit 0 一致；Desk 票号、标题在应用、收据和答复中一致且唯一。范围是隔离小项目/应用夹具。 |
| 5. 工具错误修正、中途追加/取消、普通长 context 继续 | **PASS** | Gemini 缺路径 → 搜索 → 正确读取的真实 trace 成立。当前 runtime/stream control、坏历史压缩、进程恢复等真实 host IO 回归通过。追加/取消/长 context 主要由自动化覆盖，不冒称四个真实任务都分别触发全部分支。 |
| 6. 旧会话可读，设置授权保留，成功写不因 retry 重复，恢复可继续 | **PASS** | 旧加密格式、设置、scope/权限、精确批准、retry 与重启回归通过。三类提交后异常均保留 unknown/原 op；runtime 独自查询原收据并清 pending，不重发业务。随后原任务完成，新任务经全新精确批准完成，原票仅一张。篡改、orphan、rejected 和异收据均不能结算/替换原 intent。 |
| 7. 同模型固定任务无明显持续退步 | **PASS，描述性样本** | 四组成对 prompt、初始文件 hash、4096 生成策略相同。三类固定任务最终完成，人工纠错和批准数量未增；搜索复测没有重现首轮调用增加。慢样本和全部旧失败保留。数据不支持提速、统计显著性或任意任务保证。 |

**第二轮 FAIL 事实保留，当前修复另判**

[INDEPENDENT-REVIEW-002.md](INDEPENDENT-REVIEW-002.md) 的第 6 条 FAIL 不改写。主审重新读取并核 hash：初始分类失败产物仍为 `019d11a0d86debb65b82f86a493659b42c24c9e12c33f5027081925e78319414`；原 pending 失败产物仍为 `4ade78c2307c41911acf43b56e190b1046f666d51e5a56c062c83edb2aefc92c`，其中 confirmed 后 pending=1、下一操作 `apps_pending_required` 的事实未变。核对见 historical-failures-verified.json（原始证据本机留存）。这些是旧快照的真实缺陷，即使 before 也有，也不能据此豁免验收。

当前 `AppTools.execute` 将与 NodeApps pending 合同相同的三类错误转成 unknown-write；其他明确拒绝仍为 `AgentToolRejected`。`AppTools.reconcile` 验证原收据后，以原 `reviewed_context` 推得的 preview hash 结算原 intent。内部 `confirm_receipt` 在同一 AppStore 原子 mutate 内核对 op/provider/consumer/app/action/arguments hash、内外 request hash、原 command 和原 preview；pending→committed，相同 receipt 幂等，异 receipt 拒绝，rejected 不复活，缺 intent 不创建。没有新增 HTTP、权限或业务 invoke/resume 路径。

**本轮故障探针和运行证据**

第二审原探针 SHA256 为 `459a52a22169353240f09f53b2cf78beab20893e942519f8fec0639dfccf3e79`。本轮 复制版（原始证据本机留存） 只调整目录层级对应的 ROOT 与输出目录，断言/业务逻辑不变；`--fault unconfirmed` 和 `--fault invalid` 加 `--skip-original-retries --expect fixed` 均退出 0。

主审发现旧探针在 `runtime.run_once()` 前还有一次直接 `impl.reconcile()`；在新代码下该调用本身会清 pending。因此，**原样探针通过不能单独证明 runtime 自动恢复**。本轮 加强版（原始证据本机留存） 删除这次直接调用，并在 runtime tick 前后解密自建 wire 计数。三类结果均为：远端真实提交一张工单 → unknown/in_flight/reconciliation pending 保留 → consumer pending 仍为 1 → runtime 仅发 `receipt` 一次 → confirmed、pending=0 → fresh review 新命令成功。没有手工 invoke/resume 原 op；模型边界脚本化，friend 认证、加密传输、AppStore/Desk 业务提交均使用真实生产路径。

| 提交后故障 | 本轮原始产物 | SHA256 |
| --- | --- | --- |
| `store_unavailable` | result.json（原始证据本机留存） | `d3d2cd0de26fa0ee5cc4364f93ab8efbfc947ab1dfef43f9281c0c1ba832a11d` |
| `unconfirmed` | result.json（原始证据本机留存） | `4db69fb46a36a57c5072602ea3e375873b201a6b3dbca766640c591636748c15` |
| `invalid` | result.json（原始证据本机留存） | `a28d17a6076afcb09527b372ddb951d26a5666ad789de184760699809f6cd89d` |

其中 `store_unavailable` 是 provider 已真实 commit 后返回经过真实认证加密的该错误，不伪称发生了实际磁盘硬件故障。invalid 是破坏加密响应；unconfirmed 是返回响应前断流。

更完整的 Agent 继续探针（原始证据本机留存）（SHA256 `a24210417074995f9bdde04d863345e30029e8df330bd955ada8a3ee8322d8e4`）同样不直接 reconcile 原 op。它还验证原任务使用恢复回写的 tool receipt 完成，第二个 Agent 任务在 waiting_approval 时未写入，经 fresh exact approval 后完成；共两张不同工单，原票恰好一张。产物见 result.json（原始证据本机留存）。

当前快照实际运行：

- **141 passed / 242.21 秒 / exit 0**，14 个文件，含新增 uncertain-response 4、NodeApps 29、App HTTP、rewrite loop、malformed-history compact、stream controls、runtime state/control/retry、process recovery、权限/审批、旧 store 与 context serialization。完整命令/分组见 results.json（原始证据本机留存）；日志（原始证据本机留存） SHA256 `cac8ce255b3b653eb3550b9000bc3f9e8b4bb2a7cca83e92741c36c02a11cbf5`；JUnit（原始证据本机留存） SHA256 `022d954d92ab2edfc610be15bae65d9bb10af75123c2d8626512ea0c35ce8dae`。
- **24 passed / 6.91 秒 / exit 0** 的新独立边界探针：逐字段篡改、命令/provider/preview 变更、orphan、真实 rejected 不复活、同收据无磁盘改写、异收据拒绝；provider 已推进到 revision 2 时仍只按原 revision 0 的 preview 找回 revision 1 原收据。恢复只发 receipt，不读 current context、不 invoke/resume。见 边界审计（原始证据本机留存）。
- 另一独立子审 **28 passed / 79.44 秒 / exit 0**，覆盖真实接线、压缩、控制及 App HTTP；与 141 项重叠，**不相加宣称覆盖量**。见 证据审计（原始证据本机留存）。

pytest 均用 `.venv/Scripts/python.exe`、关闭 cacheprovider、003 专属且适当短的 basetemp；没有覆盖 001/002 目录。原 240 项属于第二轮快照，不冒称本轮重新运行 240 项。

**真实任务与性能证据承接**

本轮直接审阅原始 result、harness、模型/工具交换、receipts、磁盘 fixture，而非只读总结。完整抽取与逐项 hash 在 audit.json（原始证据本机留存）。证据根是 `<user-home>/Documents/Codex/2026-10-18/new-chat-4/work/ryn-live-acceptance`。

| 固定同模型任务 | before → after 秒 | 模型调用 | 实际 host 工具 | 精确批准 |
| --- | ---: | ---: | ---: | ---: |
| GLM 查找 01/02 | 18.375 → 26.781 | 3 → 4 | 2 → 2 | 0 → 0 |
| GLM 查找成对复测 04 | 18.391 → 17.453 | 3 → 3 | 2 → 2 | 0 → 0 |
| GLM 修改和 shell 验证 01/02 | 22.672 → 26.219 | 4 → 4 | 3 → 4 | 2 → 2 |
| Gemini 错路径纠正 01/03 | 52.297 → 58.500 | 3 → 3 | 3 → 3 | 0 → 0 |

首轮查找多一次参数拒绝反馈与模型自纠，3 个 tool outputs 只有 2 次真实 host dispatch；编辑多读一次 verify.py。首次搜索约 +45.7%、编辑 +15.6%、Gemini +11.9% 的慢样本全部保留，搜索复测约 -5.1%。任务内人工纠错均 0；harness 仍经真实 approval API 作出受限批准，不能解释为无授权。

全部 12 次比较尝试 **8 pass / 4 fail** 原样保留；availability probes 不计 Agent 成功。第四类任务 Desk 为 3 model / 2 tool / 23.281 秒，唯一票号 `69f4844002894ae3ba06769d9e869d98`、标题 `RYN_LIVE_DESK_4b2a49d5aabb` 在应用 HTTP、confirmed receipt、模型答复中一致，settings/grants 对照未变。这里是 TestClient 真实路由/插件/存储及真实 GLM 推理，未声称安装版浏览器部署验收。

这些真实任务跨越既有修补快照，不能写成四任务都在本轮最终 hash 下重新推理。第二审全量 295 Python hash 与本轮相比恰好只有 node_apps 两文件不同，其余 **293 文件字节一致**；未改的正常推理路径由原实际样本、第二审关键回归和本轮复验承接，新错误/恢复分支另由上述实际提交故障探针承接，符合“不再收费推理”的限制。

**受核源码与归因边界**

本轮开始、各回归结束及最终取 hash 时 295 个生产 Python 文件均未变化。source-final.json（原始证据本机留存） SHA256 为 `7e72ad7d72116c84e39d8fe00cafb12452ad0a2818ddb45844dbcefacccf24a5`。完整源码集合指纹为 **`b8b267ccef9f16debecb5f0c71dc38ef0be0fc8e3bd21340a7d7c005e984830c`**，计算方法是对 source-final.sha256（原始证据本机留存） 的 UTF-8/LF 内容取 SHA256；内容逐行是按路径排序的 `path<TAB>file_sha256<LF>`，含全部 295 文件。本报告针对这份实际 dirty 工作树，不以 Git HEAD 代替受核 hash。

| 关键生产文件 | 最终 SHA256 |
| --- | --- |
| `rynmesh/node_apps/agent.py` | `d2b0da4f570d941a2befb2e989cb09f6163fb4fd0b56cb150acf3fd1982ba8be` |
| `rynmesh/node_apps/service.py` | `1081573ae91122537f95069c546b5f590c89835b8d435b6e2467b15d4bd1b105` |
| `rynmesh/agent_runtime/loop.py` | `3840654433ed1f8c83d91c21a737bcfc5463bb5d599520d195055f11fb0857e5` |
| `rynmesh/agent_runtime/runtime.py` | `bf999bcf4fc60795efca45e776297064f7ccd4aeeebfb3ce9953ce9afc063112` |
| `rynmesh/agent_runtime/checkpoint_refs.py` | `750dfdc2d3e5e884918e93ed01abd53279c60ef7eb75382bcc6582299ff15e92` |
| `rynmesh/agent_runtime/store.py` | `78ee529a79d20553485ef4b570d238baacec2d6f5b1cf7037a7653ee881c73d9` |

新增 `tests/test_agent_app_uncertain_response.py` SHA256 为 `a874b56872fb792e4717738e90703510a1f228fc2e84a7863eaad9ee059f9b3d`。与第二审差异清单在 source-comparison-002.json（原始证据本机留存）。dirty baseline 仍按既有 31 项快照和 `feature/ryn-integrated-workspace@0146c0255fb056dcbb1b4939e27cd81c05073ef7` 理解；Services UI/design-qa 的并行改动不归入本次 Agent 修补。

主审现场核对公开参考目录 HEAD 为 `8c076942cb70fba8e5c4732c23f1faabc4aed484`，读取历史正规化、工具错误、完整压缩等适用机制，并追查 Ryn 实际 prompt/context/router/turn/bridge/loop 接线。Ryn 的固定可信只读白名单、写等待整响应和原档加 checkpoint 是明确实现边界，不声称复制全部平台分支、namespace/CodeMode、远程压缩或闭源服务器。

三个固定 12KB 容量反例的 before 失败证据继续保留；本轮没有删 owner/权限/exact refs、提高 cap 或把这些案例记绿。有限窗口装不下固定信息不等于普通压缩/继续机制失效；也不能据本报告承诺无限上下文、全仓全绿或所有负载提速。

本轮未编辑生产源码、部署、改用户数据/模型设置/权限、重置清理 Git、读取用户旧对话或凭证明文；仅新增本轮报告、探针及隔离测试产物。后续生产源码变化需要新 hash 和按风险复验，不能由本报告自动追认。
