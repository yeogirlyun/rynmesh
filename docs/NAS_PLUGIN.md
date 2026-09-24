# NAS plugin

The bundled NAS plugin adds a native file browser to Ryn. Enable it at
Settings → Plugins. Disabled is the default: NAS navigation is absent and the
backend refuses file operations. Configuration is retained when disabled.

## Connections

Add several independent connections, including more than one of the same NAS
system. Feiniu, Synology, QNAP, TrueNAS, Unraid, OMV, ASUSTOR, TerraMaster and
Other NAS are **configuration presets for standard file protocols**, not claims
of proprietary API integration or brand-specific acceptance. SMB is supported by
all presets; WebDAV appears on systems that normally offer it. Enable the service
on the NAS first. A source is saved only after its root directory can be listed.

Each connection is hosted on the **current Ryn node**. It must reach the NAS
directly. Personal-space P2P forwarding of these file operations is not part of
this implementation; pairing alone does not grant NAS access.

Use a NAS account restricted to the intended shared folder. Ryn can further
select a subfolder. Read-only is the default; upload/folder creation and AI access
are separate opt-ins. System administration, deleting files, proprietary photo
libraries, snapshots, full-text indexing and automatic synchronization are not
provided by this version.

## File behavior

- List directories and filter names within the current directory.
- Upload and download files up to 64 MiB; preview UTF-8 text up to 256 KiB.
- Create folders. Uploads refuse to overwrite existing files. SMB uploads use a
  temporary sibling and publish with a non-overwriting rename.
- Open a selected text file in the existing AI chat. The document stays in
  memory until Send; the selected provider is shown. At Send, Ryn rechecks source
  access and reads the current file. Chat uses its existing encrypted/session
  history mechanism. The source body is never included in a navigation URL.
- Save a completed AI response to a new timestamped text file in the source's
  directory, after showing the destination. The server rechecks write permission.
- Remove a connection without deleting NAS files.

## Boundaries

All APIs are below `/api/local/plugins/nas` and use the existing local-control
authentication boundary. No anonymous peer file routes are added. Credentials
are encrypted in `RYNMESH_HOME/plugins/nas/state.enc`; the key is stored alongside
it in the node's private directory. This protects against accidental plaintext
disclosure, not compromise of the OS account. Protect/back up the directory as a
unit. Secrets are excluded from status responses and transport errors.

SMB sessions are isolated by connection and discarded after each operation.
Paths reject traversal and SMB reparse points are checked; NAS-side account and
share ACLs remain essential, particularly if other users can modify directories.
WebDAV redirects are refused and HTTPS uses normal certificate verification.
Unencrypted HTTP WebDAV should only be configured on a trusted test/LAN path.

Operations are bounded and serialized by the plugin. Disable waits for a current
connector operation, then rejects new operations. Data already delivered to a
browser or submitted to an AI task cannot be recalled. Already prepared downloads
may finish. There is no background crawler, persistent NAS file cache or retry
queue in this version.

## Local test deployment

`deploy/nas-test/compose.yml` boots official fnOS 1.2.0604 inside a QEMU container.
It uses separate named volumes for the system and data disks and binds all ports
to loopback. It never mounts host personal data or physical disks.

1. Run `python scripts/nas_download_iso.py` from the checkout. It downloads the
   official alternate image and verifies the checksum published over HTTPS.
2. Run `docker compose -f deploy/nas-test/compose.yml up -d`.
3. Open `http://127.0.0.1:18006` for the installer. Use the first blank 32 GiB disk
   for the system and the other blank disk for test storage.
4. Initialize fnOS at `http://127.0.0.1:15666`, create test storage and a dedicated
   account/shared folder, and enable SMB.
5. Add it in Ryn with host `127.0.0.1`, port `1445`, and the configured share.
6. Stop with `docker compose -f deploy/nas-test/compose.yml stop`. The volumes and
   files are preserved. Never run `down -v` unless intentionally deleting this lab.

This configuration uses software emulation (`KVM=N`) for hosts without nested
virtualization. It is suitable for functional tests, not throughput benchmarks.
The container requires writable access to the hybrid boot ISO. Keep the verified
download/checksum record; do not substitute an unverified image.

### Repeat the real file acceptance

Use an isolated Ryn node. Save a private connection JSON under the ignored
`build/nas-test/connection.json` directory, using `system`, `protocol`, `host`,
`port`, `share`, `root`, `username` and `password` fields from the Add NAS form.
Run `python scripts/nas_acceptance.py --config build/nas-test/connection.json`.
The default target is `http://127.0.0.1:18841`; override with `--node` if needed.
Use `--config -` to supply JSON through standard input without creating a
plaintext configuration file.

The script creates two connections and a uniquely named synthetic test folder,
then checks byte-identical upload/download, preview, the AI document-read gate,
non-overwrite behavior, traversal rejection, scoped read-only access, secret
redaction and disable/re-enable. It temporarily disables the whole plugin on
that node, so do not point it at a node being used for real work. It preserves
the files and connections for browser inspection and writes a credential-free
result to `build/nas-test/acceptance.json`. This script does not run AI inference;
the chat submission and saving flow have separate frontend tests.

Official setup references: [fnOS installation and initialization](https://help.fnnas.com/articles/v1/start/install-os)
and [creating storage](https://help.fnnas.com/articles/v1/volume/create).
## 2026-09-23 图片预览与配色更新

Web 和 Windows 共享同一套 NAS 组件，支持深色与浅色主题。取消设备插画，使用薄荷绿操作/选中状态、淡紫色文档、暖黄色文件夹。选中文件后在右侧预览；较窄窗口改为上下布局。

JPEG、PNG、WebP 可自动生成可见文件的缩略图，并支持缩放、适应窗口和查看大图。预览读取上限 20 MiB、40 百万像素，服务端去掉元数据并重新编码为 WebP：缩略图最长 160 × 120，预览最长 2560 × 2560；下载仍保留原始字节。图片接口沿用本机控制鉴权、共享目录范围和插件开关。SVG、损坏图片等不送入图片渲染。文本预览上限和 AI 授权不变，图片不显示 Ask AI。

图片请求和对象 URL 在切换文件、目录、连接或关闭预览后取消/释放；图片预览不写入浏览器持久存储。新增运行依赖 Pillow。
