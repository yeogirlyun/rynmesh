# 参考运行时准备记录

状态：环境与接口核对中，尚无参考组效果分数。此项属于 T01/T16，不将源码下载或构建当成通过。

## Codex

2026-10-09 查阅 [官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)。该协议提供线程、轮次和事件；动态工具通过客户端调用请求交回集成方，相关字段属于实验接口。Ryn 现有 codex_session.py 已有该适配结构，但当前真实环境下的工具能力探测失败，因此不能作为已通过的参考执行器。

约束保持：模型提供端不得为了兼容而获得消费者的文件、Shell 或工具执行权。官方接口存在不等于当前二进制和账户组合实测可用。

补充本机 `codex-cli 0.162.0-alpha.2` 实测：导出的实验 schema 支持 DynamicToolSpec.deferLoading；保持无执行环境和私有临时 home，显式设置 deferLoading=false 后，GPT-6.1-Sol high 仍未发出 `item/tool/call`。模型的最终说明为 callback 返回 `code-mode host is disabled`，这只是模型报告，尚无二进制内部原因的独立证明。保留 `.codex-tmp/research/codex-dynamic-probe.json`；没有开启 code-mode host 或授予提供节点环境权限来强行通过。普通模型目录可读取，工具主控兼容仍未通过。

## ZCode

用于准备参考组的仓库是 zai-org/ZCode，固定提交 29628c9acdb81b703bbd4080c207a0e7ce5e276e（3.14.3），克隆于 .codex-tmp/research/zcode-runtime-reference。根 package.json 声明 Apache-2.0；保留上游 LICENSE/NOTICE。此前只阅读部分文件，本轮已取得该提交完整工作树，仍不代表已全部审计。

本轮补查 AgentRuntimeDeps、Model、ToolRegistry、ToolExecutor、SessionEventStorePort 及工具注册逻辑。接口允许注入模型、工具、存储；内置工具有配置白名单。准备仅注入相同 Ryn 工具与测试资料，关闭宿主执行、子 Agent、浏览器、工作流和额外模型调用，不给参考组额外资源权限。

参考适配不能仅靠提示词限制能力：注册表应只含获准工具，实际执行仍走相同 Ryn 业务网关；启动时检查暴露的工具集合。模型使用与候选组相同的 GLM 来源，先通过协议适配自检，再运行同题和同预算的真实任务。若集成不成立，标未验，不用其他模型或工具条件混算分数。

依赖已使用锁文件和 pnpm 10.33.2 完成安装，跳过安装脚本；工作区安装器同时解析了根开发依赖，未启动上游桌面或 CLI。系统 Node 为 22，核心自检及真实接线试验使用已存在的隔离 Node 24.19.0，与上游 mise 指定 24.14.0 的差异保留记录，不改变用户默认 Node。

## Claude Code

2026-10-09 补查 [官方运行机制](https://code.claude.com/docs/en/how-claude-code-works)：它说明了收集上下文、操作、核验的循环，以及运行中更正、本地会话恢复、上下文压缩和技能按需加载。另定位官方 [自定义工具](https://code.claude.com/docs/en/agent-sdk/custom-tools) 与 [权限接口](https://code.claude.com/docs/en/agent-sdk/permissions)，用于后续接口核对；未运行该 SDK，未形成真实对照成绩。

这部分采用机制参考，未把第三方泄露镜像认定为可维护的完整发行版源码，也没有把复制提示词当作完成 Agent 实现。

## 参考机制与 Ryn 落点

以下是设计映射，不是已实现能力表。各项通过仍以交付记录中的代码和真实证据为准。

| 机制 | 已查参考 | Ryn 落点 / 必须保留的边界 | 验收 |
| --- | --- | --- | --- |
| 模型—工具—结果循环 | Codex 客户端工具回调；ZCode 注入 Model/ToolExecutor；Claude 官方循环 | 消费节点 runtime/bridge，提供节点仅推理，工具准入由消费者决定 | T01 基线、Q04、P04 |
| 线程、轮次、事件与中途更正 | Codex thread/turn/item；Claude steer/resume；ZCode event store | T02/T03 的 goal/control/event 三种版本；更正立即废止旧排队动作，不能等模型自觉遵守 | R02/R04/R05 |
| 权限与实际副作用 | ZCode 工具注册/执行接口；Ryn 既有业务权限服务 | 所有运行时共用 Ryn 业务网关；绑定精确参数和最新权限；未知写入查询原回执 | T04/T05、R03、P01/P03 |
| 上下文与按需能力 | ZCode context/compaction；Claude 官方压缩/技能说明 | 权威目标、范围和未完成条件独立持久；摘要不能取得权限；大结果用受范围限制的引用 | T10/T11、F08/R06/P02 |
| 结果核验 | Claude gather/action/verify；ZCode completion verification 接口 | Ryn 自己的业务验证器与独立评分；模型停止或自称完成不能写 verified | T04、Q04/F02/C01 |
| 预算与可控停止 | Ryn 一次操作一 tick、持久计数；参考运行接口有 turn 上限 | T12 统一调用/工具/token/时间准入，未知价格标未知；同题参考组使用相同可比较预算 | R01/R06、T16 |

## 架构决策记录

Ryn 消费端仍以自己的目标、权限、业务回执和验收规则为权威。G1 三次真实业务闭环已通过，暴露的问题可在工具证据和有界推理恢复边界定位，因此第二批采用当前执行层增量完善 T02–T06；这不是已经证明优于参考框架。参考组的用途是比较相同业务工具下的任务完成效果，不能用成熟运行时的名字替代结果核验。若最终同题比较暴露当前循环的结构问题，再依据失败证据调整。

## 可执行参考适配进度

- 核心构建、公开包导入、空工具运行时及内存事件存储自检已通过，上游 tracked 源码与 lock 无差异。
- [ZCode 适配器](reference/zcode-adapter.mjs) 保留上游默认上下文组装和执行循环，只注入同样的 Ryn 模型/业务端口；注册后及每次模型调用前校验工具集合，未提供 shell、文件系统、浏览器、MCP、子 Agent 或工作流端口。出站模型参数仅转换公开类型；不支持的媒体和未知工具明确拒绝，未知 usage 不补零。
- 确定性协议自检完成实际两轮“模型→只读工具→回执→模型”；另验证精确审批允许时派发 1 次、拒绝时派发 0 次，见 [protocol-smoke.json](reference/protocol-smoke.json)。这三项使用合成响应，不计真实模型效果。
- [Python 管道宿主](reference/worker_host.py) 与 [Node worker](reference/zcode-worker.mjs) 已接通：没有监听网络端口，凭据留在后续 Python 业务网关内；校验固定上游提交和 tracked 差异，约束消息尺寸/模型请求数/运行时间并清理子进程。实际两轮跨进程确定性自检见 [pipe-smoke.json](reference/pipe-smoke.json)。
- 真实 SourceAdapter、精确审批和相同业务连接器的接线试验见下文。正式 collector 的 scope/出站准入、持久回执、重启/中断仍须接入；还没有 30 题成绩。不能把接线试验称作完整参考组通过。

## 同源真实接线试验

`reference/20261009T092254Z/run.json`：ZCode 核心以现有 GLM 5.3 Flash 来源运行与 G1 相同的模型分享/撤销任务，6 次真实模型请求、7 次业务工具调用，89.094 秒。两个精确批准、分享后读取、撤销后读取、兄弟模型保持私有、助手默认不变等客观条件通过，owner 来源文件未修改。参考模型收到自己的上游默认提示词及相同的 Ryn 业务工具，无额外终端/文件工具。

主协调者阅读报告：分享状态和自节点未进行 HTTP/LAN 探测的说明正确；默认模型结论与宿主实际状态一致，但模型引用的 available/agent_supported 字段并非默认选择的直接证据，保留为 T08 诊断字段改进项。本次业务源码由其他子任务继续修改，`code_unchanged=false`；这是实际接线可行性证据，不能用于最终同快照分数，更不能据一次任务宣称八成能力。

[参考工具宿主](reference/tool_host.py) 不接受缺失、错 call ID 或过期预览的写入批准；未知写入不重放，资料读结果不从重复操作缓存重发。5 项边界测试通过。这一接线宿主的批准/操作记录仍在内存，明确不支持作为 R02/R03/R06 恢复评分依据。

后续参考适配已接入同一提交的公开 `SqliteSessionStore` 与 `continueActiveTargetLoop`，默认目标核验开启，使用同一个用户目标并把核验、失败后的自动续跑计入统一预算。[目标流程自检](reference/goal-smoke.json) 覆盖直接通过与首次不通过后续跑，分别2/4次确定性模型调用，独立SQLite连接确认会话已持久化。这不是重启恢复或真实任务成绩，旧的无持久会话协议自检仍保留原分类。

上游该版本 `contracts/src/tools/target.ts` 对核验器坏JSON采用fail-open；保持其原行为，不修改参考源码。正式外部判分必须独立读取业务结果和审阅报告，不能把参考的`target_status=complete`当成功凭证。Ryn自身保持缺证据不通过的策略。

第二次真实接线`reference/20261009T095103Z`在第5次模型请求失败：分享、确认和撤销已有回执，但末次回读/总结及新接入的目标核验未完成；第5次请求120.875秒、错误类型AgentError，总215.985秒。此次只记录错误类型，时间接近超时上限不是独立错误码证据。owner来源未修改，业务源码开发中仍有变化；失败保留，不与先前成功混成完整参考成绩。
