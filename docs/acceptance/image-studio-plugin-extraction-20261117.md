# 做图应用拆分验收 — 2026-11-17

## 交付边界

做图应用迁到本地独立仓库 `D:/code/ryn-image-studio`（develop，未设置远程/未推送）。
Ryn 保留节点网页通道、权限、通用 Python 插件加载和通用管理页承载；移除做图专用后台、页面、翻译和打包声明。
Agent 的通用工具与 Codex 接入不属于本次共享网页应用拆分范围。

插件声明 `rynmesh.node_sites` entry point，只有 `<home>/site-plugins/enabled.json` 显式启用的本地包才会加载。
插件加载失败隔离，不阻断节点启动。无处理器的站点不再向同事宣告可用；原站点设置保留。

## 验证

- Ryn Python：`test_node_site_plugins.py`、`test_node_sites.py`、`test_node_site_status_timeout.py`，14 passed。
- Ryn TypeScript：`tsc -b --noEmit` 成功。
- Ryn UI：NodeSites 和 NodeSiteOwner，6 passed。原 worktree 的 Vitest worker 多次启动超时；在独立 UI 测试目录运行，已逐文件核对受测源码与工作树字节一致。
- 独立插件 Python：13 passed，含两节点传输、旧图片/历史/参考图/队列兼容和管理页授权。
- 独立插件管理 UI：3 passed；Vite 构建成功。
- 独立 wheel 在全新临时节点中安装，由 Ryn 实际加载器读取包内 entry point，核实模块来自独立安装目录、页面/API 可用、匿名业务访问被拒绝。
- 正式节点数据仅做只读 SQLite 快照；用新插件打开快照后，14 条任务与 19 个文件保持原值/原哈希。正式数据未修改。

## 使用与发布

先按独立插件 README 构建并安装 wheel，再更新兼容 Ryn；保留旧桌面程序与插件启用清单以回滚。
同事端无需安装做图插件。站点 ID、远端页面地址、管理路由、数据库路径和图片路径保持不变。
本次完成源码集成和独立插件包验收；未替换正在运行的桌面二进制，也未触发正式节点任务。
