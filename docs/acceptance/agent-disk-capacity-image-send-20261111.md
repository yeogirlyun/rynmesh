# Agent disk capacity and pending image sends — 2026-11-11

## Findings

- The active Agent store still imposed a 24 MiB plaintext aggregate ceiling and a 34 MiB encrypted-file ceiling. Earlier storage relief archived old conversations; it did not remove the quota.
- On this machine, C: reported zero free bytes while D: had approximately 228 GiB free. The installed desktop uses a C: data directory. Current encrypted task blobs total approximately 17 MiB; the evidence does not establish that the screenshot's failure hit the aggregate quota. Real disk exhaustion is independently confirmed.
- Pending messages rendered text only. Attachments stayed in the composer until the server acknowledged the request.

## Change

- Remove the history byte quota and byte-triggered archival. Preserve the existing task-index archival policy, encrypted layout, integrity checks and user data.
- Admission checks actual free disk space, allowing for the changed blobs, root replacement and outstanding execution receipts. Unrelated historical tasks no longer require a second full copy of their bytes as dispatch headroom.
- Show pending attachments alongside the submitted text immediately, retaining the sending/uncertain indicator. Hide only those submitted attachments from the composer; preserve subsequent selections and restore submitted attachments on rejection.
- Keep attachment bytes out of the separate pending-message session record; the existing request recovery record remains authoritative.
- Explain a disk-space failure as insufficient free space on the data volume, rather than asking the user to delete conversations to satisfy an Agent quota.

## Validation

- Python: 39 tests passed across store layout, capacity/reservations, storage relief and input attachments. Includes reopening a 26 MiB task, adding another task, and dispatch with large unrelated history on limited free space.
- Frontend: 36 tests passed across pending sends, navigation while sending and error messages. Includes immediate image rendering and attachment restoration after rejection. The first run hit an existing 5-second test timeout under machine load; the sequential rerun with a 20-second timeout passed.
- TypeScript `tsc -b --pretty false` passed.
- Tests use D: temporary directories because C: cannot allocate temporary files.

## Local deployment

The user freed C: space (approximately 13 GiB initially). Data remains in its original location; no conversations, credentials, plugins or user files were deleted or relocated.

- Built committed develop `3c48e5e42ad86c6d902e57bd77f9fb39cf41de9f` in an isolated checkout. Tauri changed only Cargo.toml line endings; Git reported no content diff. The first build lacked Cargo on PATH, and the next rejected the build-cache junction as an isolated-venv path mismatch. The successful build used the existing Rust toolchain and canonical cache paths, with the same committed source.
- Core setup artifact: SHA256 `70febd1991bbb0bab6845eb96a3c675a60b6c492644ffec6d1c60fb79f098ac3`, 62,022,219 bytes. Core binaries were installed by verified file replacement, preserving the NSIS distribution marker; the setup executable was not run over the existing company installation.
- Installation: `C:/Users/子陵/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Ryn`. Rollback: `D:/code/rynmesh/artifacts/local-release-20261111-storage-image/program-backup` and `rollback.ps1`; 913 backup files verified before replacement.
- Desktop automation was unavailable (`failed to write kernel assets`, OS error 3). An initial compound process-stop/install command was rejected by automatic review. After the user explicitly instructed Codex to operate it, the three freshly inspected Ryn/peer process IDs were stopped successfully with a separate scoped command. There were no executing Agent tasks. This was a process stop, not a UI-driven normal exit.
- Installed core `3c48e5e42ad86c6d902e57bd77f9fb39cf41de9f`; existing company runtime `ba1fc166f6640a5bb620aacb3b3fc5ff651375b0` retained and its manifest files verified. Desktop config and plugin service config checksums unchanged. Build-info and node release records updated; executable hashes match the new receipt.
- Restarted installed Ryn. Node health reports desktop-managed; company health is `ok=true`, `status=ready`. C: still had approximately 13.9 GiB free after verification.
- Live synthetic image test through the installed node: attachment persisted and acknowledged in **0.750 s**, task completed in **6.859 s**, correctly recognized `314159`, no error. This is one observed run, not a latency guarantee. The user's financial task was not replayed.
- Pending-image rendering is covered by frontend tests, including the final two focused tests after recovery initialization changes. Native visual acceptance was not performed because the desktop automation runtime was unavailable.
