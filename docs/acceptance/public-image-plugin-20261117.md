# 独立公开做图插件交付记录

日期：2026-11-17。用户授权直接实施，公开插件放老板的 `rynmesh` 仓库；Gshopper 保持私有安装和私有更新。

## 交付位置与版本

- 主程序与插件源码：[yeogirlyun/rynmesh](https://github.com/yeogirlyun/rynmesh)，`develop`。
- 独立插件源码：`plugins/image-studio/`。
- 功能提交：`a3948f12`；已合入远端同期完成的改动，再集成到本机 `develop`。
- 构建源码提交：`9e90324112dc27df150fa7ab502415411e960a8e`。构建使用干净任务工作树，其 HEAD 与当时集成分支 `develop` 一致。
- 公开目录发布提交：`464ab369`，`plugins/catalog.json`。
- 插件版本：`1.0.0`，宿主接口：`host_api = 1`。
- [正式插件 Release](https://github.com/yeogirlyun/rynmesh/releases/tag/plugin-image-studio-v1.0.0)。
- 安装包：`image-studio-1.0.0.zip`，27,049 字节。
- SHA-256：`4dac1eeb18e901aa214ac5704e5ef76f5fbe8f08f4aa3207cdab026a58b5942f`。
- 发布时使用 `make_latest: false`；复核 Ryn 主程序的 latest 仍为 `v0.9.1`。
- 发布私钥、ZIP、测试数据均在源码仓库外；仓库仅保存发布公钥及签名目录。

## 实现范围

原 `rynmesh/image_studio` 页面与后台移入独立插件；主程序不再启动内置做图。主程序新增公开插件签名目录、独立包下载校验、运行管理、更新、回退和本机隔离页面入口。

插件页支持发现、安装、已安装版本、最新版本、手动检查更新、更新说明、启停、卸载、回退，以及做图本机打开和现有分享管理入口。

公开下载不使用公司登录和私有更新源。Gshopper 仓库、安装包配置和公司更新服务未修改。做图跨电脑分享沿用现有公司节点网页授权，不新增匿名公网服务或任意好友分享。

旧数据目录保留，首次迁移关闭旧分享开关，安装后由用户重新开启。更新保留原有分享开关和范围，卸载保留作品并关闭分享。有排队或执行任务时禁止切换运行版本。

## 验证

| 检查 | 结果 |
| --- | --- |
| Python：node sites、image studio、public plugins、unified plugins、runtime results | 51 passed |
| 前端：PublicPlugins、ImageStudioOwner、NodeSites、CraftApps、UpdatesPanel | 54 passed |
| TypeScript `tsc -b --noEmit` | 通过 |
| Vite 生产构建 | 通过；有既有大包和静态/动态导入提示 |
| 新增及拆分 Python 文件 Ruff | 通过 |
| `git diff --check` | 通过 |
| 错误签名、错误发布公钥、下载包篡改、安装文件篡改 | 拒绝安装/启动 |
| 版本更新、回退、坏版本启动失败恢复、重启复用已安装包 | 通过 |
| 本地接口未经 owner 授权 | 拒绝访问 |
| 本机使用且不接公司服务、传入伪造身份头 | 使用宿主生成的 owner 身份 |

先前测试进程退出遇到系统临时目录 `pytest-current` 权限异常；改用本任务独立 `--basetemp` 后，最终 51 项正常退出成功。

扩大检查 `tests/test_plugin_updates.py` 时发现原有网页插件更新夹具将最高宿主版本固定为 `0.9.0`，当前主程序已为 `0.9.1`，导致期望有更新的断言失败。其中 `test_signed_update_survives_restart_and_retains_disabled_state` 在未合入本任务的原工作区单独重跑同样失败。本次未修改该旧更新器或绕过其兼容性校验，不将这组测试记录为通过。

## 真实公开下载验收

从线上签名目录获取做图条目，从正式 GitHub Release 无凭据下载 ZIP，在隔离数据目录安装实际发布包：

- 下载包哈希和签名目录校验通过，实际安装版本 `1.0.0`。
- 使用真实本机网页网关请求 `/`、`/app.js`、`/api/me` 成功，页面有 CSP。
- 身份为本机 owner；管理状态接口成功。
- 停用插件后，已有页面地址返回 403；重新启用后恢复 200。
- 分享默认关闭。
- 验收目录：`D:/ryn-plugin-releases/image-studio-1.0.0/live-acceptance/`，仅测试用途，不是用户正式安装目录。

本任务未执行真实 Codex 出图；生成、队列和图片处理使用原做图回归测试中的替身生成器验证。未重新进行两台真实电脑分享验收，节点间分享由现有协议测试覆盖。

## 本机客户端状态与保留事项

**本次没有打包、替换或重启用户正在运行的桌面 Ryn。** 新的插件管理入口需要包含本任务主程序代码的构建；已发布的旧版 `v0.9.1` 安装程序不会凭插件目录自动获得这个入口。

用户现有桌面路径 `C:/Users/子陵/AppData/Local/Ryn/Ryn.exe` 未修改，现有用户数据、凭据、配置和公司插件服务未修改。正式本机升级应另按 `develop` 已提交源码构建、保存回滚版本并验证健康与公司插件服务。

主工作区已有未提交的 `docs/acceptance/installer-read-only-trust-20261110.md` 保留，合并前后 SHA-256 相同。未提交本地日志、数据库、账号、密钥或构建产物。

面向使用者的完整说明：[公开与私有插件安装、更新和发布说明](../PUBLIC_AND_PRIVATE_PLUGINS.md)。
