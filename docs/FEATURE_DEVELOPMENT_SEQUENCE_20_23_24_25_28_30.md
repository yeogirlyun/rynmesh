# Rynmesh 下一阶段开发顺序与验收总路线

状态：可执行计划  
覆盖 Issue：#28、#24、#25、#23、#20、#30  
制定日期：2026-09-02

## 1. 总体顺序

主线按以下顺序开发和验收：

```text
#28 Transport POST
  ↓
#24 Private AI Provider/模型切换
  ↓
#25 Ask about this item
  ↓
#23 Private AI 流式输出
  ↓
#30 Friend Mesh（安全设计批准后）
```

#20 Linux 桌面包不依赖上述产品主线，可以从第一天并行开发：

```text
主线：#28 → #24 → #25 → #23 → #30
并行：#20 Linux desktop package
```

顺序依据：

- #28 先统一 LLM Peer POST 的 Transport 路径，避免 #23 和 #30 新增第二、第三套网络旁路。
- #24 先解决会话与 Provider/模型的正确绑定，#25 才能安全地把长文章交给合适上下文窗口的模型。
- #25 先完成稳定的文章上下文、截断和 Prompt 边界，之后 #23 只改变输出交付方式，不同时改变输入语义。
- #23 扩展 Peer streaming 协议，必须建立在 #28 的 Transport 能力之上。
- #30 涉及邀请凭证、好友 ACL、撤销和 Private AI 授权，安全设计必须先批准。
- #20 主要涉及 Rust/Tauri/PyInstaller/CI，与服务协议主线基本独立。

## 2. 通用开发规则

所有 Issue 都必须满足以下通用规则：

- 一个 Issue 使用独立分支和可审查 PR，不把多个大型需求混进同一提交。
- 从最新 `upstream/main` 开始；依赖任务必须先合入或明确以其已审查提交为基线。
- 保留现有用户修改和不相关工作树内容。
- 新增网络或存储协议必须版本化，并有拒绝未知版本的测试。
- Browser 不直接访问 Provider；所有服务请求继续经过本地 node。
- Prompt、输出、文章正文、邀请秘密和关系凭证不得进入 Registry、普通日志或错误文本。
- 所有大小、超时、重试、队列和持久化行为必须有上限。
- 后端至少通过 focused tests、ruff 和完整测试；Webapp 至少通过 Vitest、TypeScript 和 production build。
- 不能仅凭单元测试关闭用户功能 Issue；需要对应的交互或端到端证据。
- 验收证据中不得包含真实密钥、Prompt、模型输出正文、邀请链接或朋友关系秘密。

## 3. 阶段一：#28 Transport POST

开发文档：
[ISSUE_28_TRANSPORT_POST_WORK_PLAN.md](ISSUE_28_TRANSPORT_POST_WORK_PLAN.md)

### 进入条件

- `upstream/main` 的现有 LLM direct、Relay 和 strict P2P 测试基线已记录。
- 确认所有内置 Transport 和插件测试替身的列表。
- 明确不在本 Issue 实现 streaming。

### 开发结果

- `Transport` 提供有界 POST。
- 所有内置实现支持 POST 或明确失败，不能静默回退到裸 `urllib`。
- `HttpPeerClient.post_json` 统一 JSON、大小限制和错误分类。
- LLM task、settlement、cancellation 全部迁移到 Transport。

### 放行门槛

- 2 MiB 边界、禁止重定向、network key、TLS/fronting/CDN/plugin 行为有测试。
- Direct 与 Relay LLM E2E 通过。
- `rynmesh/llm_package/routes.py` 不再自行执行 Peer HTTP POST。
- 完整测试和文档更新通过。

只有 #28 合入主线后，#23 才能进入网络实现阶段；#30 的好友认证 POST 也必须复用该路径。

## 4. 阶段二：#24 Provider/模型切换

开发文档：
[ISSUE_24_PRIVATE_AI_PROVIDER_SWITCHING_WORK_PLAN.md](ISSUE_24_PRIVATE_AI_PROVIDER_SWITCHING_WORK_PLAN.md)

### 进入条件

- 当前 `llmServiceRecordKey(peer_id, package_id)` 保持为唯一服务身份格式。
- 已确认会话历史不能自动跨 Provider 复制。
- 至少准备两个 Provider fixture，其中模型别名相同但 peer/package 不同。

### 开发结果

- 聊天页内可选择和比较 Provider/模型。
- 每个服务加载自己的加密会话桶。
- 切换时 URL、详情、历史、提交目标保持一致。
- 请求执行期间不能切换到造成归属混乱的服务。

### 放行门槛

- 同名模型不会选错节点或包。
- Provider A 的历史不会显示或发送给 Provider B。
- 离线/消失 Provider 不删除历史，并禁止新提交。
- Webapp tests、TypeScript 和 production build 全部通过。
- 录制或截图证明选择、切换、刷新恢复和离线状态。

## 5. 阶段三：#25 Ask about this item

开发文档：
[ISSUE_25_ASK_ABOUT_ITEM_WORK_PLAN.md](ISSUE_25_ASK_ABOUT_ITEM_WORK_PLAN.md)

### 进入条件

- #24 已合入，或实现分支明确固定一个 Provider 并保留后续切换兼容性。
- ReaderArticle 的稳定字段已确认。
- 文章上下文不得写入 URL、localStorage、sessionStorage 或普通日志。

### 开发结果

- 可从 For You/内容查看器打开一段新的 grounded Private AI 会话。
- 正文通过一次性内存 handoff 进入聊天，并随会话加密保存。
- Prompt 明确把文章当作不可信证据，而不是指令。
- 按当前 Provider 上下文窗口确定性裁剪，并在发送前提示。

### 放行门槛

- 可读文章成功进入 grounded 会话。
- 无正文、Reader 失败、无 Provider、上下文过小均有安全状态。
- 长文章不会超过 Provider 输入预算。
- URL 和浏览器明文存储中找不到文章正文或标题标记。
- 注入式文章文本不能突破 ARTICLE_CONTEXT 边界。
- Webapp 交互测试与现有 Reader 后端测试通过。

## 6. 阶段四：#23 Private AI 流式输出

开发文档：
[ISSUE_23_PRIVATE_AI_STREAMING_WORK_PLAN.md](ISSUE_23_PRIVATE_AI_STREAMING_WORK_PLAN.md)

### 进入条件

- #28 已合入 `upstream/main`。
- `stream-v1` 请求、事件封装、序列和最终结果协议获得 maintainer 批准。
- 明确 Relay、strict P2P 和不支持 streaming 的 Transport 使用整段回退。
- 当前非流式 direct/Relay/P2P 与结算测试作为回归基线通过。

### 开发结果

- 支持的 direct Provider 逐段输出到 Consumer node。
- Consumer node 通过本地 SSE 向 Browser 输出事件。
- 增量事件有签名、加密、序号、大小和总量限制。
- 最终 `llm_response` 仍是成功、usage 和结算唯一依据。
- Relay/P2P/非流式模型保持整段结果。

### 放行门槛

- 首个 delta 能在完整生成结束前显示。
- 丢包、乱序、重复、超限、断线和取消均有 fail-closed 测试。
- 部分输出不会作为成功结果保存或结算。
- Prompt/delta 不进入 Registry、Relay metadata、task metadata 或日志。
- 最终结算和 Provider earning 恰好一次。
- Direct streaming E2E 与全部非流式回归路径通过。

## 7. 并行阶段：#20 Linux 桌面包

开发文档：
[ISSUE_20_LINUX_DESKTOP_PACKAGE_WORK_PLAN.md](ISSUE_20_LINUX_DESKTOP_PACKAGE_WORK_PLAN.md)

### 进入条件

- 初始格式固定为 x86_64 Debian/Ubuntu `.deb`。
- 支持的 Ubuntu LTS 版本和 CI runner 固定。
- 接受第一版不覆盖 ARM64、RPM、AppImage 和 Windows。

### 开发结果

- Rust 桌面壳消除 macOS 路径和命令硬编码。
- PyInstaller sidecar 脚本能在 Linux 构建精确 target triple。
- CI 构建、检查和启动 Linux `.deb`。
- Tagged release 上传 `.deb` 和 SHA-256。

### 放行门槛

- 安装后不需要系统 Python、Node 或源码 checkout。
- sidecar 与包架构一致，daemon/UI/restart/quit 均验证。
- 至少一个真实受支持 Linux 桌面完成安装、升级和卸载验收。
- macOS Intel/Apple Silicon 验证没有回归。
- README、系统依赖、数据/日志路径和已知限制更新完成。

## 8. 阶段五：#30 Friend Mesh

开发文档：
[ISSUE_30_FRIEND_MESH_WORK_PLAN.md](ISSUE_30_FRIEND_MESH_WORK_PLAN.md)

### 进入条件

- 文档中的八项安全决策已在 Issue 上得到 maintainer 批准。
- #28 已合入，好友请求认证可以通过统一 Transport。
- 已确认 friendship 与 `trusted_roots` 完全分离。
- 已确认邀请不包含 `RYNMESH_NETWORK_KEY` 或 local-control 凭证。
- 已确定 per-friend 关系凭证、请求 MAC、重放保护和撤销语义。
- 已明确 v1 的网络可达范围；如承诺跨公网扫码加入，已批准加密接受通道，且依赖
  strict P2P 时 #22 已完成不同公网出口的物理验收。

### 开发结果

- 可创建短期、一次性、可取消的签名邀请和本地 QR。
- Join 前显示网络、身份指纹、端点、权限和有效期。
- 接受后轮换成独立 per-friend 关系凭证。
- 本地好友 Store、授权、诊断和撤销完成。
- friends-only Private AI 在 Provider admission 前检查好友 ACL。

### 放行门槛

- 并发双重接受只有一个成功。
- 泄漏的已使用/过期/取消邀请无法再次使用。
- 无全局 mesh secret 泄漏，接受好友不会修改 trusted roots。
- 在线和离线撤销都立即阻止本地后续授权，远端最终收敛。
- 非好友、被撤销好友、权限不匹配和伪造 peer 均无法运行推理。
- 两台全新节点完成邀请 → Join → 使用 Private AI → 撤销 → 再次拒绝的完整验收。
- 安全证据和日志中不出现邀请或关系秘密。

## 9. 每个 Issue 的关闭材料

每个 PR 合并前必须在对应 Issue/PR 留下：

- 基线 commit 和实现 commit；
- 改动范围与明确未做内容；
- focused test、完整 test、lint/typecheck/build 结果；
- 对应用户流程的截图、录屏或结构化 E2E 报告；
- 隐私/安全负面测试结果；
- 已知限制和后续 Issue；
- 回滚方法；
- 文档链接。

不得用“代码已提交”“CI 绿色”单独代替用户验收。

## 10. 总体验收完成定义

这一阶段全部完成时，应能证明：

- LLM Peer POST 统一经过 Transport，没有隐形网络旁路。
- 用户可在聊天内安全切换 Provider/模型且历史不串线。
- 用户可针对 For You 文章使用有边界、可解释截断的 Private AI。
- Direct Private AI 可流式显示，其他路径可靠回退，结算不重复。
- Linux 用户可以安装经过验证的自包含桌面包。
- 两个受信任用户可以安全邀请、加入、使用 friends-only Private AI 并撤销。

## 11. 可能中断目标的风险登记

### 硬阻断

| 风险 | 影响 | 解除条件 |
|---|---|---|
| #28 只有本地提交、没有 PR | #23 和 #30 的网络实现缺少可依赖主线 | 认领 #28，推送 PR，通过测试并合入 |
| #23 `stream-v1` 未获批准 | Peer wire contract 可能反复返工 | Maintainer 批准事件、回退、最终结果和结算语义 |
| #30 好友凭证/撤销未获批准 | 可能造成无法按好友撤销或误共享全网密钥 | 八项安全决策全部批准 |
| #30 跨网络邀请没有可达通道 | 不同 NAT 下 Join 在联系 inviter 前失败 | 明确 LAN-only，或实现加密 Relay/P2P 接受；依赖 strict P2P 时先完成 #22 |
| #20 缺最终 Linux 环境 | Windows 上无法关闭 Linux 安装包 Issue | Linux CI 通过并完成一台真实受支持桌面验收 |

### 可在开发中消除的高风险

| 风险 | 影响 | 处理方式 |
|---|---|---|
| #25 `chars/4` 低估中文 token | 长中文文章超窗或到模型层才失败 | 先统一多语言安全预算，价格估算与上下文安全估算分离 |
| 当前主工作树有大量未提交内容 | 串改、误提交、难以回滚 | 每个 Issue 使用从 `upstream/main` 创建的独立干净 worktree |
| 所有 Issue 仍为 `status:available` 且无人认领 | 与其他贡献者重复开发或分支相撞 | 开工前按仓库 contribution claim 流程认领并贴计划 |
| #24/#25/#23 都修改 `PrivateAIChat.tsx` | 顺序错乱时产生大规模冲突 | 严格按 #24 → #25 → #23 合入，每次基于最新 main |
| #23 代理缓冲、断线和取消 | “假流式”、部分结果误判成功、重复结算 | 首包时间证据、故障注入、终态唯一结算、整段回退 |
| #20 Rust 壳存在 macOS 硬编码 | Linux 编译或启动失败，同时可能回归 macOS | 先拆 OS helper，再加 Linux CI，保留 macOS 双架构 gate |
| #30 deep link/QR 平台差异 | 扫码后无法进入同一安全 Review 流程 | deep link 与复制粘贴共用一个解析器，QR 完全本地生成 |

### 当前不会中断的事项

- `upstream/main` 最新 CI 当前为成功。
- #28 本地分支干净并领先 `upstream/main` 一个提交；focused tests 当前
  `67 passed`，ruff 通过，因此没有发现代码级硬阻断。
- #24 是纯 Webapp 主线改动，没有后端协议前置条件。
- #20 可与服务主线并行，不需要等待 #28/#24/#25/#23。
