# Ryn 开发分支完整交付说明

交付日期：2026 年 10 月 15 日。目标仓库：`yeogirlyun/rynmesh`。目标分支：`feature/ryn-integrated-workspace`。这是开发分支交付，不是合入 `main` 或发布新版本。

本分支把手动 AI 工作台、可连续执行的 Agent、统一模型池、云盘和 NAS、设备文件与消息、插件管理及 Windows 编辑器入口连接起来。用户可以选择模型和项目、提供资料、让助手使用本机工具完成任务、检查文件产物，并通过已有设备或好友权限使用共享模型。本次交付既包含 10 月 7 日起的基础工作，也包含 10 月 13 至 15 日的后续实现与修复，不能只按最后一个保存提交理解范围。

## 范围和代码量

统计基线为老板仓库 `main` 的 `e9cde6c8ce097efe02376692923850b44815fc79`，功能快照为 `63f9b97`。两者是祖先关系。本次说明及清单在后续文档提交中补充，不改写既有功能提交。

| 类别 | 涉及文件 | 新增文本行 | 删除文本行 | 说明 |
| --- | ---: | ---: | ---: | --- |
| Python 功能代码 | 127 | 26,365 | 584 | Agent、模型适配、网盘、Drop、目录和插件等 |
| 前端界面与领域模型 | 112 | 11,765 | 728 | 工作台、Agent、模型池、设备、插件与多语言 |
| 桌面原生入口和构建 | 9 | 422 | 7 | Tauri、编辑窗口、文件选择与 Windows 安装器 |
| 可选原生 P2P 传输 | 8 | 2,296 | 0 | Rust crate、Python 绑定和依赖锁定文件 |
| 后端测试与测试资源 | 189 | 34,525 | 19 | 用例、固定协议、恢复和权限边界 |
| 前端测试 | 43 | 5,425 | 55 | 页面交互、缓存、文件产物及语言检查 |
| 验收资料、记录和夹具 | 1,730 | 1,621,052 | 0 | 包括 255 个二进制文件；大量行数来自结构化试验记录 |
| 文档、设计和现有清单 | 70 | 12,716 | 0 | 包括 20 个二进制设计资源 |
| 其他配置、脚本和规划资源 | 28 | 1,465 | 50 | 包括 19 个二进制文件 |

上述合计 2,316 个文件，为补充本交付说明之前的快照。文本行数包含锁定文件、语言资源、协议夹具和日志，不表示等量手写业务代码。新增文件、修改文件与逐文件用途见 [完整分支文件清单](2026-10-15-upstream-files.tsv)，提交顺序见 [提交清单](2026-10-15-upstream-commits.tsv)。

## 一、统一 AI 工作台与资料历史

原先 Ask Ryn 与私人 AI 聊天的入口和历史分散。本分支统一手动 AI 工作台及路由，保留指定节点、服务、资料及旧历史链接的上下文。节点加密会话与原浏览器会话可以一起查看，并标明保存位置；模型来源不可用时仍可查看已有记录。

“添加资料”支持已授权 NAS 文本和收藏内容，添加资料与实际发送分开。发送前检查接收模型和资料范围；回答保留所用资料引用，能展开查看片段。回答保存回 NAS 时仍绑定该回答产生时的目录，后来切换附件不会把旧回答写到其他目录。刷新及节点重启后可以恢复节点侧任务、消息与来源引用。

主要代码：`rynmesh/ask_ryn/{context,routes,runs,store}.py`、`webapp/src/components/{AIWorkspaceMaterials,UnifiedAIHistory,AskMaterials}.tsx`、`webapp/src/screens/{AIWorkspace,PrivateAIChat,AskRynConversation}.tsx`。

证据：[统一工作台验收](../acceptance/unified-ai-workspace-20261007/README.md)、[历史测试说明](../RYN_INTEGRATED_WORKSPACE_TEST_REPORT.md)。

## 二、Agent 执行循环、任务生命周期和权限

新增完整 `rynmesh/agent_runtime` 模块。模型提出工具调用，宿主验证并执行，工具结果回填同一对话，模型继续工作或直接回答。任务支持追加要求、暂停、继续、取消、预算调整、持久化历史和重启恢复。晚到的模型回复不能覆盖取消或用户的新要求。

10 月 15 日将对话投影、模型响应、工具队列、上下文压缩与内部控制集中到 `AgentLoop`；宿主控制器继续负责加密存储、派发前意图、预算结算、模型和目标绑定以及不确定操作恢复。普通结束不再默认触发独立角色复审或语义修补循环；需要独立文字核验时由明确设置开启，历史核验测试也显式请求该模式。

权限模式区分：默认 `workspace` 允许在已选工作范围内连续执行本机任务；`read_only` 拒绝修改；`confirm_changes` 对修改展示精确预览并审批；`automatic` 使用所有者选择的工具授权。任务权限快照与当前宿主权限同时约束操作。外部配置、访问授权等变更仍有相应审批边界，已有待确认修改不会自动获得批准。

字面只读命令识别减少普通搜索、查看文件的多余审批，但拒绝命令拼接、替换、重定向和可执行搜索选项。操作效果、是否需要确认、崩溃恢复记账分别处理。不确定是否执行成功的修改不自动重放。任意 shell 最终以本机用户身份执行；这一权限体系不构成 OS 沙箱。

主要代码：`loop.py`、`runtime.py`、`execution_policy.py`、`protocol.py`、`control.py`、`state.py`、`lifecycle.py`、`operation_permissions.py`、`operation_ledger.py`、`budget.py`、`verification.py`。

证据：[当前循环与权限契约](../design/ryn-agent-loop-v4.md)、[v4 构建与验收](../acceptance/agent-loop-v4-20261015/README.md)。历史固定任务集、早期失败与正式评测缺口仍保留在 [原交付记录](../acceptance/ryn-agent-delivery/README.md)，不以新循环实现追认旧失败为通过。

## 三、本机工具、项目接入和实际工作范围

增加文件列表、分页读取、搜索、写入、编辑、移动及补丁工具；提供进程启动、输出读取、取消和命令运行；增加浏览器页面/元素分页、同源框架范围、Windows 桌面读取和操作、图像/页面渲染与检查能力。图片输入按提供方能力检查，不能支持的 CLI 输入明确拒绝。

项目由所有者在界面选择并登记，不能因为模型提出某个绝对路径就获得权限。项目说明可发现 `AGENTS.md`、`README.md`、`AI_HANDOFF.md`、`PROJECT_MAP.md`，并提供结构和测试入口。任务绑定项目、授权代际及模型目标；默认模型切换或 CLI 配置变化会触发绑定校验。普通任务提供默认文件目录，已有任务也可通过明确入口绑定目录并续做。

多文件补丁支持添加、上下文更新、移动和删除，先解析全部 hunks，再执行；保留 CRLF、BOM 和文件结尾约定。命令运行前后发现的文件变化只作为观察记录，不能据此断言文件一定由该命令创建。

项目实测记录包含：真实模型通过实际项目工具发现规约，执行指定的隔离 Jest 入口，1 个测试套件、7 项通过。初次使用根 Jest 配置导致 Node 堆内存耗尽的失败也保留；隔离测试通过不等于该项目类型检查、全部集成或生产行为通过。

主要代码：`projects.py`、`local_tools.py`、`local_patch.py`、`local_processes.py`、`local_browser.py`、`local_desktop.py`、`local_render.py`、`local_specs.py`。

证据：[项目接入与真实任务记录](../acceptance/agent-project-20261014/acceptance.json)、[实机可靠性问题与修复](../acceptance/agent-peer-cli-20261014/RELIABILITY.md)。

## 四、文件交付和 Agent 界面

Agent 页面加入项目选择器、输入组件、模型选择器、推理强度、执行进度、预算和产物展示；侧栏和任务栏可以分别折叠。处理发送状态及缓存恢复，避免等待期间误报发送成功或用过期任务覆盖当前任务。

工作区产物绑定真实文件引用、路径、版本和内容哈希；回答内匹配到的文件链接可以进入产物卡片及预览。支持文本、图片及适用文档的展示，提供原生打开和定位入口；路径歧义、权限撤销和文件版本变化会重新检查。手机预览和底部菜单也有专门布局。UI 合成文件演示与真实系统默认应用启动分开记录。

主要代码：`workspace_artifacts.py`、`artifacts.py`、`results.py`、`RynAgent.tsx`、`RynAgentComposer.tsx`、`RynAgentProject.tsx`、`RynAgentModelPicker.tsx`、`RynAgentFileContext.tsx`、`RynAgentArtifacts.tsx`、`agentWorkspaceCache.ts`。

证据：[文件交付检查与截图说明](../acceptance/agent-file-delivery-20261014/verification.json)。本次整理修复项目路径输入框硬编码文案，并复验语言检查及项目交互测试。

## 五、性能、上下文和存储

核心文件/命令工具在容量允许时持续暴露，动态加载工具采用增量集合，避免每轮重新发现或覆盖已有工具。最多 8 个连续只读本机调用可并行处理；工作器可连续推进同一任务最多 20 秒，再让出执行机会，减少每一步固定调度间隔。

上下文上限提高到 600,000 字符，但继续受具体模型声明窗口约束；未知窗口保留 120,000。旧默认值有一次性迁移，显式配置继续保留。大命令输出提供首尾摘要和中间分页，避免仅保留开头丢失错误信息。

原单体加密会话存储改为小型加密根索引与按任务分开的加密内容文件，根索引提交并校验内容 SHA-256。只重写变化任务，再提交根索引；读取缓存、摘要和无变化 UI 轮询减少全量拷贝与下载。旧格式首写迁移；旧版程序遇到新版格式会拒绝读取，不能误读为空并覆盖。

历史 VM 合成测量中，约 6.8–9 MB 状态下单事件事务由 414–419 ms 降至 56–60 ms。这是存储微基准，不等于真实模型端到端速度提高相同比例，也不作为本轮重新测得的数据。

证据：[逐项优化、测量条件与降级说明](../acceptance/agent-speedups-20261015/README.md)。

## 六、统一模型池、来源配置与分享

模型池按来源下的具体模型统一展示 API、本地服务、CLI、受管 GGUF 与授权远端模型，区分普通聊天能力、客户端工具能力、探测时间和当前可用性。默认 Agent 模型、模型分享与 Agent 开关分别控制。

逐模型支持上下文/输出上限、并发、每日请求额度、来源默认值继承及具体参数覆盖，兄弟模型配置保持独立。模型连通、目录发现、客户端工具探测分别记录，变更配置后按影响重新核对；来源凭据通过独立入口加密保存，不放入普通模型对话。

来源草稿可探测后确认保存；Agent 可发现并接入已安装 CLI、选择默认模型、准备精确模型分享。定向好友分享将模型与接收者绑定，撤权按当前授权版本生效；新模型保持私有。不把来源能力自报直接当作真实工具探测成功。

主要代码：`agent_runtime/{models,default_model,model_switch,source_drafts,credential_control,sharing}.py`、`llm_package/{model_settings,model_dispatch,native_policy,tool_capabilities,space_catalog}.py`、`ModelPool.tsx`、`ModelPoolSettings.tsx`、`AISources.tsx`。

证据：[完整产品和参数说明](../RYN_INTEGRATED_WORKSPACE_PRODUCT.md)、[模型与授权测试](../RYN_INTEGRATED_WORKSPACE_TEST_REPORT.md)。这不包含其他分支尚未整合的“好友 AI 邀请续接”实现。

## 七、Codex 和 Antigravity 适配与可靠性

Codex 工具循环通过受约束的提议协议把工具请求交给消费端执行，提供端不因此获得项目执行能力。CLI 模型目录、推理强度、可执行文件及配置/上游模型绑定可核验；配置变化后阻止旧绑定继续派发。

增加会话复用：线程按命令、模型、推理强度、经认证调用方和窗口隔离，初次播种完整记录，后续发送必要增量；模型、策略、上下文纪元或历史不再兼容时重建。当前 `runtime.py` 已向支持会话键的模型桥接器传入任务 ID，跨轮接线已存在；早期会话复用 README 中“runtime 待接线”的记录属于当时状态。

会话默认最多保留 4 个、闲置 30 分钟回收，可用开关关闭复用。取消、超时、无效提议、执行边界请求和进程异常会关闭对应会话。只有满足条件的断连允许一次完整历史回退；已流式输出或已取消时不重试，避免重复表现。

修复长回复增量事件挤满队列、扫描时临时目录消失、Windows 清理文件占用覆盖正常结果等问题。流文本合并唤醒，保留执行边界及必要请求/响应。诊断仅记录异常类型和代码位置，不携带异常文本、局部变量或凭据。Antigravity 未知 usage 在结算中保持未知，不伪造零用量。

主要代码：`codex_session.py`、`codex_threads.py`、`codex_tools.py`、`cli_adapter.py`、`cli_models.py`、`cli_binding.py`、`antigravity_session.py`、`process_guard.py`。

证据：[会话复用及隔离测试](../acceptance/codex-session-reuse-20261015/README.md)、[实机故障与修复](../acceptance/agent-peer-cli-20261014/RELIABILITY.md)。真实 Codex 单步延迟与闲置进程 RSS 没有在该 VM 历史测量中完成，不据模拟用例宣称真实效果。

## 八、云盘、NAS 和跨设备 AI

接入百度网盘、Google Drive 和 OneDrive 的授权、目录、新建文件夹、上传、下载及令牌刷新。读写和 AI 权限分开配置；上传保留同名文件，限制单文件大小。目录使用加密缓存，重进先展示已有内容，再按时效后台刷新；保留位置、分页与路径，迟到请求不能覆盖新目录，移除或重新授权清理相关缓存。

NAS 支持多 SMB/WebDAV 连接、路径导航、排序和当前目录搜索，文本/图片侧栏预览、上传下载及新建目录；与聊天资料和 Agent 资源工具接通。移除连接不删除远端文件。后续将 NAS 纳入统一插件生命周期，同时保留连接权限。

跨设备模型调用优先新鲜 LAN 地址，再按现有连接策略使用发布地址或 ICE/UDP，保留会话复用、流式、取消、背压和授权检查。提供端执行推理，消费端管理工具与对话；普通设备名称/租约变化与授权身份变化分开，避免正常刷新误伤已绑定模型。

主要代码：`cloud_drives.py`、`drive_cache.py`、`nas.py`、`personal_space.py`、`llm_package/{peer_binding,p2p,p2p_session,consumer_cancellation}.py`、`webapp/src/personal/{Drives,NasPreview}.tsx`。

历史证据：[三家云盘真实账号及缓存记录](../acceptance/integrated-workspace-20261012/cloud-drives-passed.json)、[两机 AI 和取消记录](../acceptance/integrated-workspace-20261012/two-host-passed.json)。历史真实调用保留成功和 unverified 状态，未据此声称所有模型质量通过。

## 九、设备文件夹传输、消息与统一目录

新增 Device Drop：以设备签名身份寻址，消息采用签名信封，文件通过 X25519/HKDF/ChaCha20-Poly1305 分帧加密；离线队列保留在发送设备。实现文件/文件夹预检、发送、接收确认、进度、暂停、恢复、取消、重试、会话已读、自动接收偏好及 SSE 状态更新。浏览器走流式上传，桌面可选择本机文件和目录。

直连传输不静默转为中继；中继能力未提供时如实显示不可用。大整数使用安全数字或十进制字符串表示。UI 包含设备筛选、搜索、传输列表、消息与文件会话、上下文菜单、深浅色和手机布局。

设备与 AI 服务改用统一目录快照，合并设备记录和服务广告中的提供者，统一新鲜度策略、关系、共享在线观察与可用性判断，避免出现设备路由未确认而其 AI 已显示 Ready 的冲突。广告和缓存不能自行提升设备关系或分享权限。

历史双物理机 LAN 记录：正向 134,217,759 字节文件接收端落盘哈希一致；反向 3 个文件、共 16,777,302 字节哈希一致；双向消息及重启后传输和会话状态恢复。该记录明确没有验证公网链路或 100 GB 传输。

主要代码：`rynmesh/drop/`、`peer_directory.py`、`llm_package/service_view.py`、`services/peer_health.py`、`peer_http.py`、`webapp/src/personal/drop/`、`dropStore.tsx`、`domain/{directory,dropClient,dropDesktop}.ts`、Tauri `drop.rs`。

证据：[接口和加密契约](../DEVICE_DROP.md)、[双机 LAN 数据与边界](../acceptance/device-drop-lan-20261013/result.json)、[重启回读](../acceptance/device-drop-lan-20261013/final-restart-verification.json)。

## 十、可选 Rust 原生 P2P

增加 `native/ryn_transport`：Rust 处理 UDP socket、并行 STUN、带认证的打洞、QUIC 握手、流和拥塞控制，Python 保留信令、信任判断、业务协议及上层状态。证书指纹通过既有签名信令协商；与旧端协商失败或缺少扩展时保留 aioice/aioquic 路径。特定严格验收模式和固定端口设置继续按原路径处理。

历史 VM 同机测量包含原生 Python 扩展 loopback 约 133 MiB/s、双进程 Device Drop 约 35–46 MiB/s；这些是同机磁盘与网络条件下的记录，不是本次实测，也不是跨公网吞吐承诺。仍按可选原型交付，不移除兼容回退。

证据：[协商、性能条件与部署阶段](../NATIVE_P2P_TRANSPORT.md)。

## 十一、统一插件、业务应用和独立编辑窗口

前序提交将 Resume、Recruit、Desk 的私有节点托管、安装启动与好友访问接入已有审阅流程，保持业务操作回执与数据边界。随后引入固定版本及 SHA-256 的 Craft 目录、程序包下载解压校验、原子发布、管理状态、活动调用租约、动态 Agent 能力和卸载保留数据。

应用页和 `/settings/plugins` 统一办公、设计、业务及存储工具，提供安装选择、发现库、搜索分类、启停、详情、限制提示和卸载。NAS 与内置业务应用进入同一管理体系，兼容原入口与授权。插件启用后暴露已实现的 Agent 能力，停用后拒绝旧调用；文档编辑器目前的能力查询不能冒充 PDF 合并或 Word/Excel/PPT 自动编辑。

Windows 安装器默认勾选 PDF、Word、Excel、PPT，可取消，仅记录偏好；用户确认后才下载固定包。升级保留已有选择，静默安装不触发下载。编辑器 ZIP/WASM 不塞入主安装包，不自动监听或启动编辑器。

桌面 Craft 入口改为 Ryn 独立 WebView2 编辑窗口。按节点和插件隔离源站与本机存储；再次打开聚焦已有窗口。只接受固定插件 ID，核对安装、启用与启动 ticket，编辑器无 Tauri 能力，导航限于自身源站。保存和导入使用系统文件对话框，关闭编辑器不退出主应用。

历史本机记录已安装/启用 16 项；实际打开 PDF、Word、Excel、PPT 原生窗口；PDF 新建空白页、另存为、解析和重新打开通过，其他编辑器使用同一入口但未逐一完成文件往返。Word 中文字体覆盖、PhotoCraft 字体清单等限制继续明确提示。

主要代码：`node_apps/{builtin_plugins,plugins,plugin_agent,craft_host,service,routes}.py`、`CraftApps.tsx`、`CraftPlugins.tsx`、`CraftPluginLibrary.tsx`、`CraftPluginIcon.tsx`、Tauri `craft_browser.rs` 和 Windows 安装钩子。

证据：[初期固定 Craft 托管](../acceptance/craft-20261013/README.md)、[插件管理](../acceptance/craft-plugins-20261014/README.md)、[统一插件和历史回归](../acceptance/unified-plugins-20261014/README.md)、[原生编辑窗口与 PDF 往返](../acceptance/native-editor-windows-20261015/README.md)。

## 十二、构建、交付与验收资料

更新 Tauri 权限、文件夹选择、编辑窗口和主窗口命令约束，调整 sidecar/Windows 构建将所需模块与可选传输纳入冻结包；通知依赖版本对齐以支持 Windows 构建。固定验收夹具按原始字节保存，参考运行时可选依赖和测试状态隔离有独立修复提交。

保留多批 Windows 构建、冻结节点检查、实际安装升级、模块哈希和节点身份核对记录。安装成功、包内模块一致、模型任务成功分别记录，不能互相替代。本次推送本身不重新安装应用、不创建 Release。

生成安装包、重复源码快照、浏览器缓存、临时节点数据库、私钥与控制令牌均位于忽略的 `output/` 或既有临时目录，保留本机，不提交程序包。安装器本地文件和哈希见 [本地包清单](2026-10-15-local-packages.tsv)。根目录项目规划 PPTX、检查记录和预览图属于规划资料。

Claude 在 `D:\code` 共享仓库写入的改动与本次目录相同，已统一保存；Git 无法对未提交状态可靠区分作者。两个登记的 Claude 临时分支没有当前分支之外的提交。VM 机内独立副本尚未核实的边界见 [本地整理记录](2026-10-15-workspace-review.md)。

## 当前验证和未完成项

不同日期的检查覆盖不同源码阶段，结果不相加，不使用历史通过数冒充当前全量回归。推送前新增原始日志和结构化结果见 [本次交付检查](../acceptance/upstream-handoff-20261015/verification.json)。

| 检查 | 结果与适用范围 |
| --- | --- |
| 本轮 TypeScript | `npm run lint` 通过；语言修正后再次通过 |
| 本轮前端初次全量 | 114 个文件，847 项通过、1 项硬编码文案失败；该失败已修复 |
| 修复后定向复验 | `Localization.test.tsx` 与 `RynAgentProject.test.tsx`，22 项通过 |
| 推送前前端全量复验 | 114 个文件、848 项全部通过，193.65 秒；保留一条 MSW 未匹配请求的 stderr 提示 |
| 推送前前端生产构建 | `npm run build` 通过，包含 TypeScript 和 Vite；保留大 chunk 与通知模块混合导入提示 |
| 本轮后端专项 | 188 项通过、2 项跳过、1 项 Windows 键盘输入回读断言失败；覆盖 loop、项目、本地工具、产物、会话复用、插件、目录、Drop 和原生传输 |
| 历史基础集成 | 10 月 12 日记录前端 660 项最终通过、后端全量结合定向复验去重 3,697 项通过；属于当时快照，见原测试文档 |
| 历史项目任务 | 隔离 Agent 套件 1,395 项通过、1 项桌面测试未选；CLI 绑定最终边界另有 130 项通过。不是本次重跑 |
| 历史 v4 检查 | 运行时/权限等 359 项、前端 72 项、CLI 49 项、流控制 5 项通过；冻结节点与 Windows 安装器检查见对应记录 |

仍需明确保留的边界：

1. Windows 桌面键盘输入回读测试未解决，不能宣称所有桌面自动化稳定可用。
2. Rust 传输仍需更多跨网络、NAT、丢包及带宽条件验证；本次未重建 Rust 或复跑公网性能测试。
3. 办公编辑器未完成全部“新建、修改、导出、重开”验收，没有新增可宣称完成的 Agent 文档编辑接口；现有字体限制保留。
4. 正式固定任务集、早期真实模型失败和恢复验收缺口不因本次提交自动消失；没有宣称达到 Codex/Claude 的固定百分比能力。
5. `0.7.0` 是现有开发标签，没有发布新的正式版本或完成跨平台发布验收。

## 提交和其他分支处理

现有 16 个提交保持原顺序，完整 SHA、主题和日期见 [提交清单](2026-10-15-upstream-commits.tsv)。本说明覆盖其全部差异，不只描述 `63f9b97`。

未批量合并其他功能分支。多数已包含；好友 AI 邀请续接和桌面邀请深链保留为后续针对当前代码移植及验收的候选，旧综合分支不直接覆盖当前实现。详细本地分支核对见 [45 个分支清单](2026-10-15-branches.tsv)。

本次目标是把这一完整、可追溯的开发状态提交到老板仓库的同名分支，供逐功能审阅。后续合入 `main`、发布或上线应依据各功能剩余验收推进。
