# Ryn Agent Memory Natural Architecture (v2)

This document describes the current implementation. It is not an acceptance report or a record of an installed desktop deployment.

## Opt-in and capture eligibility

With `capture_enabled=true` and `activation_policy="automatic"`, the owner authorizes background extraction and consolidation for the configured exact model destination. Ordinary stable facts and habits do not need a separate “remember” or “update” command, manual entry, or candidate acceptance for each statement. `use_enabled` separately controls whether saved memory may be disclosed during inference. The `owner_review` policy remains available: background proposals remain candidates until an actual owner decision accepts them. Explicit owner memory commands also remain supported.

`MemoryExtractionWorker._collect_and_claim` collects owner messages from eligible completed tasks. `_is_task_eligible_for_capture` excludes archived/deleted tasks and tasks with in-flight work, queued operations, pending owner turns, approvals, input, or control changes. Project bindings must still be valid. Credential messages, empty sources, and explicit memory commands are excluded from this natural-background collection path. Source fences and job idempotence govern subsequent eligibility.

When automatic capture is enabled or its consent changes, existing owner messages are stamped before `automatic_capture_frontier_seq` is set. Ordinary automatic collection skips messages at or below that frontier; it does not silently reinterpret the existing conversation archive as newly authorized material. Explicit relearning follows its separate source and restore rules.

## Two zero-tool model stages

The registered worker calls the native Bridge with `tool_specs=[]` for both stages and reads the normal `response["message"]["content"]` envelope. Tool calls in either background response are rejected. Nonempty automatic observations need an actual second model call; a failed second stage does not fall back to deterministic activation of Stage-1 text. A valid empty `observations=[]` may finish as a no-op after one charged call.

1. `build_phase1_source_analysis_messages` supplies verified owner messages, with quoted/fenced content removed, to the source observation extractor. It also supplies bounded `known_memory_slots` selected for the same scope and authorized destination. Those handles contain scope/category/key/kind, not existing fact values or source excerpts. They help the model classify a paraphrase into an established slot; they do not replace the model's language judgment with a list of camera or query phrases.
2. `parse_phase1_source_analysis_response` and `verify_natural_observation_grounding` validate the returned observations. Required fields include `kind`, `scope`, `subject`, `stability="durable"`, `ownership_mode`, `fact_key`, `text`, source message IDs, and a nonempty verbatim `evidence_span` grounded in every cited owner message. Kinds are `owner_fact`, `owner_preference`, `project_fact`, and `reusable_lesson`. Ownership modes distinguish `owned_or_habitual` from `replaced_or_sold`. Project observations cannot become personal facts. Invalid observations reject the batch with a specific reason; a rejected batch is distinct from a valid empty result.
3. `select_existing_memories_for_consolidation` selects current active memories whose scope, sources, project generation, and exposure permit the exact Stage-2 destination. `build_phase2_consolidation_messages` sends that permitted snapshot and the validated observations to the consolidator.
4. `parse_phase2_consolidation_response` validates `add`, `update`, `retract`, and `noop` operations. `_validate_single_consolidation_op` binds each operation's `observation_ids` to validated witnesses. An operation cannot invent another value, slot, source, scope, or evidence span. Update/retract targets and `expected_entry_revision` must match the dispatched snapshot and the witness's scope and slot. A retired-body witness cannot authorize changing an unrelated workflow. A same-slot replacement uses the new owned observation to update the existing slot.

The Stage-1 prompt instructs the model to exclude third-party ownership, temporary rentals, hypotheticals, quoted material, secrets, and one-off data. Host grounding checks enforce source authority and evidence boundaries; model interpretation of ordinary language remains a separate quality concern.

Each observation describes one independently changeable property. Keys identify stable roles rather than the current value, model name, or date; a matching known slot is reused for a replacement. Canonical text must not repeat changing equipment or software values in an additional activity/background summary. Current task motivation is not automatically an enduring condition or preference. Stage 2 treats source-grounded observations as candidates, not as semantically approved facts: it may omit transient, inferred, redundant, or composite candidates and keep a smaller useful set.

## Truthful foreground behavior

`current_automatic_memory_capture_note` reads current host settings and, only while automatic capture is enabled, adds a developer policy note through `AgentLoop._context`. Ordinary owner statements and natural corrections need no special remember/update wording. They are considered by the background pipeline after task completion; the foreground should answer using the latest owner information without calling the separate explicit-operation tool for incidental statements.

The policy note does not establish that a write has completed. A genuine successful host operation result is still required for a positive save claim. Rejection by the explicit foreground tool does not mean background capture is disabled. This note contains capability state, no saved facts, and remains accurate when use is off but capture is on. Owner text cannot enable the host policy. The owner-review and capture-off cases do not receive the automatic policy note.

## Storage, sources, and commit ordering

Memory remains inside the real encrypted `AgentStore`, using the existing `data["memory"]` dictionaries for entries, candidates, jobs, tombstones, and source fences. There is no parallel plaintext memory store. `commit_natural_consolidation_job` commits automatic operations transactionally through the existing memory helpers.

`stamp_single_owner_message_in_tx` and `stamp_task_owner_messages_in_tx` assign `owner_source_seq` when owner messages are accepted, including create, append, edit, and input-answer paths. The job preserves immutable `task_message` references with message identity, content hash, conversation/context revisions, project binding, and source sequence. Dispatch and commit revalidate these sources.

The commit path checks the real lease owner/token/expiration, reset epoch, project generation, the permitted snapshot, and the current entry revision. It compares each operation's trusted source order with the entry's `last_source_seq` and `owner_acceptance_seq`; an older late job cannot overwrite a newer natural correction or explicit owner mutation. Automatic updates preserve an existing entry's exposure policy instead of obtaining a wider grant. New automatic entries derive exposure through `_derive_new_explicit_entry_exposure_policy`, with incomplete authorization falling back to `local_only`.

`_matching_tombstones_for_slot` matches scope and slot HMACs without mutating the HMAC salt on reads. Owner deletion suppresses background relearning until an authorized restore. `natural_retraction` uses slot-scoped fencing so retiring one fact need not invalidate a surviving sibling fact from the same source. Read projections still validate each surviving source and omit revoked facts.

## Short factual profile and on-demand detail

In automatic mode, `select_memories_for_task` leaves the eager full-entry `selected` list empty and builds a bounded `profile_summary` plus `navigation_index` from the verified, authorized current view. `build_navigation_and_summary` may include actual current values in the short profile; it is not restricted to slot counts. It currently reserves at most 512 bytes, within one third of the available projection budget, for up to four complete short facts. Facts longer than 180 UTF-8 bytes are left for detail retrieval rather than truncated into a partial claim. The navigation index contains up to eight references with ID, scope, category, key, topic, kind, and revision.

`format_memory_context_messages` presents this data as lower-priority context. The model may answer directly from a sufficient factual profile. When more detail is useful, `ryn_memory_search` can read permitted active entries using actual index IDs/keys or a query/topic. Tool output includes current ID, text, scope, and revision; it is not a promise to expose a complete source transcript or timeline. Search retains lexical/structural relevance behavior; automatic discovery does not require the user's new question to repeat a device model or a fixed trigger phrase.

A small global profile may be present during an unrelated task; the model should ignore unrelated facts. Raw source excerpts/history and whole entry payloads are not disguised as the profile. Scope, destination, source validity, and budgets apply before projection and detail reads. Turning use off, deleting a fact, or revoking authorization also invalidates affected derived checkpoints/provider context through the memory integration and disclosure watermark paths.

## Worker bounds and accounting

`install_agent` registers `MEMORY_WORKER_NAME="ryn-agent-memory"` with `MemoryExtractionWorker.run_once` and `BackoffPolicy.fixed(1.0)`. The 1.0-second value is a scheduler backoff/poll setting, not a promise that a conversation will be learned within one second. Eligibility, queueing, model availability, runtime load, budgets, and model execution affect completion time.

Both automatic stages share a bounded attempt deadline and the same genuine lease. `_attempt_reason`, `_live_attempt_reason`, and `_mark_dispatch` check current capture consent, activation policy, exact destination tuple, source state, project binding, lease, and deadline before disclosure and on completion. A matching model name alone cannot renew a revoked grant.

Current defaults allow 180 seconds for the entire two-stage attempt and a 240-second lease. The lease does not extend the shared request deadline. Cancellation polling uses `_stored_attempt_reason`, a local state check, and never starts CLI discovery. Dispatch, second-stage preparation, and commit still verify the exact live destination. Missing/unavailable catalog metadata may trigger one fresh recheck within the same deadline and live consent/source guards; a verifiable identity mismatch is rejected immediately. No stale cached binding is accepted as fallback authority. Antigravity model inventory processes have a bounded 30-second discovery limit.

Automatic accounting reserves each stage's input estimate plus bounded output before dispatch. `_reserve_stage`, `_mark_dispatch`, and `_settle_stage` retain a per-lease/per-stage ledger in `dispatch_usage`; each reservation records its UTC day. Dispatched calls are charged and settled once, including failure/cancellation paths; an undispatched reservation is released. A stage crossing a UTC day boundary is not charged again to the new day's ledger. The automatic worker calls `commit_natural_consolidation_job(..., usage_accounted=True)` so commit does not duplicate those charges. Daily call/token budgets can prevent Stage 2, in which case Stage 1 is not silently activated.

`owner_review` retains its candidate/owner-decision path and existing accounting integration. Its presence is a supported mode, not a claim of complete backward compatibility or completed regression acceptance.
