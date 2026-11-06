# Ryn 助手“暂时无法刷新”修复方案（交给 Codex 执行）

- 日期：2026-11-05
- 基线：`develop` @ `235f7c4`
- 现场诊断记录：`artifacts/ryn-connection-diagnosis-20261105.json`
- 本文只描述要做什么、怎么判定做对了，不含代码。行号基于 `235f7c4`，只作参考，以函数名为准。

---

## 0. 执行规则（开始前必须读完）

1. **只做本文列出的改动。** 没写到的文件、函数、行为一律不改。不要“顺手优化”，不要重命名，不要格式化无关代码。
2. **分支：** 从最新 `develop` 切出 `codex/connection-starvation-fix`。开始前先 `git status`，工作区里别人的未提交改动保持原样：不 stash、不 checkout 覆盖、不删除。
3. **每个步骤单独提交**（提交信息见各步骤末尾）。每步提交前必须跑完该步列出的测试并全部通过；没通过不得进入下一步。
4. **对不上就停。** 如果本文描述和代码对不上（函数不存在、行为和描述不同、某项改动会牵连本文没提到的文件），停下来，把情况写进验收文档的“待确认”一节，不要自己猜着改。
5. **不得提交：** `artifacts/`、日志、数据库、缓存、凭据、构建产物。
6. **本方案不涉及** `D:/code/chat-backend`，不要改它。
7. **第一个提交：** 把本文件加入仓库，提交信息 `docs: plan the WebView connection starvation fix`。

---

## 1. 已确认的故障链（为什么要这么改）

### 1.1 现象

助手页同时出现“暂时无法刷新任务列表”“暂时无法刷新当前任务”“无法加载已创建的文件”，任务读秒停止。

### 1.2 原因

- Ryn 界面（WebView2）的所有请求都发往同一个地址 `http://127.0.0.1:8791`。Chromium 对同一地址的普通 HTTP/1.1 请求，最多同时开 **6 条**连接；多出来的请求在浏览器内部排队，根本不会发到网络上。
- CORS 预检（OPTIONS）不带凭据，走另一组连接，所以总能成功。现场看到的“预检成功、正式 GET 没发出”就是这个原因。
- 以下轮询在**每个页面**都在跑（`PersonalProvider`、`DropProvider` 都挂在 `App.tsx` 顶层）：

| 位置 | 请求 | 频率 | 现场耗时 | 等上一次结束 | 超时 |
|---|---|---|---|---|---|
| `App.tsx` 设备列表 quick | `GET /peers?quick=1` | 2 秒（setInterval） | 约 6.7 秒 | 否 | 无 |
| `App.tsx` 设备列表 full | `GET /peers` | 前 30 秒每 3 秒，之后 15 秒 | 超过 12 秒 | 是（与 quick 之间会重叠） | 无 |
| `personal/model.tsx` | `GET /directory` + `GET /space` | 15 秒（setInterval） | 38.7–42.9 秒 | 否 | 无 |
| `App.tsx` 发现状态 | `GET /discovery/status` | 5 秒（setInterval） | 未测 | 否 | 无 |
| `personal/dropStore.tsx` | SSE 事件流 | 常驻 | 长连接 | — | — |

- 结果：5 条连接被 `/peers`、`/directory` 占住，1 条被 SSE 占住。助手的任务列表、任务详情、文件列表请求只能排队。前端每个读请求限时 10 秒（`webapp/src/domain/rynAgent.ts` 的 `READ_TIMEOUT_MS`），超时后出现三条报错。读秒只在拿到新数据后最多再走 3 秒（`RynAgentProgress.tsx` 的 `RynAgentTurnDuration`），所以也停住。
- 后台本身回应很快（curl 直连任务列表 125 ms），节点健康检查发现不了这个问题。

### 1.3 `/peers`、`/directory` 为什么慢（代码已确认）

1. **积分榜重复计算。** 每一行设备都调用一次 `credit_ledger.account(peer_id)`（`rynmesh/peer_http.py` 的 `account_for`，在 `peer_item` 里调用）。而 `FileCreditLedger.account()`（`rynmesh/credits.py`）每次都重算整个积分榜：把 `events` 目录下全部积分事件文件（现场 480 个）从磁盘读一遍，每个签名验两遍（`list_events` 验一次，`scoreboard` 又验一次）。总量是“设备数 × 480 × 2”次验签。
2. **同步等公共注册中心。** 非 quick 请求在请求内部访问公共注册中心（`rynmesh/registry.py` 的 `HttpPeerRegistry`，单次超时 10 秒）。11-03 的记录显示公共注册中心不可达，所以一次 `/directory` 里的设备列表、广告容量、服务发现，每一项都可能等满 10 秒，再加上 `probe_peers` 的网络探测。
3. **并发越多越慢。** 多个请求同时跑，互相抢 CPU，形成恶性循环。

### 1.4 另一个独立缺陷

用 `\\localhost\C$\...` 路径启动安装版 `Ryn.exe` 时，`webapp/src-tauri/src/node.rs` 的 `ordinary_windows_launch_path` 拒绝 UNC 路径，`sidecar_path` 随即回退到编译时写死的开发目录 `D:/code/rynmesh/webapp/src-tauri/binaries/...`，实际跑的是开发版后台，而不是安装版。

---

## 2. 验收标准（全部做到才算完成）

- **A. 连接数：** 在新安装的版本里打开助手页、运行任务 10 分钟，每 2 秒采样一次 WebView2 到 `127.0.0.1:8791` 的已建立连接。除 SSE 外，同时在途的连接 ≤ 3 条；任何时刻 ≥ 5 条的状态都不得持续超过 5 秒。
- **B. 后台耗时**（本机回环 curl，每个接口请求 20 次，取 p95）：
  - `GET /api/local/peers?quick=1` < 300 ms
  - `GET /api/local/peers` < 1 s
  - `GET /api/local/directory?quick=1` < 1 s
  - `GET /api/local/directory` < 3 s
  - 公共注册中心不可达时，以上四项同样要满足。
- **C. 真实任务：** 运行一次 5 分钟以上的真实助手任务（例如 Gshopper 七个问题那类）。期间切到“设备”“服务”页再切回来，三条报错一次都不出现，读秒一直在走。
- **D. 自动恢复：** 任务运行中用托盘菜单“重启节点”。节点恢复后 20 秒内，页面自己恢复（报错消失、读秒继续），不需要点“刷新”。
- **E. 前端单元测试证明：** 即使 `/peers`、`/directory` 每次都要 60 秒，每个轮询器同一时刻最多 1 个在途请求（见步骤 1 的测试）。
- **F. 启动路径：** 安装版不论从开始菜单启动，还是从 `\\localhost\C$\...` 启动，节点日志里记录的后台路径都是安装目录下的 `rynmesh-peer.exe`。Release 构建找不到安装版后台时，不得启动开发目录或 PATH 里的其他后台。

---

## 3. 步骤 1：前端轮询改成“一次只发一个、有超时、离开页面就取消”

### 1.1 新建公共轮询工具 `webapp/src/domain/polling.ts`

提供一个启动轮询的函数（名字自定，例如 `startPoller`）。行为必须**严格**如下：

1. **参数：** 要执行的异步函数（接收一个 `AbortSignal`）、正常间隔、超时时长、窗口隐藏时的间隔、失败退避上限。
2. **不得使用 `setInterval`。** 上一次执行结束（无论成功、失败还是超时）之后，才用 `setTimeout` 安排下一次。
3. 同一个轮询器同一时刻**最多 1 个**在途请求。
4. 每次执行新建一个 `AbortController`，超时就 abort，并把它的 signal 传给异步函数。
5. **窗口隐藏**（`document.visibilityState === "hidden"`）时使用“隐藏间隔”。窗口重新可见时，如果距上次开始执行已超过正常间隔，立刻执行一次；如果当时有请求在途，则不重复发。
6. **失败退避：** 间隔 = 正常间隔 × 2^(连续失败次数 − 1)，不超过退避上限；成功后清零。
7. **“立即刷新”方法：** 没有请求在途时立即执行；有请求在途时只记一个标记，等在途请求结束后马上再执行一次。期间调用多少次都只算一次。
8. **停止方法：** 清掉定时器、abort 在途请求，之后不再执行，也不再调用任何回调。组件卸载时必须调用。
9. 超时引起的 abort 要能和其他错误区分开（调用方能判断“这是超时”）。

测试 `webapp/src/domain/polling.test.ts`（用 vitest 假定时器，写法参照 `webapp/src/domain/rynAgentRequests.test.ts`），至少覆盖：

1. 异步函数耗时 60 秒、间隔 2 秒、超时大于 60 秒：任何时刻在途数 ≤ 1，60 秒内只调用 1 次。
2. 超时 5 秒：第 5 秒 signal 被 abort，随后按退避安排下一次。
3. 窗口隐藏时使用隐藏间隔；变为可见时立即执行一次，且不与在途请求重叠。
4. 连续失败时间隔递增并封顶，成功后复位。
5. 请求在途时调用“立即刷新”3 次，结束后只额外执行 1 次。
6. 停止后：在途请求被 abort，不再有任何调用和回调。

### 1.2 让节点客户端支持取消和超时

涉及文件：`webapp/src/domain/liveNodeClient.ts`、`webapp/src/domain/nodeClient.ts`、`webapp/src/domain/fixtureNodeClient.ts`、`webapp/src/domain/digestClient.ts`、`webapp/src/domain/fixtureDigestClient.ts`。

1. `liveNodeClient.ts` 的 `requestJson`：
   - 确认调用方通过 init 传入的 signal 不会被覆盖。
   - **新增：** GET 请求如果调用方没有传 signal，默认 60 秒超时，防止任何读请求无限占用连接。POST/PUT/PATCH/DELETE **不加**默认超时（可能是长操作）。
   - 超时抛出 `NodeClientError`，status 为 `0`，消息可区分（例如 “Local Ryn node request timed out”，经 `tr` 翻译）。
2. `digestClient.ts` 有自己的 `requestJson`，按同样规则修改。
3. **只给以下方法**加取消能力，其他方法签名不动：
   - `listPeers`：在**末尾新增**一个可选参数 `{ signal?: AbortSignal }`。**不要**把 signal 放进 `filters`，因为 `filters` 会被 `qs()` 拼进查询字符串。
   - `directory`：在现有 options 对象里加可选的 `signal`；确认 `qs()` 仍然只拼 `quick`。
   - `spaceStatus`：新增一个可选参数 `{ signal?: AbortSignal }`。
   - `digestClient` 的 `getDiscoveryStatus`：新增一个可选参数 `{ signal?: AbortSignal }`。
   - `nodeClient.ts` 的接口声明、live 实现、fixture 实现都要同步修改；fixture 实现忽略 signal 即可。

测试（已有测试文件就加进去，没有就新建 `webapp/src/domain/liveNodeClient.test.ts`）：

- GET 不传 signal 时，60 秒后抛出 status 为 0 的错误。
- 传入 signal 后 abort 能取消请求。
- POST 不会自动超时。
- `listPeers` 传了 signal 后，请求 URL 的查询字符串里没有多余参数。

### 1.3 改造常驻轮询（必须改的清单，只改这些）

**(1) `webapp/src/App.tsx` 设备列表轮询**（`listPeers({ quick: true })` 的 `setInterval(quick, 2000)` 和 `full` 链）合并成一个轮询器：

- quick 和 full 合计，同一时刻最多 1 个在途。
- 启动时先执行一次 full；之后每 5 秒执行一次 quick；距上次 full 超过 15 秒时，这一轮用 full 代替 quick。取消“启动后 30 秒内每 3 秒 full”的旧行为，统一为 15 秒。
- 超时：quick 5 秒，full 20 秒。窗口隐藏时间隔 30 秒，并且只跑 quick。
- 现有的 `setIfChanged` 合并逻辑（quick 合并、full 替换）保持不变。
- 组件卸载或 `node?.peer_id` 变化时，停止轮询并 abort 在途请求。

**(2) `webapp/src/App.tsx` 发现状态轮询**（`digestApi.getDiscoveryStatus`，5 秒）：

- 改用轮询器，间隔 5 秒，超时 8 秒，隐藏时 30 秒。
- `ryn-discovery-seen` 事件改为调用“立即刷新”。
- 通知逻辑不变。

**(3) `webapp/src/personal/model.tsx` 中 `PersonalProvider` 的 `refresh`**（15 秒，`/directory` + `/space`）：

- 改用轮询器，间隔 15 秒，超时 25 秒，隐藏时 60 秒。
- 同一轮里的 `/directory` 和 `/space` 共用这一轮的 signal。
- **回退规则要改：** 现在 `/directory` 只要失败，就会再发 `listLLMServices` 和 `peersHealth`（后者是 POST，会做完整发现加探测，同样很慢）。改为：**只有** `/directory` 返回 404 或 501（节点没有这个接口）时才回退。超时、取消、5xx、网络错误一律不回退，保留上一次的结果，错误提示按现有方式显示。
- `refresh` 还通过 `PersonalContext` 暴露给各页面（`PersonalState.refresh`，约第 91 行声明、第 447 行放进 context），页面会手动调用并 await。先用 `git grep` 列出所有调用方，写进提交说明。手动调用要走轮询器的“立即刷新”，不能另起并发请求，并且必须保持可以 await（等到这一轮结束才 resolve）。`PersonalState.refresh` 的类型签名不变。如果做到这一点会牵连较多文件，先保证手动调用不会与轮询重叠，并在验收文档“待确认”里写明。

**(4) `webapp/src/personal/model.tsx` 待加入成员时的 `spaceStatus` 轮询**（1.5 秒，`setInterval`）：改用轮询器，间隔 1.5 秒，超时 5 秒。

**(5) `webapp/src/screens/Home.tsx` 的 `updateDiscovery`**（4 秒，不等上一次结束）：改用轮询器，间隔 4 秒，超时 8 秒，隐藏时 30 秒。

**(6) `webapp/src/screens/Devices.tsx` 的 5 秒轮询：** 已有防重叠（`polling` 标记），只需在组件卸载时 abort 在途请求。如果 `load` 不方便传 signal，就保证卸载后不再处理结果，并在验收文档里注明。

**不要改的（本次）：**

- `personal/Desktop.tsx`：走 Tauri `invoke`，不占 HTTP 连接。
- `personal/Shell.tsx`：已有防重叠。
- `CraftApps.tsx` 的安装进度轮询。
- 其余页面级轮询（见第 8 节）。
- 所有 SSE（`dropStore.tsx`、`Chat.tsx`、`useLLMStream.ts`）。

**测试：**

- 设备列表轮询：新建 `webapp/src/App.peersPolling.test.tsx`（或放进已有的 App 测试）。用一个 `listPeers` 永不返回的假客户端，推进 60 秒：`listPeers` 在途数始终 ≤ 1；卸载后 signal 被 abort。
- `webapp/src/personal/directoryModel.test.tsx` 新增三个用例：
  1. `/directory` 超时时，不调用 `listLLMServices` 和 `peersHealth`。
  2. `/directory` 返回 404 时仍然回退。现有用例 “falls back to the separate lists when the node has no usable directory” 要继续通过；如果它模拟的是非 404 错误，改成 404，并在提交说明里写明。
  3. `/directory` 永不返回时，60 秒内只有 1 个在途请求。
- 运行顺序：

  1. `cd webapp`
  2. `npx vitest run src/domain/polling.test.ts src/domain/liveNodeClient.test.ts src/personal/directoryModel.test.tsx src/App.peersPolling.test.tsx`
  3. `npm run lint`
  4. `npm test`（全量）

提交：`Stop background polls from exhausting the WebView connection pool`

### 1.4 助手页：出错后不再停止轮询

涉及文件：`webapp/src/screens/RynAgent.tsx`、`webapp/src/screens/RynAgentArtifacts.tsx`。

1. **任务列表轮询**（`rynAgent.tasks` 那个 useEffect 的 catch，约 525–531 行）和**任务详情轮询**（约 556–563 行）：删除“非临时错误就 `return` 停止轮询”的行为。改为：
   - 任务不存在（`isMissingAgentTaskError`）：保持现有处理不变。
   - 其他所有错误：继续轮询。间隔用 `agentReadBackoff(failures)`，但非临时错误的间隔最小 5 秒、最大 30 秒。
   - 报错提示的显示规则不变（列表连续失败超过 1 次才显示；详情按现有规则）。成功一次就清除提示（现有逻辑已经这样）。
2. **`refresh()`**（约 391–442 行，窗口获得焦点时也会调用）：现在任何一次失败都会立刻显示提示。改为：如果错误是临时错误（`isTransientAgentReadError` 为真），并且已有缓存数据（任务列表加载过 / 当前任务在 `taskCache` 里），就不显示提示，交给轮询去判断。其他情况维持现状。
3. **`RynAgentArtifacts.tsx` 文件列表加载**（约 208–214 行）：非临时错误也要重试，间隔 15 秒，最多连续 4 次，之后停止，保留现有的“刷新”按钮。临时错误保持现有退避。已有文件列表时出错不清空列表（现状已是如此，确认不变）。
4. **不改** `READ_TIMEOUT_MS`（10 秒），**不改** `readWithRecovery`。

测试：加到现有测试文件里——轮询相关的加到 `webapp/src/screens/RynAgent.test.tsx` 或 `webapp/src/screens/RynAgentConnection.test.tsx`（先读这两个文件，选已经在模拟 `rynAgent.tasks` / `rynAgent.task` 的那个），文件列表相关的加到 `webapp/src/screens/RynAgentArtifacts.test.tsx`。模拟 `rynAgent` 的方式照抄文件里现有的写法：

- `tasks` 先返回两次 409，再返回 200：轮询一直继续，最终提示消失。
- 任务详情返回 409 后继续轮询。
- 已有缓存时，`refresh` 遇到 status 0 的错误不显示提示。
- `artifacts` 返回 409：15 秒后重试，最多 4 次。

运行：上面这些测试 → `npm run lint` → `npm test`。

提交：`Keep agent polling alive after read errors`

---

## 4. 步骤 2：积分榜只算一次（后端）

涉及文件：`rynmesh/credits.py`、`rynmesh/peer_http.py`；测试 `tests/test_credit_policy.py`（已有）、新建 `tests/test_credit_scoreboard_cache.py`。

### 2.1 `FileCreditLedger` 内部缓存

1. **只读一次、只验一次签：** 重建缓存时，`events` 目录下每个文件只读一次、只验签一次，得到“已验证事件”列表；按类别筛选时复用这份列表，`scoreboard` 里不再二次验签。
2. **结果必须完全一致：** `list_events`、`scoreboard`、`account`、`find_by_dedupe` 的返回值和现在完全相同（同样的过滤、同样的排序、同样跳过坏文件）。
3. **缓存失效条件**（三条都满足才复用，任一不满足就重建）：
   1. 事件目录“指纹”没变：只用 stat（不读文件内容）得到文件数量、最大修改时间、总字节数，任一变化即失效。这样能发现其他进程写入的事件。
   2. 距上次重建不超过 60 秒。原因：验签结果依赖签发方的身份等级（`tier_resolver`），等级变化不会改动文件。
   3. 本进程在此期间没有调用过 `append` / `record`（写入后立即失效）。
4. 积分榜结果按 category 和“是否使用 tier_resolver”分别缓存。
5. **线程安全：** 用一把锁保护重建。多个线程同时需要重建时只算一次，其他线程等待结果，不得各算各的。
6. **不能泄漏缓存对象：** `scoreboard` 每次返回新的 dict，里面的 `CreditAccount` 要复制；如果 `CreditAccount` 本身是不可变的（frozen dataclass），在代码注释和提交说明里写明，可以不复制。调用方修改返回值，不能影响缓存。
7. **新增公开方法 `accounts_for(peer_ids, category=GLOBAL_CATEGORY)`：** 一次取多个 peer 的账户；没有记录的 peer 返回和现在 `account()` 一样的空账户。

### 2.2 `peer_http.py` 设备列表

1. `discover_peer_items`：在循环外调用一次 `accounts_for(全部 peer_id)`。`peer_item` 改为接收已经取好的账户，不再逐个调用 `account_for`。
2. `peer_item` 的输出字段和取值完全不变。
3. 每个设备都会调用的 `peer_tier`（即 `_resolve_identity_tier`）：先测单次耗时。超过 5 ms 时，在同一次 `discover_peer_items` 调用内对同一 peer_id 只算一次；否则不动。把测量结果写进验收文档。

### 2.3 测试

新建 `tests/test_credit_scoreboard_cache.py`：

1. 构造 50 个以上的事件（包含多个签发方、多个对象、多个类别，外加一个坏文件）。有缓存时和清空缓存后算出的 scoreboard 完全相等。
2. 连续调用 `account()` 10 次，验签函数（monkeypatch 给 `verify_credit_event_with_policy` 计数）总调用次数等于事件数，即只重建一次。
3. `append` 一个新事件后，下一次 scoreboard 包含它。
4. 绕过 ledger 直接在 events 目录写入一个合法事件文件（模拟其他进程），下一次 scoreboard 包含它。
5. 修改 tier_resolver 的返回值，并把时钟推进 60 秒以上（monkeypatch 时钟），结果反映新的等级。
6. 8 个线程同时调用 scoreboard，只重建一次。
7. 修改返回的 `CreditAccount` 或字典，不影响下一次的返回值。

另外，在 peer_http 层加一个测试：20 个设备、480 个事件时，一次 `GET /api/local/peers?quick=1` 的验签次数 ≤ 480。搭建 app 的方式参照 `tests/test_peer_directory.py`。

运行（使用仓库 `.venv`）：

1. `python -m pytest tests/test_credit_policy.py tests/test_credit_scoreboard_cache.py tests/test_peer_directory.py tests/test_peer_health.py tests/test_peer_health_responsiveness.py tests/test_peer_http_auth.py -q`
2. 全量后端测试。

提交：`Compute the credit scoreboard once per change instead of per device`

---

## 5. 步骤 3：`/peers`、`/directory` 不再同步等公共注册中心（后端）

### 3.1 先测量，再改

1. 在 `GET /api/local/peers`（非 quick）和 `GET /api/local/directory` 中按阶段计时。阶段包括：注册中心取设备（`discover_peers`）、广告容量（`advertised_capacities`）、积分（步骤 2 之后）、探测（`probe_peers`）、服务发现（`discover_services` / `app.state.llm_discover`）、成员与好友、`build_snapshot`。
2. 总耗时 ≥ 1 秒时写一行 warning 日志，只包含阶段名和秒数。**不要**写 peer_id、IP、URL、名字。格式参照 `rynmesh/agent_runtime/diagnostic_log.py` 的 `performance`。
3. 在现场机器上记录改动前的各阶段耗时，写进验收文档。这个计时日志保留在最终代码里。

### 3.2 请求只读缓存，注册中心由后台刷新

**关键事实：** `store.discover_peers(..., cache_only=True)` 已经能做到“注册中心部分读缓存、局域网发现记录实时读取”。而非 cache_only 的调用成功后，会通过 `_cache_peer_records` 更新这份缓存。广告容量也有自己的缓存（`peer_http.py` 里的 `advertised_cache`）。所以只需要：**请求永远读缓存，由后台去刷新缓存。**

新建 `rynmesh/peer_directory_refresh.py`，实现一个“目录刷新器”，每个 network_id 一份状态：

1. **刷新内容：**
   - 调用 `store.discover_peers(cache_only=False, ...)` 刷新注册中心缓存；
   - 调用广告容量的非缓存路径，刷新 `advertised_cache`；
   - 刷新服务发现结果。先检查 `llm_discover` 有没有只读缓存的模式：有就用同样的“请求读缓存、后台刷新”方式；没有就由刷新器保存最近一次结果，请求读这份结果。
   - 刷新完设备记录后，执行一次 `probe_peers`，结果写入现有的 `peer_presence`（`PeerPresenceCache`）。
2. **专用线程池：** 最多 2 个线程，线程名前缀 `ryn-directory-refresh`。**不得**使用 `asyncio.to_thread` 或默认线程池（默认池里同时跑着助手任务和所有后台任务）。
3. **同时只刷一次：** 同一 network_id 同一时刻最多 1 个刷新在途，两次刷新的开始时间至少相隔 15 秒。
4. **路由行为：**
   - quick 请求：维持现状。
   - 非 quick 的 `GET /api/local/peers` 和 `GET /api/local/directory`：注册中心部分一律按 `cache_only=True` 读取；`/directory` 不在请求内同步探测，改为读 `peer_presence`。如果距上次刷新已超过 15 秒且没有刷新在途，就触发一次后台刷新，**不等待**它完成。
   - **冷启动**（完全没有缓存的设备记录）：触发刷新，最多等 8 秒；等不到就按现有 cache_only 的结果返回。
   - 积分、成员、好友、`build_snapshot` 仍在请求内计算（步骤 2 之后很快）。
5. **不走刷新器的接口：**
   - `POST /api/local/peers/discover`（用户主动发现）：保持现状，同步访问注册中心。
   - `POST /api/local/peers/health`（用户主动检查）：保留同步探测，但要做到同时只探测一次——并发的检查请求共享同一次探测结果。
6. **响应格式不变。** 不新增必填字段；如果要标记“旧数据”，只能加可选字段，前端不依赖它。
7. **关闭：** 节点关闭时（app lifespan 结束）关闭专用线程池，不阻塞退出。

### 3.3 测试

新建 `tests/test_peer_directory_refresh.py`：

1. 注册中心的假实现每次 sleep 30 秒：已有缓存时，连续 5 次 `GET /api/local/directory` 每次都在 1 秒内返回，注册中心只被调用 1 次。
2. 冷启动没有缓存：第一次请求在 8.5 秒内返回。
3. 15 秒内重复请求不触发新的刷新；超过 15 秒触发一次。
4. 刷新抛异常：保留旧缓存，下一次仍能刷新，不影响响应。
5. 刷新在名字以 `ryn-directory-refresh` 开头的线程里执行（断言线程名）。
6. 并发 5 个 `POST /api/local/peers/health`，只发生一次探测。
7. 现有的 `tests/test_peer_directory.py`、`tests/test_peer_health*.py` 全部通过。如果有用例依赖“请求内同步探测”，按新行为调整，并在提交说明里列出改了哪些断言、原因是什么。

提交：`Serve the device directory from a background-refreshed cache`

---

## 6. 步骤 4：UNC 启动路径（桌面壳）

涉及文件：`webapp/src-tauri/src/node.rs`。

1. **`ordinary_windows_launch_path`：** 在现有规则基础上，**只**额外接受本机的管理共享：
   - 接受的形式：`\\?\UNC\localhost\<盘符>$\<其余路径>`、`\\localhost\<盘符>$\<其余路径>`，以及主机为 `127.0.0.1` 的同样两种。主机名不区分大小写，盘符是单个字母。转换为 `<盘符>:\<其余路径>`。
   - 其他任何 UNC 路径（其他主机、非 `X$` 共享、`\\?\UNC\server\share`、`\\server\share`）仍然返回 None。
   - 现有测试 `packaged_sidecar_launch_uses_ordinary_physical_local_path` 的断言全部保持不变。
2. **`sidecar_path`：** 编译期开发目录回退（`CARGO_MANIFEST_DIR/binaries`）只在 debug 构建（`cfg(debug_assertions)`）里启用。Release 构建找不到安装版后台时返回 None，并写一条 error 日志：“installed node sidecar not found next to the app executable”。
3. **`build_command`：** 当 `sidecar_path` 返回 None 时，现在还会继续尝试 PATH 里的 `rynmesh-peer` 和 python，在 release 构建里这同样会跑错版本。改为：release 构建中，如果没有设置 `RYNMESH_PEER_CMD`，并且 `sidecar_path` 返回 None，就直接启动失败并记录日志，不再尝试 PATH 和 python。debug 构建保持现状。
4. **启动日志：** 节点启动时写一条 info 日志，记录实际使用的后台可执行文件路径（用于验收 F）。
5. **测试**（`node.rs` 的 tests 模块）新增断言：`localhost`、`127.0.0.1`、大小写混合都能正确转换；`\\otherhost\C$\...` 返回 None；`\\localhost\share\...` 返回 None。
6. 运行：`cd webapp/src-tauri`，然后 `cargo test`。

提交：`Resolve localhost admin-share launches to the installed node sidecar`

---

## 7. 步骤 5：集成、打包、安装、现场验收（按 AGENTS.md）

1. **合并：** 全部提交完成后，把分支合并到 `develop`。解决冲突后重新跑步骤 1–4 的全部测试。记录合并后的 develop 提交号。
2. **构建：** 从 develop 的已提交源码构建安装包，用之前 full-r* 打包的同一流程；不得从脏工作区构建。记录构建提交号和安装包 SHA-256。
3. **备份：** 安装前，把当前安装版（`Ryn.exe`、`rynmesh-peer.exe`、`plugins`、`plugin-services`）备份到 `%LOCALAPPDATA%\Ryn-install-backups\connection-starvation-<提交号>`，保留现有配置和用户数据。只结束已核实属于 Ryn 的进程。
4. **安装后检查：** 从开始菜单正常启动，然后检查：版本和提交号、节点健康、公司插件服务健康、日志里记录的后台路径是安装目录。
5. **逐条完成第 2 节的验收 A–F。** 测量方法：
   - **A：** 每 2 秒采样一次 `Get-NetTCPConnection -RemotePort 8791 -State Established`，按 OwningProcess 筛选 WebView2 的 network service 进程（命令行包含 `network.mojom.NetworkService`，父进程链能追到 `Ryn.exe`）。持续 10 分钟，记录最大值和分布。
   - **B：** 在本机用 curl 访问回环地址（本机直连被信任），每个接口 20 次，记录 p50/p95。另做一轮“公共注册中心不可达”的测量：断开公司 VPN，或在 hosts 里把注册中心域名指到不可达地址；测完立即恢复。
   - **C、D：** 用真实任务、托盘“重启节点”完成，截图或录屏存证。
   - **F：** 从 `\\localhost\C$\Users\<用户>\AppData\Local\Ryn\Ryn.exe` 启动一次，查看日志里的后台路径；之后再从开始菜单正常启动。
6. **验收文档：** 写 `docs/acceptance/connection-starvation-20261105.md`，内容包括：改动前后对比数据（3.1 的阶段耗时、B 的 p95、A 的连接数）、各步骤测试结果、提交号、安装包哈希、备份位置、“待确认”事项。不写 IP、peer ID、账号、凭据、个人路径。提交到 develop。

---

## 8. 明确不做（本次）

- 不提高 10 秒读取超时，也不关闭超时来掩盖问题。
- 不把助手请求换到 `localhost` 等另一个主机名来绕开 6 条连接的限制（这只是掩盖积压）。
- 不改 HTTP/2，不加代理。
- 不改积分规则、验签规则、注册中心协议、探测超时数值。
- 不改其余页面级轮询：`OfflineReading.tsx`（2 秒）、`Search.tsx`（3 秒）、`CraftApps.tsx` 安装进度（180 毫秒）、Friends/FriendFeed/SharedReading 等 5 秒轮询。它们只在对应页面打开时运行，记为后续：以后统一改用 `polling.ts`。
- 不动 SSE 连接。
- 不改助手后端的线程池。之前怀疑的线程池问题，这次的现场证据不支持，不在本次范围。

---

## 9. 回滚

- **代码：** 每步单独提交，可以单独 revert。
- **安装：** 用第 7 节第 3 步的备份目录覆盖回去，重启 Ryn，检查版本和健康状态。
