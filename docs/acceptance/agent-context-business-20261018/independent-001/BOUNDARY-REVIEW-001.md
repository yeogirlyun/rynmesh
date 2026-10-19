> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`ee0a37e9362e552f76ca627c5283fe53a90c8ea3d2f6baa50758bb855bc5ea35`。

# Independent boundary review 001

Reviewer scope: production transport limits and Agent/source context controls only. Read-only production inspection; synthetic local adapters, temporary credentials/keys and source stores only; no real owner settings, user conversation reads, external inference, deployment or commits. This fragment does not sign the final business acceptance or the full production freeze; the parent reviewer must compare the recorded hashes with the final snapshot.

## Results

| Item | Result | Evidence |
| --- | --- | --- |
| Shared finite request ceiling | PASS | `rynmesh/llm_package/safety.py:18` defines 16,777,216 bytes. `chat.py:161`, `api.py:214`, `routes.py:454` and `routes.py:2828` use it. JSON validation in chat/routes uses `ensure_ascii=False` and UTF-8 bytes. The API and middleware count actual request bytes. |
| ASCII/Chinese requests above old 1 MiB | PASS | Read and independently reran `tests/test_model_context_transport.py` through the real FastAPI inference installer and authentication path. ASCII 1,150,000 characters and Chinese 1,200,000 UTF-8 bytes reach the scripted adapter unmodified. Existing test plus inference API suite: **33 passed** in 24.07 seconds. |
| Exact raw limit and one byte above | PASS | New independent probe authenticates with a fresh temporary key, rejects an invalid bearer with 401 before adapter dispatch, accepts a 1,200,083-byte Chinese JSON request and that request padded with JSON whitespace to exactly **16,777,216 bytes**, then rejects a chunked **16,777,217-byte** stream with 413 and no extra adapter call. This distinguishes raw transport capacity from normalized prompt size. |
| Encrypted P2P capacity and u16 framing | PASS | `p2p.py:56-61` uses 32 MiB / 900-byte chunks. Independent probe seals/signs a full **16,777,216-byte** synthetic plaintext request using the production task protocol, serializes its **22,370,159-byte** encrypted envelope, emits **24,856 actual frames**, decodes/reassembles, verifies signature and decrypts to the original body. Separately `_encode_frames` accepts exactly **33,554,432 bytes** / **37,283 chunks**, packs the last header, and rejects one byte above. 37,283 fits u16. This is local frame/protocol execution, not a real UDP/network performance test. |
| Agent auto/manual and invalid manual save | PASS | `RynAgent.tsx:835-840,846-860`: null selects auto, integers select manual; `Number.isInteger` and 4,000..2,000,000 validate UTF-8 byte limit, invalid input disables save. Only an explicit changed value is sent. Focused original UI tests: **2 passed / 69 skipped**; manual 3,000 disables save, valid 250,000 saves, then explicit auto sends null. Unknown policy renders unconfirmed-capacity text. |
| Source auto/manual, old number retained, unknown warning | PASS | New source factory defaults explicitly to auto. Independent UI tests with an old source lacking mode preserve numeric **60,000**, leave auto unchecked, and save without fabricating a mode. Explicitly checking auto disables the numeric field, shows the unknown/conservative-fallback warning, and sends `context_window_mode=auto`. **2 independent UI tests passed**. Existing source/domain/model-parameter suite: **15 passed**. |
| Source-level invalid manual values blocked by UI | FAIL if this contract is intended to cover source-level controls too | `AISources.tsx:82-89,144,165` only sets input min/max; the save button uses `onClick` without form constraint validation or an explicit integer/range guard. Independent UI probes actually open the source dialog and click Save with **12** and **10000.5**. Each captures one PUT; a contract requiring no invalid save dispatch fails in **2 cases**. |

Source-level invalid handling is a bounded, existing deficiency: `sources.py:107-110` rejects 12 with `ValueError("上下文或输出上限不正确")`; `routes.py:2144-2145` maps that to HTTP 400; the page's `perform` catch shows the server error. The same backend converts **10000.5 to 10000** via `int`, confirmed in a separate synthetic `SourceStore.prepare` execution. The UI tests mock the response and establish invalid request dispatch, not persistence on the real HTTP API. The 12 rejection is a direct backend execution plus route inspection, not an independently executed full HTTP route. No authorization bypass or large-context admission bypass was established. Both the numeric truncation and absence of a source-level form/save guard also exist in Git HEAD; do not label them newly introduced regressions. The new Agent settings validation passes its stated contract independently.

## Executed evidence

All files below are in this `independent-001` directory:

| Final evidence | Outcome | SHA-256 |
| --- | --- | --- |
| `boundary-pytest.log` / `boundary-pytest.xml` | 33 passed | log `d3563466dfa7ae7a0d0f8d3e80657f8dcfaef43ee34d32c6ecce04b9ffecb90b` |
| `boundary-independent-final.log` / `.xml` | 4 passed, 3.12 seconds | log `66ff06c0bd49a8ed15b935edfd5dbb6bb27bc86812af04de227cc2b12813802b`; XML `03f1f493ac6099ce69b738084a8bf9cdd630248df473ee1640a4c306b8c99b76` |
| `boundary-ui-existing.log` | 15 passed | `8a0b0fac20628dda05040ed32fe3fd42180ffad3bfa5f469e9f1eb1834596c25` |
| `boundary-agent-ui.log` | 2 passed, 69 skipped | `521750d2f12b76ddf7438dbe435ec0e9778ff0f706f4a91354a4ec0d1aa319fe` |
| `boundary-ui-independent-005.log` | 2 passed, 2 contract-probe failures described above | `71bde57654dda65201beac1fc7a0af227da181ab882753e38c47ed76fe6f2594` |

Final independent backend/protocol test source is `test_boundary_independent.py`; independent UI source is `boundary-ui-probe.test.tsx`; its config stays in the evidence directory. Earlier UI setup attempts (`boundary-ui-independent.log`, `-002`, `-003`, `-004`) were harness/config errors, including missing JSDOM `showModal`/`close` stubs. They are retained and are **not production failure evidence**. The final `-005` run contains the proper stubs, two passing control cases and two actual save-request failures.

Across these distinct final invocations: **56 passing test cases and 2 failing source-level UI contract probes**. No full-repository test suite was run. Both pytest runs used fresh, short, previously nonexistent `D:\tmp\ir1*` basetemp paths; no preexisting directory was reused or cleared.

## Snapshot and limits

`boundary-production-test-hashes.json` records the exact byte length and SHA-256 of 12 reviewed production files and 6 original test files. Key production hashes:

| File | SHA-256 |
| --- | --- |
| `rynmesh/llm_package/safety.py` | `386531dcaa9c186b1351a8566fe7b3eb64122376f5c25aa250603112c3b4f671` |
| `rynmesh/llm_package/chat.py` | `8f37a9f699450071b3919252bb33cae44864e4c249fb17b4e650c4723e08db40` |
| `rynmesh/llm_package/api.py` | `b33aede66b6b397d9e346a8b18a2d79b11fe88e096c591c24d9156aacc7fc690` |
| `rynmesh/llm_package/routes.py` | `330322edcdbc4f7f1c4f918c24784f83d1780b071ad99de42be8fcd62f3be293` |
| `rynmesh/llm_package/p2p.py` | `516d0f3ea6c362d3d997b29d7b6d9d32c024948387e5c3c0518ddb50447e9daf` |
| `rynmesh/llm_package/sources.py` | `3ab150b40205d84db91edab45b93775a688e8509573d82ad534c0522d73d8b6b` |
| `webapp/src/screens/RynAgent.tsx` | `38bbb91045e24b08ccdbd375085e63f52c3b13de62c414cdc73c122691143360` |
| `webapp/src/screens/AISources.tsx` | `e43256c90148bb4d7419371b1a17c56f631e13952837617f9b98ef2238b90899` |

The scripted adapter's usage values are synthetic. These tests establish transport acceptance/rejection, authentication and unchanged messages, not a paid 1M-token model invocation, exact tokenizer capacity, real provider token usage or throughput. HTTP peer ciphertext middleware permits 4 * MAX_REQUEST = 64 MiB for encrypted envelopes; this is distinct from the 32 MiB P2P frame ceiling and the 16 MiB decrypted request ceiling. Unknown-model safety, exact CLI capacity freshness and business file quality are outside this fragment and remain for the other independent reviewers.
