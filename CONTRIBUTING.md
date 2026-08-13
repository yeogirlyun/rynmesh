# Contributing to Rynmesh

Rynmesh is MIT-licensed with no proprietary dependencies. This document is the
working agreement: how code gets from an idea to something running on a user's
machine.

## Two hats, two entry points

The single most common mix-up: **rynmesh.ai is for using Ryn, GitHub is for
building it.** Everyone who works on Rynmesh wears both hats.

| Hat | Entry point | What you get |
|---|---|---|
| **User** (dogfooding) | `curl -fsSL https://www.rynmesh.ai/download/install.sh \| sh` | The released app in `~/.rynmesh/app`, updated by re-running the same command. Untouched by your working tree. |
| **Contributor** | `git clone` + `./scripts/dev_setup.sh` | A `.venv`, an editable install, webapp deps, and a green test suite. |

Run both. The installed app is how you notice what's actually broken for a real
user; the checkout is where you fix it. They never collide — the installed node
lives in `~/.rynmesh/app` and the dev node runs from your checkout.

## Getting set up

```bash
git clone https://github.com/yeogirlyun/rynmesh.git
cd rynmesh
./scripts/dev_setup.sh
```

That creates `.venv`, installs `rynmesh` editable with dev extras, installs
webapp dependencies, and runs the tests. If the suite is red on a fresh clone,
that's a bug — please open an issue.

## Running it while you work

**Day-to-day** — two processes, hot-reloading UI:

```bash
./.venv/bin/rynmesh-peer      # node + API on :8791
cd webapp && npm run dev      # UI on :5173, proxies /api/local to the node
```

**Before you ship** — exactly what a user gets, one process:

```bash
./scripts/build_release.zsh --skip-tests   # folds the built UI into rynmesh/webui
./.venv/bin/rynmesh-peer                   # open http://127.0.0.1:8791
```

The second form matters. In dev the UI is served by Vite; in a release the node
serves it. Anything that depends on the dev proxy will look fine in dev and
break for users — always smoke-test the packaged form before opening a PR.

## The loop

```
   feature branch ──► PR ──► CI green + review ──► merge to main
                                                        │
                                          scripts/build_release.zsh   (maintainer)
                                          push version tag vX.Y.Z
                                                        │
                                                  GitHub Releases
                                                        │
                    everyone re-runs install.sh and uses the new build
```

1. **Branch** off `main` — `feat/digest-scheduling`, `fix/watcher-dedupe`.
2. **Write a test first** where it's practical. Every bug fix should come with a
   test that fails before it and passes after.
3. **Keep the checks green** before pushing:
   ```bash
   ./.venv/bin/python -m pytest tests/ -q
   ./.venv/bin/python -m ruff check rynmesh/ tests/
   cd webapp && npx tsc -b
   ```
4. **Open a PR** against `main` with what changed and why, plus how you verified
   it. Screenshots for UI work.
5. **CI runs the same three checks.** Green CI plus one review approval merges.
6. **Releases are cut from `main`** by a maintainer (see below).

Direct pushes to `main` are for maintainers doing releases and trivial fixes.
Everything else goes through a PR — including maintainers' feature work, so the
history stays reviewable.

## Cutting a release (maintainers)

```bash
# 1. bump the version in pyproject.toml, commit
# 2. build: runs tests, builds the webapp into the package, and builds the wheel
./scripts/build_release.zsh

# 3. publish: push the matching version tag; GitHub Actions verifies and releases it
git tag -s vX.Y.Z -m "Rynmesh vX.Y.Z"
git push origin vX.Y.Z
```

The release workflow builds the UI-backed wheel from a clean checkout, verifies
the installed package, generates checksums and `latest.json`, and publishes the
artifacts to GitHub Releases.

Then everyone (including you) updates the way a user does:

```bash
curl -fsSL https://github.com/yeogirlyun/rynmesh/releases/latest/download/install.sh | sh
```

## House rules

- **No module over 10,000 lines.** Approaching ~8K means it's time to split by
  responsibility, not to add "one more method."
- **Stdlib-first in the node.** New third-party runtime dependencies need a
  reason in the PR description. Optional integrations (Ollama, the Anthropic
  SDK) must degrade gracefully when absent — the node always runs without them.
- **No proprietary dependencies, ever.** Third-party services plug in through
  open seams like the manifest `attestations` field. See
  `docs/DECISION_AVARYN_SEPARATION.md`.
- **Tests are the contract.** Never make a red test pass by weakening its
  assertion; decide whether the code or the test encodes the right behavior and
  fix that one.
- **Match the surrounding code.** Comment density, naming, and idiom included.

## Where things live

| Path | What |
|---|---|
| `rynmesh/` | The node: daemon, store, crypto, credits, transports |
| `rynmesh/services/` | Digest, model provider, messaging, egress, updater |
| `webapp/` | React UI (built into `rynmesh/webui/` at release time) |
| `tests/` | pytest suite — the release gate |
| `scripts/` | Dev setup, release build, publish |
| `docs/` | Vision, architecture, requirements, decisions, milestones |
| `rynnet/`, `sim/` | Network testbed and scale simulator |

Start with `docs/PRODUCT_MILESTONES.md` for where the project is headed, and
`docs/ARCHITECTURE.md` for how the pieces fit.
