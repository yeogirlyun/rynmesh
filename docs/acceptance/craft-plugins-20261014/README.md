# Craft 插件管理接入（2026-10-14，Windows 本地开发）

本批在当前 `feature/ryn-integrated-workspace` 工作目录中开发，沿用先前接入的固定版本、SHA-256、解压校验、好友授权和操作审批。没有升级 MS-1/MS-2、重建节点身份或改动 Exchange 结算。源码目录已有大量其他工作，本批未整体提交或推送这些修改。

后续已将 Resume、Recruit、Desk 和 NAS 接入同一生命周期，新增验证与界面见 [统一插件管理验收](../unified-plugins-20261014/README.md)。下方测试数字及早期截图属于首批 Craft 接入记录。

## 已实现

- `/apps` 使用已确认的工具架 UI，包含办公、业务应用、设计图像、音视频、存储分组。`/settings/plugins` 统一管理安装、启停、详情与卸载。Resume/Recruit/Desk 的工作区及好友授权保留在 `/apps/workspaces`；NAS 管理行已合并进同一列表，连接设置保留在 `/nas`。
- Windows 安装器的小型可选组件页默认勾选 PDF、Word、Excel、PPT，支持取消选择。它只记录 HKCU 中的安装偏好，**不在安装包中包含编辑器，也不在后台自动下载**。首次打开桌面应用时进入应用设置页；用户确认后按选择逐项同步下载、校验和原子发布，失败可重试且保留已成功项目。静默安装不下载插件；升级保留原选择。
- 程序包位于 `%LOCALAPPDATA%/Ryn/plugins/<节点标识哈希>/<应用>/<固定版本>/`，注册状态在该节点的 `craft-plugins.json`。其他平台使用相应用户数据目录。安装包携带轻量目录、管理器和适配器代码，编辑器 ZIP/WASM 与浏览器文档均不放进该包。
- 无需重启即可安装、启用、停用、卸载。安装和启用不启动监听器；明确确认打开后才启动独立的 loopback 静态服务。停用/卸载关闭对应监听器，活动适配器 lease 阻止并发卸载。节点重启后不自动恢复编辑器监听器。
- 卸载仅删除当前固定版本程序包，默认保留 Ryn 插件偏好、浏览器存储和用户导出/任务文件。可选清理 Ryn 插件偏好，不能将它误认为清除上游浏览器存储。
- 启动确认展示应用、版本、节点 ID 和完整目标地址；两分钟的一次性确认 ticket，消费时重新校验文件、启用状态与端口。前端拒绝错误节点/版本/地址。Windows 原生窗口入口只接受白名单 app ID，通过本机控制 API 再检查运行状态，并由固定 app→端口映射生成地址；不接受模型 URL 或 shell 命令。远端控制页面暂不提供 loopback 启动，避免打开访问者机器上的其他服务。
- 每个编辑器保持独立源站和原有 CSP、Host 检查及只读静态边界。没有新增上传、文档 JSON 存储、自动更新、系统守护或自动启动注册。
- 新增完整中英文 `craft.*` 资源，Windows 可选组件页也有中英文。全局语言审计另发现两处既有遗漏（设备 OS 标签、Agent 工作目录示例），本批仅补齐对应文案翻译。
- PhotoCraft 的字体回退/语言限制在选择、应用卡片、启动确认及详情中明确提示；没有创建伪造的字体清单。本次真实 WordCraft 浏览器截图还观察到中文缺字，已增加独立的中文字体限制提示；Ryn 管理界面的中英文语言包不能替代上游字体覆盖。

## Agent 的实际范围

插件启用后默认向 Agent 提供其**已实现的能力**，没有“允许 Agent 使用”开关。统一发现接口为 `ryn_list_plugins`、`ryn_plugin_info`，兼容 `ryn_list_craft_plugins` 和 `ryn_craft_editor_info`。停用/卸载会撤下本机能力并拒绝旧调用。业务操作继续走 `ryn_node_app_action` 及逐项审批；好友目录只公布提供节点当前启用且已授权的应用，不扩大原好友授权。

**文档编辑适配器尚未实现。** 现有固定办公构建没有在本批接入可验证的文档命令，不能把示意中的合并 PDF、生成 Word、填写 Excel、生成 PPT 当成可调用功能。前端会明确显示当前仅有版本、来源和格式查询；信息查询不等于打开编辑器，也不等于处理完成。

后续每项编辑工具需要真实上游命令/API、受限文件引用与大小限制、现有执行审批、输入/输出哈希和操作回执，以及真实文件的导出/重开验证。本批没有用通用 shell、安装器或桌面控制来替代这些接口。

## 已验证

- 专项后端 58 项通过：`test_craft_plugins.py`、`test_craft_host.py`、`test_node_apps.py`、`test_antigravity_usage_accounting.py`；覆盖真实静态 HTTP、所有者校验、请求容量上限、下载失败原子性、固定哈希、活动 lease、启停、票据过期、端口冲突、卸载保留文件/偏好、Agent 动态注册与旧调用拒绝，以及安装器偏好不触发下载。
- 全前端 107 个测试文件、737 项测试通过。额外受影响的插件、Node Apps、NAS、语言包、Agent/设备页面测试也通过。TypeScript/Vite 生产构建通过。
- 本地重新下载 WordCraft v0.6.1，SHA-256 `85e458cb5adaa75afa871a9858c735ef7fd1e05741c6bf5d444dc09a1eb29e0a` 与固定值一致。实际控制 HTTP→启动预览→用户确认→独立浏览器窗口→WASM 初始化、1280×900 画布通过；停用关闭监听后，重新启用并再次打开通过。无控制页或编辑器 JavaScript 错误。
- WordCraft QA 缓存在用户数据目录 `Ryn/plugins/review-craft-20261014`，不在仓库内。所有本批临时浏览器、开发/控制/编辑器监听器已关闭。
- 本地 Python wheel 构建通过；检查 wheel 中包含轻量管理器且不含编辑器 WASM/ZIP。NSIS 组件页与钩子用本机 `makensis` 编译通过，中英文 LangString 无缺失。
- Ruff（本批 Python 文件）及 Git whitespace 检查通过。

真实下载和浏览器证据见 [pinned-word-result.json](pinned-word-result.json)、[http-ui-result.json](http-ui-result.json)、[打包检查](package-result.json)、[启动确认](real-word-confirmation.png)、[实际编辑器](real-word-editor.png)。这些证明安装/启动/热启停，**不证明文档编辑或导出正确性**。实际编辑器截图中的中文缺字已作为限制记录，而非正确性验收。四件套界面截图的目录状态使用测试数据，见 [应用页](apps-implemented.png)、[管理页](plugins-implemented.png)。

## 未完成或未宣称

- 未产出新的完整 Windows/Tauri 安装包：本会话环境找不到 Cargo/Rust 编译器。NSIS 钩子单独编译、Web 构建和 Python wheel 不能替代完整桌面打包验证。
- 扩展 Agent 后端回归有 3 项上下文预算/压缩测试失败：`test_compaction_preserves_owner_constraints_closed_tool_groups_and_original_history`、`test_compaction_fails_closed_on_tool_call_from_summary`、`test_followup_uses_compacted_projection_instead_of_archived_transcript_size`。这些用独立 `Tools` 测试替身，不调用本批 Craft 适配器；本批没有修改 runtime/tool_menu 的既有工作。不能宣称完整后端全绿。
- 未验证 12 个编辑器的“新建→修改→导出→重开”文件闭环，未完成字体包来源核实，未部署 MS-2 网络静态服务，未验证远程浏览器 HTTPS/WebGPU/文件权限。
- 未接入 Rynmesh/NAS/Exchange 文档存储，未增加收费、抽成、铸币或奖励；原有计费流程不变。

## 复验

在 checkout 根目录的独立 Python 环境中运行：

```powershell
$env:PYTHONPATH = 'D:\code\rynmesh'
.\.venv\Scripts\python.exe -m pytest tests/test_craft_plugins.py tests/test_craft_host.py tests/test_node_apps.py tests/test_antigravity_usage_accounting.py -q
npm --prefix webapp test -- --maxWorkers=4
npm --prefix webapp run build
```

测试截图和缓存不得替代用户文档闭环验收；也不要提交上游 WASM/ZIP 或本机插件缓存。
