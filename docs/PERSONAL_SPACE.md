# Personal spaces — local ownership, no cloud account

Implemented on `development/personal-first-implementation`.

For the newer Windows direct/ICE connection flow and three-computer acceptance,
see [Personal-space connections](PERSONAL_SPACE_CONNECTIVITY.md).

## Use it

1. On the home computer, open **Settings → Personal space → Create space**. This device becomes the coordinator and first management device.
2. Choose **Create invitation** (1 hour, 24 hours or 3 days). Copy the entire `ryn-invite-v1:…` invitation and keep it private. It preauthorizes one ordinary device; nobody needs to click a second approval at home.
3. At the other computer, open **Settings → Personal space**, paste the invitation under **Join an existing space**, and enter a device name.
4. Keep Ryn running on the coordinator until joining finishes. Requests and replies travel through the existing Registry mailbox, so neither device needs an inbound public HTTP port. Both must use the same reachable Registry and compatible network-key configuration.
5. On the computer providing the model, configure/start its AI service and select **AI access → My space devices**. The default for a personal space is **This device only**.
6. Optionally make a private laptop a management device. Company computers should remain ordinary members. Managers can invite, promote/demote or remove members; the coordinator serializes these operations and must be online for them.

The sidebar space button and **Add device** open the same workflow. Names and private notes do not determine membership. Remote edits in Devices are private nicknames; they do not rename the other computer. Change a computer's actual name on that computer.

## Identity and access

- Each node retains its Ed25519 identity. The signed space ID and member public keys determine membership; IP changes, travel and device renaming do not create a new identity.
- Reinstalling without restoring the identity creates a new device and requires a new invitation.
- The coordinator signs versioned member snapshots. A member cannot appoint itself manager or forge another device's membership. Old revisions and conflicting snapshots are rejected.
- Invitations contain a random 256-bit bearer secret, the space ID, pinned coordinator signing/messaging keys and expiry. Redemption is atomic, single-use, and bound to the joining node's signature. Repeating the exact in-flight request can recover its original reply after a crash without consuming another invitation.
- Registry work orders and results carry signed, encrypted envelopes using the existing X25519 messaging encryption. The Registry can see routing IDs, timing and ciphertext sizes, but not invitation secrets, member names or member lists. No new Registry endpoints or cloud account code are required.
- Membership copies expire after 24 hours. Nodes attempt refresh every 30 seconds through a worker that polls every 5 seconds. This provides bounded offline access, not instantaneous global revocation. Once refreshed, removed identities are rejected; a disconnected provider may accept an old authorization until its snapshot expires. Expiry blocks space AI access and preserves the stored identity for later refresh.
- Personal-space permissions are enforced inside the AI provider before inference, across transports that call the common provider handler. Requests already running are not forcibly cancelled by removal.
- This pass does not grant remote administration, files, NAS, RDP or arbitrary API access. Separately issued inference API keys have their own lifecycle and must be revoked separately.
- Nodes that have never enabled personal spaces keep their existing published-service behavior. Creating/joining a space enables the new local/space AI policy. Leaving defaults that policy to local-only.

## Local storage and recovery

State lives in the node's `RYNMESH_HOME`: `identity.ed25519`, `personal-space.json`, `space-messaging.key`. Never copy a node identity onto two simultaneously running computers. Managers do not receive the coordinator's signing key.

On the coordinator, **Download encrypted backup** requires a password of at least 12 characters. The recovery file uses scrypt and AES-GCM and includes the coordinator identity, its space messaging key and the saved signed member snapshot. It excludes active invitations and pending requests. Store the password separately.

Recovery is an offline operation, not a UI import in this version. Stop the old coordinator and restore into a **new empty directory**:

```powershell
# From the installed app directory; the bundled tool needs no Python installation.
./rynmesh-peer.exe --restore-space C:/Backups/ryn-space-recovery.json D:/RynRecovered

# Start Ryn using the restored node home.
$env:RYNMESH_HOME = 'D:/RynRecovered'
./Ryn.exe
```

Source checkout equivalent:

```powershell
python -m rynmesh.personal_space C:/Backups/ryn-space-recovery.json D:/RynRecovered
```

The tool prompts for the recovery password without placing it in command arguments. The directory must be empty. Never restart the old coordinator alongside the restored one. Configure the same Registry/network settings on the restored installation.

Recovery uses the member list as of the backup. Sharing starts local-only; review members, remove obsolete devices and create a fresh backup before enabling sharing. Members with a newer cached revision reject older backup revisions; recovery from an old snapshot can therefore require rejoining those devices. This is disaster recovery, not coordinator failover or a way to revoke a compromised coordinator identity.

## Verification and limits

- Tests cover three-node joining, single-use races, expiry/cancellation, restart/retry, delegated management, malicious management requests, removal propagation, stale/forged membership, encrypted backup/restore and local API authorization.
- Compatibility tests run the unmodified Registry HTTP routes through the HTTP registry client; public Registry state is not modified by tests.
- AI-provider tests prove an unauthorized, stranger or removed device cannot execute inference, while a permitted member can.
- Browser acceptance uses three independent local node processes and isolated storage, with real membership APIs: create, invite, join, promote and remove; both UI themes checked.
- Verification: 52 frontend tests and 118 focused backend tests passed. The frozen Windows node was launched and exercised through its real create/invite endpoints; the native app rendered the personal-space page. The bundled offline recovery command's entry point was also verified.
- Different physical networks, public Registry deployment compatibility and NAT traversal remain field acceptance tasks. This feature does not add a new transport or guarantee that every company firewall permits AI traffic.
- One coordinator per space, at most 64 recorded device identities in this initial implementation. There is no cloud account, transparent coordinator failover, or cross-space guest sharing UI.
