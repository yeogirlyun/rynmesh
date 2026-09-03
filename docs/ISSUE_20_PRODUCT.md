# Issue #20 product requirements: Linux desktop package

## User problem

Linux users can run the Python node and webapp from source, but they do not have
the same install-and-launch experience as macOS users. Requiring Python, Node,
and repository setup excludes ordinary desktop users and makes the installed
runtime diverge from the released product.

## Product decision

The first supported Linux artifact is one versioned Ubuntu 24.04 x86_64
`.deb`. It includes the Tauri desktop shell, production webapp, and a frozen
node sidecar. The operating system supplies normal GTK/WebKit desktop
libraries; the user does not supply Python, Node.js, Vite, or source code.

## User experience

- The user verifies the published SHA-256 and installs the `.deb` with APT.
- Launching Ryn opens the bundled UI and starts exactly one local managed node.
- Closing the window leaves the tray app running; Open, Open Logs, Restart Node,
  and Quit are available from the tray.
- A crash of the managed node is recovered by the desktop watchdog.
- Upgrading preserves owner data; uninstall removes application files but
  retains owner data and logs.
- Linux desktop logs follow XDG conventions.

## Supported boundary

Supported: Ubuntu 24.04 LTS, x86_64, interactive desktop session. Not claimed:
ARM64, other distributions, headless use, Windows, AppImage/RPM/Flatpak/Snap,
automatic updates, package signing, or every tray implementation.

## Completion boundary

Development is complete when the selected `.deb` format, self-contained bundle
design, architecture checks, lifecycle smoke, documentation, release wiring,
and preserved macOS verification are implemented and pass all locally available
checks. The original Issue still has two release operations after that point:
the installed Linux lifecycle smoke must pass in CI, and the `.deb` plus
checksum must be published through the existing release workflow.

A physical GNOME install/upgrade/uninstall exercise is recommended release QA,
not an extra requirement added to the seven upstream acceptance criteria.
