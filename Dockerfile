# Multi-stage build for flowcus
# Stage 1: Build the Rust binary
FROM rust:1.85-bookworm AS builder

WORKDIR /build

# Copy workspace manifests first for layer caching
COPY Cargo.toml Cargo.lock ./
COPY crates/flowcus-core/Cargo.toml crates/flowcus-core/Cargo.toml
COPY crates/flowcus-ipfix/Cargo.toml crates/flowcus-ipfix/Cargo.toml
COPY crates/flowcus-storage/Cargo.toml crates/flowcus-storage/Cargo.toml
COPY crates/flowcus-query/Cargo.toml crates/flowcus-query/Cargo.toml
COPY crates/flowcus-server/Cargo.toml crates/flowcus-server/Cargo.toml
COPY crates/flowcus-app/Cargo.toml crates/flowcus-app/Cargo.toml

# Create stub lib.rs files so cargo can resolve the workspace
RUN for crate in flowcus-core flowcus-ipfix flowcus-storage flowcus-query flowcus-server; do \
        mkdir -p crates/$crate/src && echo "" > crates/$crate/src/lib.rs; \
    done && \
    mkdir -p crates/flowcus-app/src && echo "fn main() {}" > crates/flowcus-app/src/main.rs

# Pre-build dependencies (cached layer)
RUN cargo build --release --workspace 2>/dev/null || true

# Copy real source code
COPY crates/ crates/

# Embed an empty frontend dist so the build succeeds without node
RUN mkdir -p frontend/dist && \
    echo '<!DOCTYPE html><html><body>test</body></html>' > frontend/dist/index.html

# Build the actual binary
RUN touch crates/*/src/*.rs crates/flowcus-app/src/main.rs && \
    cargo build --release -p flowcus-app

# Stage 2: Minimal runtime
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/target/release/flowcus /usr/local/bin/flowcus

RUN mkdir -p /data/storage /data/config

EXPOSE 2137 4739/udp

ENTRYPOINT ["flowcus"]
CMD ["--config", "/data/config/flowcus.toml"]
