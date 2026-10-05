# Rynmesh Services

Status: design draft, 2026-08-19. This document records the M3 service
direction described in [`RYNMESH_VISION.md`](RYNMESH_VISION.md). It is not a
claim that an open marketplace or cash settlement exists today.

## Product position

A Rynmesh service is a signed, discoverable, bounded job that one node can ask
another node to perform. Rynmesh should sell a verified outcome, not remote
shell access or an unqualified number of GPU-hours.

Initial services remain private/friend-mesh features and settle in
non-transferable Credits. Public untrusted providers, transferable value, and
cash settlement remain gated by open-network hardening, abuse controls, and
legal review.

## What exists in the repository

This table describes implementation status, not live provider availability.

| Capability | Current status | Important limitation |
| --- | --- | --- |
| `signal50.veo_motion.v1` | Specialized asynchronous provider with work orders, an exclusive worker lock, relay bundles, and result references | Depends on a separate Signal50/browser automation environment; not a general local-GPU service |
| `net.egress` | Functional MVP broker for SSH SOCKS5, Nebula SOCKS, or Hysteria2 egress sessions | Uses preconfigured/shared credentials and a local JSONL credit record; not ready for anonymous public use |
| `rynmesh.llm.complete` | Protocol demonstration | Default backend is deterministic stub output; real backends are not wired into the worker |
| `rynmesh.image.generate` | Protocol demonstration | Generates a deterministic gradient PNG, not a diffusion-model image |
| `rynmesh.embedding.compute` | Protocol demonstration | Generates a deterministic hash vector, not a model embedding |

Modules such as Search & Ask, Digest, Reader, Recap, peer messaging, peer
health, and the local model provider are node application features. They are
not currently published marketplace services.

The shared primitives already present are `JobCapacityRecord`, `WorkOrder`,
`WorkResult`, signed records, registry mailboxes, content hashes, a relay, and
non-transferable Credits. The current `WorkOrder.params` field is plaintext and
must not carry private prompts or media on an untrusted registry.

## First prepared flagship: private asynchronous GPU generation

### Capability

`rynmesh.video.generate.v1`

This service lets a node offer a local model running on an RTX 4090 or other
supported GPU. It is asynchronous, one job at a time on a consumer GPU, and is
priced per accepted result rather than per uptime or raw GPU-hour.

The first release should support one pinned model and one tested workflow. A
generic arbitrary ComfyUI graph, arbitrary container, remote shell, and
provider-supplied executable are explicitly out of scope.

### Provider operation

1. The owner enables **Provide compute**.
2. A preflight reads the GPU model, GPU UUID, VRAM, driver, CUDA compatibility,
   free disk, and a standard benchmark.
3. The owner selects an approved local model directory. Rynmesh hashes the
   model manifest and mounts model weights read-only; the weights never leave
   the provider.
4. The owner selects availability, limits, and the price per successful job.
5. Rynmesh publishes a signed offer. The provider pauses automatically when
   free VRAM is below the service threshold or the owner is using the GPU.
6. A provider supervisor claims leased jobs, runs one isolated job container,
   uploads an encrypted result, and destroys the job workspace.

An RTX 4090 has no MIG partitioning boundary. The safe default is therefore an
exclusive GPU lock and `max_concurrent=1` for the whole device.

### Signed offer shape

The existing capacity record is not expressive enough for a buyer to compare
or safely invoke compute. M3 should add a versioned `ServiceOffer` containing at
least:

```json
{
  "kind": "service_offer",
  "schema": "rynmesh.service-offer.v1",
  "capability": "rynmesh.video.generate.v1",
  "provider_peer_id": "<signing public key>",
  "hardware": {
    "gpu_vendor": "nvidia",
    "gpu_model": "RTX 4090",
    "gpu_uuid": "<stable local device id>",
    "vram_bytes": 25757220864
  },
  "runtime": {
    "adapter_id": "<approved adapter>",
    "image_digest": "sha256:<digest>",
    "model_id": "<model name and version>",
    "model_manifest_digest": "sha256:<digest>"
  },
  "limits": {
    "max_concurrent": 1,
    "max_width": 1280,
    "max_height": 720,
    "max_duration_seconds": 5,
    "max_job_seconds": 3600
  },
  "price": {
    "unit": "accepted_result",
    "credits": 20
  },
  "privacy_tier": "community-encrypted",
  "updated_at": "<ISO-8601>"
}
```

The public offer should expose only data needed to select and verify the
service. It must not publish a hostname, home-directory path, private IP,
account name, model filesystem path, or other local-machine metadata.

### Model adapter contract

Approved workers implement a small contract instead of calling the owner's
everyday Ollama or ComfyUI instance:

- `probe()` verifies the GPU, runtime, model files, and model digest.
- `estimate(request)` validates limits and estimates VRAM, time, and price.
- `run(request, workspace)` performs the bounded job.
- `cancel()` stops a leased or expired job.
- `cleanup()` removes runtime state and releases the GPU lock.
- `health()` reports availability without leaking host metadata.

The runtime image is pinned by digest. Model weights are a separate read-only
mount so the owner can retain large local weights without rebuilding an image.

### Private job and result flow

1. The requester creates an ephemeral result key pair and encrypts the prompt
   and input assets before submission.
2. The registry stores signed routing metadata, ciphertext references, hashes,
   limits, price ceiling, expiry, and an idempotency key. It does not store the
   plaintext prompt in `WorkOrder.params`.
3. The provider atomically claims a time-limited lease. Expired leases may be
   reassigned without settling twice.
4. The supervisor creates a one-job runtime with an exclusive GPU lock,
   read-only root and model mounts, no host home mount, no privilege escalation,
   no core dump, no swap, resource/time limits, and no general outbound network.
5. The worker decrypts inputs only inside a per-job encrypted workspace and
   runs the pinned model adapter.
6. The worker encrypts the output to the requester's ephemeral public key
   before it leaves the isolated workspace.
7. The supervisor uploads ciphertext through a scoped, expiring upload grant.
   The relay verifies ciphertext length and hash but cannot decrypt it.
8. The requester downloads, decrypts, verifies, and accepts or disputes the
   bounded result. Credits settle once for an accepted idempotency key.
9. The provider destroys the workspace key, unmounts the workspace, terminates
   the container, releases the GPU, and publishes a signed execution/cleanup
   receipt.
10. Relay ciphertext is deleted after confirmed download or a short TTL.

Large files require chunked authenticated encryption. Use a random file data
key, authenticated chunks, and an X25519-wrapped file key for the requester.
The existing peer-message X25519/HKDF/ChaCha20-Poly1305 code can seed this
design, but its single-message envelope is not the large-file format.

### Deletion and privacy claim

Small plaintext workspaces should use memory-backed storage. Larger video jobs
should use a per-job encrypted scratch volume whose random key exists only in
memory. Cleanup destroys the key before discarding the volume and container;
ordinary file deletion on an SSD is not an adequate guarantee.

The relay stores ciphertext only and should disable plaintext thumbnails,
media inspection, content-derived logs, job-bucket versioning, and long-lived
backups. Operational logs contain IDs, timestamps, byte counts, status codes,
and ciphertext hashes only.

The exact product claim for an ordinary community RTX 4090 is:

> Registry, relay, and unrelated nodes cannot read the encrypted job or result.
> The standard worker performs automatic cryptographic cleanup after the job.
> A provider with administrator control of the host could inspect or copy data
> while it is being processed, so community hardware is not confidential
> computing.

A signed cleanup receipt proves what the provider software reported; it cannot
prove that a malicious host owner did not copy plaintext. Sensitive work must
stay local, use an explicitly trusted/official provider, or later use supported
confidential-compute hardware with remote attestation.

### Execution receipt

The provider signs a receipt over the work-order ID, idempotency key, provider
ID, offer ID, GPU UUID, adapter/image/model digests, start/end time, peak VRAM,
input ciphertext hash, output ciphertext hash, terminal status, and cleanup
status. This makes substitution and accounting visible; it is not proof that a
consumer GPU executed honestly. Random challenge jobs, benchmark history,
duplicate sampling, output validation, and provider reputation add practical
detection.

### Required implementation work

1. Versioned `ServiceOffer`, encrypted work-order envelope, lease, acceptance,
   dispute, and settlement records.
2. Large-file encrypted relay protocol with scoped upload/download grants and
   TTL cleanup.
3. Provider supervisor, NVIDIA preflight, exclusive GPU lock, capacity pause,
   one-job runtime, and cleanup receipts.
4. One pinned `rynmesh.video.generate.v1` adapter and model profile.
5. Requester UI for encrypted submission, progress, download, decryption, and
   acceptance; provider UI for model selection, limits, schedule, and price.
6. Retry, cancellation, idempotency, official fallback, reliability scoring,
   and observability that never logs job plaintext.
7. Threat-model review and destructive-cleanup tests before use with untrusted
   nodes.

### First acceptance test

Use two private test nodes: one requester and one dedicated RTX 4090 provider.
The test passes when a real five-second video job uses the advertised GPU and
model, the registry and relay contain no plaintext, the requester alone can
decrypt the result, a killed/expired job can be retried without double charge,
and no recoverable job plaintext remains in the standard provider workspace
after cleanup.

## Recommended service sequence

The privacy, lease, relay, and settlement substrate above should be built once
and reused by every asynchronous service.

| Order | Capability | Why it belongs in Rynmesh | Gate before release |
| --- | --- | --- | --- |
| 1 | `rynmesh.media.transcode.v1` | Best protocol pilot: asynchronous, useful on CPU/GPU nodes, bounded, and outputs are cheaply verified with hashes and media metadata | Fixed codecs/profiles only; sandbox hostile media; encrypted artifacts and cleanup |
| 2 | `rynmesh.video.generate.v1` | Flagship use of idle 4090-class hardware; result-priced work differentiates Rynmesh from raw GPU rental | Complete the private GPU design above; one approved model/workflow first |
| 3 | `rynmesh.artifact.hold.v1` | Nodes can contribute encrypted temporary storage and delivery bandwidth; also supplies the artifact layer required by other services | Ciphertext-only storage, quotas, TTL, replication, proof of possession, abuse/availability policy |
| 4 | `rynmesh.audio.transcribe.v1` | Long recordings are naturally asynchronous and can be retried or chunked across intermittent home nodes | Encrypted chunks, deterministic language/model declaration, timestamp-quality validation |
| 5 | `rynmesh.render.blender.v1` | Uses consumer GPUs for bounded batch rendering with clearer inputs and verification than arbitrary code execution | Approved Blender/runtime versions, strict scene sandbox, asset limits, no scripts by default |
| 6 | `rynmesh.document.ocr.v1` | Useful CPU/GPU batch work that can run on lower-end nodes and feed the existing Reader/Search features | Private artifacts, file parser sandbox, output schema and confidence reporting |

`net.egress` should continue in trusted friend/company meshes first. An open
public proxy has materially higher abuse, credential, bandwidth-accounting,
complaint, and legal risk than the bounded asynchronous services above.

Realtime LLM inference should not be the first community-node product. Home
node churn and latency make it difficult to promise an API-like SLA. It may be
offered later by official/reliable nodes, while community nodes focus on
retryable batch work.

## Build order

1. **Protocol slice:** offer, encrypted envelope, lease/idempotency, encrypted
   artifact relay, acceptance, Credits settlement, and privacy-safe metadata.
2. **Low-risk proof:** implement `rynmesh.media.transcode.v1` end to end and use
   it to harden retries, cleanup, accounting, and the two-sided UI.
3. **Flagship proof:** add the dedicated 4090 provider supervisor and one real
   `rynmesh.video.generate.v1` adapter.
4. **Private beta:** friend/company nodes plus an official fallback provider;
   Credits only, no promise of cash value.
5. **Open-network work:** reputation, challenges, disputes, abuse controls,
   stronger isolation, policy enforcement, and legal review before accepting
   unknown providers or transferable payment.
