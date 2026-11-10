# DEV-02 迁移、发布与回滚

## 迁移

- 已有健康 `settings.ai_model`：继续直接使用，不创建新安装任务。
- 已有 Ollama：保留模型发现与选择；默认恢复入口改成应用内安装页。
- 旧 setup job 处于 queued/running/cancelling：启动后映射为
  `setup_interrupted`，允许用户重试；下载器根据固定摘要和 validator 决定是否续传。
- 旧 managed/import manifest：继续由原生命周期加载；只有
  `native_llama_cpp` 使用新 supervisor。
- 不认识的 profile、runtime 或版本：fail closed，不自动执行。

## 灰度

Backend 设置 `RYNMESH_LOCAL_AI_V1_ENABLED=0` 可关闭 profile/readiness 新接口；Webapp 设置
`VITE_RYN_LOCAL_AI_V1_ENABLED=0` 可隐藏 `/local-ai` 路由。Windows 是 v1 唯一对普通用户
宣称支持的受管平台。

## 回滚

1. 关闭前端 flag，入口回到高级 Provider 页面；
2. 关闭后端 flag，拒绝新的 profile 安装；
3. 已运行的受管 runtime 可通过现有 stop action 安全停止；
4. 回滚应用代码时保留已校验模型，避免重复下载；
5. 不删除 imported GGUF、Ollama 数据或用户配置；
6. 若某个 runtime release 有问题，从 allowlist 撤销该版本，摘要校验不可降级。

## 清理

物理验收结束后，已按绝对路径验证并停止隔离临时目录下的 runtime；残留测试进程数为 0。
临时模型可以由本机维护者随后删除，不属于 Git 或发布产物。

