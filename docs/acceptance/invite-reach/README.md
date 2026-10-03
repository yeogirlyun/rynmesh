# Invitation reach before creation and acceptance (#30)

## Change

Friends loads the configured sharing endpoint and its address category before
enabling Create invite. This owner-only read neither creates an invitation nor
contacts a peer. Failure keeps creation disabled and offers retry. Creation sends
the reviewed endpoint; a changed endpoint is rejected before an invite is saved.
Older callers may omit this new optional local API field.

Creation, acceptance review and copyable installation instructions explain the
LAN/already-reachable boundary. Loopback is explicitly this computer only; a public
address or hostname is not presented as a successful reachability check. No NAT
traversal, automatic port opening or additional friend permissions are introduced.

## Recorded evidence (2026-09-17)

- Backend friend route, protocol and pairing-recovery tests: 28 passed.
- Node 22.22.1 frontend: 326 passed in 52 files, including 20 Friends tests.
- TypeScript/build, Ruff and diff whitespace checks passed.
- CI exposed an existing Settings keyboard-recovery test race: the alert could
  render before its effect assigned focus. That assertion now waits for focus;
  the subsequent Tab/Enter retry checks remain unchanged.
- Built a wheel with the compiled web UI and installed it into an isolated
  package directory. Both running nodes verified that they imported that package.
- Opened Friends on an unseeded inviter. Before creation, its configured loopback
  address and this-computer-only limitation were visible. Created an invite through
  the page; copied the complete installation instructions and invitation in memory.
- Started the receiver with a fresh node home only after invitation creation.
  Onboarding was dismissed through Continue later. Pasted the full invitation,
  chose Review invite and observed local signature verification, permissions and
  loopback limitation before Add this friend. Confirmed joining and sent a synthetic
  first message through the page. Delivery reached “confirmed by your friend”.
  Stopped and restarted the recipient process; the page retained the friendship
  and confirmed message without pairing or sending again.
- No invitation, message body or QR screenshot is included in this record.

The browser run uses two real TCP nodes on one Windows host. It proves the
installed Python package and fresh-node UI path. It does not prove downloading and
installing the supported desktop installer, two physical devices or cross-NAT
reachability. Therefore this PR addresses the missing UI disclosure but does not
claim all of #30's physical invite → install → pair acceptance complete.

## Remaining physical acceptance

1. On two separate supported desktops on the same LAN, keep the recipient's Ryn
   uninstalled. Create an invitation and send its plain-text installation guide.
2. Install the candidate desktop build on the recipient. Paste the invitation,
   review identity/address/permissions, and join. If installation exceeds the
   invitation expiry, request a new invitation and retry visibly.
3. Send a text, small attachment and content card; require explicit confirmation
   before fetching the card. Restart and verify retained relationships/messages.
4. Revoke while the other node is offline, verify local denial immediately, and
   retry delivery after recovery. Record timings, counts and safe codes only.

Cross-NAT reach remains #35. Desktop acceptance remains open until recorded on
the actual supported installer, rather than inferred from CI compilation.
