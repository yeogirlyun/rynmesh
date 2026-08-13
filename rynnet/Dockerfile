# rynnet node image: the STOCK rynmesh package, unmodified.
# The node binary is identical to production; only RYNMESH_* env and the
# (out-of-process) network namespace differ. Build context = repo root:
#   docker build -f rynnet/Dockerfile -t rynnet-node:latest .
FROM python:3.13-slim

# iproute2 = tc/netem (transparent shaping), iptables = partition/NAT.
# curl only for the orchestrator's health checks via `docker exec`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 iptables curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/rynmesh
# Install deps first (better layer caching), then the package itself.
COPY pyproject.toml README.md ./
COPY rynmesh ./rynmesh
RUN pip install --no-cache-dir .
# Reuse the authoritative LAN QA driver (publish-sample / fetch-matrix) as the
# in-node scenario driver — exercises the real store/discovery/fetch API.
COPY qa ./qa

COPY rynnet/entrypoint.sh /usr/local/bin/rynnet-entrypoint
RUN chmod +x /usr/local/bin/rynnet-entrypoint

# RYNNET_ROLE=peer|registry selects the stock console script.
# RYNNET_NETEM (optional) is applied to eth0 before the node starts.
ENV RYNNET_ROLE=peer
ENTRYPOINT ["/usr/local/bin/rynnet-entrypoint"]
