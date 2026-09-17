# Device sync health (#73)

The overview shows one row per owned-device pairing: outgoing pending acknowledgements, receiver rejections and the last recorded acknowledgement. Rows link to existing pairing, scope and retry controls. A historical acknowledgement never asserts current connectivity or incoming replication completeness.

Unknown counters stay unknown; truncated rejection counters are lower bounds. Local capture and quarantine counts remain explicitly node-wide. Poll failures or a status snapshot older than 15 seconds suppress success labels; a successful reload clears only the refresh error, preserving unrelated operation errors.

Validation on Node 22.22.1: 335 frontend tests passed (53 files), including failure/hanging-poll recovery, paused/no-scope/conflict/unavailable/revoked cases, diagnostic truncation and preserved control behavior. TypeScript and production build passed. No backend protocol changed. This PR accepts the optional truncation flag from #67 and is also usable on v0.7.0.

The UI checks use jsdom; existing sync transport acceptance is recorded separately in #67. No new physical multi-device or cross-network acceptance is claimed here.
