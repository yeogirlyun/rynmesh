# Single-file recipient diagnostic tool

The earlier CMD/PowerShell collector required matching files and correct timing.
Replace that support workflow with `RynDiagnostics.exe`, a Windows Forms program
compiled against Windows' .NET Framework. No Python, PowerShell or elevation is
requested. This is diagnostic tooling, not a fix for either reported incident.

The window runs network snapshots and installer monitoring on independent tasks.
It records allowlisted space membership/AI-access fields, local service state,
public provider reachability with/without the system proxy, executable hashes,
process changes, selected installer dialog text, and extracted payload manifests.
It verifies payload hashes only after observing the provisioner or an installation
failure; incomplete verification is explicitly marked. Users may launch their
existing installer from the window. No installer runs automatically.

Reports are append-only JSONL with periodically regenerated ZIP snapshots. Closing
waits for pending bounded checks and saves a final ZIP. Writable destinations are
tried in order: desktop, documents, local app data, temp, executable directory.
If final ZIP publication fails, the UI identifies the retained JSONL folder.
No configuration, firewall policy, permissions, runtime installation, membership
or AI grants are modified. No messages are sent and no raw app logs, credentials,
invitations, user database or chat contents are copied.

Validation performed locally:
- Framework compiler succeeded with Windows Forms and ZIP framework references.
- Self-test passed: failed-primary-destination fallback; invitation-field exclusion;
  continuing after access denied; good/corrupt payload verification; repeated ZIP
  export preserves the log.
- Live read-only smoke test queried the current node and provider endpoints and
  produced a valid ZIP. Secret-field checks passed.
- UI rendering inspected; automatic close completed with background work and
  report saving. No product installer was executed for this test.

Limits: unprivileged process inspection cannot see every elevated process; a
program that cannot start because of OS policy cannot diagnose itself. Native
installer crashes before monitoring catches them can still require further
evidence. The tool retains observations, not a guaranteed traceback from the old
R7 helper, which did not persist one. Recipient-machine execution is still pending.

Delivery is compiled from committed `develop` source. The adjacent build receipt
records the commit, source and EXE hashes, validation, and artifact location.
No existing Ryn installation or company plugin was replaced.
