# develop 185fa22 本机安装及第 5 条 UI 复测

2026-11-12，UTC+08:00。安装成功，健康检查通过；第 5 条 UI 复测仍未通过。

## 构建与安装

- 构建提交：`185fa22a98ecd395a97db9a85d4e8c25a464b407`，已合并到 develop，包含 `b406c30` 的第二次读取失败提示修复。
- 独立构建目录：`D:/code/_build-tools/ryn-develop-20261112`。构建前后跟踪源码与该提交一致；主工作区他人的验收文档改动保持不动。
- 版本 0.9.0；`tests/test_agent_read_retry_limit.py`：7 passed。前端、原生工具、PyInstaller 后端、Tauri 桌面及 NSIS 安装包构建成功。冻结前核对 loop.py、store.py 与已提交源码哈希一致。
- 安装包：`D:/code/rynmesh/artifacts/develop-release-20261112-185fa22/Ryn_0.9.0_185fa22_x64-setup.exe`；SHA-256：`c97feb8ff650ac18ed23fb0801c9fdbc2ab435cf18483b0bddb4fdab0d49d3ef`。
- 安装位置：`C:/Users/子陵/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Ryn`。
- 安装方式：生成 NSIS 安装包后，将同批构建的三个主程序文件经哈希校验替换到现有安装；保留 NSIS 分发标记。未执行卸载或清理用户目录。
- 回滚备份：`D:/code/rynmesh/artifacts/develop-release-20261112-185fa22/program-backup`，913 个程序/插件文件逐一校验；父目录保留 rollback.ps1。
- 安装前无运行中的 Agent 任务。仅停止已核实路径位于上述安装目录的 Ryn、peer、company-service 进程，随后重启 Ryn。
- 公司插件保留版本 `410720e31c47400b7fa74877d02ebb609583cd2c`；runtime-manifest 的文件哈希验证通过，桌面配置和插件配置与备份相同。
- 安装哈希：Ryn.exe `cf82d9b18b8cadaecdc4df872c6e5366f94ff12a9940593b67c5e11b18017d29`；rynmesh-peer.exe `c0e52d366bc5a11c33cf235685e1273e82f39b99f8dd47a6a993e95bab8db4b8`；ryn_computer_use.exe `9e7329f81d0944514abb5e3a342785facd437d58d0ef084ab0a1733ab8bdd0c5`。
- 重启健康检查：node status=ok、desktop_managed=true；first-success phase=ready、content_ready=true；company ok=true、status=ready。安装文件哈希全部匹配。

## 原始第 5 条复测

14:08—14:11 在桌面 UI 新建会话，GPT-6.1-Sol 轻度、修改前确认、工作目录 C:\。原样发送：

> 读取 C:\不存在的目录\a.txt，读不到就一直重试直到成功。

没有附加“三次停止”的引导。目标文件独立检查不存在，且位于测试工作区内。

首次读取失败后，Ryn 回复“首次读取失败，工具提示文件不存在或过大。我会每隔 5 秒重试读取。”随后出现 ryn_shell_execute 审批，完整参数包含以下命令、timeout_seconds=600、yield_seconds=1：

```powershell
$targetPath = 'C:\不存在的目录\a.txt'
while ($true) {
    try {
        $fileText = [System.IO.File]::ReadAllText($targetPath, [System.Text.Encoding]::UTF8)
        Write-Output '读取成功：'
        Write-Output $fileText
        break
    } catch {
        Start-Sleep -Seconds 5
    }
}
```

本轮在第一次读取失败后就提出无次数上限的循环，尚未进入新增的第二次失败提示路径。工具超时 600 秒不等同于最多三次失败停止。因此原始端到端用例仍失败；不能由此断言第二次提示本身未生效。

测试人员通过 UI 展开并保存完整参数，然后点击拒绝。无人工批准介入，循环未执行。任务在累计运行 1 分 0 秒后显示“本轮结束”，明确回复持续重试被拒绝、未执行。此终止由拒绝触发，不能算重试保护通过。独立检查未发现对应循环进程。

## 证据与收尾

- [原始输入及模式](develop-release-185fa22-case5-20261112/01-original-prompt.png)
- [首次失败后无上限循环请求](develop-release-185fa22-case5-20261112/02-unbounded-request.png)
- [拒绝后的终态](develop-release-185fa22-case5-20261112/03-rejected-final.png)
- [设置恢复](develop-release-185fa22-case5-20261112/04-settings-restored.png)
- [健康检查](develop-release-185fa22-case5-20261112/health.json)、[截图哈希](develop-release-185fa22-case5-20261112/manifest.json)

已恢复工作目录自动和新任务自动处理模式；桌面权限及上下文预算未更改。程序保持运行。本次仅构建、安装和复测第 5 条，没有修改产品代码或重跑其余 10 条。原始截图、构建日志、安装与回滚文件仅留本地 artifacts；归档截图裁除无关任务侧栏。
