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
- 前端：合并到 develop 后在主工作区跑相关 vitest（见下）。
- 未做：主人电脑安装新包后实际看卡片消失；真实模型任务。
