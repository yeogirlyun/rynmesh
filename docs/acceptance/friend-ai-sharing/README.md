# Friend AI sharing — independent FAI ledger

Baseline: `1cc8352fcbb106db07511ed2d2794c509ff01f00`. This ledger does not modify the preceding 95 cases.

## Step 1 — existing friends (2026-09-15)

Windows Chrome, two independent node homes, production built UI and HTTP, A port 19041 / B 19042 / CPU runtime 19043. Only the existing model manifest was imported into A, with a fresh runtime key and explicit zero pricing; model and executable are read-only references to previous assets. B has no GGUF or API key setup. No relationships or answers were seeded.

Browser sequence: A started the imported Qwen2.5-0.5B package and ran self-test; opened **Share my AI**; ordinary friend invitation was reviewed and accepted by B with AI explicitly off. A enabled sharing and reviewed the named B, package and actual zero price, then authorized using the confirmation dialog keyboard. B refreshed friend AI and followed its exact Ask link. Chat continuously identifies A.

The first zero-price remote request failed. Reproduction found the provider rejected every remote zero hold, even for zero-priced models. Fixed only that guard and tested zero vs paid pricing and revoked permission. The original failed task remains in history; checking it did not resend. A second, explicitly submitted question returned **15** from the actual CPU model. No fake answer or browser injection was used.

Evidence: [page](step1-browser.txt), [production HTTP state](step1-http.json). Targeted checks: FriendAI component (6), PrivateAIChat (6), AI permission store/provider (8 baseline plus new zero-price case); TypeScript and Vite build. Counts are check summaries, not an acceptance inference.

| Case | Status | Evidence / remaining work |
|---|---|---|
| FAI01 | Passed locally | Real Start runtime and self-test; ready Qwen package in step1 HTTP. |
| FAI02 | Passed locally | Named grant then exact friend/service Ask navigation in actual pages. |
| FAI03 | Passed locally | Step 2 actual review → acceptance → exact Ask navigation, real answer 13. |
| FAI04 | Passed locally | Browser answer 15, succeeded real task, zero B GGUF files. |
| FAI05 | Pending | Ordinary Windows installer and continuation next. |
| FAI06 | Passed in fault tests | Real signatures/stores; simulated expiry, cancellation, replay, other identity; no extra grant. |
| FAI07 | Passed in fault tests | Lost response, partial writes, reconstructed coordinator, concurrent cancellation and revision fences. Fault injection is explicit. |
| FAI08 | Pending | Targeted recovery verification next. |
| FAI09 | Passed in mixed scope | Actual page revoke, real signed HTTP denials for B/C, old operation refused; running-cancel/settlement branch uses simulated engine regression. |
| FAI10 | In progress | Stale share target blocked; invitation and old-chat checks next. |
| FAI11 | Pending | Final two-node restart check next. |
| FAI12 | In progress | Confirmation and sending used keyboard; full route still to check. |

Limits: same Windows host, loopback. No macOS, distinct public egress, cross-network evidence or recruited external user trial. These are not claimed as passed.

## Step 2 — AI invitations (2026-09-15)

A creates a signed, one-person AI invitation from the sharing panel. A fresh B at 19044 has no prior friendship, model or key configuration. The actual review page checks A's signature and fetches current service details before displaying provider/model/zero price/recipient. Keyboard acceptance commits the relationship and revision-1 grant and confirms private discovery before opening A/local-small Ask. No sample question is auto-sent. B explicitly asks `What is 6 plus 7? Reply with only the number.` and receives **13** from the real CPU model.

Evidence: [review page](step2-review.txt), [answer page](step2-answer.txt), [HTTP/store results](step2-http.json). The complete consumer operation no longer retains the invitation capability. A's page subsequently revokes new B. [Signed HTTP denials](step2-denials.json) show both revoked B and a never-authorized C rejected by Provider; resuming the old completed invitation also refuses access.

Automated evidence is the named cases in `tests/test_friend_ai_sharing.py` (13 targeted scenarios), plus three AI review/navigation/recovery scenarios in `webapp/src/screens/Friends.test.tsx`. They use real identity signatures, production stores and the real coordinator, with clearly simulated transport/disk failures and engine cancellation. The new tests cover ordinary invites not granting AI, one-use binding, lost response after provider writes, unfinished grant persistence, owner cancel/revoke/remove barriers, progress-write loss, cancel-write loss, model fingerprint/price changes, expired/not-ready/busy/paused states, discovery loss, local abandonment, completed-operation price re-review, and accept/cancel serialization. Existing affected friends and permission regressions pass.

Implementation note: an actual page check found the internal provider snapshot lacked `publication_enabled` even though owner status showed it; this was corrected before acceptance. The coordinator uses the authoritative persisted provider sharing setting. The invitation ID is the operation ID; its first grant always expects revision 0. Cancellation first persists a revocation fence and then changes invitation status, so interruption between those writes cannot restore permission.
