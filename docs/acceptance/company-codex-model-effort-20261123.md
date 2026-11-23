# 公司同事共享：指定 Codex 模型与推理档位（2026-11-23）

## 结论

82 通过公司通道选择发布端共享的 **GPT-6.2 Sol / medium** 跑通。82 上的任务回执写明实际执行的模型和档位：`model_served = {"model": "gpt-6.2-sol", "reasoning_effort": "medium"}`，回复 `RYN-COMPANY-82-SOL-MEDIUM-OK`，37 秒。发布端「公司同事用量」同时记到这次请求。

## 原因（三处）

1. **发布**：勾选「公司同事」只把 Codex CLI 的默认绑定发到公司目录（`provider_model_status` 只取 `model_binding`），中心 `modelRecords` 也拒收任何额外字段。82 因此只看到一个「CLI 默认」，没有子模型和档位。模型池下方的单个模型行本来就不显示勾选框（`ModelPool.tsx` 的 `!row.catalog?.cli_model`），但它们也是同一个 Codex CLI 服务。
2. **82 校验**：「CLI 默认」这一行没有 `supported_reasoning_efforts`，传 `reasoning_effort: "medium"` 被 `reasoning.validate` 拒绝为 `agent_reasoning_effort_unsupported`。
3. **执行端默认模型**：共享进程在临时 `CODEX_HOME` 里生成自己的 `config.toml`，故意不读发布者的 `~/.codex/config.toml`（不带插件、MCP、技能等）。不传模型时用的是 Codex 内置默认（`model/list` 的 `isDefault`），档位也是内置默认：早先记录是 gpt-6.1-sol，当前 Codex 0.167 是 gpt-6.2-sol / low，而不是发布者配置里的 gpt-6.2-sol / medium。

## 修改

- 中心（gshopper-ryn-node `7eb9c46`，develop `a9c9d8e`）：模型记录可带 `cli_models`，每个上游模型有自己的精确绑定、档位、窗口，严格校验。
- Ryn（`8d7e842a`、`0687b45d`，develop `17b616a5`、`13dc7876`）：
  - 勾选 Codex CLI 即发布它的全部模型和档位；发布端对其中任一模型的绑定放行。
  - 82 解析并显示每个模型（含档位），可指定模型和档位创建任务。
  - Codex `thread/start` 返回的实际模型和档位作为 `server_model` / `server_reasoning_effort` 回传，任务记录 `model_served`。
  - 发布端 30 秒目录同步发现已发布服务的绑定变化（如 Codex 更新、模型元数据刷新）时，自动用同一选择重新发布。实测：发布后 3 分钟 Codex 绑定变化一次，82 立刻收到 `agent_model_destination_changed`，手动重新勾选前一直失败。

## 部署与验证

- 中心 hk036：2026-11-23 21:49 上线，`rynPeerMetadata.js` 以活文件拼接（sha256 `4bcd11b3…`，原 `b6386add…`），新增只读 compose 固定。13 秒就绪，容器内探针通过。回滚：`sudo python3 /home/ops/company-cli-models-20261123/deploy.py rollback`。
- 发布端（本机 E:\Apps\Ryn）：运行 develop `a6cb4c37`（其它会话 18:12 安装，包含 `17b616a5`）。重新发布后公司目录含 10 个 Codex 模型及档位。自动重新发布（`0687b45d`）尚未装到本机。
- 82（C:\Users\ziling\AppData\Local\Ryn）：静默升级到同一 `a6cb4c37` 安装包（sha256 `AF0E244E…`），三个程序哈希与构建回执一致，7 个用户配置文件哈希不变，health 200。回滚：`C:\Users\ziling\Ryn-0368805b-setup.exe` 或 `C:\Users\ziling\Ryn-rollback-20261123-0368805b`。
- 测试：中心 `rynPeer.test.ts` 通过（`rynPeerHttp.test.ts` 在未改的 develop 上同样 4 项失败，宿主网络隔离所致）；Ryn `test_peer_providers.py`、`test_company_ai_sharing.py` 等 354 项通过，其中新增两节点端到端 GPT-6.2 Sol / medium 测试和自动重新发布测试。

## 未覆盖

- 82 的模型池界面未截图核对（无 GUI 自动化），验证走本地 API。
- 发布端仍是逐服务勾选：勾选 Codex CLI 即共享全部模型，暂不能只共享其中几个。
