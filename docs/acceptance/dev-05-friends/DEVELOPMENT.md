# DEV-05 开发文档

## 架构结果

### 后端

- `rynmesh/friends/crypto.py`：invite 编解码/验签、端点 fail-closed、HKDF domain separation、canonical request HMAC、±120 秒时钟窗。
- `rynmesh/friends/store.py`：public state 与 secret 分文件，临时文件 + fsync + atomic replace，线程/跨进程文件锁，bounded nonce cache。
- `rynmesh/friends/service.py`：create/inspect/cancel/join、独立 relationship secret、固定 X25519 key、消息/附件/卡片、离线重试、本地优先撤销。
- `rynmesh/peer_http.py`：local control API、通用 404 peer API、body/rate 限制、privacy status/export/erase。
- `rynmesh/transport.py` / `transport_plugins.py`：Transport 增加 `post_bytes`，`HttpPeerClient.post_json` 是新好友 outbound 的唯一 HTTP seam。
- `rynmesh/services/messaging_store.py`：锁、flush、尾部损坏跳过。

### Webapp

- `Friends.tsx`：本地 QR、copy/cancel、paste/review/join、active list、revoke。
- `Chat.tsx`：数据源从 discovered peers 改为 active friends；消息 delivery state、附件边界、内容卡片选择。
- `qrcode` 作为打包依赖，仅在浏览器内生成 data URL；运行时代码不含 fetch/XHR。
- `/peers` 仍保留为高级网络诊断路由，但不再作为 Chat 联系人来源。

## Wire v1

Invite 为 `rynmesh://join/<base64url-envelope>`。Envelope 含 Ed25519 signed payload 与一次性随机值；payload 绑定 invite ID、inviter identity、IP endpoint、network、固定权限、X25519 public key、expiry 和 secret hash。磁盘只保存 keyed use-proof 所需 hash，不保存 raw invite。

Accept 请求由 accepter Ed25519 identity 对完整字段签名。邀请被原子 consume 后生成新的 256-bit relationship secret，并使用 accepter 的 X25519 key 密封返回；invite secret 与 relationship secret 不复用。

每个关系请求使用：

```text
X-Ryn-Friend-Id
X-Ryn-Friend-Peer
X-Ryn-Friend-Time
X-Ryn-Friend-Nonce
X-Ryn-Friend-MAC
```

MAC key 由 relationship secret 经 HKDF(`rynmesh-friend-request-mac-v1`) 派生；canonical request 绑定 version、sender、receiver、method、path、timestamp、nonce 和 SHA-256(body)。接收端使用 constant-time compare，并在认证成功后把 nonce 写入每关系 512 项有界缓存。

消息与内容卡片都用双方配对时固定的 X25519 key + ChaCha20-Poly1305 密封。消息携带的 `from_pub` 不参与 key 更新，避免退回 TOFU。内容卡片只含 title/summary/kind/source/opaque Library ID，不会自动 fetch。

撤销先把关系标记 revoked 并移走 active secret，再 best-effort 发送 Ed25519 signed revocation。远端离线时仅保留隔离的 pending-revocation secret 和有界 wire record；它不能通过 active auth，送达后删除。

## 安全边界

- v1 只接受 literal IPv4/IPv6；hostname 一律拒绝，因此 DNS rebinding fail closed。
- loopback、link-local、metadata、unspecified、multicast、userinfo、fragment 和非 HTTP(S) 拒绝。测试专用 `RYNMESH_FRIEND_ALLOW_LOOPBACK=1` 不在桌面默认值中。
- Transport 禁止 redirect；network-key header 与 friend MAC 可合并。
- invalid invite/auth/body/relationship 对 peer surface 返回相同 404。
- pairing feature flag：`RYNMESH_FRIEND_PAIRING_V1=0` 停止新 invite/accept/join，但不阻止本地 revoke。
- friendship store 与 `trusted_root_peer_ids` 无引用关系；accept/revoke 不写 Registry。

## API

Local：`POST/GET /api/local/friends/invites`、`inspect`、`DELETE invite`、`join`、`GET friends`、`DELETE friend`、revocation retry、messages send/list/retry、content-cards send/list。

Peer：`accept`、relationship-authenticated `message`、`content-card`、`revoke`。旧 `/api/peer/msg` 保留兼容，但新 Friends UI 不调用。

## 回滚

关闭 pairing flag 可停止新关系。UI 回滚不删除 `RYNMESH_HOME/friends`；已有关系仍可本地撤销。不得把 v1 secret 降级给旧 TOFU route。恢复功能时直接重新启用同一 wire v1；如 wire 改动必须升版本。
