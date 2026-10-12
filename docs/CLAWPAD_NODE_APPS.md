# Clawpad apps on Rynmesh — first local migration

Owner-authorized on 2026-10-12: review `../clawpad`, select relevant apps and provide them through Rynmesh or a physical node such as MS-2. Implementation and verification run on the owner's local workstation; no cloud coding or orchestration.

The first set is Resume (resume/cover-letter preparation), Recruit (jobs and candidate pipelines) and Desk (service requests/tickets). The local Clawpad catalog has 126 entries. Its generic engines, sample-data seeding, automation recipes and catalogue claims are not acceptance evidence. The original Clawpad checkout is left untouched.

The migration uses Clawpad Resume's bounded pure business methods and selected Recruit/Desk manifest workflows. Rynmesh owns encrypted persistence, authenticated friend transport, per-consumer workspaces, explicit sharing, revision-bound mutation previews and durable idempotent receipts. New workspaces are empty. Each provider authorizes an app separately for an existing friend. Grant changes invalidate old approvals. Remote calls can access only the caller's workspace; provider-local data and other consumers' data are excluded.

Manual UI writes require an exact reviewed preview. Agent app actions use the same business boundary and always require explicit approval, including private reads and automatic mode. Provider inference never runs app tools. No scheduler, polling worker, automatic sharing or new currency issuance is added. Hosted app usage is free in this first version; app records do not pretend to be Ryncoin payments. Paid work continues through reviewed Exchange v2 orders and encrypted textual delivery.

Acceptance: real Resume drafting/export, job/candidate transitions, ticket/comment/closure workflows; disposable workspace views leave business state unchanged; reviewed reads retain audit receipts; ciphertext-only persistence; owner/peer authentication; separate consumers; revocation and stale preview rejection; lost-response retries without duplicate writes; bounded storage; agent approval before mutation; fresh package and real HTTP checks. Physical MS-2 staging uses a separate node home, identity and port, preserving existing services.

This branch builds on the still-open integrated-workspace PR #103 at `c55b9df3dd0a361e2c7a4444a86ba49cac3e47ef`. It is not yet part of merged upstream main. It also fixes the independently reproduced Antigravity missing-usage accounting bug before using that agent integration.

## Selection and source provenance

| App | Local source review | Decision |
| --- | --- | --- |
| Resume | 1,618-line custom engine; real sections, contacts, cover letters and export | Port 27 reachable pure methods; expose 13 bounded operations and text export |
| Recruit | Thin wrapper around shared generic CRUD; 42 catalog operations | Adapt nine concrete job/candidate operations; no automated screening or claims of hiring analytics |
| Desk | 225-line domain layer with automatic seeded tickets | Adapt six ticket/comment/resolution operations; start empty |
| Folio/PDF Editor | Larger native/file/export surface and shared dependencies | Later: scoped document operations with upload/resource contracts |
| Vault | Wiki overlaps existing Rynmesh knowledge/content infrastructure | Later: explicit shared knowledge-workspace design |
| Studio | Thin generic engine despite 42 advertised operations | Media organization candidate; actual video generation/rendering needs real provider adapters |
| Invoice integrations | Existing account/portal-specific adapters | Later: separate Ryncoin quote/invoice model; do not import fiat accounting or account credentials |

Source revision: Clawpad `1bf21d840`; manifests retain source SHA256 in the app catalog and the Resume module records its original file SHA256. The owner authorized this migration. No original database, vault, credentials, logs, demos, Qt host, scripts or scheduler is copied. Resume filesystem/PDF-export handlers are excluded; exports return text for explicit download or Exchange delivery. Recruit/Desk contracts and selected workflows are adapted to Rynmesh, rather than claim every original catalog operation works.

## App boundary and recovery

There are 28 app operations. Workspaces are bounded to 128 KiB; creation collections and ticket comments are bounded to 64 entries. The store allows 96 workspaces/grants and 128 durable operations/intents per workspace/consumer store. This is a small-pilot limit, not an unlimited hosted platform. Unknown consumer operations reserve 256 KiB before dispatch. Disposable user-facing reads do not change business state; reviewed app operations retain encrypted audit receipts.

A remote preview fetches only the caller's revision/grant context; draft arguments stay on the consuming node until confirmation. The provider recomputes the exact approved hash before execution. A revoked/re-paired friendship does not revive old app grants. Signed encrypted requests bind the verified relationship. A lost response leaves the original encrypted intent pending; explicit resume uses the same provider, parameters and operation ID. It cannot silently create a replacement. Uncertainty blocks new operations only in the affected provider/app workspace; unrelated workspaces remain available. A closed/unavailable provider can leave the original outcome unknown, and cannot be promised automatically recoverable.

Agent `ryn_list_node_apps` exposes public capabilities. `ryn_node_app_action` uses the same app boundary and always requires explicit approval, including private reads and automatic mode. Its preview identifies the selected model receiving the result. Read-only agent mode can inspect capabilities but cannot release private app data. Provider inference does not execute tools. Mechanical checks verify original receipts and unchanged current revisions; they do not assert semantic goal completion.

## Local operation

Integrated Rynmesh exposes `/apps`, `/api/local/node-apps`, previews, grants, pending intents and resume, plus `/api/peer/node-apps`. All local APIs use the existing owner gate. The standalone `rynmesh-apps` host uses the same business module and friend pairing, requires owner authentication for remote control and browser cross-site protection, and starts no model, registry or polling workers.

```sh
rynmesh-apps --home /path/to/separate/node-home \
  --name "MS-2 Apps" --host 0.0.0.0 --port 8795 \
  --endpoint http://172.30.1.9:8795
```

The address above is the observed MS-2 LAN address on 2026-10-12; recheck addresses before a later launch. `MS-2.local` SSH remains pinned to the owner's existing direct-link configuration. Do not change firewall settings, stop other services, reset the ordinary `.rynmesh` home or reuse an existing process/port. Only explicitly selected friends receive the selected app grants. There is no installer publication or auto-start registration in this migration.

Local branch: `codex/clawpad-node-apps` at `/Users/yeogirlyun/.codex/worktrees/clawpad-node-apps/rynmesh`. The review-only #103 checkout is `/Users/yeogirlyun/.codex/worktrees/ziling-pr103-review/rynmesh`; the owner's main checkout at `/Volumes/ExternalSSD/rynmesh` remains unchanged. Keep any future handoff in this local repository.

A write preview also binds the complete client/catalog workspace context. Preparing a preview does not silently rebase a stale page or agent observation onto newer records. The agent obtains this non-content context from the app capability listing. Any intervening state or grant change requires refresh and a new review.

Final local evidence: 573 focused backend regressions and 664 frontend tests pass on the final implementation (frontend runtime Node 22.23.2). The focused suite includes all 27 new app/adapter tests. Headless two-node HTTP browser checks cover all three workflows, pre-write review, reload and Chinese UI, with no page errors, no model calls, no global input and no plaintext in app storage. See `docs/acceptance/node-apps-20261012/`. The wider Mac backend run is not green; its recorded limitations and baseline reproduction are in the Ziling review. Do not describe this as a full-backend or independent security audit.


## Physical pilot and privacy

The installed source is commit `916724512c02994453b807a36eb0b5cd192fcea0`. All 250 Python modules were hash-verified on both physical hosts. Actual MS-1 → MS-2 pairing, grants, all three workflows and exact retry without duplication passed. A temporary acceptance identity was revoked and its listener stopped. The owner's workspaces remain empty; test records belong to the separate revoked identity and remain encrypted audit evidence.

Open `http://127.0.0.1:8796/apps` on MS-1 and select MS-2 Apps as provider. MS-2 grants the three apps only to the new MS-1 pilot identity. This is a private, bounded first deployment, not public marketplace discovery. The full Ryn assistant integration exists and is tested on this branch; these standalone pilot hosts run only the apps and friend transport. They run neither the assistant nor Exchange settlement. Job budgets are Ryncoin quote records, not debits, balances or payments.

A hosted workspace is processed and retained by the chosen provider node. Transport and storage are encrypted and consumers are isolated, but the provider operator has the keys; this does not hide hosted records from that operator. Draft arguments stay on the consumer until approval. An assistant using app results additionally sends those results to the selected model, identified in its approval preview.

See [the local handoff](NODE_APPS_LOCAL_HANDOFF.md) for exact installed homes, authentication, access and recovery boundaries. No autostart, scheduled monitoring, public release, push or main merge was performed.
