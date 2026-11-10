# 安装包自带插件的只读工具不再弹确认（2026-11-10）

## 起因

主人在 Ryn 助手页顶部看到：“「dashboard」有 58 个工具声明为只读（只查询、不修改），允许它们直接运行吗？”
并问：以后每次安装都有吗？默认就是“自动处理”。

核实结果：不只每次安装，**每次 Ryn 启动**都会再弹。

- 卡片来自 `RynAgentReadOnlyHints.tsx`，条件是插件服务器有 `server_hint_candidates`
  （插件声明只读、但主人还没确认的工具）。“以后再说”按 `实例:发现版本` 记住，发现版本一变就失效。
- 公司插件服务由安装包写入的 `plugin-services/services.json` 启动（`plugin_services.rs`），
  每次启动都会走 `activate_local_service` 换一次服务凭据。这一步会清空工具列表并调用 `_clear_server_hints`，
  把点过“允许”的只读确认也一起清掉；随后重新发现工具，发现版本变了，卡片又出来。
- 只读确认不只影响弹窗：没确认的查询工具按“写操作”处理（先预览、记操作凭据、出错按写入处理、
  压缩后重复同一查询会被拒绝），“自动处理”模式下也一样。插件数据页（`PluginViews`）也要求工具已确认只读。

## 改动

- `extensions/store.py`
  - `activate_local_service`：记下 `installer_service_generation = config_generation`，表示这个服务器
    当前的配置就是安装包服务绑定的配置。
  - `record_mcp_discovery`：服务器仍是安装包服务绑定的配置时，插件声明 `readOnlyHint` 且名字唯一的工具
    自动设为只读，来源记为 `installer`。下次发现时不再声明只读的工具会被移出。
  - 主人在插件设置里把某个这类工具改回“会修改”，记在 `owner_write_tools`，之后重启、重新发现都不会再加回，
    也不会再出现在待确认列表里。主人重新勾上即移出该列表。
  - 主人改了服务器地址、认证等配置（`config_generation` 变化）后，安装包信任立即失效，
    回到原来的“由主人确认”流程，直到安装包服务再次绑定。
  - 读取旧数据时保留 `installer` 来源。
- `webapp/src/domain/agentExtensions.ts`：`read_only_sources` 接受 `installer`。

不变：主人自己添加或导入的第三方 MCP 服务器，仍按原规则需要主人确认一次；卡片组件本身没有改。

## 行为变化

- 装了公司插件包的电脑：Ryn 启动、插件服务绑定并发现工具后，dashboard 等插件声明只读的工具直接是只读，
  Agent 页不再出现这张卡片，插件数据页也不用再确认。
- 已经弹过、点过“以后再说”的电脑：装上新版本后第一次启动即生效，不需要任何操作。

## 验证（VM，基线 develop 0b236bf）

- 新增 3 个用例（`tests/test_agent_local_services.py`）：
  - 未绑定时仍是待确认；绑定后自动只读、待确认为空、非只读工具仍是写；连续两次“重启”（重新绑定）后仍保持；
    工具不再声明只读后移出；
  - 主人改回“会修改”的工具重启后不被加回、不再进待确认；重新勾上后来源为 `owner`；
  - 主人改地址后安装包信任失效，回到待确认。
- 3 个新用例在旧代码上全部失败，新代码上通过。
- `test_agent_local_services.py` + `test_mcp_data_policy_store.py`：69 passed。
- 回归（21 个插件/扩展/MCP/OAuth 测试文件）：分支与同一基线逐个对比，失败清单完全相同，没有新增失败：
  - `test_agent_plugin_discovery`（12000 上下文）、`test_extension_data_policy_runtime` 自动压缩 ×2、
    OAuth 晚到交换 ×6：两边都失败；
  - `test_agent_extensions_oauth.py` + `test_mcp_oauth_incident_replay.py` 串行：两边都是 35 failed / 34 errors，
    主要是本机本地测试服务器 `httpx.ReadTimeout`，与本改动无关。
- 合并到 develop（c8ef194，含期间 develop 新增的 9fd0351 等提交）后复查：
  - `test_agent_local_services.py` + `test_mcp_data_policy_store.py` + `test_agent_extensions_store.py`：76 passed；
  - vitest `RynAgentReadOnlyHints` / `McpDataPolicy` / `PluginViews`：31 passed；
  - `tsc -b --noEmit`：通过。
- 未做：主人电脑安装新包后实际看卡片消失；真实模型任务。

## 本机复验与补修（2026-11-10，Asia/Hong_Kong）

结论：**505c754 原包不能整体验收通过**。本地安装包服务的自动只读路径有效，但本机 Dashboard MCP 实际为远端服务，不在该路径；另复现主人反复切换选择后被覆盖的问题，已在任务分支补修并补回归。没有覆盖本机安装程序、修改真实工具权限或发送业务消息。

### 版本、入口与范围

- 当前进程及桌面 `Ryn.lnk`、开始菜单顶层 `Ryn.lnk` 均指向 `C:/Users/<user>/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Ryn/Ryn.exe`。
- 实测三个程序哈希与 505c754 安装验收凭据一致：Ryn `568bc541803750d1a4a0ab3d5f66e00cf3e35747b0efec659e210f9208084a9d`；peer `2f240998c8d2f12c85716991e5d3ca6d07a884641c5f4408d4f729aabcf7efad`；computer `2e2034ab2e5ef6c7a58884613a8df441a0662a09b09b026541d37b2374bd7d21`。`git merge-base --is-ancestor c8ef194 505c754` 返回 0。不能只看 0.8.0 版本号。
- 交付遗留：安装目录 `build-info.json` 仍记录 054ba81，已被实测哈希证明过期；开始菜单 `Programs/Ryn/Ryn.lnk` 仍指向 `D:/code/rynmesh/outputs/ryn-desktop-current/Ryn.exe`（10 月 29 日构建，哈希 `9df7feb578c0e810829ed57daf7a334989cbd4ae361614493ad41fc9d22aa47e`）。保留原状，没有覆盖或删除入口。无法证明用户以后不会选择这个旧入口。
- 实际 `plugin-services/services.json` 的 `company.mcp_servers` **仅含 `company-workspace-mcp`**。Dashboard 是 `https://dashboard-hk-api.gshopper.com.cn/ryn/mcp`，所以不能因它属于公司插件就将其视为本地安装包服务。直接将远端 ID 加入本地握手会被 `agent_local_service_binding_invalid` 拒绝；新增隔离用例固定了这个边界。

### 接口、界面与重启证据

读取 `GET /api/local/agent/extensions`，正常退出再启动同一安装包，随后受控结束经完整路径核对的 company-service 进程，由 Ryn 监督器恢复。两次都等待服务发现完成：

| 阶段 | 本地 workspace config_generation / discovery_revision | Dashboard config_generation / discovery_revision |
| --- | --- | --- |
| 初始 | 374 / 375 | 239 / 348 |
| 正常重启 | 376 / 377 | 239 / 348 |
| 受控公司服务重启 | 378 / 379 | 239 / 348 |

- 本地服务发现 10 个工具：7 个声明只读的工具均已只读，来源 `installer` 或既有 `owner`；3 个非只读工具未被放入只读列表；候选始终 0。存量 owner 列表还含历史已消失的名字，不能用整个数组长度充当当前工具数量。
- Dashboard 58 个工具均声明只读、候选 0，但来源全部为 **`server_hint`**，不满足本次要求的 `installer/owner` 来源。它的发现版本在重启中没有变化，不能声称测到了 Dashboard 的激活/重新发现路径。
- 其他旧远端配置：affiliate-finance `134/202`、affiliate-affiliate `61/201`，均 NeedsAuth；两个已禁用 finance 配置分别 `158/146`、`157/142`，各有 36 个候选；已禁用 operations `248/0`。它们不在本地服务绑定列表，不应隐去后声称“所有公司服务器候选都为空”。
- 实际 WebView 的 `ryn.agent.read-only-hints-dismissed` 为 **null**，未修改或清除真实 localStorage。两次重启后进入 Agent 页均没有 `.ra-read-only-hints` 卡片，并有原生界面观察。
- Dashboard 实际页面已认证并展示查询数据，无只读确认。另调用插件数据视图 `balance-overview/query` 成功，未返回 `agent_plugin_view_read_only_confirmation_required`。这是既有 server_hint 确认下的成功，不能归因于 installer 信任。

### 主人选择、代码审查与补修

- 原有隔离测试证明：installer → 写操作 → 重启仍为写且无候选；再改只读来源为 owner；修改地址使配置代际变化并恢复待确认。
- 新复现：installer → 写操作 → 只读（owner）→ 再次写操作 → 服务重启，原代码错误地把工具重新加入只读。失败断言为期望 `[]`，实际 `['lookup']`。
- 修复 `_apply_mcp_policy_fields`：配置仍受 installer 信任时，所有被主人从只读列表移除的工具都记入 `owner_write_tools`，包括曾经重新确认为 owner 的工具。保留原有恢复只读时移除 opt-out 的行为。仅在隔离 tmp_path 中修改权限，真实配置无须复原。
- 新增边界验证：重名工具（一个 readOnlyHint=true、一个 false）不自动只读；installer schema 变化且仍声明只读时继续信任；重新打开持久化 Store 保留 installer 来源；重导入后先待确认、下次服务激活后恢复 installer；远端服务器不能冒充本地服务。
- 旧数据读取仍默认缺失来源为 owner；非布尔 true 不视为 readOnlyHint。server_hint 的 schema 变化会撤销确认；installer 则信任当前绑定配置下的新声明。owner 明确选择优先，不随声明变化自动撤销，这是现有行为。
- 未发现本次新增自动信任会把未声明只读或重名工具设为只读的路径。该机制仍依赖安装包服务诚实声明工具效果，不等于验证工具内部绝无副作用。
- 没有把远端 Dashboard 自动纳入信任：这需要独立的安装包声明/远端配置绑定方案，不能通过扩大 localhost 凭据握手来安全解决。本次仍将 Dashboard installer 来源验收记为不通过。

### 真实模型与回归

- 在现有 `automatic` 模式和现有 GPT-6.1-Sol 模型配置下创建专用验收任务，只执行 `ryn_dashboard_identity`。任务 completed，实际 MCP 工具 effect=read，身份查询成功。
- 无预览/审批事件、pending_approval 为空、对应 task 的加密 business-operations 账本记录 **0**、无 `agent_write_outcome_unknown`。仅输出脱敏结果计数，不提交身份响应和账本。
- 指定 Python 三文件：原包对应源码 76 passed；补修与 4 个新增用例后 **80 passed**。另 `test_plugin_views.py` **84 passed**。
- vitest `RynAgentReadOnlyHints`、`McpDataPolicy`、`PluginViews`：**31 passed**；`tsc -b --noEmit`：通过。前端未修改。
- 可选第三方确认流程有隔离单测覆盖（声明先待确认、确认后保存 server_hint）；未新建真实第三方服务并做完整界面确认。
- 补修没有重新打包安装，因此本机 505c754 仍存在反复切换选择的遗漏；源码通过不代表已安装包修好了。原回滚目录 `artifacts/session-stability-20261110/rollback-20261110-040848` 保留。
- 本地脱敏摘要位于 `artifacts/installer-readonly-acceptance-20261110/`，不纳入 Git；仅提交此文档、实现和测试。

### 收尾

- 补修提交 `d322893`，合并 `develop` 为 `d5b53d8`，已推送正式主程序仓库 `yeogirlyun/rynmesh` 的 develop；没有修改公司插件仓库。
- 最后再次经程序自己的退出确认正常退出，然后不带调试参数启动原 505c754。节点 8791 与公司服务 18140 健康接口均 200，9333 无监听。本地服务 config/discovery 为 `380/381`，Dashboard 仍为 `239/348`，二者候选均 0，Agent 页无确认卡。
- 真实账号、工具权限、配置、localStorage 与回滚文件未改动；新增的本地验收 Agent 任务保留可查。新的主人选择补修尚未打包安装；远端 Dashboard 自动信任与旧快捷方式/过期 build-info 仍列为待解决项。

## 远端 Dashboard 补修（2026-11-10）

复验指出：截图里的 Dashboard 是远端连接（`dashboard-hk-api…/ryn/mcp`），不在本地服务握手的范围里，第一版修复没有覆盖它。

### 原因

- 安装包每次运行都会执行 `migrate_release_defaults`。新版本只要改了 Dashboard 的 `auth` 声明，它就按“连接已更换”处理：
  清空工具列表、`discovery_revision` 归零、`_clear_server_hints` 清掉全部 `server_hint` 确认。重新发现后 58 个工具又全部待确认。
- `migrate_finance_oauth_clients` 改认证、插件启用状态切换、主人改配置，也会走同一条清除路径。
- 所以 Dashboard 的卡片和安装有关：安装包迁移了连接，就需要再确认一次；全新安装第一次发现时也一定会出现。

### 改动

- `ExtensionStore.trust_installer_connections(declarations)`：只由已验证的安装脚本在所有迁移之后调用。
  对公司插件声明的远端连接（不含 `local_service`）逐个核对：
  服务器存在、`plugin_id` 一致、`streamable_http`、地址与声明一致、认证规范化后与声明一致，
  且没有主人加的请求头、令牌环境变量。通过后记下当时的配置指纹 `installer_config_sha256`。
- `_installer_service_trusted`：本地服务仍按握手的 `config_generation`；远端连接按 `installer_config_sha256 == config_sha256`。
  指纹不含启用状态，所以插件或服务器的停用/启用不会结束信任；地址、认证、请求头等任何改动都会结束信任。
- 绑定时立即处理已经发现的工具：声明只读且名字唯一的工具设为只读，来源 `installer`；
  以前主人从卡片确认的 `server_hint` 改记为 `installer`；主人改回“会修改”的工具（`owner_write_tools`）不动。
  之后每次发现仍按同一规则处理。
- `upsert_mcp_server` 重建记录时保留 `installer_config_sha256`，并让策略字段看到修改前的指纹，
  使“主人改回会修改”在远端连接上同样被记住（与 d322893 对本地服务的修复一致）。
- `scripts/full_package_install.py`：迁移和本地端点配置之后调用上述方法，回执增加 `trusted_connections`。
- 导入路径为每个服务器新建记录，不会带入信任字段，第三方插件包不能借此获得安装包信任。

不变：主人自己添加的服务器，即使地址相同、或连接已被主人改过，仍按原规则确认一次；
主人在界面里手动重新导入插件后，到下次运行安装包之前回到待确认（与现有“重导入重置主人选择”一致）。

### 验证（VM，基线 develop d50ff6e）

- 新增 `tests/test_installer_connection_trust.py` 9 个用例，以及 `test_full_package_install.py` 中走完整 `install()` 的 1 个用例：
  - 安装包绑定后已发现的只读工具立即生效，原 `server_hint` 改为 `installer`，非只读工具仍是写，重复安装无变化；
  - 发现时 schema 变化、重新打开 Store、插件停用/启用、服务器停用/启用之后仍然信任；工具不再声明只读后移出；
  - 主人“改回会修改 → 重新确认 → 再改回”之后，重新发现和再次安装都保留选择；
  - 主人改地址后信任结束并出现待确认，下次安装也不为主人的地址担保；
  - 主人改过认证范围、加了 Authorization 请求头或令牌环境变量的连接不被信任；
  - 同地址的主人自建服务器、`plugin_id` 不符、带 `local_service`、不存在的声明均不被信任；
  - 安装包认证迁移先清掉确认，随后绑定，重新发现后无待确认；
  - 完整 `install()`：回执 `trusted_connections` 含 Dashboard，发现后只读工具来源 `installer`、待确认为空。
- 这 10 个用例在旧代码上全部失败，新代码上通过。
- `test_installer_connection_trust.py` + `test_full_package_install.py` + `test_agent_local_services.py`
  + `test_mcp_data_policy_store.py` + `test_agent_extensions_store.py`：**123 passed**。
- 未做：重新打包安装后在主机上确认 Dashboard 来源变为 `installer`；本机 505c754 不含此补修。

## 远端 Dashboard 第二轮本机验收（2026-11-10）

结论：代码、回归、实际安装、存量账号 Dashboard 信任和真实模型只读查询通过；完整验收尚未结束。两次正常重启、空账号登录后的发现/界面，以及主人选择的完整原生重启验收仍待完成，因此没有推送正式仓库。以下不将离线回放冒充真实登录结果。

### 构建、安装和入口

- 核心从无已跟踪改动的 `develop` 提交 `22783aa24406ff3bf19f63d70fe528146a40d96b` 构建。公司插件从正式仓库 `yeogirlyun/gshopper-ryn-node` 的干净 develop `93f5046bd36c4636dff6498ac8f03666fff0653c` 构建，已核对与远端 develop 一致；未使用有其他未提交改动的 chat-backend 工作区。
- 单一安装包：`artifacts/remote-readonly-20261110/Ryn_0.8.0_22783aa_company-93f5046b_full-setup.exe`，145459021 字节；SHA-256 `98a7859cd21bc68a00397255e9b5e616bff78dea77e97728767ec50cbe5a4021`。包含主程序及三个公司插件，保留已有 Office 注册。
- 实际进程与安装位置为 `%USERPROFILE%/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Ryn/Ryn.exe`。该绝对路径也写在既有 `ryn-desktop.json` 中，用户数据仍为 `%USERPROFILE%/.rynmesh`，公司数据路径未变。
- 安装前停止的进程均按完整路径和 Ryn 专属名称核对。旧程序、公司运行包、公司数据库、节点配置与密钥、Ryn 注册表项和快捷方式已备份至 `artifacts/remote-readonly-20261110/rollback-20261110-115034`；回滚脚本及目录保留可用，均不提交。
- 安装退出码 0，身份与公司配置密钥哈希保持不变；公司运行包 902 个文件通过清单校验。新节点和电脑工具二进制与构建产物完全一致，桌面程序仅有已核对的 NSIS `UNK`→`NSS` 三字节标记差异。
- 已备份并修复 `Programs/Ryn/Ryn.lnk`：由 10 月 29 日的 `outputs/ryn-desktop-current/Ryn.exe` 改指实际新安装；桌面、开始菜单主入口也核对为同一路径。旧构建目录未覆盖或删除。
- 已将安装目录过期的 `build-info.json` 更新为实际核心/公司提交、安装包哈希、安装二进制哈希及回滚位置。
- 实际安装回执 `trusted_connections` 包含 `company-dashboard-dashboard`、`company-dashboard-operations`、`company-affiliate-affiliate`。未为了匹配声明修改老板的地址、认证或请求头。

### 代码与自动化验证

- 审查 `git diff d50ff6e 22783aa -- rynmesh scripts tests`：绑定只由安装迁移调用，且限定公司声明、同 plugin_id、远端 HTTP、相同 URL/规范化认证、无自定义请求头或令牌环境变量；实际指纹必须一致。普通编辑和导入不接受信任标记。
- 地址、认证、secret_headers、env_http_headers、bearer_token_env 变化后，指纹变化并撤销信任；再安装也不会为自定义连接绑定。启用状态不影响指纹。
- 共用授予逻辑仍要求布尔 true 和唯一工具名；主人 opt-out 优先。schema 变化后仍由当前受信配置的声明决定；不再声明只读的 installer 工具会被移出。旧数据缺失来源按既有 owner 行为读取。
- upsert 暂时保留旧指纹仅用于策略变化识别，保存前重算新指纹；同时修改地址与只读选择的隔离用例通过。导入伪造信任字段无法取得信任；重导入会丢弃绑定，远端到下次安装迁移才重新绑定。
- 指定六个 pytest 文件：**207 passed**；vitest `RynAgentReadOnlyHints`、`McpDataPolicy`、`PluginViews`：**31 passed**；`tsc -b --noEmit`：通过。
- 另做 7 项隔离配置边界用例，全部通过。未发现需要修改源码的新缺陷；未重跑用户列明的三个无关旧失败。
- 新冻结节点空目录连续启动两次通过；公司冻结运行包三次隔离启动通过；本次打包的安装迁移程序首次安装/重复安装、身份密钥、禁用插件、已有 Office 和用户文件保留检查通过。

### 实际接口、界面与模型

启动后主动调用 Dashboard `/test`，确实重新发现 58 个工具，再读取 `GET /api/local/agent/extensions`：

| 服务器 | config_generation | discovery_revision | 当前工具 / 声明只读 | 来源 | 候选 |
| --- | --- | --- | --- | --- | --- |
| Dashboard | 239 | 388（安装前 348） | 58 / 58 | installer 58 | 0 |
| workspace | 386 | 387（安装前 381） | 10 / 7 | installer 7 | 0 |
| affiliate | 61 | 201 | 40 / 40（历史发现） | owner 36、installer 4 | 0 |

- Dashboard 58 个声明只读工具全部在列表中；workspace 的 3 个非只读工具未被误标。affiliate 仍为 NeedsAuth，未把历史发现冒充本轮认证成功；operations 已禁用，未擅自启用。
- 实际 WebView 中 `ryn.agent.read-only-hints-dismissed` 为 null，未清除或修改真实 localStorage。进入 Agent 页原生观察与 DOM 检查均无确认卡片。
- Dashboard 页面保留登录并展示数据；插件数据视图 `balance-overview/query` 成功，无只读确认错误。未触发任何发送、写入或财务更新。
- 现有真实模型在 automatic 模式执行专用任务，仅调用 `ryn_dashboard_identity`，任务 completed，实际 effect=read；预览/审批事件 0、pending_approval 为空、该任务写操作账本记录 0，无 `agent_write_outcome_unknown`。仅保留脱敏结果摘要，不提交身份响应、任务日志或账本。

### 隔离验收与待续项

- 独立空数据目录经本次冻结安装迁移程序初始化，回执含 Dashboard 信任。使用新冻结节点实际启动并探测远端，得到 `NeedsAuth / agent_mcp_auth_required`，工具数 0；未复制真实账号凭据。**尚未由用户登录并发现工具，不能认定全新安装界面验收通过。**
- 另一隔离目录回放真实 Dashboard 的 58 个工具声明（仅元数据），将 `ryn_dashboard_identity` 改为写，重新打开 Store 后仍为写且无候选；再运行本次冻结安装迁移程序并重新发现，仍为写且无候选；改回只读后来源 owner。真实用户配置未改变。此项证明持久化与再安装路径，**不等同于已完成原生 Ryn 重启及登录界面验收**。
- 在真实 Ryn 的“设置 → 桌面端 → 退出 Ryn”打开正常退出确认框后，Computer Use 点击“确定”连续失败，错误为 `foreground window did not report a process id`；重新激活并观察确认框后重试仍失败。已请求用户手动确认，未以强制结束进程冒充正常重启。**两次正常重启尚未完成。**
- 本轮为读取 WebView localStorage 临时以 9333 调试端口启动。待用户完成退出后需无调试参数启动并验证端口关闭；不把当前待退出状态描述为已完成收尾。
- 脱敏证据位于 `artifacts/remote-readonly-20261110/`，构建产物、数据库、凭据、Cookie 和日志均未纳入提交。验收文档通过任务分支合并本地 develop；因仍有上述未验项，遵照“全部通过后再推送”的要求暂不推送。

### 用户协助退出后的续验

- 用户确认已正常退出，核对原 Ryn 进程消失后启动同一正式安装。第一次重启：Dashboard `239/391`、workspace `389/390`（config/discovery）。第二次经原生退出确认正常关闭再启动：Dashboard `239/392`、workspace 服务就绪后为 `393/394`。两次 Dashboard 均 58 个 installer、0 候选；workspace 均 7 个 installer、0 候选，3 个写工具未误标。
- 换用另一原生界面接口后成功操作退出确认，未通过强杀正式安装冒充正常重启。两次均进入 Agent 页确认无卡片；首次重启读取 localStorage 仍为 null。第二次不带调试参数启动，9333 无监听；Dashboard 数据视图查询再次成功。第 6 项两次正常重启验收已完成。
- 随后再次正常退出正式安装，启动 `artifacts/remote-readonly-20261110/fresh/install/Ryn.exe`，仅使用 `fresh/home`、`fresh/config`、`fresh/profile`，无真实账号或 Cookie 复制。原生空账号界面已显示 Dashboard 登录入口，等待用户亲自扫码，尚未确认任何只读提示。
- 隔离环境最初使用 18991 节点端口，公司页面返回 `authorization_sync_pending`。定位公司插件 `application.py` 的 `HOST_EXTENSIONS_BASE` 固定为 8791，账号同步不随握手的 `RYN_PLUGIN_HOST_PORT` 改变；切换隔离实例到空闲标准端口 8791 后账号接口从 503 恢复为预期的未登录 401，登录页正常。这是非标准节点端口的已有兼容性限制，不是只读信任失败；没有绕过认证或更改正式安装。
- 当前正式安装暂停运行，以便隔离实例占用标准端口；隔离实例临时使用 9333 读取 localStorage。待登录与主人选择验收后，应退出隔离实例，恢复正式安装、不带调试参数启动并确认健康。全部通过前仍不推送。

### 登录后异常的操作溯源（并行任务干扰）

- 用户完成隔离账号扫码登录后，Dashboard 实际发现 60 个只读声明工具（远端已新增 2 个工具），但候选为 60、只读列表为空。此结果不能作为原始空目录安装失败的证据：该实例在登录前已被另一个 Codex PPT 开发任务重新导入插件并直接回写内部状态。
- 已读取该任务的实际命令与成功退出结果：其导入包在隔离目录于 12:18:58 落地；随后从 GET extensions 的公开响应恢复 plugins/skills/mcp_servers，将公开字段写回加密 Store。该恢复未恢复内部 installer_config_sha256，且将 command/cwd/bearer_token_env 的 null 改成空字符串，保留了旧 config_sha256。记录中的指纹与实际重新计算的指纹不一致；并非用户操作导致。
- 用户登录后接口为 ready，Dashboard config_generation/discovery_revision 为 6/42、60 个候选；affiliate 为 8/41、60 个 installer、0 候选，workspace 仍为 7 个 installer、0 候选。重新执行本次冻结安装迁移程序后，trusted_connections 仅包含 affiliate，Dashboard 未获信任，未为通过验收而修改其配置或伪造信任指纹。
- 同一 PPT 任务随后使用正式数据目录的独立节点端口 18891，重新导入 Dashboard 并调用 apply-server-hints。因此本轮之前记录的正式安装验收结果是当时状态的证据，不能冒充并行操作后的最新状态；其手动确认也不能证明 installer 自动信任。
- 当前本轮隔离实例已停止，正式桌面界面尚未恢复；18891 的正式数据节点属于另一任务，未结束它，也未回滚其插件更新。已请用户暂停另一任务后再继续，避免同时读写同一配置。原安装回滚目录保持可用。
- 第 7 项全新安装登录验收、第 8 项原生主人选择/重启/再安装验收仍未完成（此前元数据回放通过不变）；不宣称全部通过、不推送正式远端。未提交公开响应原文、加密配置、身份信息或日志，本节仅记脱敏操作和结论。

### 干扰确认后的隔离恢复与主人选择验证

- PPT 任务已明确承认误导入及公开快照回写，并停止修改本轮隔离实例；它的正式数据节点仍在 18891 完成独立任务，本轮不并行启动相同正式 HOME。
- 保存隔离配置加密备份后，通过正常 upsert_mcp_server 空更新规范化被错误回写的字段，断言 URL、auth、请求头、启用状态和 instance_id 均未改变；随后执行原冻结安装迁移程序。没有直接写入或伪造 installer_config_sha256。安装回执恢复包含 Dashboard，用户刚完成的登录保持有效。
- 恢复后真实发现：Dashboard 6/47、60 个 installer、0 候选；workspace 28/29、7 个 installer、3 个写工具未误标。Agent 页未见卡片，localStorage dismissed 为 null。这是恢复后验收，不冒充未经干扰的全新安装。
- 在该隔离实例将 ryn_dashboard_identity 通过 API 改为写，原生设置正常退出 Ryn，再启动同一隔离程序：Dashboard 6/51、59 个 installer，该工具仍为 write、0 候选，助手页无卡片、dismissed 为 null。
- 随后仅停止已核实路径属于该隔离安装的进程，再执行原冻结安装迁移程序并启动：Dashboard 6/55、59 个 installer，该工具仍为 write、0 候选；改回只读后 source=owner。第 8 项真实登录、原生重启和再安装的策略验证通过；隔离再安装使用安装包内同一冻结 helper，没有运行会改变全局安装注册信息的外层 NSIS。
- 为补齐第 7 项，另建 fresh-pristine 的空 home/config/profile，未复制账号、Cookie、配置或工具目录；复制同构建程序并执行同一冻结安装迁移程序，回执含 Dashboard/operations/affiliate。首次启动 Dashboard 为 NeedsAuth、6/0、0 工具，信任指纹绑定有效；已打开扫码入口等待用户登录。之前 fresh 目录和登录完整保留。

### 最终验收结论与正式环境恢复

- 全新 fresh-pristine 空目录已完成用户扫码，原服务器 instance_id 未改变，首次真实发现 Dashboard 为 config/discovery `6/28`，60 个声明只读工具全部来源 installer、候选 0；affiliate 为 `8/29`、60 个 installer、候选 0，workspace 为 `26/27`、7 个 installer，3 个写工具未误标。实际 Agent 页无卡片，`ryn.agent.read-only-hints-dismissed` 为 null。第 7 项通过；此目录没有重导入、复制认证或手动确认工具。
- 隔离新装及再安装使用正式单一安装包内的同一冻结安装 helper，加上同构建桌面/节点二进制；未在隔离目录重跑会修改全局安装注册信息的外层 NSIS。这一验证范围明确保留，不声称外层 NSIS 在第二个目录完整运行。
- PPT 任务完成并停止其 18891 节点后，本轮停止隔离实例，恢复正式安装桌面；确认正式进程与开始菜单入口均指向 `C:/Users/子陵/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Ryn/Ryn.exe`。build-info 为 core `22783aa24406ff3bf19f63d70fe528146a40d96b`、company runtime `93f5046bd36c4636dff6498ac8f03666fff0653c`；独立 PPT 任务更新的技能包另行交付，不将其冒充本次安装包原始技能。
- 正式插件经另一任务重导入、手动确认后来源为 server_hint。本轮先保存加密配置备份，然后仅用本次安装 payload 的公司声明重新执行安装步骤 `trust_installer_connections`；该方法自行执行全部匹配约束，没有直接赋值信任字段，没有重导入或回滚新技能。断言 plugins/skills 完整相同，所有服务器 URL/auth/headers/启用状态/instance/config_generation/owner_write_tools 不变。成功绑定 Dashboard/operations/affiliate。
- 最终正式接口：Dashboard `239/425`，60 个 installer、0 候选；workspace `420/421`，7 个 installer、0 候选，3 个写工具未误标。affiliate 保留历史 owner 36 + installer 4，仍为 NeedsAuth；operations 仍禁用，不扩大本次认证或启用范围。真实助手页无卡片，自动处理模式保留；仅 8791 正常节点监听，18891 与 9333 均未监听。
- 汇总：第 1 项审查、第 2 项指定测试（pytest 207、vitest 31、tsc）、第 3 项正式构建安装、第 4 项安装回执、第 5 项接口、第 6 项两次正常重启与界面/数据查询、第 7 项全新登录、第 8 项主人选择、第 9 项真实模型只读查询、第 10 项快捷方式/build-info 均通过上述范围的验收。另有 7 项边界测试通过。未执行无关已知失败用例的修复；未发送消息或做财务写入。并行任务误操作已单独记录并恢复，不作为产品缺陷。
- 单一安装包 SHA256：`98a7859cd21bc68a00397255e9b5e616bff78dea77e97728767ec50cbe5a4021`；回滚目录 `artifacts/remote-readonly-20261110/rollback-20261110-115034` 保留。隔离目录及登录也保留、不提交。最终文档经任务分支合并 develop 后向正式 `yeogirlyun/rynmesh` 推送；以实际 Git 推送回执核实，不推旧 origin。
