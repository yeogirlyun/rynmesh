# DEV-03 隐私与安全负面验收

## 标记词

- Prompt：`PRIVATE_PROMPT`
- Context：`PRIVATE_CONTEXT`
- Output：`PRIVATE_OUTPUT`

## 结果

| 检查面 | 预期 | 结果 |
|---|---|---|
| run 普通元数据 JSON | 不出现 Prompt/Output | 通过 |
| privacy export | 不出现 Prompt/Context/Output | 通过 |
| URL/search params | 不携带正文，只允许 `content:<opaque-id>` | 通过 |
| localStorage/sessionStorage | 不保存对话正文 | 通过；会话走 AES-GCM IndexedDB，失败时仅内存 |
| Registry/通知 | 不发送 prompt、附件或 output | 通过；Assistant API 无对应写入路径 |
| 错误响应 | 只返回稳定码与安全文案 | 通过 |
| context owner/expiry | 跨会话或过期 ref 被拒绝 | 通过 |
| delimiter/prompt injection | 材料只能作为 JSON 编码的不可信证据 | 通过 |
| 跨隐私边界 | 不复制历史和 context | 通过；新会话确认 |
| cancel late result | 不得变为 completed | 通过 |

## 上下文边界

- 单份材料硬限制：4 MiB。
- 单次 run 默认实际材料预算：12 KiB。
- 预算使用 UTF-8 bytes，不使用不安全的 `chars / 4` gate。
- 截断使用 UTF-8 可解码边界，CJK/组合字符测试通过。
- Citation locator 只能位于本次实际发送的截断文本范围内。

## 数据擦除

`assistant` 擦除 scope 只允许删除 `RYNMESH_HOME/assistant/contexts` 和 `runs` 下的直属文件；路径在删除前 resolve 并验证父目录。Web 设置页同时清除 `assistant` IndexedDB bucket，并重置共享 active conversation。
