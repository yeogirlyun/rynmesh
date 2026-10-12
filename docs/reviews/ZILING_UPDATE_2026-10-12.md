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
