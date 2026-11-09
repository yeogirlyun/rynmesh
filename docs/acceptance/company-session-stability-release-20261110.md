# 公司会话与页面稳定性组合交付 — 2026-11-10

四阶段改造已集成、打包并安装到本机。最终安装版完成 **2 小时 1 分 25 秒**连续观察和 **300 次真实页面切换**。会话、iframe 和输入状态保留检查通过；业务接口仍出现短暂身份/授权不可用，不能将本轮验收描述为“所有业务请求零错误”。

## 交付版本

| 项目 | 已提交的 develop 构建版本 |
| --- | --- |
| 主程序正式仓库 | `https://github.com/yeogirlyun/rynmesh` |
| 主程序构建 | `505c754ad28cad23d1ebe3a2be78204563c0dd32` |
| 公司插件正式仓库 | `https://github.com/yeogirlyun/gshopper-ryn-node` |
| 公司服务构建 | `93f5046bd36c4636dff6498ac8f03666fff0653c` |
| 单一安装包 | `D:/code/rynmesh/artifacts/session-stability-20261110/Ryn_0.8.0_505c754_company-93f5046b_full-setup.exe` |
| 大小 | **145,463,657 字节** |
| SHA256 | `BFE194F70388808F97F1C3FA0A3EBE6D3D8B801770B99928DDA0EC197414B831` |

两仓源码在构建时均为干净、已提交的 develop；两个构建提交已推送上述正式仓库。此后的验收文档提交不是二进制构建提交。

安装包仅包含 Ryn、`company-workspace`、`company-dashboard`、`company-affiliate` 及必要共享后台。包清单和隔离安装检查确认插件恰好三个、无 Office payload，已有 Office 安装保持不变。历史 `yyeogirl/chat-backend` 未作为正式交付目标。

## 改造内容

1. **宿主授权通道**：持久化有序日志、全局/单插件撤销水位、服务代次绑定、推送/拉取/ACK、待同步状态和离线登出回执。单插件登出不能借兄弟连接重新取得权限；旧账号离线登出不能撤销后来的登录。凭据正常轮换和严格等价重导入保留登录。
2. **公司浏览器会话**：不透明 HttpOnly/Secure/SameSite=None Cookie，服务端保存摘要。业务请求本地检查成员、版本、scope、到期和撤销；浏览器不回退到旧 localStorage/Bearer 登录。一次性 bootstrap ticket 绑定来源和授权时序。授权通道超过 20 秒未更新时拒绝业务访问，保留 Cookie 和页面。
3. **Dashboard**：有界权限快照和单次后台刷新；401 仅撤销 Dashboard，403 使快照失效，写操作不自动重放。六个代理入口保留本地权限检查。财务签名证明仅覆盖财务目标，不宣称其他上游已部署同一验证器。
4. **页面保留**：LRU 与 DOM 插入顺序分离，避免切换时重建 iframe 浏览上下文；慢服务不销毁已打开页面。隐藏页暂停 outbox/分析面板读取，恢复时合并刷新，防止重复在途读取及旧响应更新；实际发送和上传不变。

## 实机发现后补修

- 同步 SQLite 授权事务曾阻塞 ASGI。授权读写和相关 RBAC 转入线程，授权事务单独采用 100ms 等锁预算；忙时不 ACK、不续授权新鲜度，后台重试。业务事务原预算不变。
- 旧候选版六次原生切换造成六次 OOPIF detach/attach 和六次额外账号读取，即使 iframe DOM 元素身份不变。稳定 DOM 顺序修复后，最终 300 次切换无 detach、无额外 account GET。
- 原生企微身份外层原等 3 秒、底层探测允许 8 秒，产生提前失败。最终版共享单个在途探测，总预算 10 秒；取消/超时不堆积 worker，账号切换重新核验，不延长成功身份缓存、不用旧身份兜底。
- 保留页面后，outbox/分析面板仍有隐藏读取，已补可见性判断和防重入；测试确认隐藏时实际发送仍可完成。

## 隔离检查

| 检查 | 结果与边界 |
| --- | --- |
| 当前主程序组合 | 187 项宿主授权/安装信任/存储/数据策略 + 58 项 OAuth/事故重放，共 **245 项通过** |
| 公司会话、Dashboard、数据库争用综合组 | **122 项通过、0 跳过**；随后原生探测修复安全定向回归 **34 项通过**。两组有重叠，不相加为独立覆盖数 |
| 页面前端 | 原相关组 48 项通过；DOM 顺序修复后三文件 30 项通过，TypeScript 构建通过 |
| 最后隐藏轮询修复 | 正式 owner 源码上的三个文件 **7 项通过** |
| 最终公司冻结服务 | 三次冷启动/强制结束恢复 **2.718 / 1.610 / 1.609 秒**，密钥和加密设置保留，导入前阻断外联 |
| 最终冻结主节点 | 两次独立启动 **7.656 / 4.437 秒**，认证边界和身份/密钥持久化通过 |
| 组合安装包 | 隔离首次及重复安装通过，身份/密钥/禁用状态保留，三个插件页面与公司健康检查通过 |

SQLite 检查含持续 20 秒写锁、健康接口保持响应、授权新鲜度到期拒绝、解锁恢复且 Cookie 保留。原生探测检查含慢探测并发、取消、重复超时、账号切换与健康接口响应。测试使用合成数据与隔离网络边界，没有生产财务写入。

历史 Tracker 和旧演示 UI 断言在修改前也失败，已保留基线日志；未为通过旧断言而扩大业务修改范围，不能宣称整个前端测试库全绿。

## 最终实机观察

时区均为 Asia/Hong_Kong。观察从 **04:14:48.665 到 06:16:13.807**，合计 **7,285.142 秒**。使用实际安装的 Windows WebView2，点击由原生 CUA 完成；CDP 仅作被动协议和时序记录。冷启动与受控服务重启不混入该连续观察窗口。

| 指标 | 结果 |
| --- | --- |
| 原生可信页面切换 | **300 / 300 到达 ready**，分三轮进行，含长时间闲置后继续切换 |
| 点击到活跃 iframe ready | P50 **36.5ms**，P95 **59.8ms**，最大 **67.2ms** |
| iframe 浏览上下文 detach | **0**，三个 iframe 身份保持不变 |
| 额外账号 GET / bootstrap | **0 / 0** |
| 两个健康接口 | **478 / 478 成功**，最大采样耗时 441ms |
| 输入状态 | 未发送草稿与未提交筛选值在两小时后仍保留；最终均已清理 |
| 窗口最小化 | 最小化至少一分钟后恢复，草稿和登录保留 |
| Cookie | 跨升级、受控服务重启和两小时观察摘要一致；HttpOnly/Secure/SameSite=None |
| 受控公司服务恢复 | 最终版本健康接口 **7.129 秒**恢复；这是健康恢复时间，不是逐像素页面恢复时间 |

切换时长是可信点击到匹配活跃 iframe 在动画帧报告 ready 的时间，不是网络延迟或逐像素白屏时间。结合零上下文重建、零额外账号读取和实际输入保留判断，未单独以 DOM 元素身份作为充分证据。

### 不能省略的短暂失败

连续观察中共 **14 次 HTTP 业务失败**，每次均能找到同一路由后续成功响应：

- Dashboard 同步状态读取：3 次 `dashboard_desktop_identity_unavailable`、1 次 `authorization_sync_unavailable`，均为 503。
- 原生企微读取：3 次 `wecom_identity_unavailable` 503、7 次 `wecom_login_unavailable` 409；集中出现在 05:40–05:42，随后状态/会话/消息/发送回执读取恢复。
- 另有 6 次同步状态请求取消（`net::ERR_ABORTED`），没有仪器读取失败。

这些失败没有清除公司 Cookie、要求重新扫码、销毁 iframe 或丢失测试草稿。未以过期身份绕过授权检查。原生探测慢以及间歇性身份/授权不可用的全部系统级根因没有在本次定位，业务可用性仍有改善空间。

最终版本冷启动阶段曾超过 16 秒，显示“插件页面尚未完成初始化”后自行恢复，伴有授权同步新鲜度不足的 503。不能把暖切换结果推广为所有冷启动均快速。最后正常重启后的三页检查均正常识别账号，未再次观察到该提示。

## 本机安装与回滚

本机安装目录为 Codex 打包应用用户缓存下的：

`C:/Users/<user>/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Ryn`

准确未脱敏路径、原数据目录和安装结果保存在本地 `artifacts/session-stability-20261110/local-install.json`。升级保留原数据路径、身份密钥和其他插件配置，903 个公司运行文件逐项校验通过。主节点及原生工作器哈希与构建一致；Ryn.exe 仅有预期的三个字节 NSIS bundle marker 差异。

06:16:54 开始正常重启，06:17:18 两个健康接口均 200，**9333 调试端口已关闭**；06:18:42 完成三个插件的真实界面账号识别检查。程序留在正常运行状态。

回滚材料保留在同一本地交付目录：

- 原任务开始前版本：`rollback-20261110-022238`（主程序 `cc94d2a3970ce6243894a19aab05af1d9fb55064`）。
- 最后一次升级前版本：`rollback-20261110-040848`。
- 回滚工具：`rollback-local.ps1 -Backup <上述已验证目录的绝对路径>`。恢复程序、运行服务配置和注册信息，保留当前用户数据；数据库备份单独保留，不自动覆盖当前数据。
- 旧候选安装包已移至 `superseded-installers/`，不是本次交付包。

## 本地证据索引与范围

本地目录：`D:/code/rynmesh/artifacts/session-stability-20261110/`。

- 版本与安装：安装包同名 `.json`、`build-source-record.json`、`local-install.json`、`installed-core-hashes.json`、`normal-restart.json`。
- 最终观察：`installed-webview-stability-final.json`、`stability-summary.json`、`webview-phases.jsonl`、`final-ui-retention.json`、`cookie-preservation-summary.json`、`native-minimize-final.json`。
- 冷启动/服务恢复：`installed-webview-final-startup.json`、`controlled-service-restart-final.json`。
- 测试与构建：`host-combined-505c754.json`、`owner-python-db-final.log`、`owner-native-gate-final.log`、`owner-visible-polling.log`、`company-runtime-qa-93f5046b/verification.json`、`packaged-node-smoke.json`、`full-package-visible-polling.log`。

本次使用已有合法登录，没有执行新的扫码登录、真实消息发送或财务写操作。两小时观察不替代 24 小时及跨机器验证。Git 仅保存这份脱敏结论，不提交凭据、Cookie、数据库、聊天正文、原始运行日志或构建产物。
