# DEV-04：“我的内容”统一工作台——完整开发与验收文档

状态：已批准，implementation-ready  
产品需求：[USER_MY_CONTENT_WORK_PLAN.md](USER_MY_CONTENT_WORK_PLAN.md)  
优先级：P1  
前置依赖：DEV-03 的 AssistantContextRef；基础列表可提前开发  
提供给后续：DEV-05 内容卡片选择器

## 1. 开发结果

增加一个统一的“我的内容”页面，集中提供：

- 已保存；
- 继续阅读/播放；
- 历史；
- 来源和网页监控；
- 本地导入文件；
- 本地搜索、筛选、批量管理；
- 选择一项或多项交给 Ask Ryn。

本需求不复制所有数据到新的大表。Node 新增 LibraryService，把现有 consumption、
Digest sources/watchers、content store 和新增 import store 组合成稳定分页 API。

## 2. 当前实现基线

- `rynmesh/services/consumption.py`
  - 最多 1000 条；
  - opened/bookmark/unbookmark/progress/completed；
  - item 快照和原子 JSON 写入。
- `rynmesh/services/digest.py`
  - sources、read-later、watchers、health、cache；
  - 用户新增来源立即 seed items；
  - `Read later` 和 `Watchers` 已作为本地伪来源。
- `rynmesh/services/reader.py`：网页正文缓存；
- `rynmesh/peer_http.py`：consumption、sources、watchers、privacy export/erase；
- `webapp/src/screens/Digest.tsx`：上述能力集中在长页面多个 Panel；
- `webapp/src/screens/Explore.tsx`：内容筛选，但不是用户 Library；
- 目前没有本地文件 import store、统一分页 query、archive/user tags。

## 3. 架构与真相源

### 3.1 不重复的真相源

- 收藏、进度、打开/完成：`ConsumptionStore`；
- 来源与健康：`DigestService`；
- watcher：`DigestService`；
- 已发布/已获取内容：现有 content store；
- 本地导入：新 `LibraryImportStore`；
- archive、用户标签、手动标题：新 `LibraryMetadataStore`。

`LibraryService` 只建立稳定投影和索引，不把正文复制进列表 JSON。

### 3.2 稳定 library_id

- 有 content ID：`content:<content_id>`；
- 外部 URL：`url:<sha256(canonical_url)>`；
- 本地导入：`import:<uuid>`；
- watcher：`watcher:<watcher_id>`；
- source 配置不作为 LibraryItem，使用独立 source API。

URL canonicalization 必须保守，不移除可能改变资源含义的 query 参数；只处理 scheme/
host case、默认端口、空 fragment 等安全规则。

## 4. 后端模块

新增：

- `rynmesh/services/library.py`：投影、分页、筛选、批量动作；
- `rynmesh/services/library_imports.py`：文件快照、元数据和解析状态；
- `rynmesh/services/library_metadata.py`：archive/tags/override；
- `rynmesh/services/document_extract.py`：受限文本/Markdown/PDF 抽取，或复用已有 reader
  seam 后扩展。

### 4.1 LibraryItem 响应

```json
{
  "version": "ryn.library-item.v1",
  "library_id": "content:cid_...",
  "content_id": "cid_...",
  "kind": "article",
  "title": "...",
  "source": {"id":"...","title":"...","url":"..."},
  "state": {
    "saved": true,
    "progress": 0.42,
    "completed": false,
    "archived": false
  },
  "capabilities": {"open":true,"ask":true,"share":true},
  "local_copy": {"available":true,"size_bytes":1234},
  "last_activity_unix": 0,
  "tags": []
}
```

列表不返回正文、二进制、完整本地路径或敏感提取文本。

### 4.2 分页 API

```http
GET    /api/local/library?view=saved&query=&kind=&source=&cursor=&limit=50
GET    /api/local/library/{library_id}
PATCH  /api/local/library/{library_id}
POST   /api/local/library/actions
POST   /api/local/library/imports
GET    /api/local/library/imports/{id}/status
DELETE /api/local/library/imports/{id}
GET    /api/local/library/sources
PATCH  /api/local/library/sources/{id}
```

查询约束：

- `limit` 默认 50、最大 100；
- cursor 不泄漏本地路径，带版本和稳定 sort key；
- query 长度、tag 数、批量 ID 数有上限；
- 默认排序 `last_activity DESC, library_id ASC`；
- cursor 与 filter 绑定，filter 改变后旧 cursor 拒绝；
- 未知 view/action 返回稳定 4xx。

批量 action：`archive`、`unarchive`、`mark_completed`、`remove_saved`、`add_tag`、
`remove_tag`，第一版最多 100 项且逐项返回结果。

### 4.3 ConsumptionStore v2

在保持现有文件兼容的前提下：

- 增加单项 remove/patch 接口；
- 允许 progress 降低仅在用户显式“重新开始”时；
- 将 max_items 变成配置但仍有硬上限；
- 支持分页迭代，避免 API 一次加载全部；
- 明确 `unbookmark` 不删除 history；
- migration 只补默认字段，不改原有 timestamps。

### 4.4 文件导入

Browser 通过文件 input 向 local node 上传，或 Tauri 使用安全文件 picker。第一版
统一复制一份到 Ryn managed imports，原文件始终不被修改。

目录：

```text
RYNMESH_HOME/library/imports/<import_id>/
  metadata.json
  original.bin
  extracted.json
```

规则：

- 支持 `.txt`、`.md`、`.pdf`；
- 单文件和总 imports 配额；
- 文件名清理，不作为磁盘路径；
- magic/MIME/extension 交叉检查；
- PDF 页面数、解压/对象数、提取文本总量和 CPU 时间有上限；
- 原子完成：只有 checksum 和 metadata 完成后进入 ready；
- 解析失败仍可保留原文件，但 `ask=false`；
- 删除 import 只删除 Ryn managed copy，不删除用户原文件；
- 上传中断的临时文件有定时清理。

### 4.5 本地搜索

v1 搜索标题、来源、标签和有界提取文本：

- 索引只位于 RYNMESH_HOME；
- 不发往外部服务；
- 索引格式版本化，可重建；
- 不把全文放入普通日志；
- query 和结果有上限；
- 无索引时降级为元数据搜索。

优先使用 SQLite FTS5（若发布环境稳定包含），否则实现有界内存扫描作为 MVP，但
必须对 1000/10000 条规模做性能验收。

## 5. 与来源和 watcher 的整合

`GET /api/local/library/sources` 聚合：

- builtin/user added；
- enabled/paused；
- last success/attempt；
- cached items；
- safe error；
- next retry；
- watcher last change。

新增 pause/resume，不通过 remove 模拟暂停：

- pause 停止未来 fetch/check；
- 历史、缓存和用户反馈保留；
- remove 需要说明是否移除缓存；
- builtin source 可暂停，是否可删除沿用 Digest policy；
- 恢复后立即或排队一次 bounded refresh。

## 6. Webapp 实施

### 6.1 文件

- `webapp/src/screens/MyContent.tsx`；
- `webapp/src/screens/MyContent.module.css`；
- `webapp/src/domain/libraryClient.ts`；
- `webapp/src/domain/libraryTypes.ts`；
- `webapp/src/components/LibraryCard.tsx`；
- `webapp/src/components/LibraryFilters.tsx`；
- `webapp/src/components/ImportProgress.tsx`；
- `webapp/src/components/SourceHealthRow.tsx`。

### 6.2 路由与导航

- 新路由 `/library`，用户显示“我的内容”；
- `/explore` 保留为全网/本地内容探索，高级入口；
- Digest 中来源、watcher、历史管理逐步移到 Library，只保留轻量入口；
- Home 的 continue reading 和 saved 链接到对应 Library view；
- Ask Ryn 的 context picker 使用 Library API。

### 6.3 UI 状态

- tabs：已保存、继续使用、历史、文件、来源与监控；
- URL 只保存非敏感 filter/view，不保存 query 正文或文件名；
- infinite scroll 或“加载更多”使用 cursor；
- selection 跨页规则明确，v1 只保留当前已加载选择；
- 批量删除/取消保存可撤销时优先 toast undo；
- 高风险删除使用现有 ConfirmDialog；
- loading/empty/error/degraded/partial-results 都有专门状态。

## 7. 与 Ask Ryn 集成

- Library item `ask=true` 才能被选择；
- 最多选择 20 项，发送时仍服从 Assistant 总 context limit；
- 选择完成后调用 `/assistant/context/prepare`；
- UI 展示包含/排除和截断；
- 发送给 friend/cloud target 时重新确认 privacy boundary；
- 删除 conversation context 不删除 Library item；
- 删除 Library local copy 后，已有会话 ref 显示 missing，不静默改用网络重新获取。

## 8. 数据迁移与隐私

- 启动时 lazy projection 现有 consumption，无需重写所有记录；
- read-later pseudo-source items 映射为 saved；
- watcher 保持原 ID；
- metadata store 不认识的 library_id 保留为 orphan，后台可清理但不自动删除用户数据；
- privacy export 增加 library metadata、imports manifest、source paused state；
- 默认 export 不打包大文件；用户可另选“包含文件副本”；
- erase scopes 增加 `library_metadata`、`library_imports`、`library_index`；
- 清理 index 不删除原始 Library 数据，可重建。

## 9. 实施 PR 切片

1. `feat: add paginated library projection API`
2. `feat: add archive tags and source pause state`
3. `feat: add My Content saved and continue views`
4. `feat: add bounded local file imports and extraction`
5. `feat: connect Library selections to Ask Ryn`
6. `feat: add source and watcher management view`
7. `test/docs: validate library privacy migration and scale`

## 10. 测试矩阵

### Backend

- consumption v1 migration；
- saved/continue/history projection；
- stable cursor/no duplicates/no omissions；
- cursor/filter mismatch；
- archive/tags and batch partial failure；
- source pause/resume/remove；
- watcher status；
- TXT/MD/PDF import；
- wrong MIME, oversize, page bomb, timeout, interrupted upload；
- checksum/local copy missing；
- index rebuild and query limits；
- export/erase scopes；
- filename/path/body marker absent from list/log。

新增 `tests/test_library.py`、`tests/test_library_imports.py`，扩展
`tests/test_consumption.py`、`tests/test_digest.py`、`tests/test_reader_and_steering.py`。

### Webapp

- tabs/filters/cursor；
- save from Digest appears immediately；
- continue progress；
- batch selection/actions；
- import progress/failure/delete；
- source health/pause/resume；
- Ask Ryn context picker；
- item missing/local copy missing；
- responsive/keyboard/screen reader；
- 1000+ records rendering without full DOM expansion。

### E2E

- save → Library → open → progress → restart → continue；
- add source → pause → confirm no fetch → resume；
- watch page → simulated change；
- import PDF → Ask Ryn → delete Ryn copy → original remains；
- export and scoped erase。

## 11. 验收条件

### 功能验收

- [ ] 一个页面可查看已保存、继续使用、历史、文件、来源和监控。
- [ ] For You 收藏在一次刷新内出现在 Library。
- [ ] 阅读/播放进度重启后恢复，完成/重新开始语义正确。
- [ ] 搜索、筛选、排序和 cursor 分页无重复遗漏。
- [ ] 来源暂停保留历史并停止未来抓取，恢复后可继续。
- [ ] TXT、Markdown、PDF 能安全导入并显示解析状态。
- [ ] 一项或多项 Library 内容可以交给 Ask Ryn。
- [ ] 移除 Ryn managed copy 不删除用户原文件。

### 安全与隐私验收

- [ ] 本地文件默认不发布、不发送远端、不参与公开推荐。
- [ ] 列表 API、URL、普通日志不暴露完整本地路径或正文。
- [ ] 文件大小、总配额、PDF 页面/解析时间、batch/query/limit 全部有界。
- [ ] 恶意文件、路径逃逸、MIME 欺骗和解析炸弹 fail closed。
- [ ] 发送到 friend/cloud 前重新显示材料范围和 privacy boundary。
- [ ] export/erase 覆盖 metadata、imports 和 index，默认 export 不含大文件。

### 质量验收

- [ ] focused/full pytest 与 ruff 通过。
- [ ] Webapp `npm test`、`npm run lint`、`npm run build` 通过。
- [ ] 10,000 条元数据的分页首屏和常用筛选在约定性能预算内。
- [ ] 破损 index 可重建，破损单项不阻断整个 Library。
- [ ] Windows/macOS 文件导入和删除语义物理验收通过。

### 验收证据

- 保存/继续/来源/导入/Ask Ryn 五条用户流程录屏；
- 10,000 条 scale 报告和 cursor 一致性测试；
- PDF bomb/oversize/path/MIME 负面测试；
- 原文件未被删除的前后 hash；
- URL/log/API path/body marker 负面搜索；
- migration、export/erase、commits 和回滚说明。

## 12. 发布与回滚

- feature flag `library_workspace_v1`；
- 旧 Digest 管理 UI 在一个版本内保留为 fallback；
- LibraryService 是投影，回滚不删除 consumption/source/watcher 真相数据；
- imports 在 feature rollback 后仍保留，提供清理/导出工具；
- 不进行不可逆 consumption rewrite；
- 索引可随时删除重建，不作为唯一数据源。
