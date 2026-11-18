# Image studio 1.1.0

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

## Create and reuse an image

Choose a scene, attach materials or style references, and enter a headline, subtitle or badge.
Choose a style and a suggested or custom size. Check **Transparent background** to make a cutout.
An upload can be saved to **My assets** for reuse. The host can also save an asset to **Public assets** for colleagues.
Open a finished image to change the background, edit the copy, change the viewing angle, or generate several other sizes.
Derived cards link back to the original.

## Colleague API (through the node site)

Identity comes only from the node-sites host (member, name, device,
provider). History follows the person, so a colleague's second computer sees
the same jobs. Another person's job id answers 404.

| Method and path | Purpose |
| --- | --- |
| `GET /api/me` | caller name, `is_owner`, model readiness, queue length, average seconds, `purposes`, `scenes` and `styles`. Each scene includes its default purpose, width, height and a `sizes` array of width/height pairs |
| `POST /api/uploads` | raw PNG/JPEG/WebP body, at most 8 MiB, checked by content; returns `upload_id` |
| `GET /api/assets` | returns `mine` and `public` arrays with id, name, kind and creation time |
| `POST /api/assets?name=…&kind=product` | raw image, up to 8 MiB. Kinds are `product`, `logo`, `style`. Host may add `public=1`. Returns the asset metadata |
| `GET /api/assets/<id>/thumb` | visible asset’s JPEG preview |
| `DELETE /api/assets/<id>` | remove one of the caller’s assets. The host may also remove a public asset |
| `POST /api/jobs` | submit the JSON body below. Returns `{jobs}` |
| `POST /api/jobs/<id>/derive` | submit `{"sizes":[{"width":1200,"height":400},{"purpose":"main"}]}` for one of the caller’s successful jobs. Returns one batch with one job per size, up to six |
| `GET /api/jobs?state=all\|active\|done&limit=` | the caller's jobs, newest first, with `position` and `eta_seconds` while queued |
| `GET /api/jobs/<id>` | one job |
| `POST /api/jobs/<id>/cancel`, `/retry` | cancel a queued or running job; retry a failed or cancelled one |
| `GET /api/jobs/<id>/thumb` | JPEG, longest side 384 px, for lists |
| `GET /api/jobs/<id>/image` | the PNG, as an attachment |

### Job fields

```json
{
  "scene": "banner",
  "purpose": "custom",
  "width": 1200,
  "height": 400,
  "copy": {"title": "Spring collection", "subtitle": "Everyday essentials", "badge": "New"},
  "style": "fresh",
  "brief": "Leave space on the left for the headline.",
  "transparent": false,
  "count": 1
}
```

Scene IDs are `banner`, `main`, `poster`, `detail` and `social`. Style is optional. Read the scene sizes and style choices from `/api/me`.
Omitting the purpose uses the selected scene’s default. Named purposes retain their original sizes:
`main` 1024×1024, `poster` 1024×1365, `story` 1024×1820 and `banner` 1820×1024. To request exact pixels, send `purpose: "custom"` and integer width and height between 256 and 2048. The worker fits the result to those dimensions.

- `references` accepts upload entries such as `{"id":"<upload_id>","role":"material"}` and library entries such as `{"asset":"<asset_id>","role":"style"}`. A material depicts the subject. A style reference guides colors, composition and mood. A legacy upload ID string defaults to material. An asset without an explicit role defaults to style for a style image, otherwise material.
- `copy.title`, `copy.subtitle` and `copy.badge` allow 40, 60 and 20 characters respectively. The legacy `words` field accepts 200 characters. A new request needs a brief, text or an image. The brief allows 2000 characters.
- `source_job` points to one of the caller’s successful jobs. Its result becomes the first reference and counts toward the five-image maximum. Add `edit` with `background`, `text` or `angle` for a quick edit. Use `copy` for replacement text and `brief` for optional instructions.
- `transparent: true` requests an alpha channel in the result PNG. The thumbnail endpoint returns JPEG. The page uses the PNG for transparent previews.
- `count` accepts 1, 2 or 4 and creates one job per image. Job views include `reference_roles`, `scene`, `style`, `copy`, `transparent`, `edit` and `derived_from`. A size variant keeps the source image’s transparency setting.
- Each caller can have 20 active jobs and 100 personal assets. The shared public library holds another 100 assets. Jobs and result files are retained for 90 days and unused temporary uploads for one day. Library assets remain available until deleted.

Failure codes shown to the colleague: `quota_exhausted`, `codex_signed_out`,
`codex_missing`, `content_rejected`, `timeout`, `cancelled`, `interrupted`
(the computer restarted three times during the job), `generation_failed`, and
`image_reference_missing` for a missing attachment. Detailed worker errors are reserved for the owner view.

## Owner routes (this computer only)

`GET /api/local/image-studio/status` (Codex readiness, open state and
audience, queue length, running job, images today, average seconds),
`GET /api/local/image-studio/queue`, `GET /api/local/image-studio/usage`
(per person: today, last 30 days, failed), `POST
/api/local/image-studio/jobs/<id>/cancel` and `/prioritize`.

## Storage

`<node home>/image-studio/studio.db` (SQLite, WAL) and
`<node home>/image-studio/files/<per-person directory>/`. Saved asset originals and thumbnails live in `<node home>/image-studio/assets/`. One worker thread
runs one job at a time. After a restart, jobs that were running go back to the
queue.
