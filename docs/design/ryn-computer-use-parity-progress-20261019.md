# Ryn Computer Use 实施与验收记录

2026-10-19，香港时间。应用与项目测试代码由本机 Antigravity 的 `gemini-4-argon-medium` 编写；Codex 设计、审查并独立执行检查。完整方案与能力矩阵仍是交付标准，本记录不是整条电脑控制链路通过声明。

## 已通过的分项

- 共享 MCP 首连与生命周期：独立 7 组受控 stdio 场景通过，包括同 key 竞争、任务隔离、取消、撤权及初始化期间 forget/close。健康会话不被落后候选关闭。
- 共享 MCP 诊断与防重放：独立 5 组通过；真实退出码 19 正确记录为 `agent_mcp_process_disconnected`，保留 dispatched/unknown，重复调用及重建 adapter 不重发写操作。所有受控 helper、reader、watcher 均回收，源文件哈希稳定。
- 相关项目测试：`test_agent_extensions_transport.py` 加新诊断回归共 **10 passed**。完整 runtime 与后续集成回归尚待视觉修复后执行。
- 状态页：新增 domain/panel 及原 AgentExtensions 三个测试文件共 **33 passed**，生产构建通过。后端只读状态接口正在开发，实际页面暂不作端到端通过声明。

证据：`work/computer-use-parity-20261019/mcp-*-acceptance-result-v2.json`、`mcp-review-round03.md`、`shared-tests-02.log`、`ui-tests-02.log`。失败的旧轮次记录均保留。

## 正在返修或未验收

- 原生 Rust 核心：native03 已通过离线 all-targets 编译和五项精确无 UI 测试，16 份源码哈希稳定。无界 stdin 读取已替换为有界 reader；新增 framing 测试因长度断言错误失败，native04 正在修复并补自有窗口身份与真实输入计数 fixture。尚未运行原生窗口/动作，完整能力缺口保持必交。
- JS/Skill 宿主：JS03 原六项真实 QuickJS 独立探针全部通过，包括 Proxy、图像来源、会话身份、帧上限及缓存退休。项目测试 2 passed/10 failed，均被 Windows Job 接口调用错误阻断；JS04 修启动与清理后再测审批挂起/恢复和生命周期。尚未向模型暴露。
- 模型视觉：vision03 独立八项检查通过，重复推理、事后异常、路由/负能力、准备误标提交及打开文件竞态已修复。项目测试 3 passed/7 failed，均为新 fixture 调用不存在的 issue_local，vision04 仅返修测试。另有嵌套推理异常未留 attempted/unknown 记录；512px 缩图、坐标变换和图像预算仍是必须解决的完整对齐缺口。
- 统一 Python NativeBridge 新包正在由独立 Gemini worker 实现；旧工具/MCP 委托、JS 接现有审批 loop、状态 API、打包、同场景 Codex 对照、真实模型像素验证和企业微信最终任务均未完成。

追加证据：`native-safe-02-result.json`、`native-safe-02-review.md`、`js-independent-probe-v2.json`、`js-independent-review-v2.txt`、`js-pytest-v2.log`、`vision-independent-review-v2.txt`、`vision-tests-02.log`。以上均位于同一 work 目录，旧失败记录未覆盖。

## 17:35 追加验收记录

- 现有任务 runtime、LocalTools、扩展 runtime、Codex usage 和输出预算回归 **160 passed**；旧 Windows 真实输入测试1项主动排除，待新原生输入防线修完再做受控窗口验收。测试日志 `runtime-regression-01.log`。
- JS04 修了 Windows Job 接口及清理；其开发 CLI 长时无进展后精确停止自有 host 与自动启动的 REA MCP child，源码哈希未变。当前冻结候选 **12 passed**，独立 raw 六项通过，审批同 Promise 恢复和 Job/线程回收实测通过；晚回调能倒退终态的问题仍需修复。尚未接入业务 loop。
- NativeBridge 新包首轮 **19 passed/2 failed**。独立探针复现 ready 后取消仍 commit、首次并发 worker 泄漏、正常事件累计上限误杀和 pipe 写入/关闭超时四项，Bridge02 正在修复。独立报告 `bridge-review-01.md`；不把假 subprocess 结果称真实 native 互通。
- 状态后端首轮 **4 passed/4 failed**，独立检查发现懒属性初始化、未绑定缓存 false ready、未关联图像套用当前模型等，状态02已重新派工；未挂载具备完整验证绑定的 native 前保持 unknown/unverified。
- UI04 六个文件 **115 passed/2 failed**；旧页面回归通过，新 wrapper 断言及同任务模型/权限切换仍显示旧状态的问题由 UI05 返修。
- C 盘满导致一次 npm ENOSPC（该次未运行产品测试）。仅将可再生成 npm `_cacache` 1583文件/289746475B 完整搬到 D 盘任务目录作为可恢复副本，文件数/字节数一致，C盘随后约700MB空闲；未动用户文档、账号/认证或日常节点。后续 npm 缓存和测试临时目录通过本次进程环境指向 D 盘，CLI 会话原目录未移动。

CLI 启动卡住的两轮续跑已核验精确 PID、创建时间、conversation、端口/子进程并停止，源文件17个哈希未变，再用 fresh Gemini4 会话完整交接 native04b / vision04b；完整 ID 和检查结果在 task.json。它们不是成功完成的轮次。

Antigravity 官方 headless CLI 当前只支持文字；维持视觉不支持提示，不通过改模型默认值、放开 provider 工具或复制凭据绕过限制。支持的 API/Codex CLI 路由也须分别通过真实图片验证。

用户日常节点 8791 未部署或重启。企业微信消息“测试电脑控制”尚未输入或发送；仍按既有授权在整体验收后仅发送一次。

完整会话、实际模型、进程/session、范围和验收状态见 `work/computer-use-parity-20261019/task.json`。

## 18:06 追加验收记录

- Vision04d 的 10 项图像路由项目测试已全部通过；原 8 组独立检查保持为分项证据。正在由 Gemini05 实现原图像素保真、图像预算、坐标凭据与嵌套异常 attempted 记录，尚无真实模型像素通过声明。
- Native04b 离线 all-targets 编译和 7 项精确无 UI 测试通过。随后三项自有窗口零输入测试各执行一次，均失败；只读项在任何原生观察前已计数 2，因此不能归因于原生输入。所有测试/fixture/隐藏 conhost 的 Job 已回收、源码稳定；截图尚未到达执行。Gemini04c 仅修 fixture 的初始化分项证据与严格阶段计数，不 reset、不减常数、不放宽键鼠/焦点断言，其余 4 项输入测试仍未运行。
- Bridge03 项目 **26 passed**，4 个先前 P1 缺陷在 Bridge02 已独立修复，当前正复验事件幂等及并发上限。真实 Rust 互通、生产二进制验证、旧工具/MCP/JS 委托尚待集成。
- JS05 项目 **14 passed/1 failed**；原 worker 退出后的 unknown 投影和终态回退有新回归，尚在独立验收。失败为新增未 admission callback 转换用例，需核对合法的取消/拒绝语义，不能仅为测试随意改变状态机。业务 loop 集成仍未实施。
- UI06 六文件 **121 passed/1 timed out**；新增修复与测试通过，既有停止按钮 5 秒超时保留原记录，未改超时值单独复验 **6 passed**。旧模型、旧任务版本及空版本状态的独立复验在进行；实际浏览器验收未完成。
- Status02 错误使用不存在的 crypto/source-store API，测试收集失败（0 项执行）；Status03 正依据真实存储 API 与冻结 numeric-v1 DTO 修复。未验收前不会把未经绑定的 native 缓存显示为 ready。

当前未向用户日常 8791 部署或重启，19055 保留原 home 与旧 unknown 操作，企业微信消息仍未输入或发送。完整能力矩阵、打包、同场景对照及真实应用结果仍是交付条件。

## 18:21 追加验收记录

- UI07 仅修新测试的结构类型，六文件 **122 passed**（并发 worker 数限制为 2，未放宽 5 秒测试超时）；TypeScript 检查与生产构建均通过。UI06 独立旧模型/旧版本/空版本/取消/任务切换及轮询边界全部通过，产品四文件哈希稳定。实际浏览器需等状态后端通过，尚未声明通过。
- JS05 核心独立 **13 组通过**，包括旧 raw6、生命周期5、fatal fence 与合法 queued cancellation。JS06/07 修复新测试把合法排队取消视为非法、以及误写 outcome 枚举两处问题，未改核心去迎合断言。最新项目测试 **16 passed**，后续业务 loop 仍需单独集成。
- Bridge03 已通过 26 项项目测试和所有旧 P1/事件幂等/并发限额/ready 身份边界探针。真实 Rust 二进制经 Python Bridge 仅执行 initialize/session.end/shutdown，**1.219 秒通过**，PID/创建时间/可执行文件核对一致、Job 回收无残留；未枚举业务窗口、采集或输入。Bridge04 正补发布清单、文件句柄/PE/进程身份和 owner/config/纯内存缓存绑定，真实握手通过不等于生产分发通过。
- Status03 独立原 5+5 组通过，但新增门槛拒收：无真实像素来源的 loose snapshot 能自称 verified；真实 verification.record 时间字段与读取端不符；两个不存在的 image helper 名称导致 503；部分 render identity 错配仍 recorded；目的地缺字段静态匹配风险待修崩溃后动态复验。Status04 已派 Gemini 修复，不能将旧 5+5 组当整模块通过。
- Native04c 只改自有 fixture 与其测试，保留启动原始计数、分阶段有界消息证据，删除 reset；生产 native 核心未动。已交回并确认 CLI 退出，正先编译/纯测试再重跑同三项零输入。其余正向焦点键鼠测试尚未执行。

## 18:50 断连恢复与追加验收

- 用户要求继续后，确认旧 unified exec 会话不可用、OS 中无 agy 进程。Vision05/native04d 有完整 final，其余四个 writer 无 final；保留全部部分源码和哈希，按精确 conversation 恢复，未重放任何真实动作。Status04 原会话报 executor `maxMessageTokens=0` 后退出 1，使用 fresh Gemini4 完整交接，未把错误退出算完成。
- 原生 04c 的 8 项纯测试、三项自有窗口零输入现已通过，24 个阶段快照物理/焦点/鼠标/键盘/滚轮/SetText 全为 0，原始初始化 2 条 EN_CHANGE 原样保留。截图 460×320/17533B，PNG 哈希与声明一致。四项正向用例在执行前静审发现全局 Alt 兜底，实际从未启动；native04d 移除兜底后因不存在的 enum/字段编译失败，04e 修实际契约后 all-targets check 已过，12 项纯测试进行中，正向输入仍未运行。
- Vision05 旧 10 项图像路由回归重验通过。新增 fidelity 测试因不存在类名收集失败，Vision06 仅修测试构造，已交回；待产品下一轮稳定后重跑。独立验收拒收 Vision05：真实 AgentModelBridge 仍被 chat/adapter/codec 的旧小图限制阻挡；公共 native 校验器给无封印字典自签授权；非原图 descriptor 的坐标与实际附图不一致。已分别派窄 scope 的 carrier 与 geometry 修复，不能以 Runtime fake-model 图像通过冒充真实适配器通过。
- JS Phase A 三个纯模块加测试已交回，项目 **8 passed**，独立加密事务/回执/结果投递/未知结果约束在验。仍为 **UNMOUNTED**，未注册容器、未接真实任务循环，也没有集成原 Promise 恢复声明。JS 图像核心与独立资源准备各由不重叠 writer 继续，未放宽全局 JS 帧上限。
- Bridge04 交回后测试收集失败：导出、binary 模块与新测试使用了不一致的接口。Bridge05 正修一致性；不通过新增假别名或放宽二进制验证来迎合测试。该失败也暂阻依赖 routes 的广泛回归，新图像普通路由 10 项可独立运行并已通过。
- Node22.23.3 官方 win-x64 输入包已下载到任务目录，archive 与官方 SHA 一致、node.exe 签名有效，保留 LICENSE/README/来源记录；系统 Node/PATH 未变、该新二进制尚未执行。不是独立安装通过证据。
- 断连后浏览器原 tab 已不存在；重开 19055 返回 connection refused。只读核对该验收服务无进程/监听，旧 encrypted ledger SHA 仍 `0e3280e7ca95f790421b128d1c372ffa8ae879a509f681b32fc3e7b3657e6b39`、2523B 未变。待导入修复后用同一 home 恢复，保留未知操作，不新建空目录掩盖历史；没有操作用户日常 8791。

## 19:18 继续验收记录

- 仍由本机 Antigravity `gemini-4-argon-medium` 编写产品和项目测试，Codex 只写设计、独立探针与过程记录。各轮 final、CLI exit 和实际测试分开记录；没有提交、部署或操作日常 8791。
- Native04e 的 all-targets 与 12 项精确纯测试通过。四项正向 owned 测试按失败即停执行：中文 emoji 输入、焦点及按钮点击通过；第二项滚轮 canonical 计数失败，raw trace 证明 child 默认传播到 main 后 fixture 双记账；后两项未执行，0 遗留。04f 只修 fixture 后静审又发现测试复制计量实现、以及转发匹配并非限定栈顶，04g 正修真实 fixture 单测；没有通过改成预期 2、减常数或改生产滚动量放行。
- Bridge06 两套项目 **38 passed**。独立 PE/句柄 pin/实际进程身份/只读缓存/未知操作防重放边界通过，但新 barrier 探针确认重配置时连接 leader 所有权会卡死，live standalone owner 绑定还会留下旧缓存 owner，Bridge07 正修。显式任意清单仍可能错误标 `release_verified` 的分类缺口保留给下一轮包内权威修复，尚无可信发布包声明。
- Status04b/05 产品投影分项验收通过：项目 **7 passed**，旧独立 **11 组**及真实 SourceStore、PNG、verification.record、请求 linkage 的正负例 **26/26**。项目原失败为 fixture 错把整源修订号当单模型修订号，Gemini 只修测试；独立初次对 payload 阶段的过度断言也保留并纠正。GET 不接受 loose hook 自称像素验证，未接实际 render producer 时 displayed 为 unknown。
- JS PhaseA02 修复原四个漏洞后项目 **7 passed/5 failed**，独立原 A1–A8 部分退化、原 X1–X4 已拒绝。新校验误要求真实 verification receipt 中并不存在的 call_id，正在定位并准备窄修；依然 **UNMOUNTED**，不把纯模块测试作为真实任务/原 Promise 恢复通过。
- JS 图片核心旧 **16 passed**；下层真实 QuickJS 已将 **8,359,932B** 原 PNG 精确交给捕获回调，10 帧最大 962B。实际 coordinator 错参、完整 NativeWindowRef 丢失和退休 token 元数据无界均独立确认，JS03 正修；回调传输不是实际模型 submitted 或 pixel-verified 证据。
- Geometry 独立 **6/9 passed**，含真实 crop/tile 像素、5,768,591B 原 PNG 保真及有界读取/句柄关闭。仍有 original payload 自报摘要可替换、legacy WindowRef 漏 windowKey、loader 自补预期 observation 三项真实缺陷，Geometry03 正修。新测试两处错误的拒绝码/可压缩 PNG fixture 由 Gemini02 单独修正，原失败保留。
- 离线 JS 资源准备/解析的项目 **9 passed**，仍未挂载 manager/安装包。原输出路径把不存在 leaf 当 reparse、Windows 规范化吞尾点两项已修；未执行新 Node、未生成实际分发 profile。原图 actual-adapter carrier writer 尚未交回，独立真实 adapter 末端捕获 harness 已准备，等待停笔后验证。
- 19055 尚未恢复；保留同 home、旧 unknown 操作与 encrypted ledger。企业微信消息仍未输入或发送。完整 native 生命周期/共享 epoch、WGC/launch/drag 等能力、真实任务循环、包分发、模型像素验证及冻结对照矩阵仍是交付条件。

## 20:03 继续验收与接线

- 19055 已用原 home 恢复。启动前校验 Agent/desktop 均关闭、model_id 为空、updater 无待确认更新；旧 unknown 操作 attempts=1，ledger 2523B SHA 保持 `0e3280e7ca95f790421b128d1c372ffa8ae879a509f681b32fc3e7b3657e6b39`，没有重放。独立浏览器已核插件/MCP/8项技能、导入入口、全局与任务绑定状态区；390px 窄屏无横向溢出，临时视口已恢复。新发现历史 unknown flight 被误报 busy、旧 MCP session 字段形状不符，Status06 另行修复，不将浏览器验收记作全绿。
- Bridge07 的两套项目 **41 passed**，原连接 barrier 和新增 **6 组** owner/重配置/防重放检查通过，13 个自有 fake worker 全清理；发布权威仍由 R1 窄修，尚无已安装发布包证据。
- Geometry03 项目 **5 passed**、独立 **12/12**；原始 payload 摘要、windowKey 和显式 observation 绑定三缺口修复。真实 NativeBridge 截图 producer 尚需 H1 接线，不能只因私有 seal helper 通过便称已生产集成。
- Carrier03 独立 **6/6 组**通过：实际 Bridge/normalize/三协议 plain+stream HTTP 及实际 CLI/Codex pool 首次/续接保留 **8,359,932B** PNG；13 项 typed identity 元数据及旧 proof 拷贝共 **26** 个篡改拒绝。Gemini04 仅修 `.code` 测试夹具后项目 **28 passed**。没有真实模型像素识别或计费精确声明。
- Fidelity09 仅修新测试的真实 producer 契约，项目 **5 passed**：保真 marker、不可压缩 PNG、有效手动限额、private native seal/geometry、自动图片预算与 attempted/unknown 记录。辅助 reference documents 使用现成 `_context_limit(current_task)`，4 项原 reference output policy 测试通过（含 600→180、30→30 原超时上限）。该辅助任务首次 CLI 序列化错误无改动，续轮聚合 final 仍 ERROR 但实际 exit0、两处修改经独立核对及测试；保留原错误，不写为 Gemini SUCCESS。
- JS imagecore03 旧 **16 passed**、新 **7 failed**；独立正确 int Sky ID 仍在真实 coordinator 入口遇错参，且代码错误比较 int Sky ID 与 string native windowKey。JS04 窄修中；不绕开 wrapper 把下层结果当整链通过。
- PhaseA03 项目 **10/13**、原 A/X **12/12**、加强 S **7/7**；新增同步 queue/flight/completion 参数替换仍可写错误真实 receipt。A04 增加独立 admission 语义承诺，并修三处明确夹具/parent contract 问题；仍未挂载 loop。
- N05A work-only std 模型原项目 **14 passed**，实际 path dependency 独立 **8 passed/2 failed**：nonce 历史无界、取消后 Clone 票可复活；02 正修。不是实际 WinEvent、命名 mutex/mapping 或两进程证明，不原样 promote。
- Native04h 的 16 项纯测试通过；自有正向第一项 fixture_ready 检出额外 mousemove，尚未调用 engine，失败即停，其余三项未运行。04i 的初始放置修改已退出，但聚合 final ERROR；静审发现 ready bounds 回退旧值与矩形边界缺口，04j 仅修 fixture/owned validation，尚未授权新一轮 UI 输入。
- H1 已派唯一 Gemini writer 接共享 native facade、真实 builtin tuple、ready→既有 ledger→commit 和私有原 PNG producer；与后续 PhaseB/H2 在 runtime/tools 等共享文件串行。R1、JS04、A04、N05、native fixture 均各自排他。日常 8791、其他业务与默认策略未改。企业微信消息仍未输入或发送；实际识图路由选择待用户回答，独立开发验收继续。


## 20:43 continuation checkpoint

Gemini4 remains the only application/project-test writer. Native04k passed 17 exact pure tests and all four root-authorized owned-window positive tests; all owned processes were cleaned. Chinese text, click, scroll and duplicate success were verified. Child-geometry stale rejection can occur after focus and is not a zero-mutation claim. No WeCom message has been typed or sent.

R1 authority passed 44 project and 12 independent groups; Phase A passed 14 project and 22 independent groups but is unmounted; Status07 passed 7 project, 7 actual-session, 37 prior independent groups. N05A pure staging passed 16 project, 13 independent and two expected noClone compile rejects. N05B actual WinEvent integration and H1 host facade are active unique writers. R2 packaging exited 0 with aggregate ERROR and a complete response, kept as ERROR; static independent review is running. JS06 exited 0/SUCCESS and is under independent capability-key redaction review. M1 resource mount and H2 actual loop have not started.

19055 remains on the same acceptance home with old in-memory status code, desktop and agent disabled, model unset, historical unknown attempt=1 retained. Daily8791 is untouched. Actual model-image route answer, real image canary, full Codex parity matrix, remaining native capabilities, actual frozen packaging acceptance and final Ryn WeCom action remain pending.

## 后续接续记录：JS07 / H1 / N05B / R2

JS07 仅修正新测试的配额图片数据，旧 16 项与图片 7 项共 **23 passed / 16.21s**，源码哈希稳定。JS06 独立真实 coordinator 已验证字典键/值、嵌套、错误与 stdout 脱敏、碰撞保值、原截图能力和同轮跨 cell 保留；M1 已交 Gemini4 实施随包资源挂载和完整 Node/WASM 文件 pin，实际打包承诺尚未提供。

H1 作者返回 SUCCESS、实际 exit0，仍未通过验收。三个新项目测试在不完整 task row 缺少 messages 时失败。独立探针证实 reserved builtin 可因注入 manager 的形状回落通用 MCP，以及不同 owner 复用旧 facade、bridge 和任务状态。真实 NativeWorkerClient/NativeBridge/Adapter/LocalTools/OperationLedger 的自有 JSONL 进程探针中，已验证正常提交、confirmed 零重放、unknown 不重放及多项 ready 拒绝；也发现 MCP 入口缺 task 状态复核、两入口缺控制版本复核。该探针未挂实际 AgentLoop 的取消闭包，不称端到端 Loop 取消失效。PNG/几何/文件路径与缓存退休复核仍在进行，修复待合并后串行下发。

N05B 作者返回 SUCCESS、实际 exit0，已冻结等待独立静态、编译和纯测试；真实 WinEvent 与任何新窗口输入尚未授权执行。N05C 共享 mapping 仅完成设计，D 逐输入批次复核及其他能力仍待实施。

R2 初稿保留 aggregate ERROR / actual exit0。独立复核发现五项阻断：manifest 变量拼错、adapter 源路径错误、shutdown 返回契约错误、捕获自身 SystemExit、用工作 TOC 冒充最终 peer archive 验证。纯测试 5 passed / 1 failed，Gemini4 正按五项修复，真实 frozen/NSIS 打包与安装内容验收均未运行。

所有产品及项目测试仍由 Gemini4 修改，Codex 只设计、复核、写工作探针并执行验收。日常 8791 未操作，企业微信未输入、未发送。


## 21:25 scoped checkpoint

N05B03 passed all-targets compilation and 33 exact noninteractive tests; five separately authorized injected-adapter tests also passed (38 total). Sources24 remained unchanged throughout and every own Job was cleaned. Actual WinEvent/GUI/physical input remains untested; B04 is writing one strengthened ignored real-hook candidate only, with later root approval required for execution.

R2-03 passed all8 pure tests in1.10s and PowerShell syntax parsing with0errors. Five scoped files were stable and all execution/network/desktop tripwires stayed0. This accepts pure packaging contracts only: actual release build, frozen startup, NSIS/extraction and installed bytes are UNRUN. Isolated source snapshot preflight is being prepared to preserve daily8791 and original repo dist/binaries.

M1-01 safe subset13 passed, while two unsafe fakeHANDLE tests and one realNode test were excluded. Independent memory-only probes confirmed early lease release on unconfirmed exit, sticky live flags and custom source admission gaps; directory namespace lacks persistent pins. M1-02 is repairing those exact contracts. Original JS23 are a pre-M1 baseline, not current-manager regression evidence. H1-02 remains the sole host writer. No WeCom text or message, no user application input and no daily8791 operation.


## Reconnect checkpoint: Codex image route selected

The human selected Codex for final image-model acceptance. This resolves the route choice, while dedicated19055 task configuration and actual pixel canary are still pending. Global defaults, daily8791 and existing unknown-operation ledger remain unchanged.

M1-02 exited0/SUCCESS but is not accepted: custom production hooks still admit an ordinary alternate resource path, and manager/session quarantine uses opposite lock order. A new fixture lacks monkeypatch; a directory rename test is excluded under the existing review boundary. A45s safe-group watchdog is inconclusive, while the isolated pre-resume case subsequently passed with actual exit0. No approved newNode startup or old23 regression has run. B04 exited0/SUCCESS but static review found unconditional diagnostic history can overflow ordinary production after2048 callbacks. Its feature compile and realhook case have not run. Gemini will repair these candidates serially.

Packaging preparation remains work-only:299npm packages downloaded with scripts disabled; Python wheels are downloading, tested41pin constraints are prepared; official full7z MSI readonly extraction is in progress. No release package, NSIS installer or daily node has been executed or deployed.


## Scope correction requested by the user

Root acknowledged excessive expansion and adopted work/computer-use-parity-20261019/convergence-decision01.md. Packaging, complete capability/fault/race matrices and further C/D/H2 work are deferred. The immediate deliverable is Ryn builtin MCP control with the user-selected Codex image route and an actual task. H1-02 root wire02 now passes all11 scenarios and owner/routing03 passes with0spawn/0connect; project fixture APIs still need correction. No actual model image or user desktop operation is accepted yet. Current19055 catalog has only text-only Antigravity, so the next environment action is to expose existing Codex in that isolated node without changing daily8791.
