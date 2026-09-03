# Route packages

A route package is a self-contained module that wires one feature's state,
background work, and HTTP surface into the node app, without adding to
`rynmesh/peer_http.py`. Two exist today and this document describes what
they actually do — it must never say something either one doesn't:

- `rynmesh/mailbox_routes.py::install_mailbox` — the newer package. Its own
  docstrings already explain several of the conventions below; where this
  document and its comments overlap, treat the source as the primary
  reference.
- `rynmesh/llm_package/routes.py::install_llm_routes` — the older, larger
  package. It predates a couple of conventions below (see the callouts) and
  is described as it is, not as it "should" be.

`scripts/new_route_package.py` generates a new package that follows every
convention here. Read this document before hand-editing what it produces.

## Why not add it to `peer_http.py`

`rynmesh/peer_http.py` is 2437 lines against this repo's hard 10K-line
module ceiling, with mandatory restructuring triggered at 8K (see the
project's module-size rule). It is already almost a quarter of the way to
the point where the rule requires stopping feature work on it entirely.
Every route, background worker, and bit of state a new user-facing feature
needs belongs in its own module from the start — not because 2437 lines is
close to either threshold today, but because a module that keeps absorbing
one more endpoint at a time is exactly the pattern the rule exists to stop
before it becomes unreviewable. `peer_http.py` still owns wiring the
package in (one `install_<name>(...)` call) and nothing more.

## The installer signature

The convention going forward — what `install_mailbox` already does and what
the generator produces — is:

```python
def install_<name>(
    app: Any,
    *,
    store: Any,
    home: str | Path,
    workers: BackgroundWorkerRegistry,
    local_control: Callable[[Request], None] | None = None,
    ...,
) -> <Something>:
```

(`install_mailbox`'s real signature also takes `messaging_key` and
`resolve_pubkey`, because a mailbox needs both; a package that has no
messaging identity of its own does not carry them. The shape that is
constant across packages is `app`, `store`, `home`, `workers`, and
`local_control`.)

Each argument exists for a specific reason:

- **`store`** is the node's identity and registry handle (a `RynmeshStore`
  or, in a test, a stub with a `.home`). It is how a package reaches the
  node's peer ID, signing key, credit ledger, or discovery registry without
  the package owning any of those itself.
- **`home`** is the *configured* node home the package puts its files
  under. It is deliberately not the last word — see the next section.
- **`workers`** is the shared `BackgroundWorkerRegistry` the node's lifespan
  starts and stops as one unit (`rynmesh/background_workers.py`). A package
  registers its worker(s) on this registry; it never owns an asyncio task
  or its own start/stop lifecycle.
- **`local_control`** is the node-auth callable a local-only route re-checks
  before answering. `/api/local` is already gated by node-auth middleware;
  passing this in and calling it from the route is a second, per-route
  check, not a substitute for the middleware (see "Routes" below).

`install_llm_routes` predates the explicit `workers` parameter: it does not
take one at all. Instead it reads `app.state.background_workers`, creating
a fresh `BackgroundWorkerRegistry` there if the attribute is absent:

```python
registry = getattr(app.state, "background_workers", None)
if registry is None:
    registry = BackgroundWorkerRegistry()
    app.state.background_workers = registry
```

That fallback exists so standalone package tests and embedders can install
the routes on a plain `FastAPI()` with no pre-wired registry. It is still a
correct pattern for a package meant to be embeddable on its own; it is not
the one to copy for a package that, like the four upcoming ones, is always
installed by the node with a registry already on hand. Take `workers` as an
explicit parameter, the way `install_mailbox` does — a caller that controls
the registry instance directly is easier to test in isolation, and an
installer that is safe to call standalone can still build its own registry
internally and hand it to `workers=` at the call site if that ever matters.

## Where state lives

The installer sets exactly one attribute per package on `app.state`
(`app.state.mailbox` for the mailbox client, `app.state.<name>` for a
generated package's store), and other code reads it back through
`app.state`, not through a value captured at construction time.
`rynmesh/mailbox_routes.py::peer_message_fallback` is the reference for why:

```python
def fallback(peer_id: str, header: dict[str, Any]) -> bool:
    client = getattr(app.state, "mailbox", None)
    if client is None:
        return False
    return bool(client.deposit(peer_id, PEER_MESSAGE_KIND, header))
```

Its docstring explains the ordering problem directly: "The client is read
from `app.state` at call time because the messenger is constructed before
`install_mailbox` runs." Any cross-package hook built before a package is
installed has to close over `app` and read `app.state.<name>` lazily
inside the callback — closing over the package's object directly would
capture `None` (or nothing at all) permanently.

The other half of "where state lives" is *whose* home wins. `home` is
passed into the installer as the configured node home, but the package
writes under the *store's* home when the two disagree —
`install_mailbox` resolves this explicitly and explains why:

```python
resolved_home = Path(getattr(store, "home", None) or home)
if Path(home) != resolved_home:
    log.warning("RYNMESH_HOME differs from the store home; using the store home")
```

The store owns the identity a package's on-disk state is written next to
(for the mailbox, the identity the seen-cache and messages are sealed to);
splitting the two would put a package's files beside a different identity
than the one that will read them back.

## Workers

- Register with `replace=True`. `BackgroundWorkerRegistry.register` raises
  `ValueError` on a duplicate name unless `replace` is set — an installer
  has to be safe to call twice (re-installed routes, a test that builds
  the app more than once), and `replace=True` is what makes that true.
- Pick a `BackoffPolicy` that fits the work. `BackoffPolicy` has no built-in
  "flat interval" constructor; for a worker that should just poll every N
  seconds regardless of activity, build one by hand with the busy and idle
  delays equal and `idle_multiplier=1.0` (see the generated package's
  `TICK_POLICY` for the pattern). For work with a real busy/idle/error
  shape, follow `MAILBOX_POLL_POLICY` or the LLM package's per-worker
  policies instead — both vary the delay with load.
- Surface the error sink onto `app.state.<name>_error`, the same way both
  existing packages do (`app.state.mailbox_error`,
  `app.state.llm_relay_error`). This is what a status route or an operator
  reads to see the worker's last failure without touching logs.
- A worker body must never swallow its own exceptions. `BackgroundWorkerRegistry`
  is what records `consecutive_failures`, `last_failure_at`, and the
  backoff itself (`rynmesh/background_workers.py::_run`); a `try/except`
  inside the worker body that eats an error hides that failure from the
  supervisor entirely; report `WorkerRunResult(activity=...)` and let a
  real exception propagate.

## Routes

- Owner-only routes live under `/api/local/<name>/...`. `/api/local` is
  gated by node-auth middleware, and every local route in both existing
  packages *also* calls `local_control(request)` itself at the top of the
  handler — a second, explicit check on top of the middleware, not a
  replacement for it (`install_mailbox`'s status route does this; so does
  every local route the generator produces).
- Peer-facing routes live under `/api/peer/...` (see the mailbox's own
  registry routes, and the LLM package's peer settlement/cancel routes).
  They are unauthenticated by node-auth and must do their own request
  validation.
- A status route returns bounded metadata only: version/state summary
  fields and `workers.status().get(<worker_name>)` — never a full record,
  a prompt, a key, or anything unbounded. `BackgroundWorkerRegistry.status()`
  itself already returns only scheduling metadata for this reason
  (`rynmesh/background_workers.py::status`'s docstring: "never worker
  arguments/results").

## Files

Everything a package writes lives under `home / "<name>"`, written through
`rynmesh.atomic_io` (`atomic_write_json`, `read_json`, `migration_backup`),
never hand-rolled `open()`/`json.dump()`. Every write is 0600 under a 0700
directory — `atomic_io`'s defaults (`FILE_MODE = 0o600`, `DIR_MODE =
0o700`) already do this; a package's job is to call it with the right path,
not to reimplement the write.

## Errors

Use short, stable string codes (`"capacity_exhausted"`,
`"provider_unavailable"`, `"insufficient_task_balance"` — see
`rynmesh/llm_package/routes.py::_submission_error_code` for the pattern),
mapped to an HTTP status at the route edge (`HTTPException(status_code=...,
detail=...)`). A prompt, a request/response body, a file path, an API key,
or a peer's invite secret must never appear in an error message, a log
line, or a status field. `BackgroundWorkerRegistry._sanitize_error` is the
reference for why: it deliberately discards the original exception message
and keeps only the exception's class name, because "Exception messages from
service code can accidentally contain a prompt, model output, key, URL, or
private path."

## Tests

A route package's test file is expected to cover, at minimum:

- **Installer idempotence**: installing on the same app twice does not
  raise (the `replace=True` worker registration and any `app.state`
  assignment both have to tolerate this).
- **Worker registration**: the worker is registered under its documented
  name with its documented `BackoffPolicy`.
- **The status route under node auth**: it returns exactly its documented
  keys, and (if the package takes `local_control`) that the handler calls
  it.
- **The on-disk record**: a write round-trips and lands at 0600 under the
  package's `home / "<name>"` directory.

`scripts/new_route_package.py` generates a test file covering exactly this
list for a new package's skeleton; see `tests/test_route_package_generator.py`
for the generator's own tests, including one that runs a freshly generated
test file with `pytest` in a subprocess to prove the skeleton actually
passes, not just that it parses.
