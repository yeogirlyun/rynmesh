# Issue #25 test document

Status: completed on Windows 11, 2026-09-02

## Automated coverage

### Handoff and storage

- random 48-hex-character opaque ID;
- deep-cloned value, expiry, one-time consumption, no enumeration API;
- URL/localStorage/sessionStorage unique-marker leakage checks;
- title, body, source URL, and provenance encrypted at rest in the existing
  IndexedDB record;
- no plaintext persistence fallback.

### Prompt and budget

- UTF-8 fixtures: ASCII `4`, Chinese `6`, combining `3`, emoji `4` safety units;
- fixed 128-token margin and output cap included in both UI and node admission;
- deterministic long-context result across repeated runs;
- Unicode-safe truncation, exact counts, and `total <= context_window`;
- fake closing delimiter and instruction-like text remain quoted data;
- source URL is not sent to the Provider;
- too-small Provider disables grounded send.

### Product flow

- Ask action appears after readable node extraction;
- failed and empty reader extraction disable Ask and explain why;
- opaque navigation contains no title/body/source marker;
- grounding card appears before send and can be removed;
- Provider/model switching hides other buckets and restores the original card,
  including when the initial and grounded conversations share a millisecond;
- no Provider leaves the handoff unconsumed;
- expired/already-used handoffs show the reopen-item instruction;
- too-small context disables Send and explains the larger-context/remove choices;
- Provider history storage rejection releases switching and preserves current
  Provider/history/draft;
- request rejection restores the draft;
- compact recently-opened items without evidence detail reopen safely.

## Commands and results

```text
cd webapp && npm run lint
PASS — TypeScript project check

cd webapp && npm test -- --run
PASS — 12 files, 63 tests

cd webapp && npm run build
PASS — 1,741 modules; production bundle emitted

python -m pytest tests/test_llm_context_safety.py \
  tests/test_llm_package.py tests/test_llm_hardening.py \
  tests/test_reader_and_steering.py -q
PASS — 63 tests

ruff check rynmesh/llm_package/context_safety.py \
  rynmesh/llm_package/routes.py tests/test_llm_context_safety.py
PASS
```

## Full backend regression run

`python -m pytest tests -q` was re-run on 2026-09-03 with `PYTHONUTF8=1` and
completed with **521 passed, 3 skipped, 8 failed**. All eight failures are
Windows-host/platform limitations outside Issue #25:

- 3 deployment-artifact tests depend on POSIX executable bits or a working WSL
  `/bin/bash`;
- 3 owner-only `0600` mode assertions are POSIX semantics not represented by
  Windows `st_mode`;
- 1 Signal50 atomic replace/read concurrency test hit a documented Windows
  file-lock race;
- 1 MCP smoke test uses `select.select` on a Windows pipe.

The Issue #25 backend-focused suite and all Webapp tests pass. The platform
failures do not execute the Issue #25 grounded-context implementation and need
the repository's Ubuntu CI for supported-platform closure.

## Real-browser acceptance

Environment:

- isolated Vite server on `127.0.0.1:42525`;
- isolated temporary Consumer node on `127.0.0.1:8791`;
- deterministic reader cache and fixture LLM Provider;
- in-app Chromium browser.

Observed:

1. reader extract rendered with **Ask about this item**;
2. click opened a new encrypted Private AI conversation;
3. final URL contained only `peer`, `service`, and `network`—no handoff, title,
   source host, or unique article marker;
4. normal fixture: `Full article context fits: 207 characters`;
5. long multilingual fixture: `1021 of 13907 characters (1/2 blocks)` before
   send;
6. grounded order completed through the local fixture node;
7. Remove changed the visible article-card count from one to zero;
8. returning to the compact recently-opened item produced no new console error.

Screenshots were captured in the acceptance session at the reader action,
full-context card, and long-context truncation card.

The 2026-09-03 independent audit found that those historical screenshots were
not committed to this branch. A browser re-run reached the Webapp but could not
recreate the reader flow because the isolated Consumer expected at
`127.0.0.1:8791` was not running. This does not replace the recorded historical
browser observations; it is an explicit evidence-artifact gap for reviewers who
require a reproducible fresh browser package.
