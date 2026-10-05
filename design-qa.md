# NAS design QA — 2026-09-23

final result: blocked

Web visual checks passed. The remaining blocker is native Windows visual verification: Windows rejects cursor access and monitor capture. The rebuilt client and its file APIs work; this does not substitute for a native-window screenshot. No claim of completed Windows visual acceptance is made.

## Visual truth and evidence

- Source: `C:/Users/子陵/.codex/generated_images/01a0c921-937f-7c01-be86-3b9cc7509fad/exec-689b35bb-8225-4a20-837b-8280b0ec291f.png` (1586 × 992 raster mock).
- User's explicit override: remove the NAS device illustration, foliage and decorative hero; retain the file preview design, color accents and implement both appearances on Web and Windows.
- Implementation: `http://127.0.0.1:18841/nas`, actual fnOS SMB files in `RynAcceptance/预览体验`.
- Dark: `D:/code/rynmesh/build/nas-test/nas-web-dark-image.png` (1280 × 800).
- Light: `D:/code/rynmesh/build/nas-test/nas-web-light-image.png` (1265 × 791 browser screenshot).
- Text: `D:/code/rynmesh/build/nas-test/nas-web-light-text.png`.
- CSS viewport 1280 × 800, devicePixelRatio 1. The light capture is scaled by the browser capture surface to 1265 pixels; compare proportionally, not as a pixel-perfect font raster comparison. Source art is proportionally larger; original sidebar maps from ~278 to the existing 224 CSS px.
- Source, final dark and final light captures were opened together in one comparison input. File names, thumbnail, selected row, preview heading and controls are readable in these full-resolution captures; a separate region crop was unnecessary for this scoped comparison.
- Temporary 960 × 700 and 800 × 700 viewport tests: no horizontal overflow; at 800 the preview moves below the list. Viewport override reset after testing.

## Findings and iteration history

1. [P2, fixed] Duplicate connection heading consumed a full row and pushed preview controls too low. Removed the repeated workspace title and moved ready/refresh/details next to sorting. Final dark screenshot shows the resulting compact toolbar and visible image controls.
2. [P2, fixed] Focus styling overrode the mint selection background. Raised selected-row specificity to include tbody; final light text screenshot shows a mint selected row with a mint edge.
3. [Evidence issue, resolved] Browser fullPage screenshot export produced an invalid narrow rendering. Discarded that evidence by replacing files with current-viewport getScreenshot captures, inspected afterward. This was capture behavior, not accepted UI evidence.
4. [Verification blocker] Native Windows screenshot/input unavailable (`GetCursorPos` access denied; monitor capture unavailable). Read-only accessibility confirms the running Ryn client, existing device identity and NAS entry. Await desktop access to verify dark/light, photo and text in the actual client.

## Required fidelity surfaces

- Typography: retained Ryn's Segoe UI/Microsoft Chinese fallback, 13 px filenames, 14 px preview heading and clear secondary metadata. Intentional use of existing app typography instead of a new global font. Chinese filenames remain readable; long names truncate and retain titles.
- Layout: source selector above one continuous file workspace; equal list/preview columns at desktop size, bounded scroll for long lists, compact controls. Extra source selector is required for real multi-NAS support. No hero artwork. Modified/type columns collapse when space is limited.
- Colors: mint primary actions, mint selected rows, lavender documents, amber folders, blue image icons. Dark uses lighter mint on navy; light uses a darker green primary for white-label contrast. Global app chrome retained, NAS active navigation coordinated.
- Imagery: actual NAS file thumbnails and decoded previews, preserving aspect ratio with contain; thumbnails crop intentionally. Sample sunset was generated with built-in ImageGen, stored on the local fnOS test share, and retrieved through the real file API. Standard icons use the existing Lucide library. No replacement decorative NAS illustration.
- Copy/content: actual source names, filenames, image dimensions and sizes. No invented disk capacity, synchronization or remote-connection metrics. Original-download action distinguished from bounded preview. Image AI is not implied.

## Functional validation

- 69 frontend tests / 14 files pass; 20 NAS backend tests pass; TypeScript/Vite production build and Ruff pass.
- Web: select image/text, thumbnail load, 125% zoom, fit, large modal, close, both themes, responsive layout. Final console error list empty.
- Backend: JPEG/PNG/WebP decoding, dimensions/pixel/byte bounds, corrupt/active input rejection, metadata removal, retained original bytes and plugin disable gate covered.
- Actual fnOS: image 1536 × 1024; thumbnail 160 × 107; original 2,671,505 bytes. Web and Windows node API calls succeeded. Download hash equals local source. Desktop-origin CORS exposes dimensions.
- Windows: Ryn.exe, bundled backend and NSIS installer rebuilt; latest client restarted. Native dark/light visual confirmation remains pending.

## Implementation checklist

- [x] Remove decorative header; shared Web/Windows design and both palettes.
- [x] Real image thumbnails, preview, zoom, large view and text side preview.
- [x] Tests, actual fnOS reads/downloads and desktop packaging.
- [x] Web visual evidence and responsive checks.
- [ ] Native Windows dark/light UI verification after desktop access returns.

## Generated test image provenance

- Local file: `D:/code/rynmesh/build/nas-test/海边日落.png`.
- Built-in ImageGen, no API CLI. Prompt: “Create one photographic landscape image, 1536x1024, no UI, no text, no frame: a quiet ocean beach at sunset, gentle turquoise surf and reflective wet sand, golden sun near horizon, peach and lavender cloud sky, distant dark headland on the right. Natural realistic travel photograph with beautiful warm colors. This is a sample photo file for testing a NAS image preview, not a hero illustration or interface mockup.”

## Separate AI workbench polish — 2026-09-23

The shared frontend polish passed scoped visual and functional checks. See [workbench polish QA](docs/design/agent-api-sharing-v1/workbench-polish-qa.md). This result does not supersede the NAS native-window verification limitation above.

## Service navigation and AI sources — 2026-09-24

final result: passed

Scoped acceptance of the user's selected second navigation design. Full source/implementation evidence, viewport normalization, fixed visual findings, functional preservation inventory and test results are recorded in [service navigation design QA](docs/acceptance/service-navigation/design-qa.md). All existing routes and service handlers were retained; 60 related tests and the production build passed. This separate result does not change the earlier NAS native-window limitation.
