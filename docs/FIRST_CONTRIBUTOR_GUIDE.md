# First Contributor Starter Guide

Welcome to Rynmesh. This guide is the shortest path from a clean machine to a
reviewable first contribution. It complements, rather than replaces,
[`CONTRIBUTING.md`](../CONTRIBUTING.md), the
[`PRODUCT_MILESTONES.md`](PRODUCT_MILESTONES.md) roadmap, and the
[`ARCHITECTURE.md`](ARCHITECTURE.md) trust boundaries.

## Project in five minutes

Rynmesh is an MIT-licensed, local-first personal AI assistant and verifiable
content mesh. A user runs a Ryn node on their own machine. The node owns local
identity and preferences, discovers and ranks content, mediates peer and model
access, verifies signed evidence, and serves both the desktop webapp and MCP
tools.

The current `v0.6.2` release implements the first product milestone, **P1 Ryn
Companion**:

- zero-configuration recommendations from a built-in public catalog
- a private local profile shaped by direction, preferences, and feedback
- article, image, video, and feed-provided audio consumption
- discovery health, notifications, bookmarks, history, and progress
- optional local Ollama or explicitly enabled Anthropic assistance
- Search & Ask, signed publication, provenance, safety receipts, peer
  discovery, encrypted messaging, and non-transferable reputation credits
- self-contained macOS desktop packages for Apple Silicon and Intel

The release is useful but still alpha. The immediate work is reliability,
explainability, test coverage, content-viewer hardening, and desktop
portability. Friend invitations, friend-attributed recommendations, autonomous
agent budgets, and an open network of untrusted peers are later milestones.

## The system boundary

Keep this mental model while reading or changing the code:

```text
user / webapp / MCP client
            |
            v
     local Ryn node daemon
       |       |       |
       v       v       v
 local data  models  registries and peers
```

The local node is the authority. The webapp must not bypass it to operate on
peers, identities, trust, publishing, or owner data. Model use is optional and
cloud access requires explicit owner permission. Recommendations must remain
useful without an account, model, peer, API key, or preference setup.

## Where the code lives

| Path | Responsibility |
|---|---|
| `rynmesh/peer_http.py` | Node HTTP process, peer API, and `/api/local/*` control API |
| `rynmesh/services/digest.py` | Public-source discovery, caching, ranking, feedback, and digest state |
| `rynmesh/recommendation_*.py` | Recommendation profile, evidence, and shared recommendation response |
| `rynmesh/recommender.py` | Candidate sourcing, ranking, exploration, and filtering pipeline |
| `rynmesh/services/model_provider.py` | Optional local Ollama and opt-in Anthropic model seam |
| `rynmesh/store.py` | Identity-bound local store, publication, peer fetch, and verification |
| `webapp/src/screens/Digest.tsx` | Main For You screen and recommendation controls |
| `webapp/src/components/DigestViewer.tsx` | Article, image, audio, and video consumption experience |
| `webapp/src/domain/` | Typed webapp clients and data contracts |
| `webapp/src-tauri/` | Desktop lifecycle, bundled daemon sidecar, and native packaging |
| `tests/` | Backend and API contracts enforced by pytest |
| `.github/workflows/` | Public CI and release gates |

Before changing a contract, search for every backend, webapp, fixture, MCP, and
test caller. There are still Daily Digest and legacy recommendation paths being
consolidated, so a locally correct change can otherwise leave another surface
behind.

## 1. Use the released product

Install the Apple Silicon or Intel DMG from the
[latest release](https://github.com/yeogirlyun/rynmesh/releases/latest). Open
For You, inspect discovery health, open several content formats, provide More
and Less feedback, save a preference, and restart the app.

This is dogfooding, not the development environment. It establishes what a
released user sees and helps distinguish a source-code problem from a local
development setup problem.

## 2. Prepare a source checkout

Prerequisites:

- Git
- Python 3.10 or newer
- Node.js 22 and npm
- Rust stable only when working on the Tauri desktop shell
- macOS for the currently supported native desktop package; backend and webapp
  development can also run on Linux

Clone the canonical public repository and run the setup script:

```bash
git clone https://github.com/yeogirlyun/rynmesh.git
cd rynmesh
./scripts/dev_setup.sh
```

The script creates `.venv`, installs the Python package and development tools,
installs webapp dependencies when npm is available, and runs the backend test
suite. A fresh checkout that does not pass is a bug to report before feature
work begins.

For external contributions, fork the public repository on GitHub and add the
fork as your push remote. Create one focused branch for the claimed issue:

```bash
git switch -c test/issue-15-for-you
```

## 3. Run an isolated development node

Quit the released desktop app so it does not compete for port `8791`. Keep
development identity, preferences, and content out of the released profile by
using a separate node home:

```bash
export RYNMESH_HOME="${TMPDIR:-/tmp}/rynmesh-contributor-node"
export RYNMESH_AUTO_REGISTER="0"
export RYNMESH_DEFAULT_DISCOVERY="1"
./.venv/bin/rynmesh-peer
```

In a second terminal:

```bash
cd webapp
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api/local/*` to the node on
`127.0.0.1:8791`.

`RYNMESH_AUTO_REGISTER=0` prevents an ordinary UI contribution from
registering a development identity. `RYNMESH_DEFAULT_DISCOVERY=1` enables the
same built-in public discovery catalog used by the desktop app. Manual
discovery can contact public sources; automated tests must use deterministic
fixtures and must not contact YouTube, Reddit, RSS hosts, peers, registries,
Ollama, Anthropic, or any other live service.

## 4. Claim an accepted issue

The executable backlog is the
[P1 hardening milestone](https://github.com/yeogirlyun/rynmesh/milestone/1).
Choose an available item in the
[contribution center](https://www.rynmesh.ai/contribute/) and comment `/claim`
on its GitHub issue. The bot grants one primary seven-day reservation per
issue. Design-, privacy-, and large-scope work remains approval-pending until a
maintainer comments `/approve`; do not begin that implementation earlier.

Post the intended approach and expected tests after claiming, then open a draft
pull request or progress update within the reservation. Use `/extend` when more
time is needed and `/release` if plans change. One issue should normally
produce one focused pull request.

### Recommended first assignment

[Issue #15: protect the For You personal-assistant critical
path](https://github.com/yeogirlyun/rynmesh/issues/15) is the recommended first
contribution.

The expected outcome is a deterministic React component and interaction test
foundation covering:

- first-load ready, refreshing, degraded-source, empty, and daemon-error states
- written direction plus topic and platform preferences
- More, Less, Hide, bookmark, and open behavior
- representative article, image, audio, and YouTube viewer behavior
- a required webapp test command in GitHub Actions

A suitable proposal may use Vitest, React Testing Library, and a maintained
request-mocking approach, but dependency choices should be stated in the issue
before implementation. Assertions should verify user-observable behavior and
API effects. Avoid snapshot-only tests, arbitrary timeouts, and live network
requests. Production changes should be limited to small testability seams
agreed during review.

### Follow-up assignments

- [Issue #16](https://github.com/yeogirlyun/rynmesh/issues/16): expose
  per-source discovery health, cached status, failure reasons, and focused
  recovery.
- [Issue #17](https://github.com/yeogirlyun/rynmesh/issues/17): explain learned
  recommendation signals and allow individual feedback undo.
- [Issue #18](https://github.com/yeogirlyun/rynmesh/issues/18): design the
  consolidation of Daily Digest and legacy recommendation contracts. This is
  design-first and must be split after approval.
- [Issue #19](https://github.com/yeogirlyun/rynmesh/issues/19): design and then
  harden viewer formats, accessibility, and media privacy.
- [Issue #20](https://github.com/yeogirlyun/rynmesh/issues/20): add a verified
  Linux desktop package without weakening macOS release checks.

Do not begin cryptography, node identity, authentication, credit issuance,
registry trust, VPN credential sharing, or transferable-credit work without an
approved maintainer design issue.

## 5. Work in small, reviewable increments

- Reproduce the current behavior before changing it.
- Add a failing test first for a bug when practical.
- Match the surrounding types, naming, and error conventions.
- Keep backend state atomic and local-first.
- Avoid new runtime dependencies unless the issue requires one and the PR
  explains why the standard library or an existing dependency is insufficient.
- Preserve a useful no-model, no-peer, and no-preference experience.
- Never weaken signature, provenance, safety, authentication, or privacy checks
  merely to make a test pass.
- Do not combine dependency upgrades, broad refactors, or unrelated cleanup
  with the claimed behavior.
- Open a draft pull request once the test strategy and basic structure are
  visible. Link it to the issue so implementation progress and design feedback
  remain public.
- Comment on the issue when scope changes or progress is blocked; do not let an
  assigned issue silently appear active after work has stopped.

## 6. Verify before submission

Run the baseline checks from the repository root:

```bash
./.venv/bin/python -m pytest tests/ -q
./.venv/bin/python -m ruff check rynmesh tests qa
(cd webapp && npm ci && npm run build)
```

If the contribution adds the webapp test foundation, run its committed test
command locally in non-watch mode and confirm CI runs the same command.

Additional checks depend on the changed surface:

### Backend or API

- Test successful, invalid, missing, offline, and persisted-state behavior.
- Use temporary directories and injected fetchers; do not use the owner’s real
  `RYNMESH_HOME` or the live network.
- Confirm local-control authorization and peer-facing routes have not been
  confused.
- Confirm errors are bounded and do not expose keys, tokens, private paths,
  internal addresses, or raw third-party responses.

### Webapp

- Confirm loading, empty, success, degraded, and error states.
- Confirm keyboard access, visible focus, labels, and narrow-window layout.
- Confirm actions are not duplicated when clicked, retried, or rendered under
  React Strict Mode.
- Confirm the UI does not silently contact a peer or content platform outside
  the approved node-mediated data flow.
- Include screenshots or a short recording for visible changes.

### Packaged node

For changes to routes, assets, or build behavior, verify the release form with
no Vite server:

```bash
./scripts/build_release.zsh --skip-tests
export RYNMESH_HOME="${TMPDIR:-/tmp}/rynmesh-packaged-smoke"
export RYNMESH_AUTO_REGISTER="0"
./.venv/bin/rynmesh-peer
```

Then confirm `http://127.0.0.1:8791/health`, the root UI, and the affected route
work. Stop the node when finished and confirm the working tree contains no
unexpected generated changes.

### Desktop shell or sidecar

From `webapp/`, run:

```bash
cargo check --manifest-path src-tauri/Cargo.toml
./src-tauri/scripts/build-sidecar.sh
sidecar="$(find src-tauri/binaries -type f -name 'rynmesh-peer-*' -print -quit)"
./src-tauri/scripts/verify-sidecar.sh "$sidecar"
```

Confirm startup, health, UI serving, restart, clean shutdown, and architecture
matching. Native package behavior must ultimately pass the public CI runners;
local compilation alone is not sufficient.

## 7. Open the pull request

Before pushing:

```bash
git status --short
git diff --check
git diff
```

Review your own diff for generated files, credentials, personal paths, private
content, node identities, infrastructure details, and unrelated edits. Commit
with a focused message and open a pull request against public `main`.

The pull request should:

- use `Closes #<issue>` to link the accepted issue
- explain the problem and the chosen approach
- address each acceptance criterion
- list exact verification commands and results
- call out dependency, storage, API, privacy, or compatibility changes
- include screenshots for visible UI work

CI runs backend tests and lint, webapp typechecking and build, packaged-node
verification, and native desktop compilation for Apple Silicon and Intel.
Green CI is required but does not replace review. Respond to review with code,
tests, or a documented technical explanation; resolve discussions only after
the underlying concern is addressed.

## Definition of done

A contribution is complete when the linked issue’s acceptance criteria are
met, relevant tests cover the behavior, current documentation still matches,
CI passes, review is approved, and the pull request is merged into public
`main`. A local branch, draft, or open pull request is not shipped behavior.

Thank you for helping establish the engineering standard that later Rynmesh
contributors will inherit.
