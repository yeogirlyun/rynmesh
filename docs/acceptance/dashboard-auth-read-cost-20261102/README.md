# Dashboard 身份验证延迟修复

## 问题与证据

用户反馈 develop 安装包仍停留于“正在验证财务访问身份…”。运行版本为主程序 `18515d9`、公司插件 `80761ff`，包含前轮性能修复；并非遗漏插件升级。前轮记录中冷启动 desktop session 仍为 19.28 秒，不能以重复验证优化完成代替首次访问性能验收。

本轮安装前实测：desktop session 5.371 秒，finance session 0.852 秒；独立 host overview 0.607 秒，身份 tool-query 3.037 秒。只读诊断未修改财务业务数据。

源码性能采样显示单次独立连接身份证明读取插件状态 173 次，每次复制整个插件注册表；OAuth 状态查询还复制与身份无关的完整工具 schema。采样器本身有额外开销，采样耗时不作为页面实际耗时。

## 修复

主仓库提交 `3348a33` 已集成 develop。仅四个只读投影在锁内借用经文件变更检查的缓存，返回结果仍深拷贝隔离；写路径仍默认复制完整状态。OAuth 枚举省略工具 schema，其余身份绑定、登录状态、撤销、插件启停、配置代次与发送前检查全部保留。没有延长权限缓存或跳过鉴权。

公司插件本轮无需代码修改，继续使用已经集成其 develop 的 `80761fffac62acd9a4365c25585cce463b4d43b6`。

## 检查

- 145 项 store、OAuth、transport、plugin view 和 session revision 测试正常通过（97.91 秒）。新增测试检查返回对象不会污染缓存，以及另一 Store 实例禁用插件后立即可见。
- 首轮所有用例通过，但 pytest 清理旧系统临时目录失败，进程退出 1；保留此失败事实。第二轮采用项目独立 basetemp，退出 0。
- 修改后源码实测独立连接身份证明 2.617 秒，复用连接 0.349 秒。该数据不是安装包验收。

## 安装包与实机结果

- 构建提交：`3348a335935be7696b5eec67fc85fd852caae62b`，develop 已提交源码，tracked workspace 干净。Python 节点与 Tauri 主程序/NSIS 重建成功；前端资源与原生工具源码相对上一轮 `18515d9` 未变，复用已构建资源。构建 Python 环境中的两个修改文件逐一核对源码哈希一致。
- 安装包：`artifacts/dashboard-auth-read-cost-20261102/Ryn_0.8.0_x64-setup.exe`；SHA-256 `DC7120BD1ACF243CE3F72EB524FA8AFF52CA5784239F0E7EB5C1FDAC74DE527C`。文件留在本机，不提交。
- 已更新物理 `C:/Users/子陵/AppData/Local/Ryn` 及 Codex 重定向目录 `C:/Users/子陵/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Ryn`。实际启动后者。三项可执行文件均核对哈希，主程序应用与 NSIS 相同的三字节 UNK→NSS 标记；外部 build receipt 一并更新。
- 原程序与配置备份：`.codex-tmp/dashboard-auth-profile/backup/0`（物理目录）、`backup/1`（重定向目录）。回滚时先退出本项目进程，恢复对应目录三项可执行文件、build-info 与 receipt，再启动 Ryn；不覆盖用户数据库和配置。
- 仅结束核实位于这两个 Ryn 安装目录的主程序、节点和公司服务；未修改桌面配置文件（前后哈希一致），未更换公司插件注册信息或用户数据。
- 新主程序 PID 3392，节点 PID 30868/41308，公司插件 PID 23560。初次探测时服务尚未监听，随后启动完成并健康检查 200；公司服务 `/api/local/runtime/health` 0.054 秒。
- 安装包首次接口测量：desktop session 3.415 秒，finance session 1.205 秒；后续分别 0.536 秒、0.450 秒；公司数据读取 0.580 秒，均为 200。时间包含网络波动，不是所有页面端到端加载时间保证。
- 实际 Ryn 窗口：进入 Dashboard 后日记账显示数据，银行账户切页显示数据；本轮未见此前 `legacy_finance_backend_unreachable` 提示。该提示在安装前确实出现过，单次成功不能证明上游永不超时。界面仅进行只读导航，未操作更新、保存、发送或财务写入。
- UIA 文本曾与实际画面不一致，实机验收依据新截图；包含财务数据的截图不纳入提交。
