# DEV-03：统一 Ask Ryn 助手——完整开发与验收文档

状态：已批准，implementation-ready  
产品需求：[USER_UNIFIED_ASSISTANT_WORK_PLAN.md](USER_UNIFIED_ASSISTANT_WORK_PLAN.md)  
优先级：P0  
前置依赖：DEV-02；远端 Provider 部分依赖 #28 Transport POST  
可并行：本地助手 UI/存储与 #28  

## 1. 开发结果

用一个 Ask Ryn 工作区替代用户可见的 Search & Ask、Private AI Chat 和 AI curator
侧栏分裂。第一版必须完成以下真实闭环：

- 普通本地连续对话；
- 基于 Ryn 已发现内容的搜索问答；
- 从当前文章带上下文提问；
- 至少支持文本/Markdown/PDF 文件上下文；
- 会话历史、重命名、归档、删除；
- 取消、失败重试、重启恢复；
- 清楚显示“在此设备/好友设备/云端”的计算位置。

Streaming 不是上线前置条件；先交付正确、持久、可取消的非流式完整路径。

## 2. 当前实现基线

### Webapp

- `webapp/src/screens/SearchAsk.tsx`
  - 初始消息为硬编码 seed；
  - 调用 `client.submitSearchAsk()`，没有持久会话；
  - 展示底层 operation 名称。
- `webapp/src/screens/PrivateAIChat.tsx`
  - 支持服务发现、任务轮询、取消和会话；
  - 服务首次加载时确定，尚未完成统一入口。
- `webapp/src/domain/llmConversationStore.ts`
  - AES-GCM 加密后写 IndexedDB；
  - 非 extractable CryptoKey；
  - 加密不可用时只保存在 session memory；
  - 以 serviceKey 分桶。
- `webapp/src/App.tsx`
  - 有 Search & Ask、Private AI 路由和只展示策略的 AI side panel。

### Backend

- `rynmesh/services/ask.py`：基于本地 digest/mesh evidence 回答；
- `rynmesh/services/reader.py`：node-mediated 网页正文提取和缓存；
- `rynmesh/peer_http.py`：`/api/local/search-ask`、reader、model provider；
- `rynmesh/llm_package/routes.py`：远端 Private AI order/status/cancel/privacy；
- 私有任务正文已有签名加密、direct/relay/P2P 路径。

## 3. 架构决策

### 3.1 会话与运行分离

- **Conversation**：用户消息、回答、上下文引用和 Provider 快照；由 Webapp 加密存储。
- **Run**：一次正在执行的 node 请求；由 node 持久化最小状态，支持取消和恢复。
- 只有 `completed` run 的最终结果进入正式 conversation。
- failed/cancelled run 可保留用户问题和错误状态，但不作为后续模型上下文。

第一版沿用加密 IndexedDB，避免引入新的后端密钥管理。存储格式升级为 v2，并提供
v1 Private AI 会话兼容迁移。未来跨客户端同步必须单独设计，不能把明文搬到 node。

### 3.2 一个 Node API 门面

Browser 不再自己分别理解 SearchAsk 与 LLM order。新增 Assistant Run API，在 node
端根据显式 route 执行：

- `local_search`：本地模型 + Ryn evidence；
- `local_context`：本地模型 + 用户选择的上下文；
- `remote_provider`：指定复合 service key 的 Private AI；
- `cloud_provider`：仅在现有 cloud permission 开启时。

自动路由第一版只做建议，不静默改变数据边界。

### 3.3 Provider 身份

- 本地：`local:<provider-id>:<model-id>`；
- Peer：沿用 `llmServiceRecordKey(peer_id, package_id)`；
- 云端：`cloud:<provider-id>:<model-id>`。

会话有 `privacyBoundary`。切换到不同 boundary 时创建新会话或用户显式复制选定内容，
不得静默复用全部历史。

## 4. 领域模型

在 `webapp/src/domain/assistantTypes.ts` 定义：

```ts
type AssistantPrivacyBoundary = "device" | "friend" | "cloud";
type AssistantRunState =
  | "queued" | "running" | "completed" | "failed" | "cancelling" | "cancelled";

interface AssistantConversationV2 {
  version: "ryn.assistant-conversation.v2";
  id: string;
  title: string;
  archived: boolean;
  computeTarget: {
    key: string;
    label: string;
    privacyBoundary: AssistantPrivacyBoundary;
    providerPeerId?: string;
    packageId?: string;
    modelId?: string;
  };
  contexts: AssistantContextRef[];
  messages: AssistantMessage[];
  createdAt: string;
  updatedAt: string;
}
```

`AssistantMessage` 保存 role、content、status、runId、引用、token/cost、错误码和时间。
不保存网络密钥、Provider credential 或原始本地绝对路径。

`AssistantContextRef`：

```ts
interface AssistantContextRef {
  version: "ryn.assistant-context.v1";
  id: string;
  kind: "content-item" | "reader-article" | "library-item" | "local-file";
  title: string;
  localRef: string;
  sourceUrl?: string;
  contentHash?: string;
  sizeBytes: number;
  addedAt: string;
}
```

正文快照随加密 conversation payload 保存时必须有总量上限。较大文件只保存 node
提供的 opaque localRef，由 node 在 run 时按权限读取。

## 5. Assistant Run 后端

### 5.1 新模块

新增：

- `rynmesh/services/assistant_runs.py`：状态机、持久化、取消；
- `rynmesh/services/assistant_context.py`：上下文解析、预算、prompt 边界；
- `rynmesh/services/assistant_router.py`：显式 compute target 路由；
- `rynmesh/services/assistant_citations.py`：引用规范化。

运行记录路径：`RYNMESH_HOME/assistant/runs/<run_id>.json`。只保存恢复需要的元数据；
如果结果保留，必须服从本地/LLM privacy retention。Prompt 和正文不写普通 run JSON。

### 5.2 API

```http
POST /api/local/assistant/runs
GET  /api/local/assistant/runs/{run_id}
POST /api/local/assistant/runs/{run_id}/cancel
GET  /api/local/assistant/targets
POST /api/local/assistant/context/prepare
```

创建请求：

```json
{
  "version": "ryn.assistant-run.v1",
  "conversation_id": "...",
  "idempotency_key": "...",
  "target_key": "local:ollama:gemma3:4b",
  "privacy_boundary": "device",
  "mode": "local_context",
  "messages": [{"role":"user","content":"..."}],
  "contexts": [{"local_ref":"ctx_...","kind":"reader-article"}],
  "max_output_tokens": 512
}
```

响应只返回 run 状态、最终文本、引用和计量。错误使用稳定码：

- `assistant_model_unavailable`；
- `assistant_context_missing`；
- `assistant_context_too_large`；
- `assistant_target_offline`；
- `assistant_permission_required`；
- `assistant_cancelled`；
- `assistant_timeout`；
- `assistant_provider_failed`。

### 5.3 状态和幂等

- `(conversation_id, idempotency_key)` 唯一；
- 重复 POST 返回同一 run；
- cancel 幂等；
- node 重启后 queued/running 转为可重试失败，或对可恢复远端 task 重新查询；
- completed 结果不能被晚到的 failed/cancelled 覆盖；
- 并发上限按本地模型和远端 Provider 分别执行；
- Webapp 离开页面不取消 run。

## 6. 上下文准备与 Prompt

### 6.1 准备流程

`POST /assistant/context/prepare` 接收内容 ID、reader URL 或受管 Library file ref，
返回 opaque `local_ref`、元数据、估算 token 和到期时间。

- 当前文章复用 `ReaderCache`；
- content item 复用 node 的内容存储/verified fetch；
- Library file 由 DEV-04 的 managed snapshot 提供；
- 一次性 Webapp handoff 只携带 opaque ID，不把正文放 URL/history/localStorage。

### 6.2 上下文限制

- 单 context、单 run、单 conversation snapshot 都有字节上限；
- 输入预算 = context window - output cap - fixed margin - conversation；
- 使用保守多语言估算，不能用 `chars / 4` 作为安全 gate；
- conversation 从最新完整 turn 向前保留；
- 文章按 block、本地文档按 page/paragraph 截断；
- 发送前返回 included/original counts 和 `truncated`；
- 最小有效材料都放不下时拒绝发送。

### 6.3 注入防护

固定系统指令声明所有材料为不可信证据；对 marker 做不可冲突编码，而不是只替换一
个 closing tag。模型层防护不能代替工具权限：v1 Assistant 无写操作工具。

## 7. 引用协议

统一引用结构：

```json
{
  "citation_id": "cite_1",
  "context_id": "ctx_...",
  "title": "...",
  "source_url": "...",
  "locator": {"kind":"paragraph","start":12,"end":13},
  "quote": "短摘录",
  "content_hash": "sha256:..."
}
```

- quote 有严格长度上限；
- locator 必须落在实际发送的 context 范围内；
- 无法验证的模型自由文本引用不转成可信 citation；
- 点击 citation 优先打开本地 viewer 并定位，否则打开原始来源；
- Browser 显示“引用材料”而不是伪装成事实验证。

## 8. Webapp 实施

### 8.1 文件结构

新增：

- `webapp/src/screens/AskRyn.tsx`；
- `webapp/src/screens/AskRyn.module.css`；
- `webapp/src/domain/assistantClient.ts`；
- `webapp/src/domain/assistantStore.ts`；
- `webapp/src/domain/assistantPrompt.ts`（仅预览/计数，不持有路由安全逻辑）；
- `webapp/src/components/AskRynPanel.tsx`；
- `webapp/src/components/AssistantContextCard.tsx`；
- `webapp/src/components/AssistantCitation.tsx`。

### 8.2 路由迁移

- 新主路由 `/ask`；
- `/search-ask` redirect 到 `/ask?mode=search`；
- `/services/private-ai/chat` redirect/migrate 到 `/ask`；
- 侧边 AI panel 渲染 `AskRynPanel`，共享 active conversation ID；
- Peer `/chat` 保留并改名为“好友消息”，避免与 AI 混淆。

### 8.3 存储迁移

`assistantStore` 复用现有 IndexedDB database/key，升级 schema：

- 读取 `LLMConversation` v1；
- 转成 v2 computeTarget 和 message；
- 原记录在迁移成功前不删除；
- 单条损坏不阻断其余会话；
- 提供导出旧记录/清除；
- 无 WebCrypto 时继续 session-only，明确提示。

### 8.4 UI 状态

- 无模型：进入 DEV-02，不显示不可执行 composer；
- target offline：保留草稿，允许换 target；
- run active：显示取消，禁止改变当前 conversation target；
- context truncated：发送前显示；
- failed：显示安全错误、重试和调整上下文；
- completed：显示引用和运行详情；
- 应用重启：查询 active run 并恢复。

## 9. 与现有 Issue 的合并方式

- #24 Provider switching → 本文 compute target selector；
- #25 Ask about item → 本文 context prepare + viewer action；
- #23 streaming → 后续为 Assistant Run 增加 SSE，不改最终结果语义；
- #28 Transport POST → remote_provider 路径必须先使用统一 Transport；
- 旧 SearchAsk endpoint 在迁移期保留，两个版本后再删除。

## 10. 实施 PR 切片

1. `feat: add assistant run protocol and local route`
2. `feat: add bounded assistant context preparation`
3. `feat: add encrypted assistant conversation v2 store`
4. `feat: replace Search Ask with Ask Ryn workspace`
5. `feat: add grounded article and file context`
6. `feat: expose explicit provider privacy boundaries`
7. `refactor: redirect legacy AI entry points`
8. `test/docs: prove assistant privacy, recovery, and citations`

## 11. 测试矩阵

### Backend

- run state transitions and idempotency；
- local search/no evidence；
- local context/full/truncated/too small；
- ASCII/CJK/emoji/combining mark budget；
- delimiter injection；
- context ref expiry/wrong owner/missing；
- cancel before/during inference；
- node restart and late completion race；
- remote target composite identity；
- privacy permission denial；
- citation locator validity；
- Prompt/body marker absent from logs/registry/run metadata。

新增 `tests/test_assistant_runs.py`、`tests/test_assistant_context.py`，扩展
`tests/test_reader_and_steering.py`、`tests/test_llm_package.py`。

### Webapp

- 无 seed 演示消息；
- create/list/rename/archive/delete；
- side panel/full page 同步；
- article handoff one-time/expiry；
- file context；
- target selection and privacy boundary；
- run polling/cancel/retry/restart；
- citation open；
- v1 migration/corrupt record/session-only；
- URL/localStorage/sessionStorage marker absence；
- keyboard/focus/live region。

### E2E

- local normal chat；
- local search with citations；
- article grounded question；
- PDF question；
- remote Provider direct and relay；
- target goes offline；
- cancel and restart recovery；
- no model → setup → return to preserved draft。

## 12. 验收条件

### 功能验收

- [ ] 用户只需一个 Ask Ryn 入口即可完成普通、搜索和内容问答。
- [ ] 快速侧栏与完整页面共享会话、草稿和 run 状态。
- [ ] 初始页面没有硬编码对话、伪造引用或演示操作记录。
- [ ] 会话可创建、继续、重命名、归档、删除并在重启后恢复。
- [ ] 当前文章、文本/Markdown 和 PDF 至少三类上下文可用。
- [ ] 回答引用可以打开真实材料并落在实际发送范围内。
- [ ] 取消、失败重试、target offline 和 node 重启行为一致。
- [ ] 旧 Private AI 会话完成兼容迁移且不串 Provider。

### 安全与隐私验收

- [ ] Browser 只访问 local node，不直连模型/Peer Provider。
- [ ] 不同 privacy boundary 的历史不会被静默复制发送。
- [ ] 文章/文件视为不可信证据，delimiter injection 测试通过。
- [ ] 多语言上下文预算不超过 Provider 限制，截断在发送前可见。
- [ ] 正文、Prompt、输出不进入 URL、明文 Web storage、Registry、通知或普通日志。
- [ ] v1 无发布、删除、信任、设置修改等写操作工具。
- [ ] cancel/late result 不会把部分或取消结果标记为 completed。

### 质量验收

- [ ] focused/full pytest、ruff 通过。
- [ ] Webapp `npm test`、`npm run lint`、`npm run build` 通过。
- [ ] direct/relay Private AI 回归通过；非流式仍为正式支持路径。
- [ ] 500 条会话/大上下文分页与渲染性能达到约定预算。
- [ ] 键盘完成新建、发送、取消、打开引用、切换会话。

### 验收证据

- 普通、本地证据、文章、PDF、远端 Provider 五条录屏；
- v1→v2 migration 报告；
- CJK/emoji 上下文边界数字；
- injection、URL/storage/log/registry marker 负面结果；
- cancel/late result/restart 故障注入；
- commits、测试输出、已知限制和回滚说明。

## 13. 发布与回滚

- feature flag `assistant_workspace_v1`；
- 第一版保留 legacy URL redirect 和旧存储只读兼容；
- rollback UI 时不删除 v2 conversations；
- Assistant Run API 故障时可以临时回到旧 SearchAsk，但不得把 context 发送到错误
  privacy boundary；
- remote route 可独立关闭，本地 Ask Ryn 继续可用；
- streaming 后续单独开关，不影响 completed final result。
