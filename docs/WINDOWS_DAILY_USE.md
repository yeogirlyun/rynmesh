# Windows daily use — Desktop controls

Implemented on `development/personal-first-implementation`.

## Where to operate

Open **Settings → Desktop**. The page uses the existing English light/dark theme.

| Control | Behavior |
| --- | --- |
| Launch at sign-in | Current-user Windows Run entry; does not run before Windows login. |
| Start in the system tray | Adds `--background` to sign-in launch. Opening Ryn normally still shows its window. |
| Keep running when closed | Closing hides the window; the first close explains how to reopen and quit. |
| Pause / Resume AI sharing | Stops or allows new AI requests. Existing AI requests can finish. Personal-space permissions and other services are unchanged. |
| Keep awake while sharing AI | Optional, off by default. Prevents idle system sleep during AI sharing, AI work or model setup. Display sleep remains allowed. Does not override manual sleep, lid closure or shutdown. |
| Recover the node automatically | Checks local control health, retries at most three times, with backoff. A stable minute resets the budget. Manual restart resets it too. |
| Restart node | Native confirmation, including AI task / setup status when available. Restarts only the daemon owned by this app. |
| Open logs | Opens the local node log folder. Previous daemon log rotates on launch after exceeding 5 MiB. |
| Export diagnostics | Writes a JSON report under the log folder. Only app version, platform and allowlisted runtime status; no raw logs, credentials, device IDs, IPs, invitations or conversations. |
| Quit Ryn | Native confirmation also works if the node/webview is unavailable. Stops the owned daemon process tree. |

The Windows tray provides status, Open Ryn, Pause/Resume AI Sharing, Open Logs, Restart Node and Quit Ryn.

The page remains available on the offline screen. A regular browser disables Windows controls. `?client=fixture` demonstrates interactions without invoking native commands.

## Implementation notes

- Local `/api/local/desktop/status` and `/api/local/desktop/sharing` use the existing control authentication middleware. The status endpoint never probes or restarts an external model runtime.
- Native health checks honor an explicit local token and do not confuse a configured peer-network key with node failure.
- The watchdog continues after manual restart. Lifecycle operations are serialized; permanent shutdown has a separate flag.
- Windows power requests are set/released on the same watchdog thread, using [SetThreadExecutionState](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate).
- A wake resumes periodic node checks. Existing node address refresh / Registry republishing handles interface changes. This is not a promise that every network supports direct NAT traversal.
- Old desktop preferences migrate with defaults. Keys, personal-space files and node settings remain in their existing data directory. Install over the existing copy; `/UPDATE` preserves the startup registry entry. Explicit uninstall may remove the app's startup entry.
- AI sharing enabled means admission is enabled; it does not certify model health. Use Model & service setup to inspect the model.

## Validation

- Frontend: 58 tests passed; production build passed.
- Backend: 128 targeted tests passed (desktop control, LLM package/hardening/auto-connect and node auth).
- Rust: 3 tests passed (old preference migration, bounded recovery/backoff, disabled recovery).
- Actual packaged Windows app: separate disposable node and identity; startup registry command, sharing pause persistence and allowlisted diagnostic export verified through the real controls.
- Forced child-process crash recovered in approximately 12 seconds with the same identity and a configured network key.
- Occupied-port case reaches the three-attempt limit instead of retrying forever.
- Manual restart confirmed in the native dialog; a subsequent forced crash recovered in 13.7 seconds, proving the watchdog remained alive.
- Keep-awake toggle persisted and reported an active Windows power request; it was turned off again. First-close notice appeared and the node stayed reachable after its window hid.
- Native Quit confirmation stopped the application and its listening node. `--background` launched a healthy node without a visible main window. Test changes to desktop preferences and the Windows startup entry were restored.
- Light/dark page screenshots and working preview retained.

## Outside this delivery

Windows Service / operation before login, automatic updates, code signing, unattended model-runtime restart, relay fallback, and new remote desktop / file sharing features.

A real Windows reboot/sign-in, physical sleep/lid cycle, and installer upgrade on a separate PC still need acceptance testing. This session does not simulate those as completed tests.

## Deliverable

`release/windows/Ryn-0.6.2-personal-space-desktop-windows-x64-setup.exe`

SHA256: `7591a62f21fec62350a0da09e35f65488614af5727bbbb7d634375659b76b633`
