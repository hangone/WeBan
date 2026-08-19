# WeBan Docker 镜像
# 目标:
#   with-browser   — 内置浏览器，开箱即用
#   without-browser — 通过 CDP 连接宿主机浏览器

# ── Builder: PyInstaller 打包 ──────────────────────────────
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends binutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

ARG APP_VERSION
COPY pyproject.toml uv.lock ./
RUN if [ -n "$APP_VERSION" ]; then \
      python -c "import re,sys; \
p='pyproject.toml'; s=open(p,encoding='utf-8').read(); \
s2,n=re.subn(r'^version = \".*?\"', f'version = \"{sys.argv[1]}\"', s, count=1, flags=re.M); \
assert n==1, 'version field not found'; open(p,'w',encoding='utf-8').write(s2)" "$APP_VERSION"; \
    fi

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY *.py captcha_model.onnx config.example.toml ./
COPY answer/ answer/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv run pyinstaller --noconfirm --onefile \
    --name WeBan \
    --add-data "captcha_model.onnx:." \
    --add-data "answer/answer.json:answer/answer.json" \
    --add-data "config.example.toml:." \
    --add-data "pyproject.toml:." \
    --hidden-import nodriver \
    --hidden-import loguru \
    --hidden-import numpy \
    --hidden-import cv2 \
    --collect-submodules nodriver \
    main.py \
    && strip dist/WeBan

# ── without-browser: 纯二进制，CDP 连接宿主机浏览器 ────────
FROM debian:stable-slim AS without-browser

COPY --from=builder /build/dist/WeBan /app/WeBan
WORKDIR /app

# 数据目录（config.toml/logs/answer 均在此，可挂载持久化）与容器环境标识
ENV WB_DATA_DIR=/app/data \
    ENVIRONMENT=docker
VOLUME ["/app/data"]

ENTRYPOINT ["/app/WeBan"]

# ── with-browser: 内置 Chrome headless shell（CDP 模式）────
FROM chromedp/headless-shell:stable AS with-browser

COPY --from=builder /build/dist/WeBan /app/WeBan
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
WORKDIR /app

ENV WB_DATA_DIR=/app/data \
    ENVIRONMENT=docker
VOLUME ["/app/data"]

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/app/WeBan"]
