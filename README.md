# Rynmesh

[![CI](https://github.com/yeogirlyun/rynmesh/actions/workflows/ci.yml/badge.svg)](https://github.com/yeogirlyun/rynmesh/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/yeogirlyun/rynmesh)](https://github.com/yeogirlyun/rynmesh/releases)

Rynmesh is an open-source, local-first personal AI assistant and verifiable content mesh. A Ryn node can discover and rank public content, learn from local feedback, publish signed content, discover peers, and exchange content with provenance and safety receipts.

The node, web interface, recommendation agent, peer protocol, registry, and command-line tools are all included under the MIT license. No account, API key, proprietary service, or preference setup is required for the default experience.

## What works today

- Proactive recommendations from a built-in public catalog, including video, articles, research, podcasts, audiobooks, and visual content
- Local recommendation profiles that learn from More, Less, Hide, Open, topics, platforms, and written direction
- A self-hosted web interface served by the node daemon
- Signed content manifests, provenance chains, and safety receipts
- Direct peer HTTP transport with registry-assisted discovery and NAT-safe relay jobs
- Content publishing for video, images, audio, documents, slides, datasets, and other files
- MCP tools for Codex-, Claude-, and other MCP-compatible AI operators
- Non-transferable Rynmesh Credits for distribution reputation

Rynmesh is alpha software. APIs and storage formats may change before 1.0.

## Install from GitHub

### macOS desktop

Download the Apple Silicon (`aarch64`) or Intel (`x86_64`) DMG from the
[latest release](https://github.com/yeogirlyun/rynmesh/releases/latest), open it, and drag **Ryn**
to Applications. The app contains its own node daemon and web interface; Python, Node.js, Ollama,
an account, and source configuration are not required. Because community builds are not Apple
notarized yet, the first launch may require Control-clicking Ryn and choosing **Open**.

### Python package

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
curl -fsSL https://github.com/yeogirlyun/rynmesh/releases/latest/download/install.sh | sh
```

Or install the current wheel into an existing Python environment:

```bash
python -m pip install https://github.com/yeogirlyun/rynmesh/releases/download/v0.6.0/rynmesh-0.6.0-py3-none-any.whl
rynmesh-peer
```

Open [http://127.0.0.1:8791](http://127.0.0.1:8791). Ryn starts its recommendation agent automatically. Adding a YouTube channel, subreddit, or RSS feed is optional.

Versioned wheels, checksums, installer scripts, and source archives are available from [GitHub Releases](https://github.com/yeogirlyun/rynmesh/releases). Release wheels include the built web interface; editable source installs use the Vite development server described below.

## Develop from source

```bash
git clone https://github.com/yeogirlyun/rynmesh.git
cd rynmesh
./scripts/dev_setup.sh
```

For hot-reloading development, run the node and web app in separate terminals:

```bash
./.venv/bin/rynmesh-peer
```

```bash
cd webapp
npm run dev
```

The local API runs on port `8791`; Vite serves the development UI on port `5173`.

## Run a registry

Peer discovery can use a registry, but a registry is not required for the local personal-assistant experience.

```bash
export RYNMESH_REGISTRY_HOST="0.0.0.0"
export RYNMESH_REGISTRY_PORT="8790"
export RYNMESH_REGISTRY_DIR="$HOME/.rynmesh/registry"
rynmesh-registry
```

The registry stores signed peer records, work-order mailbox messages, and optional relay blobs. Nodes verify signatures and content hashes locally.

## MCP server

```bash
rynmesh-mcp
```

See [Architecture](docs/ARCHITECTURE.md), [Product milestones](docs/PRODUCT_MILESTONES.md), and [Contributing](CONTRIBUTING.md) for deeper project context.

## Verify a checkout

```bash
python -m pytest -q
python -m ruff check rynmesh tests qa
cd webapp && npm ci && npm run build
```

CI also verifies that a packaged node serves its own UI without a development server.

## Security and privacy

Node identity, preferences, feedback, and fetched content are stored locally under `RYNMESH_HOME` (by default beneath `~/.rynmesh`). Network access is used for sources or peers the node contacts; model integrations are optional.

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Do not include secrets or personal data in public issues.

## Community

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a pull request.
- Follow the [Code of Conduct](CODE_OF_CONDUCT.md).
- Use GitHub Issues for reproducible bugs and focused feature requests.
- Use GitHub Discussions for questions and broader design ideas.

## License

Rynmesh is licensed under the [MIT License](LICENSE).
