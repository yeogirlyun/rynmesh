# Image studio

The image studio is the first [node site](NODE_SITES.md). Colleagues open it
from **Apps › Shared by colleagues** in their own Ryn. Requests run on the hosting computer's
signed-in Codex (native image generation, the same path as the agent's
`ryn_image_generate`) and the images come back to them. No separate API key
or server is involved. Offline means offline.

Since 2026-11-17, its source lives in `plugins/image-studio/` and it is installed
as an independent signed public plugin, rather than shipped inside the core.
The plugin registers the in-process site `image-studio`; installation does not
enable sharing. Existing image data is retained. See
[public and private plugin instructions](PUBLIC_AND_PRIVATE_PLUGINS.md) for
installation, updates, migration, and the current company-sharing boundary.

Install from **Apps › Discover**. Open the Image Studio app detail to use it,
manage its sharing and queue, or review versions and updates.

## Colleague API (through the node site)

Identity comes only from the node-sites host (member, name, device,
provider). History follows the person, so a colleague's second computer sees
the same jobs. Another person's job id answers 404.

| Method and path | Purpose |
| --- | --- |
| `GET /api/me` | caller name, Codex readiness, queue length, average seconds, purposes |
| `POST /api/uploads` | raw PNG/JPEG/WebP body, at most 8 MiB, checked by content; returns `upload_id` |
| `POST /api/jobs` | `{purpose, brief, words?, count?, references?, source_job?, width?, height?}` → `{jobs}` |
| `GET /api/jobs?state=all\|active\|done&limit=` | the caller's jobs, newest first, with `position` and `eta_seconds` while queued |
| `GET /api/jobs/<id>` | one job |
| `POST /api/jobs/<id>/cancel`, `/retry` | cancel a queued or running job; retry a failed or cancelled one |
| `GET /api/jobs/<id>/thumb` | JPEG, longest side 384 px, for lists |
| `GET /api/jobs/<id>/image` | the PNG, as an attachment |

Purposes: `main` 1:1 1024×1024, `poster` 3:4 1024×1365, `story` 9:16
1024×1820, `banner` 16:9 1820×1024, `custom` with `width`/`height` 256–2048.
Codex always requests size `auto` but follows the ratio written in the brief,
so the brief states the shape and ratio; the result is then center-cropped and
resized to the exact pixels. `words` are put into the brief as text that must
appear exactly. `count` is 1, 2 or 4 (one job each). Up to 3 uploaded
references; `source_job` (one of the caller's finished jobs) is attached as the
first reference, for "change this one".

Limits: 20 active jobs per person, brief 2000 characters, words 200. Jobs and
files are kept 90 days; unused uploads 1 day.

Failure codes shown to the colleague: `quota_exhausted`, `codex_signed_out`,
`codex_missing`, `content_rejected`, `timeout`, `cancelled`, `interrupted`
(the computer restarted three times during the job), `generation_failed`.

## Owner routes (this computer only)

`GET /api/local/image-studio/status` (Codex readiness, open state and
audience, queue length, running job, images today, average seconds),
`GET /api/local/image-studio/queue`, `GET /api/local/image-studio/usage`
(per person: today, last 30 days, failed), `POST
/api/local/image-studio/jobs/<id>/cancel` and `/prioritize`.

## Storage

`<node home>/image-studio/studio.db` (SQLite, WAL) and
`<node home>/image-studio/files/<per-person directory>/`. One worker thread
runs one job at a time. After a restart, jobs that were running go back to the
queue.
