# DEV-04 规模与分页报告

测试数据：10,000 条 consumption metadata；Windows 本地 SSD；Python 3.12。

- 固定 sort：`last_activity_unix DESC, library_id ASC`。
- 首屏 API limit：50；最大 100。
- 10,000 条 metadata 搜索并返回首屏：pytest call duration 0.32 秒。
- 首屏 JSON 断言小于 100,000 bytes。
- 130 条数据跨 3 个 cursor page：130 个唯一 ID，无重复、无遗漏。
- cursor 含版本、filter hash、最后 sort key；换 view/filter 后旧 cursor 返回 `library_cursor_filter_mismatch`。
- Web DOM 只追加当前页 50 项；不会一次渲染全部记录。

v1 使用有界本地扫描并只读取 managed import 的最多 8 KiB 搜索前缀。SQLite index 不是唯一真相源；`library_index` 可独立擦除并从投影重建。
