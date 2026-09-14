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
| FAI03 | Pending | AI invitation and durable coordinator next. |
| FAI04 | Passed locally | Browser answer 15, succeeded real task, zero B GGUF files. |
| FAI05 | Pending | Ordinary Windows installer and continuation next. |
| FAI06 | Pending | New AI invite failure paths next. |
| FAI07 | Pending | Durable coordinator/restart fault checks next. |
| FAI08 | Pending | Targeted recovery verification next. |
| FAI09 | In progress | Existing provider revocation/stranger/cancel tests pass; new flow still needs verification. |
| FAI10 | In progress | Stale share target blocked; invitation and old-chat checks next. |
| FAI11 | Pending | Final two-node restart check next. |
| FAI12 | In progress | Confirmation and sending used keyboard; full route still to check. |

Limits: same Windows host, loopback. No macOS, distinct public egress, cross-network evidence or recruited external user trial. These are not claimed as passed.
