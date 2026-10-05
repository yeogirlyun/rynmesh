# 已批准五项用户需求：开发顺序、共同门槛与交付清单

状态：已批准，执行总计划  
批准日期：2026-09-04  
产品路线：[USER_PRODUCT_PRIORITY_ROADMAP.md](USER_PRODUCT_PRIORITY_ROADMAP.md)

## 1. 开发文档索引

1. [DEV-01 首次启动三分钟获得价值](DEV_01_FIRST_SUCCESS_IMPLEMENTATION_PLAN.md)
2. [DEV-02 一键启用本地 AI](DEV_02_LOCAL_AI_SETUP_IMPLEMENTATION_PLAN.md)
3. [DEV-03 统一 Ask Ryn 助手](DEV_03_UNIFIED_ASSISTANT_IMPLEMENTATION_PLAN.md)
4. [DEV-04 “我的内容”统一工作台](DEV_04_MY_CONTENT_IMPLEMENTATION_PLAN.md)
5. [DEV-05 两台设备简单配对与分享](DEV_05_SIMPLE_PAIR_AND_SHARE_IMPLEMENTATION_PLAN.md)

对应已批准产品需求保留在 `docs/USER_*_WORK_PLAN.md`，作为范围和用户价值的上位
文档；DEV 文档负责工程实现和验收。实现若改变用户范围，必须先更新产品需求并重新
评审，不能只修改 DEV 文档。

## 2. 总顺序

```text
DEV-01 首次成功
  ↓
DEV-02 一键本地 AI
  ↓
DEV-03 统一 Ask Ryn
  ↓
DEV-04 我的内容
  ↓
DEV-05 简单配对与分享
```

允许的并行：

- DEV-02 后端 runtime/download 可与 DEV-01 Webapp 并行；
- #28 Transport POST 可与 DEV-01/02 并行，但必须在 DEV-03 远端 target 和 DEV-05
  peer POST 前合入；
- DEV-04 Library API 可在 DEV-03 UI 后期开始，但 Ask Ryn context contract 必须先固定；
- DEV-05 store/crypto 单元测试可提前，但网络 endpoint 必须等安全 wire review 和 #28。

## 3. Release 分组

### Release A：第一次真正可用

- DEV-01 全部；
- DEV-02 至少 Windows/macOS 中通过物理验收的平台；
- 首页从控制台转为用户任务入口。

发布门槛：干净安装 → 真实内容 → 首次反馈，以及无终端本地 AI → 第一次回答。

### Release B：成为日常助手

- DEV-03 全部非流式能力；
- DEV-04 已保存、继续使用、历史；
- 旧 AI 入口完成 redirect/兼容迁移。

发布门槛：第二天能继续会话和阅读；文章/PDF 问答有真实引用。

### Release C：内容管理与两人分享

- DEV-04 文件、来源、监控和多选；
- DEV-05 全部 v1；
- Friend Mesh 只承诺已经物理验证的网络范围。

发布门槛：两台干净节点配对、分享、离线恢复和撤销。

## 4. 共同开发规则

- 每个 DEV 需求使用独立 feature flag；
- 采用小 PR，任何合入点主分支都应可运行；
- 不覆盖工作树中无关用户修改；
- 新存储/API/wire contract 必须版本化；
- 所有列表、正文、附件、下载、任务、重试、nonce 和缓存有明确上限；
- Browser 不直接访问 Provider、Peer model 或任意外部文件；
- Prompt、正文、文件路径、关系 secret 不进入普通日志/Registry；
- local-control auth 继续保护所有 `/api/local`；
- 新错误对用户返回稳定安全码，不返回 exception stack；
- migration 必须向后兼容或提供清晰只读/导出方案；
- rollback 不删除用户内容、模型、会话、Library 或消息历史。

## 5. 每项需求的 Definition of Done

一个需求只有同时满足以下条件才能标记完成：

1. 功能验收清单全部完成；
2. 安全与隐私验收清单全部完成；
3. focused tests、完整 backend tests、ruff 通过；
4. Webapp Vitest、TypeScript、production build 通过；
5. 对应桌面/两节点真实环境验收通过；
6. 用户旅程录屏或截图齐全；
7. unique marker 隐私负面搜索通过；
8. migration 和 rollback 实际演练；
9. README/用户指南/支持矩阵更新；
10. 实现 commit、基线 commit、测试输出、已知限制记录在验收报告。

CI 绿色或“代码已提交”不能单独作为关闭条件。

## 6. 总体测试命令基线

Backend：

```bash
python -m pytest tests/ -q
python -m ruff check rynmesh tests qa
```

Webapp：

```bash
cd webapp
npm test
npm run lint
npm run build
```

Desktop/打包：按现有 release pipeline 构建，并在每个宣称支持的平台运行冷启动。

此外每个 DEV 文档中的 focused、故障注入、物理 E2E 和隐私负面测试都是强制项，
不能被上述完整命令替代。

## 7. 跨需求集成验收

五项全部完成后，使用同一干净数据目录完成：

1. 安装并启动 Ryn；
2. 无配置看到真实推荐；
3. 打开并保存一项内容，完成首次成功；
4. 在应用内安装推荐本地模型并完成第一次回答；
5. 从该内容打开 Ask Ryn，得到带可打开引用的回答；
6. 重启应用，继续刚才会话和阅读进度；
7. 导入 PDF，加入“我的内容”并继续提问；
8. 第二台干净节点通过邀请配对；
9. 发送消息、小附件和该内容卡片；
10. 第二台节点主动获取卡片内容；
11. 第一台撤销关系，下一条请求被拒绝；
12. 导出个人数据并验证不包含 model/API/friend secrets。

## 8. 跨需求验收条件

- [ ] 五个用户问题均能由非技术用户独立完成，无终端操作。
- [ ] 默认单机内容体验不依赖模型、Peer 或 Registry 在线。
- [ ] AI 不可用和 Friend Mesh 不可用不会让整个应用进入 offline。
- [ ] Home、Ask Ryn、我的内容、好友消息的职责清晰，无重复伪入口。
- [ ] 所有跨页面草稿、任务、进度和会话在应用重启后按设计恢复。
- [ ] 本地/好友/云端 privacy boundary 在发送前可见且不可静默变化。
- [ ] 用户内容、模型和好友数据可以导出/擦除，秘密永不默认导出。
- [ ] 发布说明只宣传真实物理验收过的平台和网络范围。
- [ ] 旧数据、旧 URL 和旧会话有明确兼容周期。
- [ ] 完整跨需求 E2E 连续通过至少三次，无手工修改数据库或配置。

## 9. 验收报告目录建议

每个需求在 `docs/acceptance/` 下使用固定结构：

```text
docs/acceptance/dev-01-first-success/
docs/acceptance/dev-02-local-ai/
docs/acceptance/dev-03-ask-ryn/
docs/acceptance/dev-04-my-content/
docs/acceptance/dev-05-pair-share/
```

每个目录至少包含：

- `README.md`：环境、commit、结论和限制；
- focused/full test 输出摘要；
- sanitized E2E JSON；
- 截图/录屏索引；
- privacy/security negative evidence；
- migration/rollback report。

不要提交真实 Prompt、文章正文、本地绝对路径、API Key、invite 或 relationship secret。

## 10. 与原六个 Issue 的最终映射

| 原 Issue | 新位置 | 处理方式 |
|---|---|---|
| #28 Transport POST | DEV-03/05 前置 | 继续作为底层安全任务 |
| #24 Provider/模型切换 | DEV-03 compute target | 不再单独扩展旧 Private AI 页面 |
| #25 Ask about item | DEV-03 context | 扩展为文章/文件/Library 通用上下文 |
| #23 Streaming | DEV-03 后续增强 | 非流式完整闭环后再实现 |
| #20 Linux desktop | 独立发行覆盖 | 不阻断用户产品主线，按真实支持需求推进 |
| #30 Friend Mesh | DEV-05 + 后续服务 ACL | 先配对分享，后 Private AI/VPN/Agent 权限 |

## 11. 开工前最终检查

- [ ] 产品需求批准记录已关联；
- [ ] DEV 文档中的协议/平台决策有明确 owner；
- [ ] 当前 `upstream/main` baseline 和 CI 结果已记录；
- [ ] 每项建立独立 branch/worktree，避免混入现有未提交内容；
- [ ] feature flag、migration、rollback owner 明确；
- [ ] 真实 Windows/macOS/两节点验收环境可用；
- [ ] 安全相关 DEV-05 wire review 完成；
- [ ] 不以旧 issue 的页面结构约束新用户旅程。
