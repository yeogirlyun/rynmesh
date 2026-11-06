# 发布版连接认证方式迁移（2026-11-05）

## 问题

已安装插件的 MCP 连接一旦以 OAuth 方式创建，插件后续版本无法把它改成其他认证方式：`migrate_release_defaults` 对 MCP 连接只三方更新 `name` 与 `description`。当发布方把本机回环服务改为只用安装器签发的启动凭据（`X-Ryn-Service-Token`）认证时，已安装实例仍停在 OAuth 模式；没有令牌时 `access_token` 在发出请求前就返回 `agent_mcp_auth_required`，连接一直显示需要授权，工具无法发现。

## 修改（分支 `claude/plugin-auth-migration-20261105`）

`rynmesh/agent_runtime/extensions/store.py` `migrate_release_defaults`：

- MCP 连接的 `auth` 参与三方更新。比较与保存都使用 `normalize_mcp_auth` 规范化后的值。
- 只有当前值等于某个已发布默认值（上次保存的发布默认值或安装包 `previous_defaults`）时才替换；用户改过的认证设置保留，并作为冲突报告。
- 认证方式改变时与其他连接配置变更一致：分配新的 `config_generation`，更新 `config_sha256`，清空已发现工具和提示，状态置为 `NeedsTest`。不复用旧认证方式下的会话、探测结果或工具列表。
- 不改动连接身份（`instance_id`）、启用状态或用户授权记录；OAuth 凭据存储不删除，模式为 `none` 时不再读取。

## 测试

在 SG-VM 本地工作树运行（Python 3.12.10，借用仓库 `.venv` 依赖）：

- `tests/test_full_package_install.py`：24 通过，含新增 `test_release_moves_only_unchanged_connection_to_new_sign_in_mode`（未改动的默认 OAuth 连接迁移为 `none`，代次/指纹/状态重置；用户改过的连接保持原样并报告冲突；重复迁移无变化）。
- 扩展、MCP、OAuth、本机服务、导入、发布相关测试集结果见合入记录。
