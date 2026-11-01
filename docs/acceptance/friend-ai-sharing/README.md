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
| FAI05 | Passed on Windows development host | Real initial uninstall/install, pre-install invitation → copy/paste → review → answer 20. Final installer upgraded and answered 15. Protocol activation separately restored the invitation. See Step 3; no clean VM claim. |
| FAI06 | Passed in fault tests | Real signatures/stores; simulated expiry, cancellation, replay, other identity; no extra grant. |
| FAI07 | Passed in fault tests | Lost response, partial writes, reconstructed coordinator, concurrent cancellation and revision fences. Fault injection is explicit. |
| FAI08 | Passed in mixed scope | Actual offline/address-change recovery; explicit offline/not-ready/busy context and UI tests preserve input, focus error and resume the same service. Busy/disk branches are simulated. |
| FAI09 | Passed in mixed scope | Actual page revoke, real signed HTTP denials for B/C, old operation refused; running-cancel/settlement branch uses simulated engine regression. |
| FAI10 | Passed in mixed scope | Signed invitation re-review tests, actual send price review, model/price changes refused before dispatch and at Provider; old history remains fixed to its provider/service. |
| FAI11 | Passed locally | Actual A/B restarts, original grant revision 1 and two revoked revision-2 grants retained; history 20 readable and next real answer 15. Actual DHCP address change recovered through reviewed ordinary invite. |
| FAI12 | Passed in mixed scope | Actual keyboard sharing confirmation, invitation review, send confirmation and named revoke. Component tests cover error/review focus and keyboard recovery. No screen-reader or full accessibility audit claim. |

Limits: same Windows host, loopback and LAN-address traffic. No macOS, clean VM, distinct public egress, cross-network evidence or recruited external user trial. These are not claimed as passed.

## Step 2 — AI invitations (2026-09-15)

A creates a signed, one-person AI invitation from the sharing panel. A fresh B at 19044 has no prior friendship, model or key configuration. The actual review page checks A's signature and fetches current service details before displaying provider/model/zero price/recipient. Keyboard acceptance commits the relationship and revision-1 grant and confirms private discovery before opening A/local-small Ask. No sample question is auto-sent. B explicitly asks `What is 6 plus 7? Reply with only the number.` and receives **13** from the real CPU model.

Evidence: [review page](step2-review.txt), [answer page](step2-answer.txt), [HTTP/store results](step2-http.json). The complete consumer operation no longer retains the invitation capability. A's page subsequently revokes new B. [Signed HTTP denials](step2-denials.json) show both revoked B and a never-authorized C rejected by Provider; resuming the old completed invitation also refuses access.

Automated evidence is the named cases in `tests/test_friend_ai_sharing.py` (13 targeted scenarios), plus three AI review/navigation/recovery scenarios in `webapp/src/screens/Friends.test.tsx`. They use real identity signatures, production stores and the real coordinator, with clearly simulated transport/disk failures and engine cancellation. The new tests cover ordinary invites not granting AI, one-use binding, lost response after provider writes, unfinished grant persistence, owner cancel/revoke/remove barriers, progress-write loss, cancel-write loss, model fingerprint/price changes, expired/not-ready/busy/paused states, discovery loss, local abandonment, completed-operation price re-review, and accept/cancel serialization. Existing affected friends and permission regressions pass.

Implementation note: an actual page check found the internal provider snapshot lacked `publication_enabled` even though owner status showed it; this was corrected before acceptance. The coordinator uses the authoritative persisted provider sharing setting. The invitation ID is the operation ID; its first grant always expects revision 0. Cancellation first persists a revocation fence and then changes invitation status, so interruption between those writes cannot restore permission.

## Step 3 — Windows candidate and final regression (2026-09-15)

Implementation commits are `5e2b54f`, `95979b9`, and `1686f4e8487e4900c1019c519eca6b17548485f0` on `codex/friend-ai-sharing-implementation`. The original checkouts and the preceding 95-case ledger were not changed. Final build came from the third commit with `tracked_changes: false`.

Final artifact: `release/windows/Ryn-Setup.exe`, **22,914,892 bytes**, SHA-256
`86A0E3FFB47FBE62EC972DDC83AD6987B62E926DC473FCFC7FA785A6EF533B7E`.
The candidate is unsigned and includes the Python runtime/UI, not a model. Its
ordinary per-user installer was executed successfully (exit 0), creates shortcuts,
registers `rynmesh://`, and retains its own download copy. Inno was obtained from
the official vendor release and had a valid Pyrsys B.V. signature. The candidate
itself has no code-signing claim. Actual host: Windows build 28120 x64; Windows 10
and macOS were not separately tested.

The first installation acceptance verified no installed Ryn executable or default
Ryn data directory, received the invitation while uninstalled, retained it through
installation, and pasted/reviewed it in the installed application. It returned
**20** for `12 + 8`. No Python command, model, API key, peer ID or endpoint was
entered by the consumer. Installation used the real Inno silent install path;
the ordinary wizard was inspected, but not every wizard branch was clicked.
Clicking the browser download link did not yield an observed local browser
download file; the actual public download endpoint was therefore verified by
HTTP and its full SHA-256, not claimed as a completed browser download.

Evidence: [uninstalled invitation page](step3-uninstalled-page.txt),
[restored review](step3-restored-review.txt), [installed answer 20](step3-installed-answer.txt),
[OS protocol restoration](step3-protocol-restored.txt), [keyboard revoke](step3-keyboard-revoke.txt).
OS protocol activation used the registered Windows handler and restored the
invitation into the running node, focused its input, and waited for explicit
review. Cancelling review cleared continuation without changing the existing
grant. The protocol was verified through Windows Shell activation; browser-specific
permission prompts and protocol blocking remain covered by the copy/paste fallback.

After the overnight pause, DHCP changed this host's LAN address from `.187` to
`.189`. The original consumer stayed bound to the original peer and showed it as
unavailable. This exposed a real recovery gap. The final candidate offers
**Update this friend's address** when reviewing a new ordinary invitation from
an existing identity. It verifies the candidate address using the original
authenticated, encrypted friendship before updating only the address. An active
grant stays revision 1; a revoked grant stays revoked. No invitation is consumed,
new relationship created, question sent or history moved. Tests also reject an
unreachable destination and removal during the response. [Actual review](step3-address-review.txt).

Final real regression caught two more issues: the adapter's advertised chat
capability differed from its stored manifest and caused a false model-review
mismatch; a rejected Provider order also remained waiting in Ask. The review
now binds model identity, version, context limits and price; chat-format support
continues to be checked separately. `rejected` is a terminal Ask run. The original
rejected request remains visibly failed and was never automatically resubmitted.
Only after a new explicit review did the consumer send again and receive **15**
for `7 + 8` from the actual CPU model.

Both nodes were restarted. The final installed consumer retains the original
conversation ID, answer 20, the rejected record, and answer 15. A retains one
allowed revision-1 grant and two revoked revision-2 grants. Repeated real signed
HTTP requests from revoked B and unapproved C are rejected. Model restart/self-test
can pause publication; the provider explicitly re-enabled sharing during recovery.
No automatic model-start or public-network reachability claim is made.

Final evidence: [send review](step3-final-send-review.txt), [answer page](step3-final-answer.txt),
[screenshot](step3-final-answer.png), [HTTP, hashes, grants and history](step3-final-http.json),
[post-restart denials](step3-restart-denials.json). Invitation capabilities and
runtime secrets are omitted from committed evidence. The consumer has zero GGUF
files. The downloaded and installed-copy installer hashes match the delivered artifact.

Relevant backend suite: **86 cases** across Ask context/runs, friends, AI sharing,
permission store/provider/catalog and continuation. Relevant frontend suites:
**39 cases** across Friends, FriendAI, PrivateAIChat and AskRyn. TypeScript/Vite and
modified Python Ruff checks passed. The existing bundle-size advisory and upstream
Starlette TestClient deprecation warning remain. Fault tests explicitly simulate
transport/disk errors and busy engines; they are not presented as physical network
or hardware failures. The final two rejection-regression cases were run with the
affected 53-case backend subset after the preceding 84-case suite.

The proposed external trial (three providers, one or two friends each, week-one
return-use observations) has not been run. No conversion/retention numbers are invented.
