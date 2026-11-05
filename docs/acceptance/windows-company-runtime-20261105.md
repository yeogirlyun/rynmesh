# Windows 公司插件运行验证（2026-11-05）

主程序由 develop `1ef37795f82b80e0b08df3d605682bceece22ad8` 的干净已跟踪源码构建，包含连接阻塞修复。公司插件运行代码为其 develop `062bd93035ed2b7d66a79f6c78ff154085b75a6e`，该仓库后续 `11b2f9e` 只补测试与证据。

本机测试：后端缓存、目录刷新、内容扫描及 OAuth 共 66 项通过；前端 polling、peerPolling、liveNodeClient、directoryModel 共 25 项通过。完整 Windows 构建成功。安装包 SHA-256：`80f0879a61881f9738519f3325d260439d5a1834c7abd03118c11cce15a1406b`。

## 实际安装与纠正

实际安装目录为 `C:/Users/子陵/AppData/Local/Ryn`。升级前文件保存在本机私密验收目录 `D:/code/_build-tools/wecom-history-evidence-20261105/pre-install/main`，配置、插件注册及用户数据保留。

第一次安装后，仅检查进程路径不充分：由 Codex 启动的桌面进程把普通 C 盘路径解析到应用隔离副本，仍载入旧后台。新壳和实际安装目录已更新，并不代表该副本也已更新。公司账号依赖的 host 路由返回 SPA HTML，插件账号接口返回 503，界面持续显示“正在核对公司账号”。因此前次重启不构成有效运行验收。

修正时先备份隔离副本至 `pre-install/shadow-main`，关闭核实属于本项目的主壳、后台及公司服务并等待退出，再将同一 develop 构建的三份可执行文件及构建凭据同步到两个路径视图。未修改授权、配置或数据库。两个路径视图的后台 SHA-256 均为 `2301e8ae1c940ea32ede01a01adf69f9a6e7e59d42690ca9fa6d1e4ba2bdd137`。

修正重启后，`/api/local/agent/extensions/company/account-action` 实际返回 JSON；公司账号接口返回 `logged_in`，一次实测 38 ms。Windows Ryn 的 Dashboard 已实际显示已认证及业务导航，不再停留在账号核对。这里只验证页面恢复，没有触发财务写入。

## 剩余验收

工作区仍为 `needs_authorization`，真实历史工具发现与调用尚未验收。历史需求不能标记为完成；Dashboard 授权和工作区授权不能混同。主壳 SHA-256 为 `4924688a9446b9edaa330f15a9ef1ea047a90fb20dad9930015cec40b62a1624`。

回滚须停止同一进程树，分别恢复实际目录及隔离副本的升级前文件，保留现有配置与数据。不要只恢复其中一个路径视图。后续安装验证须检查响应内容确为 JSON 及真实功能路由，而非仅 HTTP 200、版本号或进程显示路径。
