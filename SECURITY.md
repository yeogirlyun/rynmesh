# Security Policy

## Supported versions

Security fixes are applied to the latest release and the `main` branch. Alpha releases older than the latest published version are not maintained separately.

## Reporting a vulnerability

Please do not disclose a suspected vulnerability in a public issue, discussion, or pull request.

Use GitHub's private vulnerability reporting for this repository:

https://github.com/yeogirlyun/rynmesh/security/advisories/new

Include the affected version or commit, reproduction steps, expected impact, and any suggested mitigation. Remove API keys, private keys, personal content, node identities, and real infrastructure addresses from the report unless they are essential to reproduce the issue.

Maintainers will acknowledge a complete report as soon as practical, investigate it privately, and coordinate disclosure after a fix is available. Good-faith research that avoids privacy violations, data destruction, service disruption, and unauthorized access is welcome.

## Security boundaries

Rynmesh verifies signatures, hashes, and receipts, but content received from public sources or peers must still be treated as untrusted. Run nodes with normal user privileges, review exposed bind addresses, and keep secrets out of repository files and logs.
