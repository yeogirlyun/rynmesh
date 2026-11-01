# Twelve follow-up PRs: integrated acceptance, 2026-09-18

This is local integration evidence, not an upstream merge, release, or a claim that external physical acceptance has finished.

## Candidate and automated checks

The candidate combines PRs #60, #62, #64, #66, #67, #71, #72, #77, #78, #79, #80 and #69. Production code was tested at dce6cf9. Subsequent integration 0f54ab3 adds only the cleanup test's pair-ID assertion fix and merges the same Settings focus-wait assertion already present from #71. That last merge required retaining one explanatory comment; no production conflict or behavior changed.

- Windows, Python 3.12: 1626 passed, 29 skipped, 4 warnings in 368.64 seconds. Skips are not counted as passes. The suite includes platform-specific POSIX native-runtime/document tests and optional dependency checks; this run did not collect a detailed skip-reason report.
- Node 22.22.1: 398 frontend tests passed across 63 files; TypeScript and production build passed.
- Ruff and `git diff --check` passed.
- After Linux CI exposed an order-dependent cleanup assertion, 8 cleanup coordinator/HTTP tests passed with targets addressed by pair ID, including explicit confirmation that the other offline device remains unconfirmed.
- CI also exposed the existing Settings focus-effect race. Waiting for focus preserves the keyboard Tab/Enter recovery assertion. Five relevant UI tests passed after that branch fix.

## Installed package

Built a wheel from a clean git archive of dce6cf9 with the compiled web UI included, then installed it into an isolated target directory. Asserted the running module came from that installed directory.

Wheel: `rynmesh-0.7.0-py3-none-any.whl`; SHA-256 `e8368c5b95a06d0d7f2b0d579354c2bdf240d027b0820d9fa5f12bc8e5a3d9a9`.

Three real uvicorn HTTP servers used fresh temporary homes (Alpha, Beta, Gamma) on one Windows host. Alpha paired with Beta/Gamma as owned devices. Alpha and Beta also established a separate friend relationship. No user stores were used. Only the relevant cleanup, shared-list and offline workers were manually run; ordinary application lifespan/discovery was disabled. This is not a full startup-worker or physical cross-NAT test.

## Observed product flows

1. **Friend offline and Ask (#78).** Beta followed Alpha's synthetic publication. Save offline displayed queued status; Offline reading then showed a verified 129-byte body. Read offline copy rendered the synthetic text plus serving-friend and recorded-publisher explanations. Ask about this navigated to the same imported material in Ask Ryn, with source review and no inference automatically started. The integrated For You card displayed both actions. The browser pass did not disconnect the source or run a model.
2. **Weekly recap (#79).** For You showed one received publication under Friends shared this week, UTC week start 2026-09-14, private-local-only wording and current-access caveat. The same publication appeared in the main friend ranking.
3. **Shared lists (#80).** Beta explicitly discovered Alpha's invitation, reviewed and joined Weekend reading. Beta added a synthetic link, saw pending count 1 then 0, and marked it read. Alpha saw Friend: Read while its own status remained Unread. Alpha added a second link; Beta displayed both acknowledged entries with separate read states. The page layout was inspected by screenshot.
4. **Sync health (#77).** Alpha's table displayed Beta/Gamma separately, missing ACKs as Awaiting acknowledgement / Not recorded, and separate pending/rejected columns. Beta showed two pending reading changes. Historical/missing ACKs were not described as current online or complete bidirectional sync.
5. **Owned-device cleanup (#69).** Alpha's browser proposed whole-category reading cleanup to Beta and Gamma. Receiving Beta showed owner review with 1 local item, 2 source entities, 2 replica entities and 2 backup files. The synthetic acceptance script submitted the exact locally previewed token through the owner API; this step was not a browser destructive-confirmation click. With only Beta complete, Alpha's browser still showed aggregate completion unconfirmed and Gamma awaiting owner review. Gamma approved an empty local plan through its owner API. Only after both bound receipts arrived did Alpha's browser show all originally selected devices confirmed. No real user data was erased.

## Real native-model fault and process-restart follow-up

Used the same installed wheel and existing local GGUF (491400032 bytes) through llama.cpp b10774, one CPU thread, 4096 context, no GPU offload, explicit friend AI permission and direct streaming. Native runtime access was restricted to authenticated loopback. Fresh synthetic stores and prompts were used; bodies and credentials are not reproduced here. No production code changed for these checks.

- **Active SSE reconnect:** received sequence 0, disconnected the local subscriber, and resumed with sequence 1 while the same Ask run was still `running`. Explicit cancellation reached `cancelled`; ledger events were exactly `hold, release`. Repeating begin with the same task returned that terminal state.
- **In-flight permission revocation:** after a native delta, the durable grant was revoked. The task terminated as `failed / inference_failed` rather than a successful partial answer, with `hold, release` and failed assistant status. This does not claim immediate CPU termination or a more specific revocation error message.
- **Native model process exit:** terminated the llama-server process after a delta. The task failed with `runtime_connection_failed`, released its hold and did not restart from the same task ID. Restarting the model and explicitly submitting a new short request succeeded with one `hold, settle` pair.
- **Provider process exit:** two separate node subprocesses used real HTTP. Terminating the provider after a delta produced `failed / direct_transport_failed`, `hold, release`; restarting the provider did not rerun the original task. A new explicit request succeeded.
- **Consumer process exit:** terminating the consumer after a delta and restarting it from the same home recovered `failed / consumer_restarted_before_completion`. Exactly two conversation messages remained, the hold was released, and resubmitting the same task ID returned the original failure. A new explicit request subsequently succeeded.
- **Successful history survives restart:** after that successful request, terminated and restarted the consumer again. Saved messages were identical, the six-character assistant answer remained, and the ledger still contained exactly `hold, settle`.
- **Plaintext scans:** 28 JSON files in the runtime/revocation fixture and 29 in the subprocess fixture contained no planted prompt marker. These scans do not claim an OS-wide memory/crash-dump audit.

Failures during acceptance are retained: an immediate recovery request in an earlier subprocess pass was rejected with `capacity_exhausted` and no ledger events. After observing provider capacity return, a separate explicit request succeeded. A fixed five-second wait in an earlier harness pass was insufficient and was replaced with an observed-capacity check; no failed task was automatically resubmitted. Harness construction also initially used the wrong friend-list/message field names and subscribed before the worker created the order; those fixture errors were corrected. One early provider-exit assertion was inconclusive because it did not capture the resulting state; it is not counted as a passing case. Final process-run output completed successfully. The fixtures manually drive the Ask worker and disable ordinary lifespan/discovery, so they are not full background-scheduler startup tests.

The SSE recovery here is an HTTP subscriber test. The earlier real-model browser pass verified generation, cancellation and final history after reload; it did not observe a still-running browser preview after reload. The deterministic installed-browser test covers that UI timing. This distinction remains explicit.

## Scope and remaining acceptance

The cleanup implementation coordinates six existing whole-category cleanup plans. Receiving owners approve their own local records, including records absent from the initiator. It is not selective cross-device record matching. Existing source-only receipts keep their old meaning. System/user-managed backups, exports, independently saved friend copies, new generations and shared-reading-list copies are excluded.

Supported desktop installation and operating-system deep-link launch, distinct physical/public-network devices, GPU behavior, and all operating-system/route combinations are not proven by this report. Maintainer review, upstream merge, release and closing issues that the contributor cannot close remain outstanding. No PR has been merged by this acceptance work.
