# Dashboard update and local service recovery

## Incident

During the weekly-report API work, the Dashboard plugin package was rebuilt and enabled. The builder copied the company workspace peer-provider declaration into Dashboard as well. Both enabled plugins then claimed the same resource. Peer-provider synchronization failed closed with `peer_provider_configuration_unavailable`. The combined `/api/local/llm/services` handler swallowed the discovery exception and returned an empty array, hiding independent local services.

This was caused by the update in this task, not deletion of the user's model sources. Read-only checks found two stored API sources (three models) and two online CLI services. Removing the duplicate Dashboard peer-provider declaration restored five online local services without modifying model records. Provider synchronization had invalidated company shared-service sessions; fresh supported login is required, and credentials must not be restored by bypassing those checks.

## Repair and validation

- Company package: only the workspace package declares company peer sharing. Dashboard retains the registered `ryn-desktop` OAuth client and `finance.read` scope. Regression test builds both packages and verifies that boundary.
- Main node: catch `PeerProviderError` only around external-provider catalog collection. Withhold that failed provider's records while retaining local sources and other catalogs. Authentication and dispatch still enforce their existing authority checks.
- Main verification: all 12 external-model-access tests pass, including a broken-provider regression proving local services remain and external records are withheld. An initial run passed assertions but hit a pre-existing Windows pytest temporary-directory cleanup permission error; a run with an isolated workspace temporary directory completed successfully.
- Live recovery before the node update: local service endpoint returned five online services, peer-provider configuration endpoint recovered, and model source records remained intact.

The PPT task was paused during the corrective update. Report data and the original/previous PPT were retained. Full PPT acceptance is still pending supported login restoration, remaining queries, editable export, and all-page visual comparison. Do not treat the previous PPT as the accepted new deliverable.
