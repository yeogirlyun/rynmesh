# Ryn Agent Memory v1 Design

## 1. Encrypted Storage Layout
- Agent memory state lives inside the encrypted `AgentStore` root payload (`<node_home>/agent/sessions.json`) under `data["memory"]`, while per-task transcripts and execution state remain in encrypted blobs (`agent/tasks/<hashed_blob_name>.json`). The default node home is `~/.rynmesh`, overridden by `RYNMESH_HOME`.
- All owner memory mutations, task lifecycle fences, branch rewrites, and background worker claims/commits execute inside `AgentStore.transaction(...)` against the single encrypted root payload.
- Manual guidance (`data["preferences"]` via `/api/local/agent/preferences`) remains a separate structure with optional per-preference destination exposure overrides in `data["memory"]["manual_preference_exposure_overrides"]`.

## 2. Provenance, Optimistic Concurrency (CAS), and Idempotency
- The base is `/api/local/agent/memory`. Owner mutation endpoints (`PUT /settings`, `POST /entries`, `PUT /entries/{id}`, `DELETE /entries/{id}`, `POST /candidates/{id}/decision`, `POST /reset`) require `expected_generation` and a nonempty `client_request_id` of at most 128 characters (clients generate UUIDs). Updating/deleting an existing entry or deciding a candidate additionally requires its revision.
- Idempotent replays are checked first by `client_request_id` and operation payload fingerprint before dynamic catalog or project validation:
  - Identical payload replay returns the current canonical flat snapshot (`list_memories_snapshot(data)`) without re-applying mutations, advancing `generation`, or re-authorizing historical catalog revisions.
  - Reusing the same `client_request_id` with a different payload raises `409 agent_request_conflict`.
- Provenance (`origin`, `owner_id="local_owner"`, verified `sources` with message SHA-256 and `conversation_revision`) is constructed exclusively by the server.

## 3. Independent Use and Capture Controls
- `use_enabled` (default `true`) and `capture_enabled` (default `false`) are independent controls.
- Every non-replay request setting `capture_enabled=true` must explicitly supply a non-null, currently available `extraction_destination` verified against the live `ModelCatalog`. Its exact tuple is `catalog_model_id`, `provider_peer_id`, `source_id`, and `destination_revision`. Native local services may have an empty `source_id`; it is an exact value, not a wildcard. Omitting the destination fails closed with `400 agent_memory_destination_consent_required`. `extraction_model_id` is derived by the server, not a writable API field.
- Turning `capture_enabled=false` immediately marks queued/leased/extracted background jobs `stale` and cancels in-flight extraction without mutating active entries.

## 4. Explicit Owner Commands vs. Candidate Review
- During authentic owner turns, `analyze_owner_memory_intent` and `execute_explicit_owner_intent_in_tx` handle explicit remember/update/forget commands in the same host transaction. Remember/update writes active facts with `origin="explicit_owner_chat"` and source message identity/hash; forget removes matching facts with tombstones and relearning fences. The use switch controls retrieval, not an owner's explicit request to write or delete.
- The `ryn_memory_propose` model tool can apply only operations grounded in the current authentic owner's explicit intent; assistant/tool/web text cannot authorize a new fact or an extra deletion. Ordinary chat is eligible only for opt-in background extraction into bounded `pending` candidates. Owner decisions (`accept`, `edit_and_accept`, `reject`) control activation. Invalid or fenced extraction output is rejected rather than converted into facts.
- When the current authentic owner message matches `row["memory_last_receipt"]` (`message_id` and `conversation_revision`) with `intent` in `{"remember", "update", "forget"}`, `AgentLoop._context` projects a bounded `role="developer"` note containing `host_memory_operation_result` (`{intent, applied, saved_count, deleted_count, reason}` with safe counts and closed reason codes only, never fact text or IDs) so the model truthfully confirms the host-recorded outcome even when business `permission_mode == "read_only"`.

## 5. Completed-Conversation Background Worker Timing
- `MemoryExtractionWorker` (`ryn-agent-memory`) is registered on `BackgroundWorkerHost` and runs bounded single-flight ticks.
- Automatic collection and job claiming run only for tasks with `status == "completed"`, non-archived, non-deleted, and without active `in_flight`, `queue`, `pending_control`, `pending_user_messages`, `pending_approval`, or `pending_input`.
- Incomplete or resumed tasks are skipped during collection and job claiming so other completed tasks are never starved behind an ineligible job. Task eligibility, destination binding, lease token, reset epoch, project generation, and source fences are re-verified before dispatch, during in-flight cancellation checks, and at commit time.
- Background extraction has a bounded 90-second default request allowance (`DEFAULT_EXTRACTION_TIMEOUT_SECONDS = 90.0`) and 120-second lease (`DEFAULT_WORKER_LEASE_SECONDS = 120.0`), retaining the 512 output-token cap (`DEFAULT_EXTRACTION_MAX_OUTPUT_TOKENS = 512`) and existing daily quotas and cancellation checks; this allows slower supported models without unbounded waits.

## 6. Scope, Destination Exposure, and Revocation
- Scope is either `personal` (`owner_id="local_owner"`) or `project` (bound to verified `project_id` and `project_generation`). Project-scoped memories never leak across projects or across project generation resets.
- Entry exposure modes are `local_only`, `destinations` (an explicit list of exact tuples), and `all_selected` (explicit owner choice). The distinct default-settings modes are `destination`, `selected_models`, and `restricted`: newly created explicit-owner chat facts derive `destinations` from `settings.default_exposure` when at least one complete destination tuple is configured and fail closed to `local_only` when `restricted` or when `allowed_destinations` is empty, whereas updating an existing active `(scope, fact_key)` slot preserves its prior `scope` and `exposure_policy`. Retrieval filters scope and verified destination before relevance and budget selection; `ryn_memory_search` stays bound to the current task.
- Host-authored context projection marks `ContextMessages.disclosed_memory` only when selected memory or destination-restricted preferences survive final context budget trimming. `Runtime._complete` binds the destination for both the current disclosure and an already disclosed epoch. Existing provider conversation keys gain a `:mem:<epoch>` suffix; original conversation-edit suffixes remain intact.
- Any memory deletion, correction, scope/exposure narrowing, `use_enabled=false`, reset, branch edit (`Conversation.rewrite`), or task archive/delete triggers `propagate_memory_revocation_in_tx`, rotating `context_epoch` and `memory_conversation_epoch`, dropping derived `context_checkpoint` and prior assistant-derived context while preserving original owner history.

## 7. Retrieval, Limits, and Verification
- The host injects relevant confirmed facts as low-priority assistant data; current task instructions and manual preferences retain priority. Optional memory yields to the full request context budget. Defaults cap active entries at 128, candidates at 64, memory state at 256 KiB, and one injection at 6 KiB / 2048 estimated tokens.
- `GET` on the base returns the flat owner snapshot; `/destinations` lists verified tuples; `/search` is the owner management search. Every route enforces the existing local-owner control and returns `Cache-Control: no-store`.
- Independent offline behavioral tests are delivered in `tests/memory_acceptance`. Run `python -m pytest tests/test_agent_memory_store.py tests/test_agent_memory_runtime_helpers.py tests/memory_acceptance --basetemp=work/mem-test`. Use a short test home on Windows. Real CLI/browser evidence and exact results are recorded separately in the delivery report; this design file does not assert those unexecuted checks passed.
- Deleting long-term memory does not erase original chat history or copies already sent to a model. Native adapter isolation remains unchanged; Ryn owns the memory registry. This change does not replace the installed desktop application.

Documentation was checked against shipped implementation symbols and corrected by Codex after Gemini's implementation draft.
