"""验证码识别 HTTP 服务（给油猴脚本 / 其他客户端共用）。

只做「看图返回坐标」，不接触任何账号、cookie、ticket 或业务接口：

- POST /captcha/point   点选验证码：传主图/提示图 URL，返回 3 个点击坐标
- POST /captcha/text    登录验证码：传图片 URL，返回 4 位字符
- GET  /health          健康检查

识别算法直接复用 captcha.py 的 detect_points / LoginCaptchaSolver，
本文件只负责 HTTP 外壳、鉴权和限流，不复制算法。

启动::

    python captcha_server.py                 # 默认 127.0.0.1:8765
    WB_CAPTCHA_TOKEN=xxx python captcha_server.py
    python captcha_server.py --host 0.0.0.0 --port 8765 --token xxx

安全约定：
- 默认只监听回环地址；要对外提供服务必须显式 --host，并设置 --token
- 只接受 HTTPS 图片 URL，并解析 DNS 后拒绝内网/回环地址（防 SSRF）
- 不落盘、不记录图片内容，日志只记耗时和识别结果
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import numpy as np

from captcha import LoginCaptchaSolver, detect_points

# ── 配置 ────────────────────────────────────────────────

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 8 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024

# 只允许这些 host suffix 的图片地址；None 表示不限制（但仍做内网校验）
ALLOWED_IMAGE_HOSTS = (
    "captcha.gtimg.com",
    "turing.captcha.qcloud.com",
    "turing.captcha.gtimg.com",
    "captcha.qcloud.com",
)

log = logging.getLogger("captcha_server")


class CaptchaError(Exception):
    """带 HTTP 状态码的业务异常。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ── SSRF 防护 ───────────────────────────────────────────


def _host_allowed(host: str) -> bool:
    """域名必须在白名单后缀内（腾讯验证码 CDN）。"""
    host = host.lower().rstrip(".")
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in ALLOWED_IMAGE_HOSTS)


def _resolve_public_ip(host: str) -> None:
    """解析域名并拒绝指向内网/回环/保留地址的结果。

    DNS rebinding 的完整防护需要把解析结果固定给 HTTP 客户端使用；
    这里做「解析结果必须全为公网地址」的第一道校验，配合域名白名单已足够。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise CaptchaError(f"域名解析失败: {host}", 400) from exc
    if not infos:
        raise CaptchaError(f"域名解析为空: {host}", 400)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            raise CaptchaError("图片地址指向内网，已拒绝", 400)


def validate_image_url(raw: str) -> str:
    """校验并返回图片 URL。"""
    if not isinstance(raw, str) or not raw.strip():
        raise CaptchaError("缺少图片地址")
    if len(raw) > 2048:
        raise CaptchaError("图片地址过长")
    parsed = urlparse(raw.strip())
    if parsed.scheme != "https":
        raise CaptchaError("图片地址必须是 https")
    if not parsed.hostname:
        raise CaptchaError("图片地址缺少主机名")
    if not _host_allowed(parsed.hostname):
        raise CaptchaError(f"图片域名不在白名单内: {parsed.hostname}")
    _resolve_public_ip(parsed.hostname)
    return raw.strip()


def fetch_image_safe(url: str) -> np.ndarray:
    """带大小上限的图片下载（复用 captcha.fetch_image 的解码逻辑）。"""
    import requests

    validate_image_url(url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Referer": "https://turing.captcha.qcloud.com/",
    }
    try:
        with requests.get(url, headers=headers, timeout=10, stream=True) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                raise CaptchaError("图片过大", 413)
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(64 * 1024):
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise CaptchaError("图片过大", 413)
                chunks.append(chunk)
    except CaptchaError:
        raise
    except Exception as exc:
        raise CaptchaError(f"下载图片失败: {exc}", 400) from exc

    import cv2

    arr = np.frombuffer(b"".join(chunks), dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise CaptchaError("图片无法解码", 422)
    return img


# ── 识别 ────────────────────────────────────────────────


@dataclass
class PointResult:
    points: list[dict[str, int]]
    missing: int
    width: int
    height: int
    elapsed_ms: int


def solve_point(bg_url: str, ans_url: str) -> PointResult:
    """点选验证码：返回相对主图像素的 3 个坐标。"""
    started = time.perf_counter()
    main_img = fetch_image_safe(bg_url)
    prompt_img = fetch_image_safe(ans_url)
    ordered, _candidates = detect_points(prompt_img, main_img)
    height, width = main_img.shape[:2]

    points: list[dict[str, int]] = []
    for item in ordered:
        if item is None:
            points.append({"x": -1, "y": -1})
        else:
            x, y = int(item[0]), int(item[1])
            points.append({"x": x, "y": y})
    missing = sum(1 for p in points if p["x"] < 0)
    return PointResult(
        points=points,
        missing=missing,
        width=int(width),
        height=int(height),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


DATA_URL_PREFIX = "data:image/"


def decode_data_url(data_url: str) -> bytes:
    """解析 data:image/...;base64,... 。浏览器里的验证码图没有公网地址，
    只能以 data URL 交给识别服务。"""
    if not data_url.startswith(DATA_URL_PREFIX):
        raise CaptchaError("dataUrl 必须是 data:image/ 开头")
    header, sep, payload = data_url.partition(",")
    if not sep or ";base64" not in header:
        raise CaptchaError("dataUrl 必须是 base64 编码")
    if len(payload) > MAX_IMAGE_BYTES * 2:
        raise CaptchaError("图片过大", 413)
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CaptchaError("dataUrl base64 解码失败") from exc


def solve_text(image_url: str, data_url: str | None = None) -> dict[str, Any]:
    """登录验证码：data URL 直接解码，否则下载图片，然后走 ONNX 模型识别。"""
    started = time.perf_counter()

    if data_url:
        data = decode_data_url(data_url)
    else:
        import requests

        validate_image_url(image_url)
        try:
            resp = requests.get(
                image_url,
                headers={"User-Agent": "Mozilla/5.0 (WeBan captcha_server)"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.content
        except Exception as exc:
            raise CaptchaError(f"下载图片失败: {exc}", 400) from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise CaptchaError("图片过大", 413)

    code = LoginCaptchaSolver.recognize(data, log)
    return {
        "code": code or "",
        "ok": bool(code),
        "elapsedMs": int((time.perf_counter() - started) * 1000),
    }


# ── HTTP 层 ─────────────────────────────────────────────


class RateLimiter:
    """固定窗口限流：每个客户端 IP 每分钟 N 次。"""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            window = [t for t in self._hits.get(key, []) if now - t < 60]
            if len(window) >= self.per_minute:
                self._hits[key] = window
                return False
            window.append(now)
            self._hits[key] = window
            return True


@dataclass
class ServerConfig:
    host: str
    port: int
    token: str
    rate_limit: int


class Handler(BaseHTTPRequestHandler):
    server_version = "WeBanCaptcha/1.0"
    config: ServerConfig
    limiter: RateLimiter

    # ── 工具方法 ────────────────────────────────────────

    def _client_key(self) -> str:
        return self.client_address[0] if self.client_address else "unknown"

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"ok": False, "error": message})

    def _json_body(self) -> dict[str, Any]:
        length = self.headers.get("Content-Length", "0")
        if not length.isdigit():
            raise CaptchaError("Content-Length 非法")
        size = int(length)
        if size <= 0:
            raise CaptchaError("请求体为空")
        if size > MAX_BODY_BYTES:
            raise CaptchaError("请求体过大", 413)
        raw = self.rfile.read(size)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptchaError("请求体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise CaptchaError("请求体必须是 JSON 对象")
        return data

    def _check_access(self) -> bool:
        """鉴权 + 限流；失败时已写出响应并返回 False。"""
        if not self.limiter.allow(self._client_key()):
            self._error(429, "请求过于频繁")
            return False
        if self.config.token:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {self.config.token}":
                self._error(401, "鉴权失败")
                return False
        return True

    # ── 路由 ────────────────────────────────────────────

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/health":
            self._send(200, {"ok": True, "service": "weban-captcha"})
            return
        self._error(404, "not found")

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path not in ("/captcha/point", "/captcha/text"):
            self._error(404, "not found")
            return
        if not self._check_access():
            return
        try:
            body = self._json_body()
            if path == "/captcha/point":
                self._handle_point(body)
            else:
                self._handle_text(body)
        except CaptchaError as exc:
            self._error(exc.status, str(exc))
        except Exception as exc:
            log.exception("识别异常")
            self._error(500, f"识别异常: {exc}")

    def _handle_point(self, body: dict[str, Any]) -> None:
        bg = body.get("bg") or body.get("bgUrl")
        ans = body.get("ans") or body.get("ansUrl")
        if not bg or not ans:
            raise CaptchaError("需要同时提供 bg 和 ans 图片地址")
        result = solve_point(str(bg), str(ans))
        ok = result.missing == 0
        log.info(
            "point ok=%s missing=%d size=%dx%d %dms",
            ok,
            result.missing,
            result.width,
            result.height,
            result.elapsed_ms,
        )
        self._send(
            200 if ok else 422,
            {
                "ok": ok,
                "points": result.points,
                "missing": result.missing,
                "width": result.width,
                "height": result.height,
                "elapsedMs": result.elapsed_ms,
            },
        )

    def _handle_text(self, body: dict[str, Any]) -> None:
        data_url = body.get("dataUrl")
        url = body.get("image") or body.get("url") or body.get("img")
        if not data_url and not url:
            raise CaptchaError("需要提供 image 地址或 dataUrl")
        result = solve_text(str(url or ""), str(data_url) if data_url else None)
        log.info(
            "text ok=%s code=%s %dms", result["ok"], result["code"], result["elapsedMs"]
        )
        self._send(200 if result["ok"] else 422, result)

    def log_message(self, format: str, *args: Any) -> None:
        """不打印默认的逐请求访问日志，避免刷屏。"""
        log.debug("%s - %s", self._client_key(), format % args)


class CaptchaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(config: ServerConfig) -> CaptchaServer:
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "config": config,
            "limiter": RateLimiter(config.rate_limit),
        },
    )
    return CaptchaServer((config.host, config.port), handler)


def parse_args(argv: list[str] | None = None) -> ServerConfig:
    parser = argparse.ArgumentParser(description="WeBan 验证码识别服务")
    parser.add_argument(
        "--host", default=os.environ.get("WB_CAPTCHA_HOST", DEFAULT_HOST)
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("WB_CAPTCHA_PORT", DEFAULT_PORT))
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("WB_CAPTCHA_TOKEN", ""),
        help="Bearer token；对外监听时必填（也可用 WB_CAPTCHA_TOKEN）",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=int(os.environ.get("WB_CAPTCHA_RATE_LIMIT", "60")),
        help="每个 IP 每分钟最大请求数，0 表示不限",
    )
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
        parser.error("监听非回环地址时必须设置 --token，否则任何人都能白嫖识别算力")
    return ServerConfig(
        host=args.host, port=args.port, token=args.token, rate_limit=args.rate_limit
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = parse_args(argv)
    server = build_server(config)
    host, port = server.server_address[:2]
    log.info(
        "captcha server on http://%s:%s (token=%s, rate=%s/min)",
        host,
        port,
        "on" if config.token else "off",
        config.rate_limit or "unlimited",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
