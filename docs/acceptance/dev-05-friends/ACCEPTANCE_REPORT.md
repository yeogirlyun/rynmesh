# DEV-05 验收报告

日期：2026-09-04  
平台：Windows 物理文件/进程 + Chromium Webapp；粘贴 fallback 为纯 Web 控件，无 OS 专用依赖

## 结果摘要

- 自动化：focused backend `9 passed`；全量结果以本目录最终 `TEST_RESULTS.txt` 为准。
- 真实双节点：pair/message/attachment/card/offline/restart/retry/revoke/cancel 全部通过。
- `trusted_roots` 前后 canonical `[]` SHA-256 均为 `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`。
- 新 friend outbound 全部调用 `HttpPeerClient.post_json` → selected Transport；代码搜索显示裸 urllib 只存在旧消息兼容、Updater/Relay 路径。
- Registry、privacy export、public friend API 和截图均不含 relationship secret、network key 或 local token。

## 功能验收

- [x] 两个干净、独立 home 的节点通过粘贴 invite 配对；同文本的本地 QR 可扫码传递。
- [x] 首次网络接触前 Review 显示签名身份/指纹、network、全部端点、地址类别、权限、有效期。
- [x] consume 后轮换独立 relationship secret，原 invite 第二次使用失败。
- [x] 文字、exact 5 MiB 附件和 Library metadata card 可发送。
- [x] content card 不自动下载正文/文件。
- [x] offline → queued → restart → retry → delivered 对用户可见。
- [x] local revoke 立即拒绝；remote offline 时不阻塞，并可安全重试通知。
- [x] restart/discovery 无代码路径可把 revoked 改回 active。

## 安全验收

- [x] friendship 与 trusted roots/credits/quarantine 完全分离。
- [x] invite 不含 global network key、local token 或长期 relationship secret。
- [x] 并发 double consume 只有一个 winner。
- [x] MAC replay、path/body substitution、wrong peer、permission mismatch 和 clock skew 拒绝。
- [x] sender 与 relationship identity、signed accept identity、固定 X25519 key 绑定。
- [x] endpoint 攻击 fail closed；v1 拒绝 hostname，直接消除 DNS rebinding 窗口；Transport 禁止 redirect。
- [x] secret 不进入 Registry/Webapp public state/log/export/验收材料。
- [x] QR 本地生成，无 fetch/XHR。
- [x] UI 只承诺 LAN/直接 IP 可达范围。

## 真实双节点关键输出

```json
{"alice_friend_count":1,"bob_friend_count":1,"relationship":"active","message_count":2,"received_text":"你好 Alice，真实双节点消息","received_attachment":"acceptance.txt","received_card":"老板验收内容"}
{"restart_relationship":"active","retry_attempted":1,"retry_delivered":1,"restored_message":"offline queued then restored"}
{"local_status":"revoked","remote_status":"revoked","post_revoke_send":"active_friend_required"}
{"after_restart_status":"revoked","secret_file_contains_relationship":false}
{"cancel_status":"cancelled","join_after_cancel":"could_not_join_friend"}
```

## UI 证据

- [带标注总图](screenshots/dev05-ui-annotated.png)
- [一次性 QR](screenshots/01-invite-qr.png)
- [加入前 Review](screenshots/02-join-review.png)
- [active-only 私密聊天与内容卡片](screenshots/03-private-chat.png)

标注图说明本地 QR、Review-before-network、可见权限边界与禁止隐藏扩权。Raw invite 仅使用 fixture 文本，截图不含真实 secret。

## 平台说明

Windows 已完成真实双进程与浏览器验收。macOS 的核心 fallback 是同一个 `<textarea>` paste/review 流程，自动化无 OS 分支；但 macOS 实机进程/文件权限仍放入五项需求的最终跨平台总验收清单，不在本报告中伪称已执行。
