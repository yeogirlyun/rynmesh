# Company login recovery — automated verification, 2026-11-06

Company OAuth requests now carry an issuer-scoped stable installation hint stored in the encrypted OAuth store. Relogin/config changes preserve it. Device quota failures remain distinguishable, and plugin startup shows a visible loading state.

Validation: 45 OAuth tests passed; 15 plugin frame/kept-page tests passed. Real QR scan and installed application verification are still pending; these automated checks do not establish end-to-end login acceptance.
