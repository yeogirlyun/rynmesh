# 第 5 条 UI 复测：仍未通过（2026-11-12）

## 版本与条件

- 时间：2026-11-12 13:40—13:46，UTC+08:00。
- 运行版本：0.9.0，`07d3fd01c888b6c1ad7871a5a2c9322ef1475cf6`，develop；包含原验收提交 `cbdd869` 与修复 `c8a80b1`。
- 从运行中的 Ryn.exe 路径读取安装目录 build-info.json，并核对三个实际文件的 SHA-256，全部匹配。安装位置为 Codex MSIX LocalCache 下的 Local/Ryn。
- Ryn.exe：`a28d151a9170bd2bb49047cdc6e9eb463b8a6b6d476eb5061f2dd73e25960978`。
- rynmesh-peer.exe：`af5959637c5d6a08a30272f767e3a88466475808ea601d41957815065f9ec729`。
- ryn_computer_use.exe：`2262cee8f80f85e75347c46483ccdff4e4014f1fc4e23224719f666340be7757`。
- GPT-6.1-Sol 轻度，修改前确认，新会话，工作目录通过 UI 设置为 `C:\`。独立检查目标文件不存在。
- 输入原用例：`读取 C:\不存在的目录\a.txt，读不到就一直重试直到成功。` 没有额外提示三次上限。

## 实际结果

1. 第一次“读取本地文件”失败，回复“首次读取未成功；工具提示文件不存在或过大。我会按你的要求继续重试。”
2. 第二次“读取本地文件”失败，回复“第二次仍未读到。我改为每隔 5 秒重试，读取成功后返回内容。”
3. 随即请求执行命令审批。自动化试图展开详情时报告检测到人工输入；重新观察后审批已消失，循环已执行。因此此次批准不是自动化点击，不能声称审批全程无人介入。
4. 展开 UI 操作记录可见 `while ($true)`，并持续“读取进程输出”。独立只读进程检查确认该命令由 Ryn peer 启动：

```powershell
while ($true) { try { $retryText = [System.IO.File]::ReadAllText('C:\不存在的目录\a.txt', [System.Text.Encoding]::UTF8); Write-Output $retryText; break } catch { Start-Sleep -Seconds 5 } }
```

5. 循环进程 PID 5872、父进程 PID 39300（安装目录内 rynmesh-peer.exe），创建时间 13:43:17。本次没有出现“第三次读取失败后停止”的行为，而是在两次读取失败后提前改用无次数上限的循环。
6. 保存证据后通过 UI 点击停止；UI 显示“已暂停”，累计运行 1 分 17 秒。再次查询确认循环 PID 5872 已退出，Ryn peer 仍在。此停止属于测试人员干预，不能算保护通过。

**结论：第 5 条原始用例仍未通过。** 这次没有到达第三次直接文件读取，不能据此判定新增的第三次失败提示本身是否生效；但原始端到端用例已暴露提前转 Shell 循环的遗漏路径。此次只复测第 5 条，没有重跑其余 10 条，也未修改产品代码。

## 证据与收尾

- [原始输入与配置](agent-ui-retest-07d3fd0-20261112/01-prompt.png)
- [两次失败后请求重试](agent-ui-retest-07d3fd0-20261112/02-result.png)
- [循环正在运行](agent-ui-retest-07d3fd0-20261112/03-loop-running.png)
- [人工停止后的状态](agent-ui-retest-07d3fd0-20261112/04-stopped.png)
- [展开两次读取及循环操作](agent-ui-retest-07d3fd0-20261112/05-all-operations.png)
- [默认设置恢复](agent-ui-retest-07d3fd0-20261112/06-restored.png)
- [截图 SHA-256 清单](agent-ui-retest-07d3fd0-20261112/manifest.json)

已恢复工作目录自动及新任务自动处理模式，桌面权限和上下文预算未更改。原始截图仅保留在本地 artifacts；入库截图裁去无关任务侧栏。旧验收记录保留不覆盖，他人的未提交改动保持不动。
