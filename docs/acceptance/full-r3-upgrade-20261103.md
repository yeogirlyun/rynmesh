# Full R3 Windows upgrade acceptance

Date: 2026-11-03. Target: user-authorized LAN Windows 82.

Artifact: `output/full-package-20261103-r3/Ryn_0.8.0_Full_7Plugins_R3_x64.exe`

- Size: 611260289 bytes.
- SHA256: `931F69B6240DFB402A05EF7300135374E81640B1D0E5EDBAA27C8BF1314B2B5A`.
- Core source: `4d1fe3d`; installer source: `16ab30d7e371821352446291cf47dee227ffe11d`.
- Company source: `45e5d07c5b8d83b2ee12310ecba51eb04e370a5d`.

## Verification

Frontend build, sidebar/Craft route tests (30), installer and LAN tests passed. The packaged helper passed isolated fresh-install and repeat-install acceptance, including disabled-plugin preservation, four office editor pages/native CLI versions, three company pages, and company health readiness with outbound network blocked.

The final artifact was transferred to 82 and its SHA256 verified before execution. With Ryn, peer and company processes still running, the final installer ran silently and returned exit code 0. All recorded old process IDs had exited. No manual pre-stop was used.

Post-install identity, messaging key, company configuration key and settings hashes matched the pre-upgrade values. Hash values and user data are deliberately omitted from this record. Old executable backups remain in the install directory's sibling `Ryn-install-backups` directory.

After interactive desktop startup, the company executable path pointed to `company-45e5d07c5b8d83b2/company-service.exe`; company health returned `ok=true,status=ready`. All three company app pages returned HTTP 200. PDF, Word, Excel and PPT reported installed, enabled and editing supported. The local service listing contained four services at verification time.

Desktop inspection confirmed all seven plugins automatically present in the left sidebar after final R3 installation. The same bundled core had also been inspected opening Dashboard and launching the PDF editor during the preceding candidate upgrade. Dashboard showed an unauthenticated page; authenticated financial operations were not tested. No real WeCom messages were sent; the company interface and binary regression evidence is recorded separately under `wecom-send-auth-20261103`.

The temporary desktop-start verification task was removed after startup. Ryn remains running. No installation was performed on 74 or the user's local desktop in this acceptance.
