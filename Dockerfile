FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends --no-install-suggests \
    chromium \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /tmp/* \
    && useradd -m -s /bin/bash appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app

USER appuser
WORKDIR /app

ENV CHROMIUM_BINARY=/usr/bin/chromium

COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000,gid=1000 \
    uv sync --frozen --no-dev --no-install-project

COPY --chown=appuser:appuser *.py .
COPY --chown=appuser:appuser captcha_model.onnx .
COPY --chown=appuser:appuser answer/answer.json answer/
COPY --chown=appuser:appuser config.example.toml .

VOLUME ["/app/logs"]

ENTRYPOINT ["uv", "run", "python", "main.py"]