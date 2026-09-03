# DEV-05：两台设备简单配对与分享——完整开发与验收文档

状态：产品已批准；安全协议按本文评审后 implementation-ready  
产品需求：[USER_SIMPLE_PAIR_AND_SHARE_WORK_PLAN.md](USER_SIMPLE_PAIR_AND_SHARE_WORK_PLAN.md)  
优先级：P1  
前置依赖：#28 Transport POST；DEV-04 内容选择器  
网络边界：v1 仅承诺同一 LAN 或已经可直接到达的审核端点

## 1. 开发结果

两个普通用户可通过本地生成的二维码或粘贴邀请码完成：

```text
创建一次性邀请 → 本地验签 Review → 用户确认 → 建立关系 → 发消息/小附件/内容卡片
→ 离线恢复 → 本地立即撤销
```

v1 不开放 Private AI、VPN、Agent、文件系统、trust root 或 Credits 转移。跨 NAT/跨公网
只有在真实端点已经可达时工作，不宣传“任何网络扫码即连”。

## 2. 当前实现基线与必须修复的边界

### 可复用

- Ed25519 node identity 和 signed peer record；
- Registry/LAN/bootstrap discovery；
- X25519 `peer_box` 加密；
- `PeerMessenger` 文本和最大 5 MiB inline attachment；
- `MessagingStore` 本地 history/blob；
- `/api/local/messages`、SSE 和 Webapp Chat；
- Manifest、provenance、内容 fetch/preview；
- Transport/HttpPeerClient 基础。

### 现状不能直接作为好友安全边界

- `/api/peer/*` 只支持全 mesh `RYNMESH_NETWORK_KEY` gate；
- `/api/peer/pubkey` + message `from_pub` 使用 TOFU cache；
- 任意发现 Peer 都可以出现在 Chat，不代表双方好友；
- `_transport` 和 `_resolve_pubkey` 仍直接使用 urllib；
- message receive 未将应用 sender 与独立好友 credential 绑定；
- history JSONL 无锁、无截断恢复、无单项删除/撤销状态；
- 没有 invite/friend/revocation store。

因此，好友消息不得仅在现有 `/api/peer/msg` 外面加一个前端按钮。必须建立独立关系
认证，并在接收端验证 credential、peer identity 与加密 sender 一致。

## 3. v1 安全决策

本文将以下决策作为实现约束：

1. Friendship 与 `trusted_roots`、quarantine、推荐 trust tier 完全分离。
2. 邀请不包含 `RYNMESH_NETWORK_KEY`、local-control token 或长期关系秘密。
3. 邀请随机、签名、短期、一次性、可取消；默认 15 分钟，最大 24 小时。
4. Review 在任何网络连接前完成，列出所有端点及地址类型。
5. 接受成功后把 invite secret 轮换为新的 per-friend relationship secret。
6. 关系请求使用带 timestamp/nonce/body digest 的 HMAC，不发送原始 secret。
7. 消息 payload sender、好友认证 peer 和签名 peer record 必须是同一身份。
8. 本地撤销立即拒绝后续请求，不等待远端在线。
9. v1 权限固定为 `friend.message`、`friend.attachment.small`、
   `friend.content-card`，不能由邀请任意扩权。
10. v1 reachability 仅 LAN/直接可达；加密 Relay acceptance 是后续独立版本。

任何一项若要修改，必须更新 wire version 和 threat model，不能在实现中静默偏离。

## 4. 协议与密码设计

### 4.1 Invite payload

自定义文本/深链：`rynmesh://join/<base64url-envelope>`。复制粘贴为所有平台的必备
fallback；QR 只编码相同文本。

签名 payload：

```json
{
  "version": "ryn.friend-invite.v1",
  "invite_id": "invite_<128-bit random>",
  "inviter_peer_id": "<ed25519 public key>",
  "node_name": "Alice's Ryn",
  "network_id": "rynmesh-main",
  "endpoints": ["https://..."],
  "messaging_pubkey": "<x25519 pub>",
  "permissions": ["friend.message","friend.attachment.small","friend.content-card"],
  "issued_at": "...",
  "expires_at": "...",
  "one_time_secret": "<256-bit random>"
}
```

Ed25519 对完整 canonical payload 签名。`inviter_peer_id` 必须等于签名公钥。

本地 InviteStore 只保存：

- keyed hash of secret；
- signed payload hash；
- timestamps/state/permissions/endpoints；
- 使用后的 accepter peer ID。

原始邀请只在创建响应中返回一次。

### 4.2 Accept request

`POST /api/peer/friends/accept` 是唯一允许 invite credential 的 route。请求包含：

- invite ID + secret proof；
- accepter 最新 signed peer record；
- accepter X25519 key；
- exact v1 permissions；
- timestamp、nonce；
- accepter 对上述字段的 Ed25519 signature。

接收端依次：

1. 使用不泄漏服务存在性的通用 404 处理无效 probe；
2. 限制 body、频率和并发；
3. 验证 invite hash、expiry、cancelled/used；
4. 验证 signed peer record 和 peer ID；
5. 验证 permission 精确相等、不允许扩权；
6. 验证 timestamp/nonce；
7. 原子 compare-and-consume invite；
8. 创建新的 256-bit relationship secret；
9. 写入 active FriendRecord；
10. 使用 accepter X25519 key 加密返回 secret、inviter current peer record 和权限。

并发接受必须只有一个 winner。数据库/JSON lock 与 consume 写入必须在同一原子临界区。

### 4.3 Relationship authentication

关系 secret 只存双方本地受限文件/系统密钥存储。请求 header：

```text
X-Ryn-Friend-Id: <relationship id>
X-Ryn-Friend-Peer: <sender peer id>
X-Ryn-Friend-Time: <unix seconds>
X-Ryn-Friend-Nonce: <random>
X-Ryn-Friend-MAC: HMAC-SHA256(derived_key, canonical_request)
```

Canonical request 必须绑定：协议版本、sender、receiver、method、normalized path、
timestamp、nonce、SHA-256(body)。使用 HKDF/domain separation 为 request MAC 派生 key，
不直接使用 raw relationship secret。

接收端要求：

- time skew 默认 ±120 秒；
- nonce cache 按 relationship 存储并有界；
- MAC constant-time compare；
- relationship active 且 route permission 允许；
- body 中签名/encrypted sender 与 header sender 一致；
- revoked relationship 在所有其他验证前不泄漏差异化信息。

### 4.4 Revocation

本地 revoke 事务：

1. FriendRecord 标记 revoked；
2. 删除/封存 active secret；
3. 新请求立即拒绝；
4. 生成签名 `ryn.friend-revocation.v1`；
5. direct best-effort 发送，失败进入有界 retry；
6. 保留不含 secret/body 的 delivery state。

远端 revocation 验证双方 peer ID、relationship/invite ref、signer、timestamp 和
idempotency。重放同一 revocation 是成功幂等，不得影响其他关系。

## 5. 存储设计

新增 `rynmesh/friends/` package：

- `models.py`：versioned Invite/Friend/Revocation；
- `store.py`：原子、加锁 stores；
- `crypto.py`：secret hash/HKDF/MAC/canonicalization；
- `invites.py`：create/inspect/consume/cancel；
- `middleware.py`：route-scoped friend auth；
- `service.py`：local API orchestration；
- `revocation.py`：local-first + retry。

目录：

```text
RYNMESH_HOME/friends/
  invites.json
  relationships.json
  revocations.json
  nonce-cache.json
  secrets/<relationship_id>
```

规则：

- Windows/macOS/Linux 使用可实现的最严格文件权限；
- JSON store 写临时文件 + fsync/replace；
- 进程内 lock，并拒绝多进程不安全并发启动；
- secret 与 public record 分文件；
- corruption 不得把 revoked 恢复成 active；
- diagnostics/export 默认只返回 secret presence 和 shortened IDs；
- personal erase 必须先 revoke active relationships，再删除本地数据。

## 6. API 设计

### 6.1 Local control API

```http
POST   /api/local/friends/invites
GET    /api/local/friends/invites
POST   /api/local/friends/invites/inspect
DELETE /api/local/friends/invites/{invite_id}
POST   /api/local/friends/join
GET    /api/local/friends
GET    /api/local/friends/{peer_id}
POST   /api/local/friends/{peer_id}/revoke
POST   /api/local/friends/{peer_id}/retry
```

Local API 可返回状态和安全展示字段，永不返回关系 secret。`create invite` 是唯一返回
raw invite 的调用，响应标记 `display_once=true`。

`inspect` 只解析、验签、检查时间/权限/端点并分类地址，不发网络请求。

### 6.2 Peer API

```http
POST /api/peer/friends/accept
POST /api/peer/friends/revoke
GET  /api/peer/friends/pubkey
POST /api/peer/friends/messages
POST /api/peer/friends/content-cards
```

- accept 使用 invite credential；
- 其余使用 relationship MAC；
- 不为旧全局 network key 自动赋予 friend 权限；
- `/api/peer/msg` 保留兼容，但 Friend UI 只调用新 route；
- 新 outbound 调用全部经 #28 后的 `HttpPeerClient.post_json`/Transport；
- 不再在新路径使用裸 urllib 或信任 `from_pub` TOFU。

## 7. 消息、附件与内容卡片

### 7.1 PeerMessenger v2

扩展/新建 `FriendMessenger`：

- envelope version 2；
- receiver peer ID 校验；
- sender identity 由 FriendRecord 固定 X25519 key，不接受消息自带 key 更新；
- msg ID 幂等去重；
- text 最大 32 KiB；
- inline attachment 最大沿用 5 MiB，类型/文件名/总存储有上限；
- JSONL 写入加锁并能跳过/隔离尾部损坏行；
- delivery state `queued/sent/delivered/failed`；
- 每个 peer 队列、重试次数、过期时间和总 blob 配额有界。

### 7.2 内容卡片

实现 `ryn.shared-content-card.v1`，只包含有界元数据、Manifest ref、hash、size、可选
短 note。接收方：

- 先验签/解密；
- 展示发送好友与原始 publisher 的区别；
- 安全/来源/大小 Review；
- 用户点击后才 fetch preview/full；
- fetch 继续走现有 Transport、provenance 和 safety；
- 卡片不能自动提升 trust 或推荐权重。

## 8. 端点安全与可达性

Invite Review 将端点分类为 loopback、link-local、LAN private、public、unsupported。

- loopback/link-local/metadata address 拒绝；
- LAN invite 只在当前网络明确匹配时允许；
- DNS 解析结果在连接前/后按现有 SSRF policy 验证；
- redirect 禁止；
- endpoint 变化需要 signed peer record；地址类别扩大时二次 Review；
- TLS/profile/proxy/network-key 与 friend MAC header 通过 Transport seam 合并；
- v1 不通过 Registry 保存 invite secret、关系 secret、消息正文或 friend list。

## 9. Webapp 与桌面

### 9.1 页面

新增：

- `webapp/src/screens/Friends.tsx`；
- `webapp/src/screens/FriendInviteCreate.tsx`；
- `webapp/src/screens/FriendJoinReview.tsx`；
- `webapp/src/screens/FriendDetail.tsx`；
- `webapp/src/domain/friendsClient.ts`；
- `webapp/src/domain/friendsTypes.ts`。

现有 Chat 只列 active friends，不再列所有 discovered peers。Peers 移到高级网络诊断。

### 9.2 QR/粘贴/深链

- QR 使用本地库生成，不发网络请求；
- 粘贴入口为必备 fallback；
- 桌面支持时在 Tauri 注册 `rynmesh://`，所有输入共用同一 parser/review；
- deep link 只把 raw invite 保存在短期内存，不能进入 analytics、普通日志和长期 URL；
- 摄像头扫描如加入 v1，权限可选且失败不阻断粘贴；
- App 单实例转发必须在 Review 前不联网。

### 9.3 UI 状态

- invite active/used/expired/cancelled；
- friend connecting/active/offline/needs_review/revoked；
- message queued/delivered/failed；
- endpoint unavailable/address changed；
- revoke pending remote delivery，但 local access 已 revoked。

所有指纹短显但可复制完整值。Review 明确列出“能做什么”和“不能做什么”。

## 10. 数据迁移

- 现有 discovered peers 不自动成为 friends；
- 现有 message histories 不自动绑定好友 credential；
- 用户与同一 peer 完成新配对后，可选择在 UI 显示旧本地历史，但旧记录标记
  `legacy-unverified`；
- `_pubkey_cache` TOFU key 不迁移为已验证 friend key；
- existing `trusted_roots` 在接受/撤销前后必须 byte-for-byte 不变；
- privacy export 包含关系公开元数据、邀请/撤销状态和消息历史选项，不含 live secret；
- erase active friend 数据前强制 local revoke，并说明远端可能保留其本地历史。

## 11. 实施 PR 切片

1. `design: approve friend v1 wire and reachability boundary`
2. `feat: add atomic invite friend and revocation stores`
3. `feat: add one-time invite acceptance and relationship MAC`
4. `feat: route friend peer requests through Transport`
5. `feat: add friend-authenticated messaging and delivery state`
6. `feat: add signed shared content cards`
7. `feat: add create review join friends and revoke UI`
8. `feat: add local QR paste and desktop deep-link handling`
9. `test/docs: complete two-node security and usability acceptance`

## 12. 测试矩阵

### Crypto/protocol

- invite sign/verify/tamper/version/expiry/cancel；
- concurrent consume one winner；
- relationship secret differs from invite secret；
- MAC method/path/body/sender/receiver binding；
- nonce replay/time skew/constant-time compare；
- permission escalation；
- sender/header/payload mismatch；
- revoke idempotency/wrong signer/unrelated relationship；
- secrets absent from logs/export/diagnostics。

### Endpoint/transport

- generic 404 for invalid probes；
- body/rate/concurrency limits；
- loopback/link-local/metadata/private/public/DNS rebinding；
- redirect rejection；
- network key + friend MAC header preservation；
- direct/fronted/CDN/plugin Transport behavior where supported；
- no urllib bypass in friend routes。

### Store/recovery

- atomic restart at invite consume/revoke；
- corrupted JSON/torn JSONL；
- secret file permission；
- nonce cache bound/expiry；
- offline queue bound/expiry/blob quota；
- erase while friend offline；
- revoked never reactivates after restart/discovery。

### Message/content

- text/Unicode/empty/oversize；
- attachment exact 5 MiB/+1、filename/MIME；
- duplicate msg；
- offline → retry → delivered；
- content card tamper/unknown manifest/unsafe content/size mismatch；
- no automatic fetch；
- legacy history labeling。

### Webapp/desktop

- create/copy/cancel/expire；
- paste/QR/deep link same Review；
- no network before Join；
- endpoint address classification；
- first message/file/card；
- offline/needs review/revoke；
- keyboard/focus/screen reader；
- QR dependency network audit。

## 13. 验收条件

### 功能验收

- [ ] 两台干净节点通过二维码或粘贴完成配对。
- [ ] Review 在首次网络接触前显示签名身份、指纹、网络、全部端点、地址类别、权限和
  有效期。
- [ ] 邀请使用后轮换为独立关系 secret，原邀请不能再次使用。
- [ ] 配对后可发送文字、≤5 MiB 小附件和 Library 内容卡片。
- [ ] 内容卡片默认不自动获取正文/文件。
- [ ] 离线状态、待发送、恢复和失败对用户可理解。
- [ ] 本地撤销立即阻止后续请求，远端离线不阻塞。
- [ ] 重新发现同一 peer 不会复活撤销关系。

### 安全验收

- [ ] friendship 不修改 `trusted_roots`，不授予 trust root 或其他隐藏权限。
- [ ] 邀请不含 global network key/local token/长期 secret。
- [ ] 并发双接受只有一个成功。
- [ ] MAC replay、path/body substitution、wrong peer、wrong permission、clock skew 均拒绝。
- [ ] message sender、relationship peer、signed identity 和固定 X25519 key 一致。
- [ ] loopback/link-local/metadata/DNS rebinding/redirect 等端点攻击 fail closed。
- [ ] secrets 不进入 Registry、Webapp state API、普通日志、diagnostics、export 或验收材料。
- [ ] QR 本地生成，无第三方请求。
- [ ] UI 只承诺物理验收通过的 LAN/直接可达范围。

### 质量验收

- [ ] focused/full pytest、ruff、Webapp `npm test`、`npm run lint`、`npm run build` 通过。
- [ ] 所有 friend outbound POST 使用 Transport；代码搜索无新 urllib bypass。
- [ ] 在线、离线、重启、取消、过期、撤销完整两节点 E2E 通过。
- [ ] Windows/macOS 深链或粘贴 fallback 均通过；深链不可用不阻断核心流程。
- [ ] 5 MiB 边界、队列/nonce/blob 配额和速率限制有负面测试。

### 验收证据

- 两台干净节点完整录屏：create → review → join → message → file → card → revoke；
- `trusted_roots` 前后 hash；
- invite double-consume、replay、wrong peer、endpoint attack、offline revoke 报告；
- QR network audit；
- unique secret marker 在 Registry/log/export/diagnostics 中的负面搜索；
- direct/LAN 物理网络拓扑说明；
- implementation commits、测试输出、已知 reachability 限制和回滚路径。

## 14. 发布与回滚

- feature flag `friend_pairing_v1`，默认只在通过验收的平台/网络模式开放；
- Peer accept route 可独立关闭，但本地已建立 friend 的 revoke 始终可用；
- 回滚 UI 不删除 FriendStore；关系默认保持安全状态，必要时提供批量 revoke 工具；
- 协议 downgrade 不允许复用 v1 secret 到旧 TOFU message route；
- 发现安全问题时先拒绝新 accept/新 friend request，不删除用户历史；
- 不通过降低 MAC、重放、端点或 active-probe 防护来恢复兼容。
