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
