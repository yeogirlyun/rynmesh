# DEV-05 产品说明：简单配对与分享

状态：已实现，等待最终跨平台总验收

## 用户价值

普通用户不需要理解 Peer、Registry 或网络信任模型，只需完成一条可解释的路径：

```text
创建一次性邀请 → 扫码或粘贴 → 本地安全审阅 → 明确确认 → 私密聊天/小附件/内容卡片 → 随时移除
```

## 用户范围

- v1 支持同一 LAN 或已经直接可达的 IP 端点；不承诺 NAT 穿透或 Relay。
- 二维码和复制文本编码同一个 15 分钟、一次性邀请；粘贴是 Windows/macOS/Linux 的必备 fallback。
- 好友只能发消息、最大 5 MiB 附件、My Content 内容卡片；卡片先收元数据，接收方
  审阅并确认后才拉取最大 5 MiB 的正文/文件，校验成功后进入自己的 My Content。
- 好友不能使用 Private AI、VPN、文件系统、trust root、Credits 或推荐权重。
- 发现节点不会自动成为好友，旧 TOFU 消息也不会自动迁移为已验证关系。

## 核心界面

- Friends：创建/复制/取消邀请，粘贴并审阅邀请，查看 active friends，移除关系。
- Friend messages：只列 active friends；显示 queued/delivered；可重试离线消息、选择
  My Content 卡片，并在卡片上审阅发送者/发布者/来源/文件大小/SHA-256 后主动获取。
- Settings / Privacy：公开关系元数据可导出，secret、消息正文、附件和 raw invite 默认不导出；可本地撤销并清除。

## 用户可见失败状态

- invalid / expired / used / cancelled invite；
- endpoint 不安全或不可达；
- 消息 queued，远端恢复后可 Retry；
- 移除通知 pending，但本地权限已立即失效；
- attachment 超过 5 MiB、消息超过 32 KiB 或队列已满。

原始批准需求见 [USER_SIMPLE_PAIR_AND_SHARE_WORK_PLAN.md](../../USER_SIMPLE_PAIR_AND_SHARE_WORK_PLAN.md)，批准后的完整工程计划见 [DEV_05_SIMPLE_PAIR_AND_SHARE_IMPLEMENTATION_PLAN.md](../../DEV_05_SIMPLE_PAIR_AND_SHARE_IMPLEMENTATION_PLAN.md)。
