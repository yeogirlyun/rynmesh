# WeCom local send fix delivery — 2026-11-03

Company source is integrated into its `develop` at `45e5d07c5b8d83b2ee12310ecba51eb04e370a5d`. No Ryn runtime source change was needed. Source fix and regression report: `integrations/company-app/docs/acceptance/wecom-local-send-auth-20261103.md` in chat-backend.

The five local send/outbox routes no longer require an unrelated dashboard administrator. They retain loopback access, plugin lifecycle and live WeCom account/login checks; all mutations require the existing send-intent header. Dashboard authorization is unchanged.

Validation: 25 new isolated HTTP cases plus 15 native/queue cases passed. The new fresh-install regression fails against pre-fix `60c564c` in `_resolve_actor`. The initial obsolete admin-login test expectation was corrected and its group rerun. No messages were sent to real colleagues.

Built from clean committed company develop using `scripts/build_desktop_service.py`, with the independent GP Finance dependency directory whose lock hash is `3c9087f8c75c339c0855cbb05708fabd2f5fe278c42f51583de030b067eee22e`. An initial build failed due to a missing dependency path; setting `GP_FIN_NODE_MODULES` to the verified installation resolved it. Runtime: `D:/code/_build-tools/company-wecom-auth-release-20261103/dist/company-service`.

Actual EXE acceptance used a new temporary home, blocked service outbound networking, and verified zero admin rows, process readiness, send payload validation without dashboard identity, rejected remote requests and missing intent headers. Dashboard access still returned `404 user_not_found` in the host desktop context (not business data). All 233 manifest file hashes matched. Initial smoke harness used the wrong `/health` URL and expected only 401 for dashboard denial; the corrected harness verified `/api/local/runtime/health` and the actual `user_not_found` rejection. Each owned smoke process was terminated afterward. See `binary-and-payload.json` for sanitized results.

R3 full-package payload: `D:/code/rynmesh/.codex-tmp/full-package-20261103-wecom/payload`. Its company commit and the nested runtime commit match `45e5d07`; executable and manifest hashes match the outer inventory. The packaging chat owns full-package creation and installation; private configuration and data are not part of this evidence.

Limits: isolated synthetic native transport tests and actual binary route/startup checks do not establish live delivery on the colleague's computer or compatibility with every WeCom binary. Existing native version restrictions remain. Rollback uses the previous runtime/installer with preserved user data.

Final R3 installer generated and independently hash-verified: `D:/code/rynmesh/output/full-package-20261103-r3/Ryn_0.8.0_Full_7Plugins_R3_x64.exe` (611260289 bytes). SHA256: `931f69b6240dfb402a05ef7300135374e81640b1d0e5edbaa27c8bf1314b2b5a`. The final installer receipt records company `45e5d07` and successful isolated first/repeat installation acceptance. See `final-package.json`. The packaging chat separately owns the final live LAN-machine upgrade verification.
