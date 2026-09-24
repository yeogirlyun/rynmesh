# Issue #20 work plan: verified Linux desktop package

Status: implementation-ready with Linux CI as the acceptance environment  
Issue: https://github.com/yeogirlyun/rynmesh/issues/20  
Recommended order: independent; land cross-platform shell changes before release workflow

## 中文执行摘要

当前发布流程只有 macOS Intel/Apple Silicon DMG。Tauri 配置和 PyInstaller
sidecar 基础已经存在，但构建脚本带有 macOS 专用签名参数，Rust 桌面壳也写死
了 `scutil`、`ipconfig`、`~/Library/Logs` 和 `/usr/bin/open`，因此不能简单在
Linux runner 上增加一行 `tauri build`。

第一版选择 **x86_64 Debian/Ubuntu `.deb`**。包内继续携带冻结后的
`rynmesh-peer` 和生产 Webapp，不要求系统 Python 或 Node.js；WebKit/GTK 等
桌面运行库由 `.deb` 正常声明为系统依赖。GitHub Actions 在 Linux 上构建、
解包、启动 sidecar、验证 UI、验证桌面壳启动/退出并发布 SHA-256。

## 1. Verified current state (2026-09-02)

- GitHub Issue is open in milestone `P1 hardening` with no matching feature
  branch, PR, local branch, or implementation commit.
- `.github/workflows/release.yml` publishes Python artifacts and macOS DMGs only.
- CI compiles desktop shells on macOS Intel and Apple Silicon only.
- `tauri.conf.json` declares an external sidecar and has Linux-capable icons,
  but no verified Linux target.
- `build-sidecar.sh` always passes PyInstaller macOS codesign and entitlement
  options.
- `node.rs` and `lib.rs` contain macOS-only machine name, IP, log path, and
  “Open Logs” commands.
- `verify-sidecar.sh` is POSIX-compatible and already validates daemon health.
- The packaged-node CI already proves that the node serves its embedded Webapp.

Linux source/developer execution exists, but a self-contained desktop artifact
does not.

## 2. Initial artifact decision

Ship one target first:

- architecture: `x86_64` / Debian `amd64`;
- format: `.deb`;
- build baseline: an explicit supported Ubuntu LTS GitHub runner;
- product name: `Ryn`;
- bundled components: Tauri shell, production Webapp, frozen
  `rynmesh-peer-x86_64-unknown-linux-gnu` sidecar.

Why `.deb` first:

- it is natively supported by Tauri;
- system WebKit/GTK dependencies can be declared and upgraded by the OS;
- installation, removal, version inspection, and file-layout verification are
  deterministic in CI;
- it avoids claiming broad AppImage portability before glibc/WebKit behavior is
  tested across distributions.

AppImage, ARM64, RPM, Flatpak, Snap, and Windows installers are separate work.

## 3. Goals

- Build and publish a Linux desktop artifact without system Python or Node.js.
- Ensure the bundled sidecar matches the desktop package architecture.
- Make node startup, health checking, logs, restart, shutdown, and tray actions
  work on Linux.
- Preserve existing macOS compile and release verification.
- Verify the node serves the bundled UI, not a Vite development server.
- Publish a SHA-256 checksum beside the `.deb`.
- Document supported distributions, install, update, uninstall, data/log paths,
  and known desktop integration limits.

## 4. Non-goals

- Do not add Windows packaging.
- Do not claim every Linux distribution is supported.
- Do not bundle a native LLM runtime or model.
- Do not remove the Python wheel or macOS releases.
- Do not introduce automatic root escalation or a system-wide node service.
- Do not require an external registry/network to pass package smoke tests.
- Do not add release signing until a maintainer key and policy exist.

## 5. Cross-platform desktop shell

### 5.1 OS-specific helpers

Refactor `webapp/src-tauri/src/node.rs` behind small platform-specific helpers:

- machine name: macOS `scutil`, Linux hostname fallback;
- LAN address: use a Rust/std socket method or a validated Linux fallback;
- logs: macOS `~/Library/Logs/Rynmesh`, Linux
  `${XDG_STATE_HOME:-~/.local/state}/rynmesh`;
- open logs: macOS `open`, Linux `xdg-open`;
- executable lookup: search `PATH` instead of hardcoding `/usr/bin/which`;
- custom command shell: `/bin/sh` on supported Unix platforms;
- graceful child termination: keep Unix SIGTERM then bounded kill/wait.

Keep platform selection compile-time with `cfg` attributes. Do not make Linux
execute missing macOS commands on every startup and silently fall back.

### 5.2 Sidecar resolution

Tauri places external binaries according to target conventions. Tests must
prove the packaged Linux executable resolves the exact sidecar, not the first
file whose name merely starts with `rynmesh-peer`.

In development, require the current Rust target triple and reject a sidecar for
another architecture. In the installed package, verify the adjacent bundled
binary is executable.

## 6. Portable sidecar build

Update `webapp/src-tauri/scripts/build-sidecar.sh`:

- resolve `rustc` from `PATH`, not `$HOME/.cargo/bin`;
- keep the temporary virtual environment and one-file PyInstaller build;
- pass codesign identity and entitlements only on macOS;
- use Linux-safe PyInstaller arguments on Linux;
- name the output with the exact Rust host triple;
- run `file` and/or ELF inspection in CI to assert x86-64 Linux architecture;
- keep Webapp assets embedded in the installed Python package;
- never download Python or Node at application runtime.

Keep `verify-sidecar.sh` as the common health check. Add a restart verification
that starts the same frozen binary twice against the same temporary home and
confirms clean shutdown and recovery.

## 7. CI and release workflow

### 7.1 Pull-request CI

Add a Linux desktop job on the selected Ubuntu LTS runner:

1. install the documented Tauri build prerequisites;
2. install Node, Python, and Rust as build-time tools;
3. run `npm ci`;
4. build the frozen sidecar;
5. verify sidecar architecture and health;
6. run Rust checks/tests for the desktop shell;
7. build `npm run tauri build -- --bundles deb`;
8. inspect the package with `dpkg-deb --info` and `dpkg-deb --contents`;
9. extract it into a temporary root and verify the desktop executable,
   production Webapp resources, icons, metadata, and exact sidecar;
10. launch under a headless display where practical, wait for node health,
    exercise restart, request shutdown, and assert no managed child remains.

The test must use an isolated `RYNMESH_HOME`, disable auto-registration, and
must not contact the public Registry.

### 7.2 Tagged release

Add a `linux-desktop` job that needs the Python package job, mirroring macOS
without modifying the macOS matrix:

- build the `.deb` from the tag;
- name it `Ryn-<version>-linux-x86_64.deb`;
- calculate `sha256sum` into a clearly named checksum file;
- verify the package version equals the tag;
- repeat package/sidecar smoke tests on the final renamed artifact;
- upload both artifact and checksum to the existing GitHub Release.

Do not use `--clobber` to hide an architecture/name collision.

## 8. Documentation changes

Update at least:

- `README.md` install section and current boundaries;
- `docs/ARCHITECTURE.md` desktop distribution status;
- a Linux desktop troubleshooting section or runbook.

Document:

- supported Debian/Ubuntu versions and x86_64-only initial scope;
- `sudo apt install ./Ryn-...deb` installation;
- system WebKit/GTK/tray dependencies declared by the package;
- update behavior (install a newer `.deb`; no silent in-app updater unless
  separately implemented);
- uninstall behavior and the fact that user data is retained unless explicitly
  removed;
- Linux data, configuration, and log paths;
- desktop environment/tray limitations, Wayland/X11 expectations, and
  headless-server non-support;
- checksum verification.

### 8.1 Implementation sequence

1. Refactor Rust OS-specific helpers and add platform-focused tests.
2. Make sidecar build and resolution target-aware on macOS and Linux.
3. Add Linux sidecar/desktop compile and package inspection to pull-request CI.
4. Build the `.deb`, verify daemon/UI/restart/quit behavior, and fix package
   metadata or dependency issues.
5. Add tagged-release artifact naming, checksum, verification, and upload.
6. Complete one real supported Linux desktop install/upgrade/uninstall run.
7. Update user and architecture documentation without changing macOS gates.

## 9. Test plan and matrix

Required automated evidence:

- Linux sidecar is ELF x86-64 and desktop package is amd64;
- no runtime `python`, `python3`, `node`, or repository checkout is used;
- daemon starts, reports desktop-managed health, and serves `/` plus a routed
  Webapp URL;
- duplicate application launch does not create a second node;
- restart replaces the child and becomes healthy;
- quit terminates the managed child;
- logs are written under the Linux path;
- the `.deb` installs or extracts with the expected files and metadata;
- macOS Intel/ARM compile and tagged DMG jobs remain unchanged and passing.

Manual release evidence on one supported Linux desktop:

- clean install from the downloaded `.deb`;
- first launch and window/tray behavior;
- recommendations page opens from bundled UI;
- restart and quit work;
- upgrade from the previous test package preserves data;
- uninstall leaves or removes data exactly as documented.

## 10. Acceptance criteria

- [ ] A versioned x86_64/amd64 `.deb` is built and published with its SHA-256.
- [ ] Package metadata version matches the Git tag and the artifact contains the
  expected desktop binary, resources, icons, and exact sidecar.
- [ ] The installed application runs without system Python, Node.js, a Vite
  server, or a source checkout.
- [ ] ELF sidecar and `.deb` architecture match x86-64/amd64.
- [ ] First startup reports desktop-managed health and serves `/` plus a routed
  production Webapp URL.
- [ ] Single-instance launch, restart, clean quit, and managed-child cleanup are
  verified.
- [ ] Linux logs and user data use documented XDG-compatible paths.
- [ ] Clean install, upgrade preserving user data, and uninstall behavior are
  verified on at least one supported real Linux desktop.
- [ ] Installation, update, uninstall, checksum, system requirements, desktop
  environment limitations, and data retention are documented.
- [ ] Linux CI and release jobs pass without weakening existing macOS Intel and
  Apple Silicon verification.

### Required acceptance evidence

- Attach `dpkg-deb --info`, package file-list, `file`/ELF architecture, and
  SHA-256 output for the final artifact.
- Attach isolated sidecar start, health, UI route, restart, quit, and orphan-
  process checks from CI.
- Attach a manual acceptance record from one named supported distribution and
  desktop environment, covering install, launch, tray/window, upgrade, and
  uninstall.
- Demonstrate runtime success with `python`, `python3`, `node`, and the source
  checkout unavailable to the application.
- Attach unchanged/passing macOS desktop job results and record the release
  commit/tag.

## 11. Readiness assessment

**Can development start now: yes.** There is no product or protocol blocker.
The first implementation work is the cross-platform Rust shell and sidecar
script, followed by Linux CI and release packaging. This Windows checkout
cannot provide final runtime acceptance; the GitHub Linux runner and one real
supported Linux desktop are required before the Issue can close.
