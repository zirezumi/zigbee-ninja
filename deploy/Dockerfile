# Build context is the repository root:  docker build -f deploy/Dockerfile .

# The frontend bundle is architecture-independent — pin this stage to the build
# platform so it never runs under QEMU emulation in multi-arch CI builds.
FROM --platform=$BUILDPLATFORM node:22-alpine AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY collector/ ./collector/
RUN pip install --no-cache-dir ./collector
COPY --from=frontend /src/dist /app/static

ENV ZN_DATA_DIR=/data \
    ZN_STATIC_DIR=/app/static \
    ZN_PORT=8686
VOLUME /data
EXPOSE 8686

CMD ["python", "-m", "zigbee_ninja"]
