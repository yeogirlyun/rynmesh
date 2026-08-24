FROM node:22-slim AS webapp-build

WORKDIR /webapp
COPY webapp/package.json webapp/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY webapp/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY rynmesh /app/rynmesh
COPY --from=webapp-build /webapp/dist /app/rynmesh/webui
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install .

ENTRYPOINT []
