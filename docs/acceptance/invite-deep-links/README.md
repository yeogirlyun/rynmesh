# Desktop friend invitation links (#41)

## Behavior and boundaries

The desktop bundle registers `ryn` and the existing `rynmesh` schemes through
Tauri's deep-link plugin. Its single-instance integration forwards warm launches;
macOS URL events also focus the existing main window. The frontend subscribes
before reading cold-start URLs, preserving a newer event over a stale startup read.

Only one bounded `ryn://join/<base64url-envelope>` or existing `rynmesh://join/`
link is accepted at a time. Unsupported routes, queries, fragments, credentials,
oversized inputs and multi-link batches produce a generic notice. The envelope is
not decoded or trusted by the shell. `ryn` is normalized to the existing invitation
transport prefix; the signed payload and peer protocol remain unchanged.

An accepted link opens Friends with a pending in-memory invitation. The user must
choose Load opened invitation, then Review invite, then Add this friend. A second
different pending link never silently replaces the first. Loading explicitly
replaces the current paste and invalidates an older review. The existing local
node verifies signatures and permissions before any peer contact.

The invitation is not put into navigation state, page URLs, browser history,
local/session storage or application logs. It is transient in the OS/plugin and
frontend memory; process restart or page reload may require reopening the link.
OS protocol delivery itself is outside the app's control: Windows/Linux may pass
the URI as a process argument. This does not claim to hide it from the OS or local
process-inspection tools. No new friend permission is granted by opening a link.

## Evidence

2026-09-17, Windows, Node 22.22.1:

- Full frontend suite: 338 passed across 54 files. The final additional Friends
  interaction test passed with all 18 Friends tests; TypeScript/build passed again.
- Contract tests cover cold/warm delivery, startup races, cleanup, malformed links,
  multi-link rejection and generic error reporting. Component tests check memory
  handoff, route/history state, collision handling, no automatic HTTP call, browser
  fallback, and explicit loading/review before join.
- Cargo resolved the exact deep-link 2.4.9 dependency and updated the existing lock
  without updating unrelated packages. Native compile is checked separately in CI.

Implementation follows the [Tauri deep-link guide](https://v2.tauri.app/plugin/deep-linking/).
Only `get_current` is exposed to the frontend in addition to existing event APIs;
runtime scheme registration and arbitrary external-URL opening are not enabled.

## Required installer acceptance

1. Install the candidate supported desktop package on a disposable test account.
   Create a fresh friend invitation on another reachable test node.
2. Quit Ryn, open the invitation using its registered scheme, and confirm that
   the app starts, reaches Friends and offers Load opened invitation. No peer
   contact or friendship should exist before explicit review and confirmation.
3. Repeat while Ryn is running and while its window is hidden. Confirm that one
   app/node remains and the existing window is focused. Test both supported schemes.
4. Receive another invitation while a paste/review or a pending link already exists.
   Verify the pending-link notice and explicit replacement/review behavior.
5. Test malformed, expired, used and cancelled invitations. Record safe codes only.
   Verify that browser URLs/history and app logs contain no invitation marker.
6. Finish joining, explicitly grant a test AI service, then narrow/revoke it and
   exercise the existing provider-side ACL and in-flight cancellation acceptance.

Native cold/warm OS launch and installer registration have not been observed on
a supported physical desktop in this record. CI compilation and mocked native
event tests cannot close that gate. Cross-NAT reach remains #35.
