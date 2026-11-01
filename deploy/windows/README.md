# Windows friend-AI candidate

`Ryn-Setup.exe` is an ordinary per-user Windows 10/11 x64 installer. It includes
the Python runtime, application and built web UI. Consumers need no Python,
terminal, API key or model installation. The application opens the browser UI
and keeps a small **Ryn is ready** window; closing it stops the local node.

The installer registers `rynmesh://` for this user, adds Start/Desktop shortcuts,
and retains a copy of itself for the provider's `/downloads/Ryn-Setup.exe` link.
Application data stays in `%LOCALAPPDATA%\Ryn\data` across upgrades/uninstall.
The default port is 19600, with 29 fallback ports when occupied. Only loopback
clients can use owner controls; the peer endpoint listens on local interfaces.
Network/firewall reachability is still required. This candidate proves one-host
loopback/LAN-address operation, not cross-network operation. macOS is untested.

An invitation link opens the provider's `/invite#...` page. Keep that page open,
download/install, then choose **Open Ryn**. If browser protocol activation fails,
copy the invitation and paste it into **Friends → Use an invite → Review**.
Clipboard APIs may be unavailable over HTTP; the page selects the invitation
for Ctrl+C. Installing never accepts or sends a question automatically. Expired
invitations require a new invitation from the provider. A saved continuation
is cleared after acceptance or explicit review cancellation, with a compare
fence so a newly received invitation cannot be cleared by an older operation.

This is an unsigned test candidate. It has no production code-signing claim.
The installer download is served by the reachable provider, without analytics;
the invitation capability remains in the URL fragment, not a query parameter.

Build on Windows from the repository root, after installing project dependencies,
PyInstaller and pypdf into an isolated environment, and `npm ci` in `webapp`:

```powershell
./scripts/build_windows_candidate.ps1 -Python '<venv>/Scripts/python.exe' `
  -Node '<node>/node.exe' -InnoCompiler '<inno>/ISCC.exe'
```

Acceptance tooling used Python 3.12.10, PyInstaller 6.22.0 and Inno Setup 6.7.3.
Outputs are `release/windows/Ryn-Setup.exe` and `candidate.json` containing source
revision, dirty-state flag and installer SHA-256. Build/run assets are gitignored.
For a provider launched from source, set `RYNMESH_WINDOWS_INSTALLER` to that output
to enable the same invitation download page. Normal installed providers discover
the installer next to `Ryn.exe` automatically.

The optional `--home`, `--port`, `--headless`, `--no-open` and
`--loopback-acceptance` launcher flags exist for isolated acceptance. Normal
users use the installer/shortcuts/protocol without entering these flags.

If a friend's LAN address changes, ask them for a new ordinary friend invitation.
Paste and review it in Friends. For an existing identity, **Update this friend's
address** verifies the peer at the signed address using the existing encrypted
friend relationship before saving it. It does not create a relationship, accept
the invitation, change an AI grant or send chat history. A revoked friendship
cannot be restored this way. Each direction can be updated independently.
