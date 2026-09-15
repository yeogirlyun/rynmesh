# PR #57 release hardening — v0.7.0 (work plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every task is test-first: write the failing test, watch it fail, implement, watch it pass, commit.

Status: in progress (maintainers, taking [PR #57](https://github.com/yeogirlyun/rynmesh/pull/57) over the line). Branch `release/v0.7.0` starts at the PR #57 head `e800fe6` so yyeogirl's ninety commits and authorship are preserved; this plan adds fix commits on top.

**Goal:** Ship yyeogirl's eight product features as v0.7.0 with the review findings fixed and the review's test gaps closed, so product work can continue on a released, tested base.

**Architecture:** Every fix stays inside the module that owns the behaviour (ask_ryn, device_sync, friends, local_search, ai_access, privacy_export, offline_reading, webapp screens). No new cross-module coupling. Wire formats that change (friend join, device-sync receipts) are versioned in place; nothing is released yet, so no compatibility shims.

**Tech Stack:** Python 3.12 FastAPI backend (`pytest`, `ruff`), React + TypeScript web app (`vitest`, `tsc -b`), Tauri desktop shell.

**Spec:** the review findings summarised in this plan's task headers (source: maintainer review of PR #57, 2026-09-15).

## Global constraints

- No source module may exceed 10,000 lines; new code goes in small, single-purpose modules.
- Backend gate: `python -m pytest tests/ -q` (baseline 1524 passed) and `python -m ruff check rynmesh/ tests/` must pass after every task.
- Web app gate: `cd webapp && npm test -- --run` (baseline 256 passed) and `npx tsc -b` must pass after every web task.
- Never show a success state the backend did not confirm; never hide a failure behind a generic message.
- Product-visible copy stays in the existing voice: plain sentences, no exclamation marks, tells the user what did and did not happen.
- Secrets (invite secrets, relationship secrets, provider credentials) never appear in URLs, logs, exports or error details.
- Commit per task with a conventional message and the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## Part A — backend blockers

### Task 1: Ask Ryn cancel can never block the status check

**Finding:** `rynmesh/ask_ryn/runs.py:202-210` calls `commands.cancel` before `commands.status` on every tick once `cancel_requested` is set. `rynmesh/llm_package/routes.py:2141` raises `HTTPException(409)` whenever the balance release fails with anything other than a missing hold, so the 409 repeats every tick, status is never read, and the run stays `cancel_requested` forever, even after the provider finished.

**Files:**
- Modify: `rynmesh/ask_ryn/runs.py` (`run_once`, lines 196-226)
- Test: `tests/test_ask_runs.py`

**Behaviour:**
- A cancel attempt that raises any `HTTPException` other than 404 is recorded on the run as `cancel_error_code` (string, sanitised with the same `[a-z][a-z0-9_]{0,95}` rule used for `error_code`; use `"cancel_rejected"` when the detail is not a safe code) and the tick continues to `commands.status`.
- A cancel attempt that raises 404 also continues to `status`; the existing status-404 path decides `interrupted`.
- A successful cancel sets `run["cancel_delivered"] = True`; later ticks skip `commands.cancel` for that run.
- Nothing else about ordering changes: the durable dispatch claim still precedes `submit`.

- [ ] **Step 1: Write the failing tests** in `tests/test_ask_runs.py`:

```python
def test_cancel_rejection_never_blocks_result_archiving(tmp_path):
    history, runs, orders, request = setup(tmp_path)
    runs.begin(request)
    runs.run_once()                      # dispatch
    runs.cancel(request["task_id"])
    def failing_cancel(task_id):
        orders.cancelled.append(task_id)
        raise HTTPException(409, detail="ledger locked")
    orders.cancel = failing_cancel
    orders.results[request["task_id"]] = {"state": "succeeded", "output": "answer"}
    runs.run_once()
    assert runs.get(request["task_id"])["state"] == "succeeded"
    assert orders.acknowledged == [request["task_id"]]

def test_cancel_is_sent_once_and_recorded_when_rejected(tmp_path):
    history, runs, orders, request = setup(tmp_path)
    runs.begin(request); runs.run_once(); runs.cancel(request["task_id"])
    runs.run_once(); runs.run_once()
    assert orders.cancelled == [request["task_id"]]      # delivered once, not per tick
    run = runs.get(request["task_id"])
    assert run["state"] == "running" and run["cancel_requested"] is True
```

Also assert in the first test that `public_run` exposes `cancel_error_code == "cancel_rejected"` (add `cancel_error_code` and `cancel_delivered` to the `public_run` projection).

- [ ] **Step 2: Run** `python -m pytest tests/test_ask_runs.py -q -k cancel` — expected: the first test fails (state stays `running`), the second fails (`cancelled` has two entries).
- [ ] **Step 3: Implement** in `run_once`: replace the single try block with

```python
        cancel_error = None
        try:
            if dispatch:
                commands.submit(run["body"])
            if run["cancel_requested"] and not run.get("cancel_delivered"):
                try:
                    commands.cancel(task_id)
                    self._mark(task_id, cancel_delivered=True)
                except HTTPException as exc:
                    cancel_error = exc
            result = commands.status(task_id)
        except HTTPException as exc:
            ...unchanged...
```

with a small `_mark(task_id, **fields)` helper that updates the run row inside a `file_transaction` (skip the write when the row is already terminal). After computing `result`, if `cancel_error` is set and the run is still non-terminal, persist `cancel_error_code` via `_mark` (safe-code sanitising). Keep the second-transaction message update as is.

- [ ] **Step 4: Run** `python -m pytest tests/test_ask_runs.py tests/test_ask_consumer.py -q` — expected: all pass.
- [ ] **Step 5: Commit** `fix(ask-ryn): cancel failures no longer block result archiving`

### Task 2: Ask Ryn worker writes history only when something changed

**Finding:** `runs.py:187-194` rewrites (encrypt + fsync) the entire history every tick just to bump `last_checked`, and `runs.py:219-225` writes again unconditionally even when `_message` changed nothing. Two full rewrites per second while any run is active, contending with every owner route on the same flock.

**Files:**
- Modify: `rynmesh/ask_ryn/runs.py` (`__init__`, `_message`, `run_once`)
- Test: `tests/test_ask_runs.py`

**Behaviour:**
- `last_checked` lives in memory: `self._last_checked: dict[str, float]`. It is no longer persisted (leave any legacy `last_checked` keys in stored rows alone; ignore them).
- The first transaction in `run_once` writes only when it changed a row (`queued -> dispatching`, or `cancel_requested` flipped because the conversation vanished).
- `_message` returns `True` when it changed the conversation; the second transaction writes only when it returns `True` or the run row changed.
- Poll fairness unchanged: pick `min(pending, key=lambda row: self._last_checked.get(row["task_id"], 0.0))`.

- [ ] **Step 1: Write the failing test:**

```python
def test_idle_running_ticks_do_not_rewrite_history(tmp_path, monkeypatch):
    history, runs, orders, request = setup(tmp_path)
    runs.begin(request)
    runs.run_once()          # queued -> dispatching -> running (writes allowed)
    writes = []
    original = history._write
    monkeypatch.setattr(history, "_write", lambda *args, **kwargs: (writes.append(1), original(*args, **kwargs))[1])
    runs.run_once(); runs.run_once()
    assert writes == []
    assert runs.get(request["task_id"])["state"] == "running"
```

- [ ] **Step 2: Run** it — expected FAIL (`writes` has four entries).
- [ ] **Step 3: Implement** as described. Keep `pending` selection and the `dispatch` logic identical otherwise.
- [ ] **Step 4: Run** `python -m pytest tests/test_ask_runs.py tests/test_ask_consumer.py tests/test_ask_history.py -q` — expected pass.
- [ ] **Step 5: Commit** `perf(ask-ryn): stop rewriting encrypted history on idle poll ticks`

### Task 3: A paid, succeeded answer is never purged before the ask worker archives it

**Finding:** `runs.py:140-144` archives `succeeded` with no `output` as `interrupted`. Output can vanish before the worker polls: setting retention to 0 purges every order's `encrypted_response` (`llm_package/routes.py:1585-1587`); the retention sweep (`task_protocol.py:215`) purges after expiry while the worker is in error backoff; retention=0 results live only in `background_orders` and are pruned after 900 s (`routes.py:2023-2031`).

**Files:**
- Modify: `rynmesh/llm_package/task_protocol.py` (consumer order store: add `mark_acknowledged`, exempt unacknowledged successes from `purge_expired_responses`)
- Modify: `rynmesh/llm_package/routes.py` (`local_llm_privacy_update`, `_prune_background_orders`, `acknowledge_result`)
- Test: `tests/test_ask_consumer.py` (or `tests/test_llm_package.py` where the order-store fixtures live)

**Behaviour:**
- `ConsumerOrderStore.mark_acknowledged(task_id)` persists `acknowledged_at` (ISO timestamp) on the record. `acknowledge_result` in routes calls it (in addition to popping the ephemeral entry).
- `purge_expired_responses` and the retention-0 purge in `local_llm_privacy_update` skip records with `state == "succeeded"`, an `encrypted_response` present, no `acknowledged_at`, and a completion time less than 7 days old. A record older than 7 days is purged regardless (hard bound; document it in the privacy route's docstring).
- `_prune_background_orders` keeps ephemeral `succeeded` entries that are not acknowledged for 86,400 s instead of 900 s; failures and acknowledged successes keep the 900 s cutoff.
- The ask worker's `interrupted` mapping stays (it still applies after a node restart with retention 0, which is inherent to memory-only results, and the message says so).

- [ ] **Step 1: Write failing tests** (use the existing consumer order store fixture pattern in `tests/test_llm_package.py`):
  - `test_retention_zero_keeps_unacknowledged_success_until_ack`: store a succeeded order with an encrypted response, call the privacy update with 0, assert the response is still present; call `mark_acknowledged`, run the purge again, assert it is gone.
  - `test_expiry_sweep_skips_unacknowledged_success_within_bound`: succeeded order with `response_expires_at` in the past and no `acknowledged_at` → `purge_expired_responses()` returns 0 and the response remains; same record with `completed_at` 8 days ago → purged.
  - `test_background_prune_keeps_unacknowledged_ephemeral_success`: an ephemeral succeeded entry recorded 1,000 s ago survives `_prune_background_orders`; an ephemeral failed entry recorded 1,000 s ago is dropped.
- [ ] **Step 2: Run** them — expected FAIL.
- [ ] **Step 3: Implement.** Find the completion timestamp the store already records for terminal transitions (history entries carry timestamps; use the last terminal history entry). Add the exemption helper `_awaiting_archive(record, now)` in `task_protocol.py` and use it in both purge sites.
- [ ] **Step 4: Run** `python -m pytest tests/test_llm_package.py tests/test_ask_consumer.py tests/test_ask_runs.py -q` — expected pass.
- [ ] **Step 5: Commit** `fix(llm): keep unacknowledged succeeded answers until the ask worker archives them`

### Task 4: One rejected sync row no longer stalls a whole scope (receiver side)

**Finding:** `device_sync/store.py:145-157` builds the batch from the first 100 pending rows in key order; `_merge_rows` (206-222) raises on the first bad row and the receiver rejects the whole batch; `transfer.send` records `sync_transfer_unconfirmed` and the identical batch is rebuilt next tick. Nothing skips or quarantines the row. Trigger: restoring `consumption.json` from a `.migrated` backup reissues an actor counter and `records.merge` raises `sync_dot_conflict` (`records.py:224-226`).

**Files:**
- Modify: `rynmesh/device_sync/store.py` (`_merge_rows`, `_pending`, `status`)
- Modify: `rynmesh/device_sync/transfer.py` (`_receipts`, `_receive`, `receive`)
- Test: `tests/test_device_sync_records.py`, `tests/test_device_sync_routes.py`

**Behaviour (wire format, versioned in place):**
- Whole-batch errors stay whole-batch: `sync_scope_denied`, `sync_batch_invalid`, `sync_batch_limit` (protocol violations).
- Per-row merge errors (`SyncError` raised by `records.validate` or `records.merge` for one row: `sync_dot_conflict`, `sync_device_limit`, `sync_value_invalid`, `sync_item_invalid`, `sync_item_link_invalid`) no longer abort the batch. The row is skipped, the other rows merge atomically in the same mutate, and the signed receipt for that row is `{"scope", "id", "revision": <fingerprint of the sent record>, "rejected": <code>}`.
- `_merge_rows(..., collect_receipts=False)` (internal source reconcile) skips such rows too and records them in `data['quarantine'][key] = {"code": code, "revision": fingerprint}`; a later identical import is a no-op; a changed record retries the merge and clears the quarantine entry on success.
- `store.status()` gains `quarantined: [{"scope", "id", "code"}]` (bounded to 100 entries in the projection).

- [ ] **Step 1: Write failing tests:**
  - `test_receive_merges_good_rows_and_rejects_conflicting_row_in_receipt` in `tests/test_device_sync_records.py`: two rows in one batch, the second reuses an actor counter with a different value → first row merged, receipt for the second carries `rejected == "sync_dot_conflict"`, no exception.
  - `test_source_reconcile_quarantines_conflicting_row_and_keeps_other_rows`: `reconcile_source` with one conflicting row → other rows merged, `status()["quarantined"]` lists it, second identical reconcile leaves state unchanged, a corrected row clears the entry.
  - `test_transfer_receive_returns_signed_receipts_with_rejections` in `tests/test_device_sync_routes.py`: through `Transfer.receive`, the ACK receipts include the rejected marker and `_receipts` validation still accepts the payload.
- [ ] **Step 2: Run** — expected FAIL (`SyncError` raised).
- [ ] **Step 3: Implement** in `_merge_rows`:

```python
PER_ROW = {'sync_dot_conflict', 'sync_device_limit', 'sync_value_invalid', 'sync_item_invalid', 'sync_item_link_invalid'}
...
            try:
                merged = previous if same else records.merge(row['scope'], row['id'], previous, row['record'])
            except SyncError as exc:
                if str(exc) not in PER_ROW:
                    raise
                if collect_receipts:
                    receipts.append({'scope': row['scope'], 'id': row['id'], 'revision': records.fingerprint(row['record']), 'rejected': str(exc)})
                else:
                    data.setdefault('quarantine', {})[key] = {'code': str(exc), 'revision': records.fingerprint(row['record'])}
                continue
            data.setdefault('quarantine', {}).pop(key, None)
```

Make `_read`/`_validate` accept the optional `quarantine` section (dict of key → {code, revision}). Update `transfer._receipts` so the receipt comparison tolerates the `rejected` key on the receiver side (the sender-side comparison changes in Task 5).

- [ ] **Step 4: Run** `python -m pytest tests/test_device_sync_*.py -q` — expected pass (Task 5 adjusts the sender comparison; if `_accept_receipt` tests break here, keep them passing by making `_receipts` produce identical output when no row is rejected).
- [ ] **Step 5: Commit** `fix(device-sync): merge good rows and report rejected rows in signed receipts`

### Task 5: Sender records peer-rejected rows and keeps syncing the rest

**Files:**
- Modify: `rynmesh/device_sync/transfer.py` (`_accept_receipt`, `_acknowledge` call site, `_record`, `status`)
- Modify: `rynmesh/device_sync/store.py` (`_acknowledge`: accept receipts with `rejected`)
- Modify: `webapp/src/domain/deviceSync.ts`, `webapp/src/screens/Devices.tsx` (display)
- Test: `tests/test_device_sync_routes.py`, `webapp/src/screens/Devices.test.tsx`

**Behaviour:**
- `_accept_receipt` accepts a receipts list whose entries equal the expected receipts once the optional `rejected` key is removed, and whose `rejected` values are strings from the per-row set.
- `_acknowledge` stores the receipt fingerprint for accepted and rejected rows alike (a rejected row is not re-sent until the local record changes), and records rejected rows under `transfer['rejected'][scope] = {id: code}`; accepted rows remove their entry.
- Device status exposes `rejected_by_peer: {scope: count}` per pair; `Devices.tsx` shows one line per scope with a count when non-zero: "N reading records could not be merged by <device name>. They will be sent again after they change on this device." (no error styling, `role="status"`).
- A restored-backup scenario ends with the other rows delivered and the conflicting row visible in both devices' status.

- [ ] **Step 1: Write failing tests:**
  - `test_rejected_row_is_acknowledged_and_other_rows_keep_flowing` in `tests/test_device_sync_routes.py`: two paired nodes (use the existing two-node fixture in that file), inject a dot conflict on one row, run the transfer worker twice → the second row is confirmed, the conflicting id appears in `rejected_by_peer`, the batch is not re-sent while unchanged, and changing the local record re-sends it.
  - `Devices.test.tsx`: a status fixture with `rejected_by_peer: {reading: 1}` renders the sentence above.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement** per behaviour.
- [ ] **Step 4: Run** `python -m pytest tests/test_device_sync_*.py -q` and `cd webapp && npm test -- --run Devices && npx tsc -b` — expected pass.
- [ ] **Step 5: Commit** `fix(device-sync): acknowledge peer-rejected rows and surface them in device status`

### Task 6: Local reading writes never fail because of sync capture

**Finding:** `rynmesh/services/consumption.py:168` calls `sync.capture()` inline; `ReadingState._write` raises `sync_capacity_exhausted` at 20,000 entities (`reading.py:129-130`) and `sync_item_link_invalid` for non-public links (`records.py:113-120`), so every bookmark or progress save fails once either condition holds. `reading_bridge.py:5` promises local writes never depend on sync.

**Files:**
- Modify: `rynmesh/services/consumption.py` (`record`)
- Modify: `rynmesh/device_sync/reading.py` (add `failures` section + `status` projection)
- Modify: `rynmesh/device_sync/routes.py` (status exposes `capture_failures`)
- Test: `tests/test_device_sync_reading.py`, `tests/test_device_sync_routes.py`

**Behaviour:**
- When `expected_sync_revision is None` (an ordinary local write), a `SyncError` from `capture` is caught: the local record is saved as before, and the failure is stored in the sync state as `failures[key] = {"code": code, "at": unix}` (bounded to 1,000 entries, oldest dropped). The write of the sync state happens in the same document save (it already lives in the source document).
- When `expected_sync_revision is not None` (an explicit conflict review), errors propagate exactly as today.
- A later successful capture for the same key removes its failure entry.
- `status()` reports `capture_failures: {"count": N, "codes": {code: count}}`; the Devices screen shows "N local changes could not be queued for sync (reason). They stay on this device." when N > 0 (Task 5's status line pattern).
- The 20,000-entity cap itself is not changed here (product decision; flagged in the PR description).

- [ ] **Step 1: Write failing tests:**
  - `test_bookmark_persists_locally_when_sync_capacity_is_exhausted`: `monkeypatch.setattr(reading, "MAX_ENTITIES", 1)`, record two bookmarks → both present in `store.read()`, `status()["capture_failures"]["count"] == 1` with code `sync_capacity_exhausted`.
  - `test_progress_persists_locally_for_non_public_link`: item link `file:///tmp/x` → record saved, failure code `sync_item_link_invalid`.
  - `test_explicit_review_still_fails_honestly`: with `expected_sync_revision` set and a wrong revision → `SyncError("sync_revision_conflict")` still raised and nothing saved.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python -m pytest tests/test_device_sync_*.py tests/test_reading_*.py tests/test_consumption*.py -q` — expected pass.
- [ ] **Step 5: Commit** `fix(reading): local reading writes succeed even when sync capture fails`

### Task 7: Friend and content identifiers move out of URLs

**Finding:** `rynmesh/local_search/routes.py:73-74` exposes `GET /api/local/search/open?identifier=…` (identifiers embed peer ids and content ids) and `rynmesh/ai_access/routes.py:51` reads `peer_id` from the query string on `POST /api/local/ai-access/friend-services`. Both end up in access logs, contradicting the module header on line 1 of the search routes.

**Files:**
- Modify: `rynmesh/local_search/routes.py`, `rynmesh/ai_access/routes.py`
- Modify: `webapp/src/domain/localSearch.ts:34`, `webapp/src/domain/aiAccess.ts:31`
- Test: `tests/test_local_search_sources.py` (or the search routes test file), `tests/test_ai_access.py`, `webapp/src/screens/Search.test.tsx`

**Behaviour:**
- `POST /api/local/search/open` with JSON body `{"identifier": str}` (max 512 chars, same validation); `GET` on that path returns 405.
- `POST /api/local/ai-access/friend-services` reads `{"peer_id": str}` from the JSON body; a query-string `peer_id` is ignored and a missing body field returns 400 `ai_request_invalid`.
- Web clients send bodies; no identifier appears in any request URL (assert with the fetch mock in the vitest tests).

- [ ] **Step 1: Write failing tests** (backend: `TestClient` calls asserting the new shapes and the 405/400; web: `expect(fetchMock.mock.calls[0][0]).not.toContain("identifier=")`).
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement** on both sides; update the search routes header comment to say identifiers also travel in bodies.
- [ ] **Step 4: Run** backend and web gates — expected pass.
- [ ] **Step 5: Commit** `fix(privacy): keep peer and content identifiers out of request URLs`

### Task 8: Friend join seals the invite secret to the inviter

**Finding:** `rynmesh/friends/service.py:139-151` posts the raw 32-byte invite secret in the join body over the inviter's plain-HTTP endpoint. A passive observer cannot reuse it (single-use invite, relationship secret sealed to the joiner), but an active on-path attacker who blocks the real join and submits their own becomes the friend under a chosen name.

**Files:**
- Modify: `rynmesh/friends/service.py` (`join`, `accept`)
- Test: `tests/test_friends.py`, `tests/test_friend_pairing_recovery.py`

**Behaviour:**
- `join` sends `"kind": "ryn.friend-join.v2"` and `"invite_secret_box": {"nonce", "ciphertext"}` produced by `peer_box.seal(self.messaging_private, signed.payload["messaging_pub"], secret, info=b"rynmesh-friend-invite-secret-v1")`. The plaintext `invite_secret` field is gone.
- `accept` requires `kind == "ryn.friend-join.v2"` and `invite_secret_box`; it opens the box with `self.messaging_private` and `body["messaging_pub"]` (so the box is bound to the joiner's messaging key that the signed `proof` also covers). Any body carrying a plaintext `invite_secret`, a missing box, or a box that fails to open is `invalid_join`. The secret-hash check and `store.accept_invite` semantics are unchanged.
- A lost-response retry from the same joiner (same body) still returns the same relationship.

- [ ] **Step 1: Write failing tests:**
  - `test_join_body_never_carries_plaintext_invite_secret`: capture the posted body via a stub `post_json`; assert `"invite_secret" not in body` and `body["kind"] == "ryn.friend-join.v2"`.
  - `test_captured_join_cannot_be_replayed_by_another_identity`: take A's captured join body, build C's body with C's peer id, messaging pub and proof but A's `invite_secret_box` → inviter `accept` raises `FriendError("invalid_join")`; then A's original body still accepts.
  - `test_plaintext_invite_secret_is_rejected`: v1-shaped body → `invalid_join`.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python -m pytest tests/test_friend*.py tests/test_mailbox_two_nodes.py -q` and `python scripts/friend_product_e2e.py` if it runs locally without Docker (check its header; skip with a note if it needs the compose stack) — expected pass.
- [ ] **Step 5: Commit** `fix(friends): seal the invite secret to the inviter's messaging key`

### Task 9: Friend revocation is never stuck pending, and manual retries do not spam the mailbox

**Finding:** `friends/service.py:828-836`: when `store.secret(rid)` is `None` at revoke time, the record still gets `revocation_delivery="pending"`, `retry_revocation` returns without changing state, and the worker (`friends/routes.py:117`) selects it forever. `service.py:849`: every manual retry deposits another mailbox message.

**Files:**
- Modify: `rynmesh/friends/service.py` (`revoke`, `retry_revocation`), `rynmesh/friends/store.py` (`revoke`: accept `delivery="undeliverable"`)
- Modify: `webapp/src/screens/Friends.tsx:111` (render the new state)
- Test: `tests/test_friend_pairing_recovery.py`, `tests/test_friend_message_delivery.py`, `webapp/src/screens/Friends.test.tsx`

**Behaviour:**
- `revoke` with `notify=True` and no stored secret commits `status="revoked"`, `revocation_delivery="undeliverable"`, no `revocation_wire`, and is excluded from the worker's selection. `retry_revocation` on it returns `{"delivered": False, "reason": "friend_credentials_unavailable"}` without raising.
- Manual `retry_revocation` deposits a new mailbox message only when there is no `revocation_mailbox_id` yet (same rule as automatic retries); the direct-delivery attempt still runs every call.
- Friends screen copy for `undeliverable`: "Removed on this device. The removal notice could not be sent because this friend's credentials were no longer available; they will see an error on their next request."

- [ ] **Step 1: Write failing tests** for the three behaviours (backend) and the copy (web).
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** friend tests + web gate — expected pass.
- [ ] **Step 5: Commit** `fix(friends): undeliverable revocations are terminal and retries reuse the queued notice`

### Task 10: Device pairing store compacts finished rows; peer rate limiter evicts instead of refusing

**Finding:** `pair_store.py:19,59-60` rejects any mutate once `invites` or `pairs` exceed 256 and nothing ever removes expired, cancelled or rejected rows. `device_sync/routes.py:224-233`: once 256 hosts are tracked, every new host gets 429 until buckets expire; a rotating attacker can lock out new peers.

**Files:**
- Modify: `rynmesh/device_sync/pair_store.py` (`mutate`: compact before validation), `rynmesh/device_sync/pairing.py` (`cancel_invite` relies on compaction)
- Modify: `rynmesh/device_sync/routes.py` (limiter)
- Test: `tests/test_device_sync_pairing.py`, `tests/test_device_sync_routes.py`

**Behaviour:**
- On every `mutate`, before the cap check: drop invites whose status is `cancelled` or whose `expires <= now`; drop pairs with status `rejected`; keep `revoked` pairs but exclude them from the cap and drop the oldest revoked pairs beyond 256 total rows. Active/awaiting rows are never dropped.
- Limiter: per-host 60/min stays. When the table holds 256 hosts and a new host arrives, evict the host whose newest stamp is oldest instead of returning 429.

- [ ] **Step 1: Write failing tests:** `test_expired_and_cancelled_invites_free_capacity` (300 invites with the clock advanced past TTL → no `sync_pairing_capacity_exhausted`; cancel then create at the cap succeeds), `test_new_peer_host_is_admitted_when_table_is_full` (257 hosts, last one gets 200/409 not 429; a host at 60/min still gets 429).
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python -m pytest tests/test_device_sync_*.py -q` — expected pass.
- [ ] **Step 5: Commit** `fix(device-sync): compact finished pairing rows and evict stale limiter hosts`

### Task 11: Offline clear forgets references; temp-file sweep covers pairing state; one oversize row cannot disable search

**Findings:** `offline_reading/cleanup.py:106-110` keeps title/url after a user-initiated clear; `services/reading_cleanup.py:116` sweeps `.tmp` orphans for source/replica/search only, not `device-sync/pairings.json`; `local_search/index.py:104-106` raises for any row with `text` over 8 MiB and `sources.py:200` never clips Ask message bodies, so one huge message fails every query and rebuild closed.

**Files:**
- Modify: `rynmesh/offline_reading/cleanup.py`, `rynmesh/services/reading_cleanup.py`, `rynmesh/local_search/sources.py`, `rynmesh/local_search/index.py`
- Test: `tests/test_offline_cleanup.py`, `tests/test_reading_cleanup.py`, `tests/test_local_search_sources.py`

**Behaviour:**
- After clear, a record's `reference` keeps only `item_id`; `title`, `url` and `source` are removed in the same mutate. The privacy export of offline reading (`privacy_export/sources.py`) tolerates the missing keys.
- `_backups` gains scope `pairing` for `device-sync/pairings.json` (`.tmp` pattern only; no `.migrated` variants), so orphaned temp files are listed and removed like the others.
- `local_search/sources.py` clips every `text` field to 1 MiB (UTF-8 safe truncation) with a `truncated: True` marker on the row; `index.py` skips (does not raise for) any single row that still exceeds the limit or fails validation, counts it in `status()["skipped_rows"]`, and keeps serving queries.

- [ ] **Step 1: Write failing tests** for each behaviour (assert residual keys after clear; a synthetic `.pairings.json.<hex>.tmp` is listed and removed; a 9 MiB message leaves search working with `skipped_rows == 0` because it was clipped, and an injected oversize row at index level yields `skipped_rows == 1`).
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the three test files plus `tests/test_product_export.py` — expected pass.
- [ ] **Step 5: Commit** `fix(cleanup,search): forget cleared references, sweep pairing temp files, clip oversize search rows`

### Task 12: Privacy export allowlists its two nested payloads

**Finding:** `privacy_export/sources.py:38-41` (`first_run.export()`) and `:126-145` (offline article body) are written as-is rather than through `pick`. Any future token/URL field in either shape ships silently.

**Files:**
- Modify: `rynmesh/privacy_export/sources.py`
- Test: `tests/test_product_export.py`

**Behaviour:**
- Read the actual shapes of `first_run.export()` and `offline_reading` `service.read()` and write explicit allowlists: scalar fields via `pick`; for the article body, `blocks`/`text` content plus `images` metadata already filtered, nothing else. Add a `pick_nested(row, spec)` helper in the same module if the body has one level of nested lists of dicts.
- An injected extra key (`"token": "x"`) in either upstream payload does not appear in the export.

- [ ] **Step 1: Write failing tests** that monkeypatch the upstream export/read to include `token` and assert absence in the produced files.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python -m pytest tests/test_product_export.py tests/test_reading_privacy.py -q` — expected pass.
- [ ] **Step 5: Commit** `fix(privacy-export): allowlist first-reading and offline article payloads`

### Task 13: Search share attribution requires a delivered card, not friend-supplied metadata

**Finding:** `local_search/sources.py:607` merges an incoming card into an existing row purely by `(source_url, sha256)` taken from the card; `friends/service.py:499-528` only shape-checks these. Friend A can claim to have shared a document I saved myself or that friend B shared, and search with `friend_id=A` shows an "Open share" target A never delivered.

**Files:**
- Modify: `rynmesh/local_search/sources.py` (card → row merge)
- Test: `tests/test_local_search_sources.py`

**Behaviour:**
- A card contributes a `friend_ids` entry and an "Open share" target only when the card is in a fetched/verified state for that relationship (the state the fetch path records after hash verification), or when the row has no other origin. An unfetched card from A that matches my own document's `(source_url, sha256)` does not attach A to that document.

- [ ] **Step 1: Write the failing test:** own document + unfetched card from A with the same url/hash → query with `friend_id=A` returns nothing; after A's card is fetched and verified → it does.
- [ ] **Step 2: Run** — expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python -m pytest tests/test_local_search_sources.py tests/test_search_source_isolation.py tests/test_friend_content_delivery.py -q` — expected pass.
- [ ] **Step 5: Commit** `fix(search): attribute shares to friends only after their card is verified`

### Task 14: Backend test gaps from the review

**Files:** `tests/test_ask_runs.py`, `tests/test_ai_access.py` (or new `tests/test_ai_access_peer_routes.py`), `tests/test_friend_routes.py`, `tests/test_device_sync_pairing.py`

Add these tests (each must pass against the current code, or expose a bug that gets fixed in the same commit with a note in the commit body):

- Ask Ryn: `status` returning 404 for a run already `running` → run becomes `interrupted` with the interrupted copy; worker tick when the history file is corrupt raises `ConversationError` and a later good file resumes; with three active runs, three ticks check each run once (poll ordering by last check).
- AI access peer route: HTTP test for `POST /api/peer/ai-access/services` with valid friend auth headers → catalogue for the receiver's stored grant; after `revoke` → 403; a header relationship id A with a body relationship id B → rejected.
- Friend routes: a peer request whose headers name relationship A but whose sealed wire names B is rejected for `message`, `content-card` and receipt paths.
- Device pairing: two joiners racing `receive_join` concurrently (threads) → exactly one pair reaches `awaiting_owner`, the other gets `sync_invite_used`.

- [ ] **Step 1: Write each test; run; fix any real bug it exposes (report it in the commit body).**
- [ ] **Step 2: Run** the full backend gate — expected pass.
- [ ] **Step 3: Commit** `test: close review gaps for ask runs, AI access peer route, friend auth binding, pairing race`

---

## Part B — web app

### Task 15: Ask Ryn export keeps the blob alive until the download starts

**Finding:** `webapp/src/screens/AskRyn.tsx:117-122` revokes the object URL synchronously after `anchor.click()`; Firefox resolves blob downloads asynchronously. Every other download path defers revocation (`Settings.tsx:168`, `productExport.ts:38`, `FriendConversation.tsx:76`).

**Files:** `webapp/src/screens/AskRyn.tsx`, test `webapp/src/screens/AskRyn.test.tsx`

- [ ] **Step 1: Failing test:** stub `URL.createObjectURL`/`revokeObjectURL` with vi.fn and fake timers; click Export; assert `revokeObjectURL` not called synchronously and called after `vi.advanceTimersByTime(10_000)`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement:** `window.setTimeout(() => URL.revokeObjectURL(url), 10_000)` in place of the `finally`.
- [ ] **Step 4: Run** `npm test -- --run AskRyn` — pass.
- [ ] **Step 5: Commit** `fix(webapp): defer Ask Ryn export blob revocation`

### Task 16: Live Ask Ryn requests time out and the composer never sticks

**Finding:** `webapp/src/domain/askHistory.ts:73-86` has no timeout; a hung `POST /ask/runs` leaves `sending` true forever (`PrivateAIChat.tsx:270-283`); `stopGeneration` on an unregistered task shows "Cancellation has not been confirmed" with no way out; the 1.5 s history poll (`:128-136`) never clears its error on recovery.

**Files:** `webapp/src/domain/askHistory.ts`, `webapp/src/screens/PrivateAIChat.tsx`, tests `webapp/src/screens/PrivateAIChat.test.tsx`

**Behaviour:**
- `request()` takes an optional `signal` and applies a 30 s `AbortSignal.timeout` by default; a timeout throws `AskRequestError(0, "The node did not confirm this request within 30 seconds. Check the original task before retrying the same reviewed request.", "ask_request_timeout")`.
- On timeout, `submitReviewed` keeps `unconfirmed` set (so retry works), clears `sending`, and shows the message.
- `stopGeneration` receiving `ask_run_not_found` shows "The node has no record of this task, so nothing is running there. The request was not confirmed; you can send it again." and clears `sending`/`activeTaskId`.
- The poll effect clears the error it set (and only that error) on the next successful list.

- [ ] **Step 1: Failing tests** for the three behaviours (fetch mock that never resolves + fake timers; 404 detail `ask_run_not_found`; poll fail then succeed).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `npm test -- --run PrivateAIChat && npx tsc -b` — pass.
- [ ] **Step 5: Commit** `fix(webapp): time out live Ask Ryn requests and recover the composer`

### Task 17: Confirmed operations are not reported as failures when the follow-up reload fails

**Finding:** `Devices.tsx:86-91`, `OfflineReading.tsx:44-54`, `components/ReadingSyncConflicts.tsx:60-69`, `FriendFeed.tsx:62-66` run `await operation(); await load()` in one `try`; a successful destructive action followed by a failed status fetch shows "Could not confirm this operation".

**Files:** the four files above; tests `Devices.test.tsx`, `OfflineReading.test.tsx`, `ReadingSyncConflicts.test.tsx`, `FriendFeed.test.tsx`

**Behaviour:** each wrapper separates the two awaits: mutation failure → error as today; mutation success + reload failure → `notice` "Done on the node. The latest status could not be loaded; refresh to see it." and no error. Extract one helper `runThenReload(operation, reload, { onError, onNotice })` in `webapp/src/domain/actThenReload.ts` and use it in all four.

- [ ] **Step 1: Failing tests** (mutation resolves, reload rejects → notice text present, no `role="alert"`).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** web gate — pass.
- [ ] **Step 5: Commit** `fix(webapp): distinguish reload failures from confirmed operations`

### Task 18: Search keeps loaded results on pagination failure and stops re-querying a stuck index

**Finding:** `Search.tsx:131` sets `page` to null on load-more failure; `:98-102` re-queries every 3 s for as long as the index reports pending/partial.

**Files:** `webapp/src/screens/Search.tsx`, test `Search.test.tsx`

**Behaviour:** load-more failure keeps `page` and shows the error next to the button; automatic re-queries stop after 10 rounds and show "The index is still being built. Refresh to check again." with the existing Refresh control.

- [ ] Steps 1–5 as in the pattern above; commit `fix(webapp): keep search results on load-more failure and bound index re-queries`

### Task 19: Cleanup panels honour the remote-confirmation flag; friend AI refresh keeps the last snapshot; reader can close without saving

**Findings:** `ReviewedCleanupPanel.tsx:69-76`, `ConversationCleanupPanel.tsx:51-53`, `FriendCardCleanupPanel.tsx:28` hardcode "remote devices remain unconfirmed" although `remote_confirmed` exists; `FriendAI.tsx:51-53` drops the last snapshot on a failed refresh; `ContentViewer.tsx:213-215` gates close on a successful progress write with no "close without saving".

**Files:** the five files above (find the FriendAI screen via `grep -rn "aiAccess.refresh" webapp/src`), tests for each

**Behaviour:**
- Cleanup panels render "Remote devices confirmed." when `remote_confirmed` is true and the existing sentence otherwise.
- Friend AI refresh failure keeps the previous snapshot and its timestamp and shows the error beside it.
- When a progress save fails, the reader shows a second button "Close without saving" next to "Retry saving"; it calls `onClose()` directly. Escape and the X keep the current save-first behaviour.

- [ ] Steps 1–5; commit `fix(webapp): show remote confirmation, keep friend AI snapshots, allow closing the reader without saving`

### Task 20: Pairing approval asks the owner to type the verification code

**Finding:** `pairing.py:234` binds approval only to the public `review_token`; the owner sees an attacker-chosen name if someone raced the real joiner. The only real check is the human comparing the verification code shown on the joining device.

**Files:** `webapp/src/screens/Devices.tsx`, `webapp/src/screens/Devices.test.tsx`; backend `rynmesh/device_sync/pairing.py` (`approve` takes `verification_code` and compares to the row's code), `rynmesh/device_sync/routes.py`, `webapp/src/domain/deviceSync.ts`

**Behaviour:** the Approve action opens the existing confirm dialog with an input "Enter the code shown on the other device" and is disabled until the entered code equals the row's `verification_code` (case-insensitive, dashes optional). The backend `approve` requires `verification_code` and rejects mismatches with `sync_verification_code_mismatch`.

- [ ] Steps 1–5 (backend test + web test); commit `fix(device-sync): approval requires the verification code from the joining device`

### Task 21: Web test gaps from the review

**Files:** new/extended vitest files.

Add tests for: Friends remove friend, retry removal notice, cancel invite, save attachment; FriendAI enable sharing; FriendFeed stop sharing; live-mode cancel through `askHistory.cancelRun`; direct tests for `AskAboutButton.tsx`, `AskMaterials.tsx`, `OfflineDownloadButton.tsx`, `ReviewedCleanupPanel.tsx`; the Ask Ryn export path (Task 15 covers it). Each test asserts the request made and the confirmed/failed copy shown.

- [ ] **Step 1: Write tests; fix any real bug they expose in the same commit with a note.**
- [ ] **Step 2: Run** `npm test -- --run && npx tsc -b` — pass.
- [ ] **Step 3: Commit** `test(webapp): cover friend, feed, AI-sharing, cancel and helper components`

---

## Part C — release preparation

### Task 22: Version 0.7.0 and release notes

**Files:** `pyproject.toml`, `webapp/package.json`, `webapp/package-lock.json` (root entry), `webapp/src-tauri/tauri.conf.json`, `webapp/src-tauri/Cargo.toml`, `webapp/src-tauri/Cargo.lock` (package entry), `docs/PRODUCT_MILESTONES.md` ("Current release" section), new `docs/RELEASE_NOTES_0_7_0.md`

- [ ] **Step 1:** bump every version string to `0.7.0`; `grep -rn "0\.6\.2"` must return only historical mentions.
- [ ] **Step 2:** write `docs/RELEASE_NOTES_0_7_0.md`: the eight user features (from `docs/product-briefs/README.md`), the hardening fixes in this plan (one line each, user-facing wording), known limitations copied from the PR #57 description (macOS device acceptance and cross-NAT runs skipped, no V100 acceptance), and the flagged product decisions (20,000-entity sync cap; friend AI catalog reveals `revoked`; revoked friend feed inbox retained until unsubscribe).
- [ ] **Step 3:** update `docs/PRODUCT_MILESTONES.md` current release to v0.7.0 with the feature list.
- [ ] **Step 4:** run `./scripts/build_release.zsh` (read it first; it runs tests, builds the web app into the package, and builds the wheel). Record the wheel path and its checksum in the release notes' verification section. If the script needs tooling not present on this machine, record exactly what failed.
- [ ] **Step 5: Commit** `chore(release): version 0.7.0 and release notes`

---

## Execution notes

- Order: Tasks 1–14 (backend), then 15–21 (web), then 22. Tasks 4 and 5 must run consecutively; Task 20 touches both sides and runs after 17.
- Each task: fresh implementer subagent → spec review → quality review → fix round → next task. After Task 22: whole-branch review, fix wave, then open the PR against `main` with the review findings and evidence.
- The PR must state honestly what was verified locally (backend pytest, ruff, vitest, tsc, production build) and what only upstream CI covers (packaged node, desktop compile, the three e2e jobs).
