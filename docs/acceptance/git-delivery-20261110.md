# Ryn 与公司插件 Git 交付（2026-11-10）

## 交付范围

Ryn 已完成的功能集成在 `develop`，交付到 `yeogirlyun/rynmesh` 并同步 `yyeogirl/rynmesh`。推送前发现上游另有 5 个提交，已通过合并提交 `0b236bf` 无冲突纳入，保留双方修改，不强制覆盖远端。

公司工作台、Dashboard、联盟营销三个插件的正式交付仓库是老板的私有 `yeogirlyun/gshopper-ryn-node`，分支 `develop`，目录 `integrations/company-app`。本轮将本地构建来源 `d86f942b` 的新增修改增量合入老板仓库，保留该仓库已有独立修复及中心服务。此前上传到 `yyeogirl/chat-backend` 的 `b9385033` 仅为误选的旧交付位置，不是本次正式交付入口。

## 本次功能变化

- Agent 工具报错后不再锁死对话；停止、重启及执行结果不确定时提供继续路径。
- 限制重复目标历史长度，先压缩旧目标，仍超过上下文时接续对话并编号。
- 统一本地与共享模型的图片传递，完善不支持图片和识图拒绝的判断及提示。
- 后台及每轮刷新插件工具目录，避免短时重复读取；让 Agent 知道插件能力及不可用状态。
- 短暂健康检查失败和凭证续期较慢时保持插件页面打开；完善公司登录说明。
- 企业微信审批查询补充能力说明、只读标记；历史读取使用稳定引用，取消 15 分钟读取限制，保留发送有效期及身份校验。
- 默认自动处理模式；WebView2 使用引导安装器，Office 改为安装后下载中文字体版本。
- 额外合入上游已审查修复：设置响应、OAuth 回调地址复用、历史缓存确定性排序、平台资源检查和桌面构建脚本。

桌面更新及插件后台更新仍分别留在 `claude/codex-style-updates-20261109`、`claude/service-pack-updates-20261109`，尚未验收，不属于本次 develop 交付。

## 验证与限制

- 原打包验收：777 通过、4 个旧失败；详情及失败名称见 [打包与安装验收](develop-package-review-20261110.md)。该结果对应原构建，不冒充最新合并源码的测试结果。
- 本次合并后 Python 定向回归：39 通过、13 跳过（平台条件），覆盖设置、插件运行、长任务、JS 资源及桌面工具链文件。
- 前端首次运行工作进程启动超时，3 项通过但有 2 个 worker 错误；后续单线程复测结果追加于下。
- 插件发布目录与本地集成目录 Git tree 一致；没有重新修改业务代码。既有源码存在空白格式警告，保持构建来源一致，没有批量格式化。
- Office 下载地址在此前验收中返回 HTTP 404，仍属未解决交付限制。

## 已安装版本及回滚

本机已经安装 Ryn 0.8.0 / `cc94d2a` 与插件 `d86f942b`，桌面、节点及插件服务健康检查通过。安装目录为 `%LOCALAPPDATA%/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Ryn`；回滚备份位于同级 `Ryn-install-backups/develop-cc94d2a-20261110-002610`。配置、密钥和用户数据保留，详细证据索引见打包验收。

本轮仅合并源码、补充文档并同步 Git，没有重新打包或覆盖安装，因此最新 Git HEAD 与当前安装构建提交不同。运行数据、凭据、日志及安装包保持本地，不随源码上传。

### 最终复测

前端改用单工作线程执行同一组文件（agentTaskStore、nodeUrl、RynAgent），3 个文件 101 项全部通过，退出码 0。首次 worker 启动超时如上保留记录。合并新增改动的 `git diff --check` 通过；新增内容高置信度私钥、GitHub token、AWS access-key 模式扫描无匹配，此检查不代表完整历史安全审计。

### 正式插件远端核验

老板仓库 `yeogirlyun/gshopper-ryn-node` 的 `develop` 已更新至 `88d244026114900aeec3050a17b5e7586681f7a2`，通过 `git ls-remote` 核验。基于该仓库已有 `396a9c8a` 增量集成，保留中心服务及其插件独立修复；合入后 Python 19 项、带网络保护的前端 20 项全部通过。正式入口：https://github.com/yeogirlyun/gshopper-ryn-node/tree/develop/integrations/company-app 。
