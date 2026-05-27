# ── Builder: PyInstaller 打包 ──────────────────────────────
FROM python:3.12-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

WORKDIR /build

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY *.py captcha_model.onnx config.example.toml ./
COPY answer/ answer/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv run pyinstaller --noconfirm --onefile \
    --name WeBan \
    --add-data "captcha_model.onnx:." \
    --add-data "answer/answer.json:answer" \
    --add-data "config.example.toml:." \
    --hidden-import nodriver \
    --hidden-import loguru \
    --hidden-import numpy \
    --hidden-import cv2 \
    --hidden-import PIL \
    --collect-submodules nodriver \
    main.py

# ── Runtime ────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends --no-install-suggests \
    chromium \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /tmp/* \
    && useradd -m -s /bin/bash appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app

COPY --from=builder --chown=appuser:appuser /build/dist/WeBan /app/WeBan

USER appuser
WORKDIR /app

ENV CHROMIUM_BINARY=/usr/bin/chromium

ENTRYPOINT ["/app/WeBan"]
