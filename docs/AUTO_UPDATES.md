# Ryn 软件与插件更新

## 软件订阅

桌面端固定检查：

`https://github.com/yeogirlyun/rynmesh/releases/latest/download/desktop-latest.json`

使用 Tauri updater 的签名安装包。前端、原生桌面壳和 Python sidecar 一起更新，不调用 pip。原来的 `latest.json` 和非打包 Python 节点更新流程保持独立。

默认启动后 30–59 秒检查，之后每 6 小时（带偏移）检查并下载。自动下载偏好按桌面配置目录持久化；关闭后仍可手动检查、下载。下载完成显示全局提示和设置页状态。安装必须点击“重启并更新”并确认已保存；有原生编辑器窗口、插件调用、Agent 运行、文件传输、模型部署或共享 AI 任务时拒绝安装。安装前停止本机 sidecar，安装启动失败时尝试恢复 sidecar。下载文件按版本缓存，安装时 Tauri 再次验证签名。

这不是全应用崩溃回滚器：尚未实现跨安装器的独立恢复启动器。新版本安装成功但运行异常时，使用前一版签名安装包修复。数据迁移仍须保持向后兼容；本更新流程不覆盖用户数据。

入口：设置 → 高级设置 → 软件更新；也可打开 `/settings/advanced?section=updates`。

## 首次配置发布签名

1. 在安全的发布机器运行 `npm run tauri signer generate -- -w <仓库外的密钥路径>`，保存私钥和密码。不得提交私钥。
2. 老板仓库配置 Actions variable `RYN_DESKTOP_UPDATE_PUBLIC_KEY` 为生成的公钥文件内容，配置 secrets `TAURI_SIGNING_PRIVATE_KEY` 和 `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`。
3. 编译时 `RYN_DESKTOP_UPDATE_PUBLIC_KEY` 被嵌入二进制；运行时不从网络或普通配置文件更换信任公钥。未配置公钥的开发构建仍可启动，但更新界面明确显示未配置，并禁止下载/安装。
4. Windows 本地签名构建：设置上述环境变量，运行 `webapp/src-tauri/scripts/build-windows.ps1 -Updater`。macOS 构建额外传 `--config src-tauri/tauri.updater.conf.json`。

签名更新密钥不同于 Windows Authenticode / macOS 开发者签名。后者仍按平台发布流程配置。

## 发布桌面版本

更新 Python、Tauri JSON、Cargo 和 npm 的版本号并创建稳定版本 tag（如 `v0.8.0`）。Tag 工作流先生成草稿 Release，避免 Python 包先上传时更新地址已经切到不完整发布。

在老板仓库手动运行 **Build signed desktop update**，指定该 tag：

- 构建 Windows x86_64、macOS Intel、macOS Apple Silicon。
- 生成 `.exe` / `.app.tar.gz` 和对应 `.sig`；桌面更新不下载 DMG。
- `scripts/prepare_desktop_update.py` 检查三个平台齐全、版本一致，生成 `desktop-latest.json`。
- 上传到同一 tag 的草稿 Release；不会自动发布，不会覆盖已发布版本。

验证三个平台安装包后再在 GitHub 发布草稿并设置为 Latest。后续客户端使用同一个固定订阅地址。尚未发布 `desktop-latest.json` 时检查失败是正常状态；不能用原来的 Python 清单替代。

## 公共 / 公司插件

当前支持现有可下载的 Craft 静态 Web 插件。内置 resume、recruit、desk、nas 仍随主程序更新。独立的原生自动化组件继续使用其已有的固定版本，不被 Web 插件更新包替换。Agent 的 Skills/MCP 扩展不走此安装器，远端 MCP 服务自身发布也不需要客户端下载代码。

在软件更新页选择已安装插件，配置：

- **公共 Git Release**：GitHub Release 的 JSON 资产地址；默认建议老板仓库的 `<plugin-id>-latest.json`。也可指定发布者自己的 GitHub 仓库。包必须来自同一仓库的固定 tag Release。
- **公司后台 URL**：公司 HTTPS 清单地址。包及重定向必须保持同源。可在服务进程环境里配置 `RYN_PLUGIN_TOKEN_<NAME>`，界面只保存环境变量名；Bearer Token 不写入更新状态，也不发往其他域名。
- **发布者公钥**：独立信任配置，不能从远程清单自我授权。由发布者提供 Ed25519 原始 32 字节公钥的 Base64。
- **空闲自动更新**：每个订阅单独开启；未配置订阅、不曾安装的插件不会自动下载。

启动后每分钟调度，距上次检查超过 6 小时才拉取清单。发现兼容版本后自动安装，已有调用租约会阻止切换；已经启动的编辑器服务保守地视为使用中（包含浏览器中可能仍有的未保存文档），关闭文档并重启 Ryn 后才能自动切换。不强制关闭编辑器。

清单采用 `rynmesh.crypto.SignedPayload`：`alg`、`public_key`、`signature` 和 `payload`。签名覆盖 canonical JSON，payload 示例：

```json
{
  "kind": "ryn.plugin.release",
  "schema": 1,
  "id": "wordcraft",
  "version": "0.8.0",
  "runtime": "static-web",
  "permissions": [],
  "min_host_version": "0.7.0",
  "max_host_version": "0.9.0",
  "url": "https://github.com/OWNER/REPO/releases/download/v0.8.0/wordcraft.zip",
  "size": 123456,
  "sha256": "64 lowercase hex characters"
}
```

包继续遵循 Craft 的 ZIP 目录结构和安全解包规则：只有一个可识别的 `index.html` 入口，保留许可证；文件大小、解包大小、文件数量均有限制。不支持预发布版本、额外权限或原生可执行代码更新；这些需要主程序升级并审核运行时支持。

生成已签名清单（从仓库根目录，在安装了项目依赖的 Python 环境运行）：

```sh
python -m scripts.sign_plugin_update --slug wordcraft --version 0.8.0 \
  --archive /release/wordcraft.zip \
  --url https://github.com/OWNER/REPO/releases/download/v0.8.0/wordcraft.zip \
  --manifest-url https://github.com/OWNER/REPO/releases/latest/download/wordcraft-latest.json \
  --key /secure/plugin-ed25519.key --min-host 0.7.0 --max-host 0.9.0 \
  --output /release/wordcraft-latest.json
```

公司地址添加 `--kind company` 并改为公司同源 URL。密钥文件是原始 32 字节 Ed25519 私钥，不是 Tauri minisign 私钥；公钥会随工具输出，但仍必须通过可信渠道配置。公司后台直接返回生成的完整 JSON，包返回 ZIP 字节即可。

## 安全切换与恢复

插件包先下载到临时目录，验证签名、宿主兼容范围、大小、SHA-256 及所有解包文件，之后才原子更新活动版本记录。保留旧版本与启用/禁用设置。更新失败保持旧版；下次启动发现新包文件损坏，尝试验证并恢复上一版。设置页可手动恢复，恢复后坏版本被隔离，不会无限重装。

Web 包的健康验证是入口与文件完整性验证，并不等价于所有 JavaScript 功能的端到端验证。发布前仍应运行编辑、保存和导出验收。

本地 API 均通过既有 `local_control` 认证：

- `GET /api/local/plugin-updates`
- `GET /api/local/plugin-updates/readiness`
- `POST /api/local/plugin-updates/{id}/configure`
- `POST /api/local/plugin-updates/{id}/check`
- `POST /api/local/plugin-updates/{id}/apply`
- `POST /api/local/plugin-updates/{id}/rollback`

配置存在节点数据目录 `plugin-updates.json`；活动/上一版记录存在 `craft-plugins.json`；代码位于原插件外置缓存目录。更新过程不删除插件用户数据。
