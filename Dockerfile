FROM rust:1.99.1-bookworm AS media-build
WORKDIR /build
COPY rust/media-relay/Cargo.toml rust/media-relay/Cargo.lock ./
COPY rust/media-relay/src ./src
RUN apt-get update \
    && apt-get install -y --no-install-recommends pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && cargo build --release --locked -j 2

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    T4_LOG_FILE=/data/logs/playback.jsonl \
    T4_COMPATIBILITY_REPORT=/data/reports/compatibility-latest.json \
    FORWARDED_ALLOW_IPS=*

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY sidecar ./sidecar
COPY --from=media-build /build/target/release/t4-media-relay /usr/local/bin/t4-media-relay

RUN pip install --no-cache-dir . \
    && pip check \
    && python -m sidecar.worker.dependencies

EXPOSE 8765

CMD ["python", "-m", "sidecar.serve"]
