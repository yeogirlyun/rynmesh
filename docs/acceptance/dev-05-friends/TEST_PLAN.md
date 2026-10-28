# DEV-05 测试文档

## 自动化矩阵

| 范围 | 覆盖 |
|---|---|
| Invite | sign/verify、tamper、expiry、cancel、one-time、两个 store 实例并发 consume 单 winner |
| Relationship | invite/relationship secret 分离、restart、trusted roots 不变、revoked 不复活 |
| MAC | HKDF、method/path/body/sender/receiver、wrong peer、replay、±120s、512 nonce bound |
| Endpoint | scheme/userinfo、loopback、link-local、metadata、unspecified、hostname/DNS rebinding、redirect seam |
| Message | Unicode、32 KiB cap、duplicate protection、queued/delivered、restart/retry、torn JSONL |
| Attachment | exact 5 MiB accepted、5 MiB + 1 rejected、encrypted blob round-trip |
| Card | bounded metadata、encrypted wire、no automatic fetch、Review 后主动 fetch、size/SHA-256 校验、Library 导入、重复 fetch 幂等、撤销后拒绝 |
| Revoke | local-first、online delivery、offline pending、restart/retry、secret removed、post-revoke send rejected |
| HTTP | generic 404、local-control auth、secret-free privacy export、Transport POST/header preservation |
| UI | local QR、copy/cancel、paste-review-join、active-only Chat、message/card actions、内容 fetch Review、verified/saved 状态、5 MiB warning |

## 命令

```powershell
.\.venv\Scripts\python.exe -m ruff check rynmesh tests
.\.venv\Scripts\python.exe -m pytest -q
cd webapp
npm test
npm run lint
npm run build
```

Focused tests：`tests/test_friends.py`、`tests/test_friend_http.py`、`webapp/src/screens/Friends.test.tsx`。

## 两节点物理流程

在 Windows 上启动两个独立 uvicorn 进程、独立 `RYNMESH_HOME`、独立 network dir 和端口。仅为同机验收显式设置 `RYNMESH_FRIEND_ALLOW_LOOPBACK=1`；生产默认拒绝 loopback。

1. A create invite；B 本地 inspect；B join。
2. B 发 Unicode text、附件、content card；A 查询 history/card，断言此时 Library 尚无正文。
3. A 审阅卡片并主动 fetch；断言密文解封、size/SHA-256 匹配、只导入一次。
4. 停止 A；B 发消息并断言 queued。
5. 使用原 home 重启 A；B retry，断言 delivered 且 A 收到。
6. B revoke；断言 B/A 都 revoked，消息与未获取卡片的后续 fetch 被拒绝。
7. 重启 B；断言仍 revoked 且 active secret 不存在。
8. A create + cancel 新 invite；B join 被拒绝。

## QR 网络审计

`webapp/node_modules/qrcode/lib` 搜索 `fetch(` / `XMLHttpRequest` 无结果。出现的 URL 仅许可证、XML namespace 和注释；二维码由本地 JavaScript 生成 data URL。浏览器验收没有第三方请求依赖。
