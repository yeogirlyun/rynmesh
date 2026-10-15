# T13/T14 第一批前端修复验证

2026-10-09；B 任务，gpt-6.1-sol high。此记录只覆盖本批 UI 行为，不能作为真实模型效果或完整产品交付的通过证据。

## 已实现

- 模型池将助手默认标记与能力验证操作分开。默认模型能力因来源编辑失效后，同一行仍能主动重验；不自动探测或改变默认、分享、启用设置。动作区允许换行。
- 助手设置、模型目录、任务列表、当前详情分别处理失败；模型目录失败不再阻挡历史。失效目录不沿用旧能力发送消息。设置暂不可读时，已知任务仍可暂停、取消、拒绝，由服务器校验真实状态。
- 页面获得焦点、恢复可见时刷新设置和目录。请求编号丢弃旧刷新，任务合并保留较高 revision；列表轮询失败不阻挡当前详情轮询。区分当前任务的固定模型、本次新任务选择、新任务默认模型，保留任务与草稿。
- 根据回执的 `not_executed` 和 `error` 字段显示未执行、错误、收到回执；未执行不计入工具执行次数，不根据模型正文猜测业务成功。按 `cancelled` 事件与回执序号说明取消后到达的证据，并说明取消不撤销已发生变更。精确回执仍可折叠查看。
- 切换任务或新建草稿不暂停旧任务。显示其他任务的服务器状态及可打开入口，任务列表收起后仍可见。保留独立大工作区、双栏折叠；没有增加未接通的计划、资料、记忆控件。

业务修改只涉及 ModelPool、RynAgent、rynAgentText、domain/rynAgent、其模块 CSS 和测试。App、Shell、全局翻译和 NAS/Drives 实现未作本批修改；无 Git 提交。

## 组件与构建证据

`npm test -- src/screens/RynAgent.test.tsx src/screens/ModelPool.test.tsx src/domain/rynAgent.test.ts src/screens/AISources.test.tsx src/App.agentRoutes.test.tsx --maxWorkers=2 --reporter=dot`：5 文件、43 项通过。覆盖失效默认重验、目录失败保留历史/草稿、返回刷新默认、设置失败时 pause/cancel/deny、列表失败独立刷新详情、focus 详情失败、乱序 focus/visibility 响应、真实执行计数、取消晚到回执、后台状态及既有导航/来源回归。日志为 [focused-tests.log](focused-tests.log)。

`npm run lint` 通过；`npm run build` 通过。构建的既有大 chunk 警告仍存在。没有重复运行全套测试。

## 实际浏览器验收：pending

授权验收目标为隔离节点 `http://127.0.0.1:19003/agent`；19004 确定性模型只用于 UI 验证，不计真实模型效果。没有访问或修改真实用户运行节点，也未修改隔离节点的业务配置。

CUA 调用失败：in-app browser 返回 `Browser is not available: iab`；Chrome 可被 inventory 发现，但两次 `createBrowserTab` 及一次 `listTabs` 都返回 `Unable to load browser request-header policy`。主线程独立调用也确认相同基础设施错误。没有绕过策略、生成替代截图或把组件测试计为布局通过。

恢复后需实际复验：默认失效行的标记与重验按钮同时可见；目录失败时历史与草稿仍能读；返回页面默认改变而旧任务模型不变；停止/拒绝操作可用；未执行与取消晚到回执可读并展开证据；切换任务和收起双栏后后台状态仍可查。分别检查桌面、窄屏、暗色下输入区及审批卡是否被新增说明挤压。

## 浏览器恢复后的实际复验（主协调者）

同日稍后 CUA 的 iab 连接恢复，使用生产 dist 与原隔离节点，未访问真实用户节点。以下已实际点击核验：

- 独立一级助手入口、无重复服务页签，主导航/任务列表四种展开组合都能切换。
- 工具验证→用于助手→编辑来源 request_defaults→保存使能力失效；默认标记与重验按钮同时出现，点击后恢复能力。全程没有开启任何对外分享。
- 返回助手默认模型正确，启用后出现输入工作区。提交确定性测试任务后到达精确审批卡；卡片可在正文区域滚动到全部参数及按钮，输入区保持独立。
- 拒绝审批后显示“已拒绝变更”；两次真实读取显示 2 次工具调用，未把拒绝算成执行。此次确定性模型故意不理解拒绝，正文仍错误说分享成功；这不是效果通过证据，已作为 T04 业务验证器回归种子。实际未批准写入。
- 1024×768 与 640×768 实际截图核对。窄窗口侧栏作为覆盖层，可从顶部独立收起，收起后正文/输入/控件可用；窗口测试后已 reset。

截图：[桌面审批卡](approval-desktop.jpg)、[1024 任务区](task-1024.jpg)、[640 双栏收起](task-640.jpg)。

仍待最终联调：目录/设置请求失败的真实页面故障注入、取消后的晚到回执、后台运行切换、深色、T02/T03 新流程和最终同快照桌面。已有组件测试覆盖不替代这些实际验收。
## T02/T03 实际页面联调补充

在同一个隔离 home 重启到 B 批后端后，旧 completed 任务可打开，显示“本轮结束 / 尚未核验”，没有把旧模型声称成功迁成 verified。新任务使用 loopback 合成模型，仅验真实 HTTP、状态机与 UI，不计真实 Agent 成绩。

- 选择型补充信息：进入“等待你的回答”，必填未填时按钮禁用；选定 acceptance-model 后提交，原 pending_input 消费且恢复执行，只统计 1 次业务读取，内部询问不计业务调用。截图 [waiting-input.jpg](waiting-input.jpg)。第一次 fixture 错把 options 写成字符串而非 value/label 对象，被 runtime 拒绝；修正测试 fixture 后重测通过，非产品修复。
- 待分享审批时更正为“不要分享，只检查”：目标升第 2 版，旧审批立即消失并记录未执行。独立读取临时 SourceStore 核对所有模型仍私有，记录 [steer-control.json](steer-control.json)，截图 [steer-applied.jpg](steer-applied.jpg)。未批准任何实际分享写入。
- 实看发现固定系统 uncertainty 和回答包装前缀直接显示英文。前者已交 frontend 映射；后者采用后端可信 UI metadata 修正，不能靠猜用户文本前缀。上述旧截图保留，不代表修复后最终布局。
- 新任务原子范围授权、已存在任务更改范围、资料发送同意与目标代际变化的实际界面验收由 frontend 继续进行。

## T14 范围实际页面联调

Chrome 隔离会话验证使用同一 19003 节点、19004 确定性模型和 19005 合成 WebDAV；未访问真实用户节点。构建 `index-IgxyTaI1.js`，focused 六文件 61 测试、lint、build 均通过，既有大 chunk 提醒保留。

- 新任务先选实际目录 `projects/orion/week-41`、显示具体模型和来源、确认发送后才可提交。创建请求原子保存 scope，再开始执行；未确认时发送禁用。证据 [scope-created.json](scope-created.json)、[640 创建前确认](scope-create-640.jpg)。
- 旧任务可修改递归范围；权限变化使旧审批失效并留下 `not_executed` 回执。第一次确定性 fixture 因旧静态 call ID 复用失败，证据原样保留于 [scope-updated.json](scope-updated.json)，未计成产品成功。随后修正隔离 fixture，第二任务取得真实目录列表和三份文本读取回执，分别 170、220、179 字符。[scope-read-three.json](scope-read-three.json)、[展开实际读取回执](scope-read-receipt-640.jpg)。
- 删除全部目录并保存，scope2 的 resource/egress grants 均为空。显式继续后新 context epoch 只收到 roots=[]，没有新的 read；旧回执仍作为审计证据可展开。[scope-read-then-revoked.json](scope-read-then-revoked.json)。模型 checker 未提供有效核验，outcome 保持 unverified；固定系统原因在最新页面显示中文，无关键词推断。
- 每目录默认只读；只有服务端声明 create_supported、writable、allow_ai 的连接能选“允许新建文件和目录”。切换动作清除资料发送确认，重新确认后才可保存。真实 GETscope 包含 list/read_ai/create_file/create_directory，但未派发任何写入；之后已撤销该测试授权到 scope4 零 grants。[scope-create-access.json](scope-create-access.json)、[640 权限说明](scope-create-access-640.jpg)。
- 实看 1280 和 640：正文独立滚动、模型输入底栏可用、双栏折叠保留。修正了全局 input 宽度影响范围复选框导致的横向溢出；窄屏复选框为16px。临时 viewport 已 reset。深色和真实云盘账户、真实模型资料产出尚未由本次证明。

此轮证明 HTTP/目录/权限/UI 状态闭环，确定性模型不计真实模型效果，也不把读取回执当完整目标达成。

## T07/T11 本地接续页面

网络权限撤销导致三个子任务终止后，主协调者接续本地工作。安全凭据表单和技能/偏好页面已接线；4文件45项测试、类型检查和构建通过。最终本批构建`index-YZOJaOOQ.js`，最后样式修改后主页面34项重新通过。凭据未用真实密钥做浏览器提交，组件/MSW证明专用接口、内存重试及不写浏览器存储；不以此代替C01实测。

- 新任务“技能 · 可选”按需读取后端目录，默认全未选，三个版本化技能均显示。1280×720实际发现全局input宽度使复选框过宽，已定宽到16px并横排；[技能面板](skills-panel.jpg)。
- 设置→我的偏好→新增测试文字→保存→重新读取→刷新页面，文字保留；独立读取隔离加密AgentStore得到revision1，[回读记录](preferences-readback.json)、[保存后截图](preferences-saved.jpg)。随后在UI移除并保存，刷新确认空列表，未更改真实owner偏好。
- 新来源/新文件/新目录权限名称补中英文显示，设置面板同类全局复选框宽度问题一起修复，去掉普通用户不需要的内部工具名。[最终设置与已清理偏好](preferences-cleaned.jpg)。保存后旧截图保留用于说明发现的问题，最终布局以后者为准。
- 当时尚待：新凭据真实HTTP+页面整链路、技能选中后实际任务快照、偏好模型输入效果、窄屏/深色复验和预算UI。后续进展如下；此批没有改变独立助手入口与双栏折叠方案。

## T10/T11/T12 本地页面续验

- 预算面板在隔离旧任务把累计模型请求上限128改为140，独立存储回读仍completed/3steps，没有自动执行；旧版本不完整用量显示未知。[预算页面](budget-final.jpg)、[独立回读](budget-readback.json)。偏好进入真实runtime后续模型输入、更新后不再注入旧偏好，已由知识模块8项回归验证；不代表真实模型会可靠遵循偏好。
- 在页面选择“系统诊断 v1”，创建明确标注“大结果 UI 验收”的合成任务，刷新后“本任务技能”仍显示原选定版本。新增任务详情技能显示，折叠时不占正文。[技能快照](task-skill-snapshot.jpg)。
- 仅隔离服务器给该任务的模型目录工具附加合成文字，实际runtime完成大结果加密落盘/返回引用；页面读取26934字节、26806字符，四页边界0/8192/16384/24576/26806，首尾标记正确，末页无下一页。完整SHA-256为`6da53de843078796a61ebbe0190ad07ab5009889592338c7e496e96e1dafa155`。独立存储回读重组四页hash一致，密文不含明文标记、任务消息不嵌入大结果正文。[最终末页](result-last-page-final.jpg)、[独立回读](result-skill-readback.json)。
- 结果按钮采用中文工具名称；SHA标注为内容指纹；字符范围取服务端游标，修复JS UTF-16使emoji范围多算的问题，专门回归通过。
- 技能/主页面/记录3文件44项通过，之后记录8项（含新增emoji用例）通过；类型检查与生产构建`index-C7ktPXd6.js`通过，保留既有大chunk提示。

该大结果任务是确定性页面fixture，目标状态为unverified（agent_output_limit），不能判为业务/真实模型通过。不会用它充正式成绩；真实模型、端到端凭据、删除界面最终动作、窄屏深色复验仍未完成。

## T09 任务模型与逐模型参数实际页面

使用独立 Chrome 页 48284692、隔离 19003 节点及 19004 确定性模型，未访问真实 owner 来源或外部模型。最终生产构建 `index-Uy4MAf86.js`；四文件联合53项通过，随后新增同模型接收方重确认回归，模型组件6项通过；本批对应共54项。类型检查和构建通过，保留既有大 chunk 提醒。

- `second-model` 显式两轮工具探测通过后，在“此模型参数”仅修改最大输出到3072，模型池立即显示未验证工具支持；再次显式验证通过。后续修正保存为只提交编辑字段的 PATCH，再实看修改到3000并显式重验。独立 GET 比对 `acceptance-model` 的生效参数、rule_revision、destination_revision 均未变化，助手默认模型也未变化。[参数前值](t09-models-before.json)、[能力失效截图](t09-model-invalidated.jpg)、[最终回读](t09-model-readback.json)。不会在打开弹窗时探测；probe期间规则变更会阻止旧编辑套新版本保存，必须显式重新读取。
- 专用任务 `agent_6f39202c8e344f2dbb7d5a9ba21f17de` 在发送前明确选择合成目录 `projects/orion/week-41` 并确认资料发送给 acceptance-model。确定性 fixture 实际仅执行roots/list两次业务工具，收到0份文件读取回执，outcome为unverified；本次不将它判作资料读取或模型效果成功。
- 在该任务本轮结束的安全点，选择 second-model，确认历史用户文本与实际接收方，保持“目录转授权”未勾选，保存后 task 为paused、steps保持5，resource/egress grants均为空。原目录选择保留且allow_model_data=false，新任务默认仍acceptance-model；没有调用resume。旧assistant气泡继续标acceptance-model，缺epoch的历史回复不猜当前模型。[确认页](t09-switch-consent.jpg)、[独立回读](t09-switch-readback.json)、[暂停及历史归属](t09-switched-paused-history.jpg)。
- 之后单模型参数变化更新了second-model目的地代际。通过“当前模型，可重新确认接收方”走相同preview/确认/PUT重新绑定，仍paused/5steps、scope3、零grants；与runtime确认同model_id重绑定合法，避免用户必须先切出再切回。旧选择没有被当作已有授权，转授权复选框保持禁用。[同模型重新确认回读](t09-same-model-readback.json)。
- 实看1280×720和640×720：双栏收起后正文独立滚动，输入栏仍可用；参数弹窗可滚动，标题/关闭与保存按钮可访问，浅色/深色均核对。临时viewport和主题已恢复。[640模型卡](t09-switch-640.jpg)、[浅色参数弹窗](t09-parameters-640.jpg)、[最终深色参数弹窗](t09-parameters-dark-640.jpg)。

尚待UX压缩：宽屏正文前四张折叠元信息卡约占240px，与固定输入区共同减少对话空间；主协调者已要求后续在独立模块/双折叠方向内收紧，不以此延迟安全凭据联调。本批不证明真实模型完成目标、真实两机资料绑定、或T15朋友授权矩阵。

## T07 安全凭据与来源草稿真实页面补验

隔离server session86273，独立Chrome页48284692，仅loopback `http://127.0.0.1:19004/v1` 与合成字符串，不使用真实凭据。专用任务 `agent_737f9bb37a3b43ac8b9a9e389fe204ba`：等待credential_ref → owner安全表单 → 草稿prepare/test → 精确审批 → 来源commit → 模型池回读。

- 未提交的合成密钥输入后离开至新任务，再返回原任务，密码输入值为空（实际DOM只记录布尔结果，不保存密钥值）。重新输入另一合成值、选择本机服务、手工核对loopback地址并勾选确认，才点“安全保存并继续”。确认消息只包含endpoint与安全存储已配置，不将密钥放进聊天。[实际表单](t07-ui-credential-form.jpg)。
- 安全保存凭据后仍只有原1个来源，任务在waiting_approval；此时没有保存来源或开启分享。发现原审批仅有通用文字和折叠JSON，已补可信preview中的来源名/地址/alpha beta/逐模型chat与tools测试/6次探测/费用未知/新模型私有摘要，保留确切参数和指纹折叠查看；新增组件回归通过，原35项页面用例同时通过。[实际审批卡](t07-ui-source-approval.jpg)。
- 批准后新增来源 `src-44516966ec61409c`（来源配置 UI 验收），models=[alpha,beta]，sharing=false、shared_models=[]。独立GET与审批前公开原来源完整比对相同，提交和丢弃的两份合成密钥均不在公开task/source JSON中；页面重读两行都显示“仅自己使用”。[独立回读](t07-ui-source-readback.json)、[模型池私有状态](t07-ui-models-private.jpg)。
- 该任务仅确定性fixture控制流程，3次业务工具（prepare/test/commit），outcome仍unverified，未猜测完整目标达成，也不计真实模型效果。构建 `index-DIjQbUyX.js` 成功（随后仅文案细化保留模型的已有分享、删除模型同时撤其分享）。

尚待固定系统凭据确认消息的可信ui.kind扩展，以便中文标签替代目前的Source endpoint/Credential；普通owner回答不按英文前缀猜测。浏览器验收证明表单与HTTP状态闭环，供应商凭据/真实外部来源没有在此测试。

## T15 接收者矩阵及单文件范围续验

Sol在额度中断前完成实际Chrome矩阵操作，落盘t15-*：后端CAS支持true；alpha仅变分享范围仍private，再单独启用并只授予合成好友A；旧grant版本冲突要求重新读取；source并发加入beta后旧UI停alpha被CAS拒绝，重读后停alpha保留beta，最终恢复alpha、清理beta私有。根确认截图/回读文件落盘；上述是隔离合成好友，不代表两物理节点通过。beta显式enable/disable使rule_revision变化，但effective参数/范围/destination保持；不把rule修订说成全程未变。

额度中断时单文件UI接线已落盘，翻译和专门组件检查尚未完成。根补两种语言及2项检查，范围组件9项通过，生产构建index-zkiPIq5Q.js。隔离19003重启加载file_scope_supported，IAB页12实际执行新任务→任务资料范围→添加单个文件→隔离资料库→projects/orion/week-41→progress.md→确认唯一模型接收方→发送→刷新。

保存任务 `agent_ae8420e4f58c41c7ab68dabd36b448f4`，独立HTTP回读确认root_kind=file、唯一精确文件、actions=[read_ai]、非递归、唯一grant、consent=true及零write事件。[刷新后的页面](exact-file-saved.png)、[独立回读](exact-file-readback.json)。本机确定性模型仅调用ryn_list_models，目标unverified；本次只证明页面授权与保存一致，不声称模型读取文件或完成业务目标。主导航与任务列表均收起，独立正文区域仍可滚动。

仍须收紧折叠元信息卡高度，并将资料说明中残留“目录权限”的总称改为“资料权限”；本轮未将此视觉细节算作最终UI验收完成。

## 本机 GGUF 参数页与资料总称（13:44 UTC）

主协调者接续本机参数页，入口为“模型池→配置本机模型→更多模型设置”，未新增顶层模块。参数含最大输出、并发、每日请求次数，展示实际上下文及当前生效值；通过 PUT 只提交改动字段的 configuration，并带 expected_rule_revision。冲突/未知保存结果阻止再次保存，要求主动重读；弹窗标题和关闭按钮滚动时保持可见。

### 任务信息栏收紧（13:58 UTC）

实际 1280×720 页面中，目标、预算、模型、资料四个收起面板原先累计占约224px，造成正文拥挤。现保留四个独立入口，收起时横向排列并自动换行，展开面板仍占完整宽度；计划和技能仍独立显示，不隐藏执行状态。相同视口中首条用户消息由 y=360 移到 y=176。640px 窄屏实测 document.scrollWidth=clientWidth=640，无横向溢出；资料入口自然换到第二行，视口已恢复。

证据：[收起布局](agent-compact-controls.png)、[预算完整展开](agent-compact-budget-expanded.png)、[窄屏](agent-compact-controls-narrow.png)。浏览器重载后的生产包为 `index-Bq9UsK2v.js`。Agent/预算/模型/资料四文件58项已有回归通过，类型检查及构建通过；原大chunk提示仍在。该页面沿用之前的合成任务，仅证明布局/展开交互，不是新增业务任务成功。

IAB 页 12 在隔离 19003 节点使用元数据合成 GGUF（明确无实际引擎、无下载或推理）操作：输出1024→512、每日不限→25，并发1保留；点击保存、整页刷新、重新打开后仍显示512/25。独立HTTP回读7项全真：参数、规则代际符合预期，模型仍停止/私有/不自动启动，助手设置及其他来源不变。证据为 [回读](native-parameters-readback.json)、[桌面](native-parameters-saved.png)、[640宽度](native-parameters-narrow.png)。这不是 GGUF 推理验收。

本机面板8项组件检查通过，包含CAS冲突后重读丢弃旧编辑和超过真实上下文拒绝发送。模型池/参数/本机/范围联合30项通过，资料中英文总称更正后范围与本机17项通过；数据接收方提示不再误称仅可使用API。最终本批构建index-Dtv03FPD.js，tsc通过，既有大chunk提示保留。截图对应同一本机参数组件，随后仅资料帮助文字变化。

## 核验纠正反馈（15:15 UTC）

服务端`ui.kind=verification_feedback`现在显示中文/英文的简洁核验记录，原始反馈放在默认收起的详情里，并提示以最新目标结果为准；没有模型署名。普通模型文本即使包含相同英文前缀仍按普通消息显示，反馈详情用纯文本，HTML不执行。Agent及domain两文件44项通过，类型检查、生产构建通过，最终包`index-hC_EJI1d.js`；原大chunk提示保留。

隔离loopback模型为这个专用UI任务故意返回一次“passed但有未满足项”，以实际runtime触发降级和一次纠正；没有修改业务runtime来伪造消息。任务`agent_2783fd344507446fbfcdb77611ff5f84`实际浏览器发送、等待结束、展开和收起详情。独立HTTP回读7项全真：一次降级、一次反馈、一次业务读取、零写入、只读任务、更正结果与完成状态。详情见[回读](verification-feedback-readback.json)、[实际页面](verification-feedback-collapsed.png)。这是确定性流程/UI证据，不是模型效果评分。

先前同名UI任务`agent_7e9518744b184e0fbb67c18a4d915184`因该隔离节点保存的1000字符工具输出上限停在unverified，没有核验反馈，记录保留。仅本次新合成UI任务临时使用20000上限，完成后节点设置恢复1000及修改前确认；冻结真实验收预算未改变。新任务自身保留只读快照。

## 新任务默认模型的精确审批与即时同步

新增批准卡展示新旧模型ID、来源、提供节点和只影响新任务的说明。[审批截图](default-model-approval.png)。第一次`agent_cf7b655483fc47349c40dffe192e962c`通过真实本机API完成一次精确批准变更，来源密文hash和其他设置不变，但页面旧默认值没有主动刷新；[初次回读](default-model-readback.json)保留这个问题。

修复写回执后配置刷新、新建任务前重读配置后，`agent_80b785a312b741c49b4be91f3e10b1c1`在同一页批准后直接显示当前模型acceptance-model、新任务默认second-model，点击新任务也自动选中second-model。期间IAB连接更新为browser5/tab1，先通过API确认原任务仍waiting_approval，再重新绑定原页面继续，没有重建任务。见[同步后页面](default-model-synced.png)、[最终回读](default-model-synced-readback.json)。测试后恢复原默认、输出上限及其他设置。

实际fixture只负责确定性模型请求，使用生产审批、写入及核验；它没有提供有效语义checker，且变更前配置读取已过时，因此整体partial，不称目标verified。这里只验证UI与默认值控制，未替代冻结C03精确节点分享或真实模型验收。前端46项通过、后续3项针对新任务门控通过；tsc/构建index-CqadKY6J.js通过。
