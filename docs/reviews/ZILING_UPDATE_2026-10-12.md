# Review of Ziling's update — 2026-10-12

Reviewed the owner's screenshot, pasted feature list, local Git objects and PR #103 at `c55b9df3dd0a361e2c7a4444a86ba49cac3e47ef`. PR #92 is merged; #103 is open, with nine existing CI checks passed and independent review still required. The owner's primary checkout remains unchanged during review.

The architecture is consistent with Rynmesh: chosen provider performs inference; consumer owns tools, permissions and task records. Exchange v2 implementation and tests are unchanged relative to upstream main. Independently ran 522 focused local tests across agent approvals, recovery, sharing, resources, model policies and Exchange: all passed.

A synthetic local reproduction found a budget defect in the new Antigravity adapter: a successful response without usage becomes input/output zero. The actual bridge and budget settlement then release all 232 reserved tokens and report usage complete. Preserve unknown counters instead of manufacturing zeros, including streaming. No real provider was called.

Documentation corrections: distinguish merged #92 from proposed #103; describe the optional allowlisted automatic-write mode rather than promise every write has individual approval; qualify CLI hard output limits, the single managed GGUF model, storage/provider restrictions, historical physical observations and actual tools. Video production and arbitrary Clawpad app access are not in the current assistant tool set. Existing green CI is regression evidence, not independent security, model-neutrality or live-account acceptance.

The screenshot's statement about referring to leaked Claude Code is a provenance question, not proof of copied code. The reviewed source search found no Zcode attribution or copied-code record. Request an explicit origin/license inventory and confirm that any implementation uses permitted source before approving it upstream; architectural inspiration and reused source must be distinguished.

No message, GitHub review/comment, merge, cloud task or recurring monitor was initiated for this review.

Follow-up local verification: Node 22.23.2 passes all 664 frontend tests after the new apps are integrated. Node 25's global localStorage behavior produced unrelated browser-storage failures, so it is not the acceptance runtime.

The broader Mac backend run was stopped after 48 failures, 670 passes and 21 skips (739 completed cases, not a full-suite pass). The first permission-world failure reproduces on the untouched #103 head: its intentionally non-listening localhost port produces a real timeout on this workstation, while `query_cases.py` requires `ConnectionRefusedError`. Removing proxy variables does not change it; the environment has no configured proxy. No firewall/security setting or fixture assertion was weakened to obtain a pass. Other failed frozen/reference-controller cases still require individual baseline/platform triage; they cannot all be claimed to share this cause. Linux CI records and the author's Windows record do not establish Mac full-backend acceptance.

The independently reproduced Antigravity budget bug is fixed on the separate app-integration branch, with tests for missing, malformed, negative, boolean, explicit-zero and partial usage, plus buffered streaming.


## Feature-list assessment

The pasted list is broadly supported by code on #103, but it should be presented as a proposed integrated build with explicit limits, rather than all features already merged and accepted on every platform.

| Claimed area | Repo evidence | Review qualification |
| --- | --- | --- |
| Assistant entry, task lifecycle, restart recovery | `agent_runtime/runtime.py`, `control.py`, `lifecycle.py`; `webapp/src/screens/RynAgent.tsx` | Implemented task state and controls; focused approval/recovery tests passed. This is bounded tool execution, not permission to do arbitrary actions. |
| Operation approvals and permissions | `agent_runtime/operation_permissions.py`, `operation_ledger.py`, `resource_writes.py` | Reviews bind object, scope and current permission. Existing allowlisted automatic-write mode must be disclosed. New node-app actions require explicit approval even in that mode. |
| Files, text/PDF, search and artifacts | `agent_runtime/resource_documents.py`, `resource_writes.py`, `resources.py`; `local_search/` | Scoped resource handling exists. Do not imply unrestricted disk access or support for every binary document. |
| Model/device/source administration; unified pool and settings | `agent_runtime/tools.py`, `llm_package/model_settings.py`, `sources.py`; `ModelPool*.tsx`, `RynAgentModel.tsx` | Concrete management tools and per-model controls exist. Unknown CLI usage must remain unknown; hard token/output control depends on the adapter. The independently reproduced accounting defect is fixed locally. |
| Sharing and personal/friend authorization | `agent_runtime/sharing.py`, `llm_package/space_catalog.py`, `friends/`; `ModelPoolSharing.tsx` | Explicit grants and revocation are real contracts. Existing focus tests are evidence; they are not a guarantee of all external provider/account combinations. |
| Unified AI chat, encrypted history, NAS/article context and references | `ask_ryn/`, `llm_package/chat.py`; `AIWorkspace*.tsx`, `AskRynConversation.tsx` | Repo implements chat/context/storage flows. Documents should distinguish current supported sources, legacy history and live-account verification. |
| Cloud drives, caching and NAS | `cloud_drives.py`, `drive_cache.py`, `nas.py`; `personal/Drives.tsx`, `Nas.tsx` and cache tests | OAuth/provider adapters and scoped file operations exist. Provider-specific limitations and actual account testing must be stated. No new owner accounts or live uploads were used in this review. |
| P2P inference and local GGUF | `llm_package/p2p.py`, `p2p_session.py`, `gguf_import.py`, `runtime_native.py` | Transport/session and managed-model paths exist. Qualify platform/connectivity evidence and the single managed GGUF limitation; do not promise every NAT or concurrent model setup. |
| Personal space, device topology, desktop and languages | `personal_space.py`; `personal/DeviceTopology.tsx`, desktop bridge, localization tests | UI and device-management implementation exists. Headless browser/frontend acceptance does not establish every native tray, startup or browser-login behavior on Mac. |
| Exchange integration | `exchange/` implementation and tests unchanged from upstream main | Existing v2 alpha is preserved. Fixed-roster availability and unproven AI neutrality remain the protocol's limits. Neither this review nor the app migration adds real-currency conversion or new coin issuance. |

Before upstream approval: include the local Antigravity accounting fix, complete individual Mac failure triage, and supply the origin/license inventory prompted by the screenshot. Update the feature document's merged/proposed status and limitations. These are review/fix requirements, not permission to start deferred product extensions.

Clawpad integration is now concrete on the separate local branch: Resume, Recruit and Desk, 28 bounded operations, explicit friend sharing and private per-consumer workspaces. The installed MS-2 pilot serves those three apps; public job discovery, consultant matching, arbitrary app installation and video generation remain future work. See [migration details](../CLAWPAD_NODE_APPS.md). Final local focused regressions passed 573 backend and 664 frontend tests, plus physical MS-1/MS-2 app acceptance; the wider Mac suite limitation above still applies.
