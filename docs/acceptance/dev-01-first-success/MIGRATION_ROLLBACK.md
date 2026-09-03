# DEV-01 迁移与回滚演练

## 迁移

- 空目录读取返回 `ryn.first-success.v1` 空里程碑；
- 已有 consumption opened + bookmark/反馈在首次聚合时回填完成；
- `onboarding_version` 不再直接判定首次成功；
- 未知版本或损坏 JSON fail closed 为默认空状态，不阻止 node 启动；
- 显式 reset 只重置展示里程碑，并用 `replay_started_at_unix` 区分旧行为。

上述路径由 `tests/test_first_success.py` 自动化覆盖并通过。

## 回滚

演练方式：

1. 设置 backend/Webapp feature flag 为 `0`；
2. backend 新端点返回稳定 404，旧业务 consumption/feedback 继续成功；
3. Webapp 不请求或打开首次流程，原 Home/For You 仍可使用；
4. 重新启用后读取原 `first-success.json`，进度不丢失。

回滚不执行删除，不修改 consumption、推荐画像、ReaderCache 或用户内容。
