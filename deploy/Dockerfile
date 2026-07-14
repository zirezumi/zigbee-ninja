# Build context is the repository root:  docker build -f deploy/Dockerfile .

FROM node:22-alpine AS frontend
WORKDIR /src
# TODO(M1): switch to `npm ci` once package-lock.json is committed
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
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
