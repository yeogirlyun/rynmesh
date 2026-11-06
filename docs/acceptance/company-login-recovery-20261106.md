# Company login recovery — automated verification, 2026-11-06

Company OAuth requests now carry an issuer-scoped stable installation hint stored in the encrypted OAuth store. Relogin/config changes preserve it. Device quota failures remain distinguishable, and plugin startup shows a visible loading state.

Validation: 45 OAuth tests passed; 15 plugin frame/kept-page tests passed. Real QR scan and installed application verification are still pending; these automated checks do not establish end-to-end login acceptance.

## Reimport follow-up

An imported HTTP connection retains its instance and authorization generation only when its owner, identity group and complete transport/auth configuration match. Plugin and execution revisions still advance. OAuth ignores plugin UI revisions and temporary disablement when retaining credentials, while disabled use and stale executions remain blocked. A refresh completed during an unchanged reimport is saved without revoking the rotated credential. Changed import connections revoke their previous grants.

Validation: all 50 OAuth tests and all 7 extension-store tests passed; the 5 reimport cases passed again after adding disable/re-enable coverage. The earlier installed build (5fc7312 / efc3a8f) completed a real user-confirmed phone scan: workbench signed in, Dashboard rendered journal rows, Affiliate rendered a real WeCom session and order statistics. Reimport acceptance on the follow-up installed build is pending.
