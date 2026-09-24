# DEV-01：首次启动三分钟获得价值——完整开发与验收文档

状态：已批准，implementation-ready  
产品需求：[USER_FIRST_SUCCESS_WORK_PLAN.md](USER_FIRST_SUCCESS_WORK_PLAN.md)  
优先级：P0  
前置依赖：无  
阻断后续：DEV-02、DEV-03 的默认入口设计

## 1. 开发结果

将现有六步“能力介绍”式 onboarding 改造成真实任务驱动的首次体验。全新用户在
本地 node 启动后，直接经历：

```text
节点就绪 → 默认来源准备 → 真实推荐可用 → 打开内容 → 保存或反馈 → 完成
```

模型、Peer、Registry 在线和 Credits 都不是完成条件。所有状态来自 node 真实数据，
不得使用 fixture 或前端假进度冒充成功。

## 2. 当前实现基线

### Webapp

- `webapp/src/App.tsx`
  - 启动时轮询 `getNodeStatus()`，最多 12 次；
  - `refreshShell()` 分别获取 node、registry、peers、settings；
  - 根据 `settings.onboarding_version` 打开 onboarding；
  - 主导航一次暴露九个产品/控制台入口。
- `webapp/src/components/OnboardingTour.tsx`
  - 当前为六步介绍；
  - 混合“当前可用”和“路线图”信息；
  - 完成时只更新 onboarding version。
- `webapp/src/screens/Home.tsx`
  - 已能获取推荐、内容、活动和 discovery status；
  - 首页仍以 local node console、指标和系统信息为主。
- `webapp/src/screens/Digest.tsx`
  - 已有真实来源、推荐、反馈、收藏、reader、来源健康与重试状态。

### Backend

- `rynmesh/peer_http.py`
  - `/api/local/discovery/status` 提供发现阶段和来源健康；
  - `/api/local/recommendations` 提供真实推荐；
  - `/api/local/consumption` 记录 opened/bookmark/progress/completed；
  - `/api/local/recommendations/feedback` 更新推荐画像；
  - settings 已持久化 `onboarding_version`。
- `rynmesh/services/digest.py`
  - 自动安装默认公共来源；
  - 单来源失败不清空整个 feed；
  - 支持缓存和提前重试。
- `rynmesh/services/consumption.py`
  - 原子、本地、有界保存阅读和收藏状态。

## 3. 关键设计决策

1. 首次体验进度由 node 保存，Webapp 只渲染，不在 `localStorage` 自己维护第二份状态。
2. “首次成功”必须包含一个真实 item 的 `opened` 以及至少一个 `bookmark` 或推荐反馈。
3. 用户关闭引导不等于完成；下次从未完成里程碑继续，但只显示非阻塞入口。
4. Registry 不可用、没有 Peer、没有模型属于提示信息，不属于阻断错误。
5. 默认来源全部失败时可以使用本地缓存完成；没有缓存时进入可恢复失败。
6. Onboarding 版本与首次成功里程碑分离：版本控制 UI 教程升级，里程碑记录真实行为。

## 4. 后端设计

### 4.1 新模块

新增 `rynmesh/services/first_success.py`：

```python
FIRST_SUCCESS_VERSION = "ryn.first-success.v1"

class FirstSuccessStore:
    def get(self) -> dict: ...
    def record(self, milestone: str, *, item_id: str = "", now_unix: float | None = None) -> dict: ...
    def dismiss(self) -> dict: ...
    def reset(self) -> dict: ...
```

存储文件：`RYNMESH_HOME/first-success.json`。

允许的里程碑固定为：

- `node_ready`；
- `content_ready`；
- `first_item_opened`；
- `first_signal_recorded`；
- `completed`。

写入规则：

- 临时文件 + `os.replace` 原子替换；
- 时间只能向前推进，重复记录幂等；
- item ID 最长 256 字符；
- 不保存标题、正文、URL、Prompt 或用户反馈文本；
- 未知版本 fail closed，但允许恢复默认空状态；
- 文件损坏时保留诊断码并重建，不导致 node 无法启动。

### 4.2 聚合服务

`FirstSuccessService` 组合以下真实状态：

- Node 是否已接收本地控制请求；
- `DigestService.status()` 的 phase/item/source health；
- ConsumptionStore 是否已有 opened/bookmark；
- RecommendationProfileStore 是否已有用户反馈；
- FirstSuccessStore 里程碑。

状态计算顺序：

```text
node not ready                       → starting_node
item_count = 0 and refreshing        → checking_sources
item_count = 0 and failed_sources > 0→ needs_action
item_count > 0 and no first open     → ready
opened and no signal                 → awaiting_signal
opened and signal                    → completed
```

若 `failed_sources > 0` 且仍有 items，额外返回 `degraded=true`，但 phase 不降级为失败。

### 4.3 Local API

在 `rynmesh/peer_http.py` 增加：

```http
GET  /api/local/first-success
POST /api/local/first-success/dismiss
POST /api/local/first-success/reset
```

`GET` 响应：

```json
{
  "version": "ryn.first-success.v1",
  "phase": "ready",
  "completed": false,
  "dismissed": false,
  "node_ready": true,
  "content_ready": true,
  "item_count": 18,
  "healthy_sources": 7,
  "source_count": 9,
  "failed_sources": 2,
  "using_cache": true,
  "first_item_opened": false,
  "first_signal_recorded": false,
  "safe_error": null,
  "recoverable_actions": ["retry_discovery"]
}
```

`reset` 仅供帮助页“重新运行入门”调用，不删除历史或推荐画像；它只重置首次体验
展示进度。所有接口继续由 local-control auth 保护。

### 4.4 在现有行为中记录里程碑

- node 完成应用初始化后记录 `node_ready`；
- discovery 首次出现真实 item 或缓存 item 时记录 `content_ready`；
- `/api/local/consumption` 收到 `opened` 时记录 `first_item_opened`；
- bookmark 或 `/recommendations/feedback` 成功时记录 `first_signal_recorded`；
- 聚合服务在前两项均满足时幂等记录 `completed`。

记录失败不能反向导致原业务请求失败；使用安全 audit 事件记录存储异常。

## 5. Webapp 设计

### 5.1 类型和 client

在 `webapp/src/domain/types.ts` 增加 `FirstSuccessStatus`。在
`webapp/src/domain/nodeClient.ts`、`liveNodeClient.ts` 和 `fixtureNodeClient.ts` 增加：

```ts
getFirstSuccess(): Promise<FirstSuccessStatus>;
dismissFirstSuccess(): Promise<FirstSuccessStatus>;
resetFirstSuccess(): Promise<FirstSuccessStatus>;
```

Fixture 只能用于 Story/test，生产启动路径不得自动选择 fixture。

### 5.2 新组件

新增：

- `webapp/src/components/FirstSuccessFlow.tsx`；
- `webapp/src/components/FirstSuccessFlow.module.css`；
- `webapp/src/components/FirstSuccessFlow.test.tsx`。

组件职责：

- 展示真实阶段和来源健康；
- phase 为 ready 时加载 3–5 条真实推荐；
- 复用现有 ContentViewer/DigestViewer，不复制 reader；
- 完成 opened + signal 后展示成功页；
- 关闭后只在 Home 保留“继续首次设置”卡片；
- 组件卸载时取消 timer，避免重复轮询。

轮询建议：前台每 2 秒、后台暂停；成功后停止。使用 request generation 防止旧响应
覆盖新状态。

### 5.3 App 启动

修改 `webapp/src/App.tsx`：

- node 可达后并行获取 shell 与 first-success；
- 未完成且未 dismissed 时打开 `FirstSuccessFlow`；
- 保留现有 `OnboardingTour` 作为帮助中心里的“产品介绍”，不再首次强制弹出；
- `onboarding_version` 不再代表真实首次成功。

### 5.4 Home 重构边界

修改 `webapp/src/screens/Home.tsx`：

- 首屏依次为：今日推荐、继续使用、需要处理；
- daemon/registry/peer 指标移入折叠 `System status`；
- 完成前显示继续首次设置；
- 完成后显示启用本地 AI 的下一步卡片；
- 不在本 Issue 删除高级导航页面，导航收敛由 DEV-03 完成。

## 6. 数据迁移与兼容

- 已有安装且存在 consumption opened + bookmark/feedback：首次读取时回填 completed；
- 已有 `onboarding_version >= 1` 但无真实行为：不直接判 completed，显示非阻塞入口；
- fixture 测试数据必须显式标注，不进入真实里程碑；
- 删除/擦除 history 或 profile 不自动重开 onboarding；只有显式 reset 才重开；
- personal data export 增加不含内容正文的 first-success record；
- erase 新增可选 `onboarding` scope，不能与 history/profile 隐式绑定。

## 7. 实施 PR 切片

1. `feat: add first-success state and local API`
2. `test: cover first-success aggregation and migration`
3. `feat: replace forced tour with task-driven first success`
4. `feat: make home task-oriented after onboarding`
5. `docs: document first-success recovery and privacy`

每个 PR 必须保持主分支可启动；Webapp 不得先依赖尚未合入的 API。

## 8. 测试矩阵

### Backend

新增 `tests/test_first_success.py`：

- 空目录初始状态；
- 原子里程碑写入和幂等；
- 非法版本/损坏文件恢复；
- 真实 item、缓存 item、部分源失败、全部源失败；
- opened 后 awaiting_signal；
- bookmark/feedback 后 completed；
- Registry/Peer/model 状态不阻断；
- 旧安装回填；
- export/erase/reset；
- 不保存标题、URL 和正文 marker。

在 `tests/test_reader_and_steering.py`、`tests/test_digest.py` 增加关键集成断言。

### Webapp

- 各 phase 渲染；
- loading/ready/degraded/needs_action；
- 打开真实内容后刷新状态；
- 收藏/反馈后完成；
- dismiss、继续和重启恢复；
- document hidden 时暂停轮询；
- 无硬编码推荐；
- 键盘、焦点、屏幕阅读器标签；
- Home 完成前后卡片差异。

### Desktop E2E

- 干净 `RYNMESH_HOME` 冷启动；
- node 慢启动；
- Registry 离线；
- 单来源失败；
- 全部网络失败但有缓存；
- 全部网络失败且无缓存；
- 关闭重开恢复。

## 9. 验收条件

### 功能验收

- [ ] 干净安装无需账号、模型、Peer 或手动来源即可进入真实首批内容。
- [ ] 用户可在三分钟内完成打开内容和保存/反馈。
- [ ] 首次成功由真实 node 行为判定，不能由前端按钮直接伪造。
- [ ] 没有模型、没有 Peer、Registry 离线不阻断。
- [ ] 部分来源失败继续提供可用内容；全部失败提供缓存或明确恢复动作。
- [ ] 用户关闭引导后仍可正常使用，并能从 Home 继续。
- [ ] 应用重启后恢复到正确阶段。
- [ ] 完成后不再次强制弹出。

### 安全与隐私验收

- [ ] 首次进度不保存标题、URL、正文、Prompt 或身份秘密。
- [ ] API 只暴露在 local-control auth 下。
- [ ] 导出/擦除行为已覆盖新记录。
- [ ] 错误响应和日志不包含内容 marker 或异常栈。

### 质量验收

- [ ] `python -m pytest tests/test_first_success.py tests/test_digest.py tests/test_reader_and_steering.py -q`
- [ ] `python -m ruff check rynmesh tests`
- [ ] `npm test`、`npm run lint`、`npm run build` 通过。
- [ ] macOS/Windows 至少各一次干净桌面冷启动验收。
- [ ] 可访问性：完整键盘路径、焦点恢复、状态使用 `aria-live` 且不过度播报。

### 验收证据

- 三条录屏：正常、部分来源失败、离线缓存；
- 冷启动至首批内容和首次成功的时间记录；
- 含唯一正文 marker 的存储/日志负面搜索；
- API 示例和 migration 测试输出；
- 实现 commit、完整测试、已知限制和回滚 commit。

## 10. 发布与回滚

- 使用 `first_success_v1_enabled` 本地 feature flag 分阶段启用；
- 首个版本保留旧 tour 的手动入口；
- 回滚 UI 时不删除已写入的 first-success store；旧版本忽略该文件；
- 不回滚现有 discovery、consumption 或 feedback 数据；
- 如果聚合 API 故障，Home 仍可通过现有接口使用，不将整个 App 置为 offline。
