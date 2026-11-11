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

The installed program has not been replaced. Data relocation versus freeing C: is pending the user's choice. No conversations, credentials, plugins or user files were deleted or relocated. These tests establish pending-image UI behavior, not a measured improvement in model response latency or native desktop acceptance after installation.
