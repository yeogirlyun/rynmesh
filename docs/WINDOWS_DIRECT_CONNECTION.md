# Windows VPN 与 Ryn 直连排查

先独立测试 UDP，再通过实际 Ryn 请求确认。服务发现成功不代表设备直连成功；获取 STUN 地址也不代表打洞成功。

设备在线状态使用最近 120 秒内的注册心跳或成功的 HTTP 健康检查。HTTP 地址不可达但心跳新鲜时仍显示在线；心跳过期且成功查询到了注册服务时显示离线。只有过期缓存、缺少时间戳或异常时钟时显示状态未知。注册约每 30 秒刷新一次，因此关闭设备后状态可能延迟约两分钟更新。`/api/local/peers/health` 的 `online` 表示设备存在状态，`httpReachable` 单独表示公布的 HTTP 地址能否访问；`presenceSource` 给出判断依据。

页面中的“设备公布地址”来自注册记录，可能是 VPN 内网 IP，并不代表 ICE 最终选中的连接地址。在线状态、HTTP 可达性和实际连接路径是不同信息。

## 独立测试

`scripts/Test-RynDirect.ps1` 不依赖 Ryn 或 Python。双方使用同一个随机测试 Token，在测试期间保持窗口打开。它用同一 UDP socket 查询两个 STUN 地址并互发探测包，输出 `PUBLIC`、`DIRECT_INBOUND` 和 `DIRECT_ROUND_TRIP_OK`。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Test-RynDirect.ps1 -Token YOUR-SHARED-TEST-TOKEN -PeerAddress PEER-PUBLIC-IP -PeerPort PEER-PUBLIC-PORT
```

公网地址、端口或网络出口变化后必须交换新地址；向旧 VPN 出口发包的结果不能验证新宽带出口。脚本仅测试 IPv4 UDP，不代表完整 ICE、身份验证或模型推理均成功。收到 `DIRECT_ROUND_TRIP_OK` 后再测试 Ryn。

## 全隧道 VPN 的目标地址例外

Windows 内置全隧道 VPN 可能使物理网卡无法使用默认路由；即使绑定物理网卡的 IP，发送也可能返回 WSAEINVAL。应先确认路由，而不是仅增加连接超时。

`scripts/Set-RynDirectRoutes.ps1` 为明确指定的对端 IPv4 地址以及解析到的 STUN IPv4 地址，建立经过指定物理网卡网关的 /32 路由。它不修改默认路由、VPN 配置、防火墙或中继设置。例外按目标地址生效，因此其他程序访问这些相同 IP 时也会走物理网卡；这不是按进程排除 VPN。

在公司端的管理员 PowerShell 中，将示例中的对端地址和网卡名替换为实际值：

```powershell
.\scripts\Set-RynDirectRoutes.ps1 -Mode Enable -PeerAddress PEER-PUBLIC-IP -InterfaceAlias '以太网'
```

查看不需要管理员权限；撤销需要管理员权限：

```powershell
.\scripts\Set-RynDirectRoutes.ps1 -Mode Status
.\scripts\Set-RynDirectRoutes.ps1 -Mode Disable
```

脚本保留已有的同目标永久路由，拒绝覆盖冲突或未管理的临时路由，并将自行创建的路由记录在 `%LOCALAPPDATA%\Ryn\direct-routes.json`，供精确撤销。路由保存后可跨重启保留；用户切换宽带、物理网卡、网关，或对端公网 IP、STUN DNS 变化时，应撤销后用当前参数重新配置。此脚本不会自动跟踪地址变化。

2026-09-24 现场验证：VPN 保持连接，配置目标地址例外后，独立脚本双向 UDP 成功；随后 Ryn 的实际 ChatGPT 请求成功，传输证据为 `ice_udp_direct`、`relay_used=false`，用户确认收到正常回复。未启用中继。

Windows 路由保存行为参见 [Microsoft New-NetRoute 文档](https://learn.microsoft.com/en-us/powershell/module/nettcpip/new-netroute)：不传 `PolicyStore` 时默认保存到活动和持久存储；显式指定 `PersistentStore` 不适用于该创建命令。
