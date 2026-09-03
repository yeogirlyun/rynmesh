# DEV-01 隐私与安全负面验证

## 唯一 marker

自动化使用 `PRIVATE_ARTICLE_MARKER` 和 `PRIVATE_PROGRESS_PATH`。断言结果：

- `first-success.json` 不出现文章标题/正文 marker；
- 里程碑写入异常不出现在 activity/audit；
- consumption audit 不再复制标题，只记录动作与 item ID；
- export 中的 first-success 仅包含版本、布尔值和时间戳；
- `onboarding` 擦除不联动删除阅读历史或推荐画像；
- local-control 校验仍由现有 `/api/local` 中间边界执行；
- feature flag 关闭时 API 返回稳定 `first_success_disabled`，不返回栈。

## 已知边界

文章正文仍由原有 ConsumptionStore/ReaderCache 按各自产品语义保存；本需求只保证
FirstSuccessStore 不重复正文。后续 DEV-04 会统一这些数据的导出、保留和范围化擦除。
