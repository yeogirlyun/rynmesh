# Friend offline reading and material review (#74)

Friend updates now offer Save offline and Ask about this. The same action component attaches to the optional private For You provenance supplied by #62. Public digest entries receive no friend action. Both actions explicitly fetch a verified, version-bound private import; saving then queues the existing offline worker, while asking prepares the local import and opens the existing Ask material review without submitting inference.

A saved copy is reused across retries. Download/extraction failures disclose that the source copy remains in My content. Unmounting a publication stops later navigation/queueing. Existing copies remain usable after sharing is revoked; fetching a new copy still requires current access. The authenticated serving friend and recorded publisher survive offline conversion, Ask preparation and download cleanup; the UI does not claim independently verified original authorship.

Validation: Node 22.22.1, 328 frontend tests across 53 files, production build and TypeScript passed. Backend: 59 tests passed covering feed access/integrity, offline download/restart, context budgeting/truncation and two new end-to-end service tests. Ruff passed. The end-to-end tests use real encrypted stores and authenticated sealed transport with an in-process network fixture; no new physical offline desktop acceptance is claimed.

The For You entry point appears when #62 is integrated. Friend updates work independently on v0.7.0. No private material enters a public cache or automatic model request.
