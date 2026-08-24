FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY rynmesh /app/rynmesh
COPY webapp/dist /app/rynmesh/webui
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install .

ENTRYPOINT []
