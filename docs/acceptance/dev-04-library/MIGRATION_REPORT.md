# DEV-04 迁移报告

- 现有 `consumption.json` 由 LibraryService lazy projection；不批量重写、不改变时间戳。
- bookmark 映射 Saved，0–1 progress 映射 Continue，opened/completed 映射 History。
- `unbookmark` 只取消收藏，不删除 history。
- 显式 `restart` 是唯一允许进度回到 0 的动作。
- 现有 Digest sources/watchers 保持原 ID；只增加可选 `paused` 字段，缺失等同 false。
- archive、tags、title override 写入独立 metadata overlay；未知 library ID 可保留，不触碰真相数据。
- import 是新 managed snapshot，不移动、重命名或删除用户原文件。

回滚后旧 Digest/consumption 功能继续读取原格式；新增字段向后兼容，Library 文件数据保留等待重新启用或用户显式清理。
