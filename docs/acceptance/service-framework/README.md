# Service experience framework (#26)

The API sketch was posted before implementation:
https://github.com/yeogirlyun/rynmesh/issues/26#issuecomment-5707084242

## Acceptance (2026-09-17)

1. Build the webapp, include it in a wheel, install that wheel into an isolated
   environment, and start a fresh node. Open its served `/services` page without
   Vite. With no model or renderer advertised, both cards report no provider.
2. Open `/services?client=fixture` for deterministic UI acceptance. Enter Video
   rendering from the catalog. Submit a synthetic project and confirm the recorded
   request, disabled duplicate-submission action and Check progress control.
3. Return to the catalog and open Secure web access. Connect, inspect the route
   status, and explicitly disconnect. Confirm that the view returns to ready.
4. Run the component suite for Private AI multi-conversation, cancellation and
   recovery; management setup/lifecycle, service selection and asynchronous orders;
   catalog navigation; video submission ambiguity and original-order tracking.
5. Run the hook contract suite with delayed reads, identity/client replacement,
   duplicate refreshes, failed reads, pending writes, terminal states and unmount.

Installed-wheel browser steps 1–3 passed on Windows / Node 22. Screenshots:
[video order](video-order.png), [secure web](secure-web.png). They use synthetic
fixture data and contain no prompts or model output. Fixture UI execution does not
prove a real paid renderer, secure egress provider, or physical P3 acceptance.
The final back-navigation query preservation is covered by source/build review;
the screenshots show the same service state layout before that URL-only adjustment.

Full frontend regression and TypeScript/build results are recorded in the PR.
No Python protocol changed. CI additionally exercises the packaged node and existing
backend/transport suites. The work preserves node-owned Ask history, final result
settlement and explicit cancellation; it does not claim durable video-order recovery
after a browser restart. See the architecture document for that remaining boundary.
