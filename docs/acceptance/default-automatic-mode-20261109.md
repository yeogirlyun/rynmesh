# 默认模式改为“自动处理”（2026-11-09）

## 起因

阿九电脑上的企业微信查询任务中断（`agent_write_outcome_unknown`，原始错误 `STALE_CAPABILITY`）。
全局模式是旧默认值“自主执行”（`workspace`），公司插件的聊天记录查询工具没有被确认为只读，
于是每次查询都弹确认；确认等了约 19 分钟，插件给的 15 分钟临时会话引用过期，搜索被拒，任务被锁。
阿九装的 23eb988 已经包含 e81e26e（“自动处理 = 完全放行”），只是没有切到这个模式。

用户决定：默认就应该是完全自动的模式。

## 改动

- `store.py`：新增 `SHIPPED_PERMISSION_MODE = "automatic"` 和根字段 `settings_permission_mode_source`
  （`shipped_default` / `legacy` / `owner`）。`AgentStore(..., permission_mode=...)` 决定新安装的初始模式；
  不传时仍是 `DEFAULT_SETTINGS` 里的 `workspace`，嵌入使用和测试不会默认放行。
- `runtime.py`：`adopt_shipped_permission_mode()` 在节点启动时运行一次：
  - 没有来源记录、且保存的是旧默认 `workspace` → 改为 `automatic`，`settings_revision` +1，记 `shipped_default`；
  - 没有来源记录、保存的是其他值（`read_only` / `confirm_changes` / `automatic`）→ 不动，记 `legacy`；
  - 已有来源记录 → 不动；
  - 全新安装（还没有 `sessions.json`）→ 不创建文件，首次写入时直接用 `automatic`。
  - `update_settings` 收到 `permission_mode` 时记 `owner`，之后启动永远不覆盖主人的选择。
- `routes.py`：应用启动时传入 `permission_mode=SHIPPED_PERMISSION_MODE`，在 `reconcile()` 之后调用
  `adopt_shipped_permission_mode`；它失败只记日志，Agent 照常启动。
- `RynAgent.tsx`：设置未加载时输入框模式的占位值从 `workspace` 改为 `automatic`。

## 行为变化

- 新对话在“自动处理”下运行：插件操作（包括发送企业微信消息）、Shell、写文件、配置变更都不再询问。
  仍需确认：`requires_explicit_approval` 工具（安装组件、把数据交给其他应用的应用操作），
  以及未开启“允许电脑和浏览器操作”时的桌面操作。
- 旧版本里主动选过“自主执行”的人无法和默认值区分，也会被改为“自动处理”。可在输入框重新选择，之后不再被改。
- 已有对话保持创建时的模式（任务与全局取更严格者，已有规则），不会被提权；要免确认需新开对话。
- 未解决：查询返回错误仍会被当作“写操作结果不明”锁住对话（`adapter.py` isError 路径），
  公司插件临时 target_ref 15 分钟过期也未改。见 `docs/AGENT_STABILITY_FIX_PLAN_ZH.md` 2a 及插件修复建议。

## 验证（VM，基线 develop 56f3405）

- 新增 `tests/test_agent_shipped_permission_mode.py`：7 个用例全部通过（新安装直接执行写工具不弹确认、
  旧默认迁移一次且幂等、已选模式保留、主人选择不被覆盖、已有任务保持原模式、裸 store 保持保守默认）。
- 受影响的 29 个 Agent 测试文件（所有经 `install_agent` 启动的，以及涉及 `automatic` 的）：
  374 通过、9 跳过、8 失败。8 个失败逐个对照 develop 56f3405：
  - 3 个上下文压缩测试、`rewrite_app_http` desk retry：develop 上已知失败；
  - `reference_sources` native input、`source_fixture` secure owner form：develop 上同样失败；
  - 2 个 `local_execution_reliability` 隔离进程测试：并行负载下 30 秒子进程超时，单独运行时在改动版通过，
    develop 上其中一个同样超时。
  - 结论：没有由本次改动引起的失败。
- 前端只改了一个占位值，未运行前端测试；未在真机或真实模型上验证；未安装到阿九电脑。
