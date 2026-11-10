# DEV-02 支持矩阵与许可证

## 正式支持矩阵

| 平台 | 一键受管安装 | Backend | 状态 |
|---|---:|---|---|
| Windows x86_64 | 是 | llama.cpp CPU | 物理验收通过 |
| macOS Apple Silicon | 否 | runtime artifact 已固定 | 等待实体设备验收，不向普通用户开放 |
| macOS Intel | 否 | runtime artifact 已固定 | 等待实体设备验收，不向普通用户开放 |
| Linux | 否 | 高级 Provider/Ollama | 不宣称一键安装 |
| GPU acceleration | 否 | CPU safe default | 未验收前不宣称支持 |

后端 `runtime_support.managed` 只有 Windows x86_64 返回 true。未支持平台仍可使用已有
loopback Ollama/OpenAI-compatible 服务，但普通页面不会承诺一键能力。

## 固定物料

### 模型

- Qwen2.5-0.5B-Instruct-GGUF，revision
  `872f8a96064a1242ac3a3359cad77c3042548405`；
- 文件 `qwen2.5-0.5b-instruct-q4_k_m.gguf`；
- SHA-256 `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`；
- 许可证：Apache-2.0。

### Runtime

- llama.cpp release `b10516`；
- Windows archive SHA-256
  `fbbbc55e0eb2e1b07f9dcb9488616c98ed47d9003b90e15e7c8c7812c4307cd3`；
- 许可证：MIT。

安装前 UI 展示组合许可证 `Apache-2.0 AND MIT`，用户必须显式确认。下载 URL、版本、
摘要和许可证通知进入受管 manifest；未知平台、未知档位或未发布版本拒绝安装。

