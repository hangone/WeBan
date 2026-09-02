"""
腾讯验证码处理模块
- 无感验证码: 考试前自动处理，无用户交互
- 图片点选验证码: 课程完成时，先尝试 OpenCV 自动识别，失败再让用户手动处理

点选验证码识别流程:
    1. 从提示图 (prompt) 顶部灰色条提取 3 个待匹配符号的二值模板
    2. 从主图 (main) 提取所有候选符号的二值 mask 及归一化特征
    3. 先用像素差值 + 旋转搜索做粗匹配，再用多尺度模板匹配做精匹配
    4. 输出 3 个按顺序的点击坐标 (相对主图像素)
"""

import asyncio
import json
import os
import platform
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Iterable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import cv2
import nodriver
import numpy as np
import requests
from nodriver import cdp
from nodriver.cdp.runtime import DeepSerializedValue

# 限制 OpenCV 线程数（默认 1）：1-2 核小服务器上，验证码识别（OpenCV
# 全核多线程）与 headless-shell 渲染并发会把 CPU 抢满，CDP 命令被饿死
# （实测 2 核机器每步从 4s 拖到 55s）。1 线程对识别耗时影响小（单张图
# 也就几秒），但能保证 headless-shell 有 CPU 可用。WB_CV_THREADS 可覆盖。
cv2.setNumThreads(int(os.environ.get("WB_CV_THREADS", "1") or 1))


# 无交互模式判定（client/main 共用，放本模块避免循环导入）：
# - ENVIRONMENT=docker（或 container）：Dockerfile 默认设置，标识容器环境
# - stdin 非 TTY：cron/后台运行/管道/SSH 无 TTY 会话，无法接收输入
# - 显式 CLI --non-interactive 由 main.py 单独叠加
def is_non_interactive() -> bool:
    env = os.environ.get("ENVIRONMENT", "").strip().lower()
    if env in ("docker", "container"):
        return True
    try:
        if not sys.stdin.isatty():
            return True
    except (AttributeError, ValueError):
        return True
    return False


def _dsv_to_py(dsv):
    """将 nodriver 的 DeepSerializedValue 递归转换为 Python 原生类型。"""
    if isinstance(dsv, DeepSerializedValue):
        if dsv.type_ == "object" and isinstance(dsv.value, list):
            return {k: _dsv_to_py(v) for k, v in dsv.value}
        if dsv.type_ == "array" and isinstance(dsv.value, list):
            return [_dsv_to_py(item) for item in dsv.value]
        return dsv.value
    if isinstance(dsv, dict):
        t, val = dsv.get("type"), dsv.get("value")
        if t == "undefined":
            return None
        if t == "object" and isinstance(val, list):
            return {k: _dsv_to_py(v) for k, v in val}
        if t == "array" and isinstance(val, list):
            return [_dsv_to_py(item) for item in val]
        return val
    return dsv


# 腾讯验证码 SDK 地址
TCAPTCHA_SDK_URL = "https://turing.captcha.qcloud.com/TJCaptcha.js"

# 验证码 appId
EXAM_CAPTCHA_APP_ID = "190330343"  # 无感验证码（考试）
COURSE_CAPTCHA_APP_ID = "195119536"  # 图片点选验证码（课程完成）

# 默认入口页面
EXAM_ENTRY_URL = "https://weiban.mycourse.cn/#/course"
COURSE_ENTRY_URL = "https://mcwk.mycourse.cn/"

# 所有浏览器/CDP 操作都必须有上限，避免远端端口“能连但不响应”时永久挂起。
CDP_HEALTH_TIMEOUT = 5.0
CDP_DISCOVERY_TIMEOUT = 2.0
CDP_CALL_TIMEOUT = 15.0
BROWSER_START_TIMEOUT = 30.0
PAGE_LOAD_TIMEOUT = 30.0
SDK_LOAD_TIMEOUT = 12.0
IMAGE_WORK_TIMEOUT = 120.0
CLOSE_TIMEOUT = 10.0
ENDPOINT_LOCK_TIMEOUT = 960.0
EXAM_FLOW_TIMEOUT = 180.0
COURSE_FLOW_TIMEOUT = 900.0

_NODRIVER_START_LOCK = threading.Lock()


def _env_positive_int(name: str, default: int, *, maximum: int = 50) -> int:
    """读取可调重试次数；非法或越界值回退默认，避免验证码流程因配置抛错。"""
    raw = os.environ.get(name, "")
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    if value < 1:
        return default
    return min(value, maximum)


def _close_step_timeout() -> float:
    """为关闭流程各步骤分配总预算中的一小段。"""
    return max(min(CLOSE_TIMEOUT / 6, 2.0), 0.01)


async def _bounded[T](
    awaitable: Awaitable[T],
    *,
    timeout: float,
    label: str,
    stop_event: threading.Event | None = None,
) -> T:
    """执行有硬超时的异步操作，并允许共享停止事件立即取消。"""
    task = asyncio.ensure_future(awaitable)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("运行已被中断")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            poll_interval = min(0.1, remaining) if stop_event is not None else remaining
            done, _ = await asyncio.wait({task}, timeout=poll_interval)
            if task in done:
                return await task
    except TimeoutError as exc:
        raise RuntimeError(f"{label}超时（{timeout:g} 秒）") from exc
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@asynccontextmanager
async def _async_thread_lock(
    lock: Any,
    *,
    timeout: float,
    label: str,
    stop_event: threading.Event | None = None,
) -> AsyncIterator[None]:
    """不阻塞事件循环地获取跨线程锁，取消时不会遗留已获取的锁。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    acquired = False
    while not acquired:
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("运行已被中断")
        acquired = lock.acquire(blocking=False)
        if acquired:
            break
        if loop.time() >= deadline:
            raise RuntimeError(f"{label}等待超时（{timeout:g} 秒）")
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


# ── JS 片段（自动识别用）──────────────────────────────

_SHOW_JS = r"""
    (() => {
      window.__captchaResult = null;
      try {
        const captcha = new TencentCaptcha(__APP_ID__, function(res){
            window.__captchaResult = res;
        }, { userLanguage: 'zh-cn' });
        captcha.show();
        window.__captcha = captcha;
      } catch(e) { window.__captchaResult = 'ERROR: ' + String(e); }
    })();
"""

_QUERY_JS = r"""
    (() => {
      const bg = document.querySelector('.tencent-captcha-dy__verify-bg-img');
      const ans = document.querySelector('.tencent-captcha-dy__header-answer img');
      const btn = document.querySelector('.tencent-captcha-dy__verify-confirm-btn');
      const refresh = document.querySelector('.tencent-captcha-dy__footer-icon--refresh');
      function rectOf(el){ if(!el) return null; const r=el.getBoundingClientRect();
        return {x:r.x,y:r.y,w:r.width,h:r.height}; }
      const bgStyle = bg ? window.getComputedStyle(bg).backgroundImage : '';
      let bgUrl = '';
      const m = bgStyle.match(/url\(["']?(.+?)["']?\)/);
      if (m) bgUrl = m[1];
      return {
        bgUrl: bgUrl,
        bgRect: rectOf(bg),
        ansUrl: ans ? ans.src : '',
        ansRect: rectOf(ans),
        btnRect: rectOf(btn),
        btnCls: btn ? btn.className : '',
        refreshRect: rectOf(refresh),
        result: window.__captchaResult,
      };
    })();
"""


# ── 图像工具 ──────────────────────────────────────────


def normalize_mask(
    binary_mask: np.ndarray, canvas_size: int = 48, symbol_size: int = 34
) -> np.ndarray | None:
    """将二值 mask 缩放到固定画布大小，用于后续模板比较。

    :param binary_mask: 单通道二值图 (0/255)
    :param canvas_size: 输出画布边长 (正方形)
    :param symbol_size: 符号缩放目标边长
    :return: canvas_size x canvas_size 的 uint8 数组，无前景像素时返回 None

    示例::

        mask = cv2.imread("symbol.png", cv2.IMREAD_GRAYSCALE)
        norm = normalize_mask(mask)        # 48x48, 居中
        norm = normalize_mask(mask, 64, 48)  # 64x64, 符号占 48px
    """
    ys, xs = np.where(binary_mask > 0)
    if xs.size == 0:
        return None

    x1, x2 = xs.min(), xs.max() + 1
    y1, y2 = ys.min(), ys.max() + 1
    crop = binary_mask[y1:y2, x1:x2]

    h, w = crop.shape
    scale = symbol_size / max(h, w)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    resized = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_NEAREST)

    canvas = np.zeros((canvas_size, canvas_size), dtype=np.uint8)
    oy = (canvas_size - new_h) // 2
    ox = (canvas_size - new_w) // 2
    canvas[oy : oy + new_h, ox : ox + new_w] = resized
    return canvas


def rotate_mask(mask: np.ndarray, angle: float) -> np.ndarray:
    """绕中心旋转二值 mask。

    :param mask: 单通道二值图
    :param angle: 旋转角度 (度，逆时针为正)
    :return: 旋转后的 mask，尺寸不变，背景填 0
    """
    h, w = mask.shape
    center = (w / 2, h / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(
        mask, matrix, (w, h), flags=cv2.INTER_NEAREST, borderValue=(0,)
    )


def crop_foreground(mask: np.ndarray) -> np.ndarray | None:
    """裁剪 mask 到最小外接矩形 (去除全黑边距)。

    :param mask: 单通道二值图
    :return: 裁剪后的子图，全黑时返回 None
    """
    ys, xs = np.where(mask > 0)
    if xs.size == 0:
        return None
    x1, x2 = xs.min(), xs.max() + 1
    y1, y2 = ys.min(), ys.max() + 1
    return mask[y1:y2, x1:x2]


def match_cost(
    query: np.ndarray, candidate: np.ndarray, allow_rotate: bool = True
) -> float:
    """计算两个归一化模板之间的匹配代价 (像素差值之和)。

    :param query: 48x48 归一化模板 (待查询)
    :param candidate: 48x48 归一化模板 (候选)
    :param allow_rotate: 是否尝试多个旋转角度取最优
    :return: 代价，越小越匹配；0 表示完全相同

    示例::

        cost = match_cost(template_a, template_b)
        if cost < 200:
            print("很可能是同一个符号")
    """
    diff = cv2.absdiff(query, candidate)
    best = float(np.sum(diff) / 255.0)
    if not allow_rotate:
        return best

    # 覆盖常见旋转的 6 个角度（原 12 个；小服务器 CPU 敏感，减半提速）
    for angle in (-60, -30, -10, 10, 30, 60):
        rotated = rotate_mask(query, angle)
        score = float(np.sum(cv2.absdiff(rotated, candidate)) / 255.0)
        best = min(best, score)
    return best


def locate_with_template(
    query_mask: np.ndarray, main_mask: np.ndarray
) -> tuple[float, tuple[int, int] | None]:
    """在主图二值 mask 中定位查询符号的位置 (多尺度 + 旋转模板匹配)。

    :param query_mask: 提示图中单个符号的原始二值 mask
    :param main_mask: 主图的全局二值 mask (用于模板匹配)
    :return: (best_score, best_center)
        - best_score: 匹配置信度 [0, 1]，越高越匹配
        - best_center: 符号中心坐标 (x, y) 相对 main_mask，未找到时为 None

    示例::

        score, center = locate_with_template(query_raw_mask, main_bw)
        if score >= 0.70 and center is not None:
            print(f"在 {center} 找到，置信度 {score:.2f}")
    """
    query_crop = crop_foreground(query_mask)
    if query_crop is None:
        return -1.0, None

    qh, qw = query_crop.shape
    if min(qh, qw) < 10:
        return -1.0, None

    best_score = -1.0
    best_center = None
    # 尺度 16→12、角度 19→13：全图 matchTemplate 是识别耗时主体，
    # 小服务器上 304 次大图匹配会抢占 CPU，降采样后仍保持足够精度
    scales = np.linspace(1.1, 3.4, 12)
    angles = range(-90, 91, 15)

    for scale in scales:
        new_w = max(8, round(qw * scale))
        new_h = max(8, round(qh * scale))
        base = cv2.resize(query_crop, (new_w, new_h), interpolation=cv2.INTER_NEAREST)

        for angle in angles:
            rotated = rotate_mask(base, angle)
            rotated = crop_foreground(rotated)
            if rotated is None:
                continue
            if (
                rotated.shape[0] >= main_mask.shape[0]
                or rotated.shape[1] >= main_mask.shape[1]
            ):
                continue
            if np.count_nonzero(rotated) < 40:
                continue

            result = cv2.matchTemplate(main_mask, rotated, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(result)
            if score > best_score:
                cx = loc[0] + rotated.shape[1] // 2
                cy = loc[1] + rotated.shape[0] // 2
                best_score = float(score)
                best_center = (cx, cy)

    return best_score, best_center


# ── 提示图 / 主图解析 ─────────────────────────────────


def _extract_query_templates(
    prompt_img: np.ndarray,
) -> tuple[list[np.ndarray | None], list[np.ndarray]]:
    """从提示图 (顶部灰色 3 格题目条) 提取 3 个模板和原始 mask。

    :param prompt_img: BGR 格式的提示图 (包含顶部灰色条和下方箭头等)
    :return: (query_templates, query_raw_masks)
        - query_templates: 长度 3 的列表，每个元素是 48x48 归一化模板或 None
        - query_raw_masks: 长度 3 的列表，每个元素是原始二值 mask (用于精匹配)
    """
    top_gray = cv2.cvtColor(prompt_img, cv2.COLOR_BGR2GRAY)
    gray_mask = ((top_gray > 110) & (top_gray < 220)).astype(np.uint8) * 255
    gray_mask = cv2.morphologyEx(gray_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        gray_mask, connectivity=8
    )
    strip_box = None
    best_area = -1
    for i in range(1, num_labels):
        x, y, w, h, area = stats[i]
        if area > best_area and area > 100:
            best_area = area
            strip_box = (x, y, w, h)

    if strip_box is None:
        top_h, top_w = prompt_img.shape[:2]
        strip_box = (0, 0, top_w, top_h)

    sx, sy, sw, sh = strip_box
    strip_roi = prompt_img[sy : sy + sh, sx : sx + sw]
    strip_roi_gray = cv2.cvtColor(strip_roi, cv2.COLOR_BGR2GRAY)
    query_cells = np.array_split(strip_roi_gray, 3, axis=1)

    query_templates: list[np.ndarray | None] = []
    query_raw_masks: list[np.ndarray] = []
    for cell in query_cells:
        _, cell_bw = cv2.threshold(
            cell, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
        cell_bw[:2, :] = 0
        cell_bw[-2:, :] = 0
        cell_bw[:, :2] = 0
        cell_bw[:, -2:] = 0
        cell_bw = cv2.morphologyEx(cell_bw, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        query_raw_masks.append(cell_bw)
        query_templates.append(normalize_mask(cell_bw))

    return query_templates, query_raw_masks


def _extract_main_candidates(main_img: np.ndarray) -> tuple[list[dict], np.ndarray]:
    """从主图提取所有候选符号及其归一化特征。

    :param main_img: BGR 格式的主图 (包含多个可点击符号)
    :return: (candidates, template_main_bw)
        - candidates: 列表，每个元素是 dict:
            - "center": (cx, cy) 符号质心坐标
            - "bbox": (x, y, w, h) 外接矩形
            - "norm": 48x48 归一化模板
        - template_main_bw: 全局二值 mask (用于精匹配)
    """
    main_gray = cv2.cvtColor(main_img, cv2.COLOR_BGR2GRAY)
    adaptive_bw = cv2.adaptiveThreshold(
        main_gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )
    global_bw = (main_gray < 90).astype(np.uint8) * 255
    symbol_bw = cv2.bitwise_and(adaptive_bw, global_bw)
    symbol_bw = cv2.morphologyEx(symbol_bw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    symbol_bw = cv2.morphologyEx(symbol_bw, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    template_main_bw = symbol_bw.copy()

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        symbol_bw, connectivity=8
    )
    candidates: list[dict] = []
    for i in range(1, num_labels):
        x, y, w, h, area = stats[i]
        if area < 150 or area > 6000:
            continue
        if w < 20 or h < 20:
            continue
        if w / max(h, 1) > 3.0 or h / max(w, 1) > 3.0:
            continue

        component_mask = np.where(labels[y : y + h, x : x + w] == i, 255, 0).astype(
            np.uint8
        )
        normalized = normalize_mask(component_mask)
        if normalized is None:
            continue

        candidates.append(
            {
                "center": (int(centroids[i][0]), int(centroids[i][1])),
                "bbox": (x, y, w, h),
                "norm": normalized,
            }
        )

    return candidates, template_main_bw


# ── 核心识别接口 ──────────────────────────────────────


def detect_points(
    prompt_img: np.ndarray, main_img: np.ndarray
) -> tuple[list[tuple[int, int] | None], list[dict]]:
    """识别点选验证码的点击顺序。

    :param prompt_img: BGR 格式的提示图 (顶部显示 3 个待匹配符号)
    :param main_img: BGR 格式的主图 (包含多个可点击符号)
    :return: (ordered_points, candidates)
        - ordered_points: 长度 3 的列表，每个元素是 (x, y) 相对 main_img 的像素坐标，
          或 None 表示该位置未识别到
        - candidates: 候选符号列表 (用于可视化调试)

    示例::

        prompt = cv2.imread("prompt.png")
        main = cv2.imread("main.png")
        points, candidates = detect_points(prompt, main)
        # points = [(120, 340), (50, 200), (300, 150)]
        # points = [(120, 340), None, (300, 150)]  # 第 2 个未识别到
    """
    query_templates, query_raw_masks = _extract_query_templates(prompt_img)
    candidates, template_main_bw = _extract_main_candidates(main_img)
    if not candidates:
        return [None, None, None], candidates

    used: set[int] = set()
    ordered_points: list[tuple[int, int] | None] = []
    base_scores: list[float] = []

    # 第一轮: 粗匹配 — 归一化模板 + 旋转搜索
    for template in query_templates:
        if template is None:
            ordered_points.append(None)
            base_scores.append(float("inf"))
            continue

        best_idx = -1
        best_score = float("inf")
        for idx, candidate in enumerate(candidates):
            if idx in used:
                continue
            score = match_cost(template, candidate["norm"], allow_rotate=True)
            if score < best_score:
                best_score = score
                best_idx = idx

        if best_idx >= 0:
            used.add(best_idx)
            cx, cy = candidates[best_idx]["center"]
            ordered_points.append((cx, cy))
            base_scores.append(best_score)
        else:
            ordered_points.append(None)
            base_scores.append(float("inf"))

    # 第二轮: 精匹配 — 原始 mask 多尺度模板匹配，修正低置信度结果
    for i, raw_mask in enumerate(query_raw_masks):
        if ordered_points[i] is not None and base_scores[i] < 280:
            continue

        score, center = locate_with_template(raw_mask, template_main_bw)
        if center is None:
            continue
        if score < 0.70:
            continue

        new_point = (center[0], center[1])
        old_point = ordered_points[i]

        # 避免与已有点重叠
        too_close = False
        for j, point in enumerate(ordered_points):
            if j == i or point is None:
                continue
            distance = (
                (point[0] - new_point[0]) ** 2 + (point[1] - new_point[1]) ** 2
            ) ** 0.5
            if distance < 26:
                too_close = True
                break
        if too_close:
            continue
        if old_point is not None and base_scores[i] < 340:
            continue

        ordered_points[i] = new_point

    return ordered_points, candidates


def render_debug(
    main_img: np.ndarray,
    ordered_points: list[tuple[int, int] | None],
    candidates: list[dict],
) -> np.ndarray:
    """在主图上绘制候选框和识别结果，用于可视化调试。

    :param main_img: BGR 格式的主图
    :param ordered_points: detect_points 返回的有序坐标列表
    :param candidates: detect_points 返回的候选符号列表
    :return: BGR 标注图 (原图副本，绘制了黄色候选框和绿色点击标记)

    示例::

        vis = render_debug(main_img, ordered, candidates)
        cv2.imwrite("debug.png", vis)
    """
    vis = main_img.copy()
    for candidate in candidates:
        x, y, w, h = candidate["bbox"]
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 255), 1)
    for idx, point in enumerate(ordered_points, start=1):
        if point is None:
            continue
        cv2.circle(vis, point, 16, (0, 255, 0), 2)
        cv2.putText(
            vis,
            str(idx),
            (point[0] - 6, point[1] + 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2,
        )
    return vis


def _write_png_atomic(path: Path, image: np.ndarray) -> None:
    """写入调试图片并保证失败时不遗留半成品临时文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}-",
        suffix=path.suffix,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        if not cv2.imwrite(str(temp_path), image):
            raise OSError(f"无法写入验证码调试图片: {path}")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _save_debug_images(
    debug_dir: Path,
    stem: str,
    main_img: np.ndarray,
    prompt_img: np.ndarray,
    ordered_points: list[tuple[int, int] | None],
    candidates: list[dict],
) -> None:
    """在线程中生成并原子保存一组验证码调试图片。"""
    _write_png_atomic(debug_dir / f"{stem}_main.png", main_img)
    _write_png_atomic(debug_dir / f"{stem}_prompt.png", prompt_img)
    _write_png_atomic(
        debug_dir / f"{stem}_debug.png",
        render_debug(main_img, ordered_points, candidates),
    )


def fetch_image(url: str) -> np.ndarray:
    """通过 HTTP 下载验证码图片并解码为 BGR 数组。

    :param url: 图片 URL (腾讯验证码 CDN 地址)
    :return: BGR 格式的 numpy 数组
    :raises RuntimeError: 图片无法解码时
    :raises requests.HTTPError: HTTP 请求失败时
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Referer": "https://turing.captcha.qcloud.com/",
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    arr = np.frombuffer(resp.content, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"无法解码图片: {url[:120]}")
    return img


# ── 登录验证码识别 ────────────────────────────────────


class LoginCaptchaSolver:
    """登录验证码识别 (4 位字母/数字图片，基于 CNN ONNX 模型)。

    ONNX 模型加载开销大，使用类级别缓存，多次实例化共享同一个推理会话。

    示例::

        code = LoginCaptchaSolver.recognize(image_bytes, log)
        if code:
            print(f"识别结果: {code}")
    """

    _ocr: Any = None  # cv2.dnn.Net 或 False (不可用)
    _initialized: bool = False
    _lock = threading.Lock()
    _charset = "0123456789abcdefghijklmnopqrstuvwxyz"
    _idx_to_char: ClassVar[dict[int, str]] = {
        i: c for c, i in {c: i for i, c in enumerate(_charset)}.items()
    }
    _char_size = 28

    @classmethod
    def get_ocr(cls, log):
        """获取 OpenCV DNN Net 实例 (懒加载，类级别缓存)。

        :param log: 日志记录器
        :return: cv2.dnn.Net 实例，不可用时返回 None
        """
        if not cls._initialized:
            with cls._lock:
                if not cls._initialized:
                    try:
                        if getattr(sys, "frozen", False):
                            model_path = Path(sys._MEIPASS) / "captcha_model.onnx"  # type: ignore[attr-defined]
                        else:
                            model_path = Path(__file__).parent / "captcha_model.onnx"

                        if not model_path.exists():
                            log.warning(
                                f"验证码模型文件不存在: {model_path}。"
                                "登录验证码自动识别已禁用；交互模式可人工输入，"
                                "无交互模式将安全失败，不会猜测验证码"
                            )
                            cls._ocr = False
                        else:
                            cls._ocr = cv2.dnn.readNetFromONNX(str(model_path))
                    except Exception as exc:  # noqa: BLE001 -- 可选模型损坏必须安全降级
                        log.warning(
                            "验证码模型加载失败，登录验证码自动识别已禁用；"
                            "交互模式可人工输入，无交互模式将安全失败"
                            f"（{type(exc).__name__}）"
                        )
                        cls._ocr = False
                    cls._initialized = True
        return cls._ocr if cls._ocr is not False else None

    @classmethod
    def recognize(cls, image: bytes, log) -> str | None:
        """用 CNN ONNX 模型识别验证码图片。

        :param image: 验证码图片字节 (bytes)
        :param log: 日志记录器
        :return: 识别结果 (4 字符字符串) 或 None (识别失败/不可用)

        示例::

            image = api.rand_letter_image(timestamp)
            code = LoginCaptchaSolver.recognize(image, log)
        """
        ocr = cls.get_ocr(log)
        if not ocr:
            return None
        try:
            arr = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
            if arr is None:
                return None
            _, w = arr.shape
            seg_w = w // 4

            result = []
            for i in range(4):
                char_img = arr[:, i * seg_w : (i + 1) * seg_w if i < 3 else w]
                resized = cv2.resize(
                    char_img,
                    (cls._char_size, cls._char_size),
                    interpolation=cv2.INTER_LINEAR,
                )
                inp = (resized.astype(np.float32) / 255.0).reshape(
                    1, 1, cls._char_size, cls._char_size
                )
                with cls._lock:
                    ocr.setInput(inp)
                    out = ocr.forward()
                result.append(cls._idx_to_char[int(out[0].argmax())])

            code = "".join(result)
            log.info(f"自动验证码识别结果: {code}")
            if len(code) == 4:
                return code
            log.warning("验证码识别结果长度不正确，正在重试")
        except Exception as e:  # noqa: BLE001 -- cv2/numpy 识别管线，失败返回 None 由调用方重试
            log.error(f"验证码识别异常: {e}")
        return None


# ── 浏览器自动检测 ──────────────────────────────────────


def _playwright_candidates() -> list[str]:
    """返回 Playwright 安装的 Chrome/Chromium 路径（最新版本优先）。"""
    pw_dir = Path.home() / ".cache" / "ms-playwright"
    if not pw_dir.is_dir():
        return []
    system = platform.system()
    patterns = {
        "Linux": "chromium-*/chrome-linux/chrome",
        "Darwin": "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        "Windows": "chromium-*/chrome-win/chrome.exe",
    }
    pattern = patterns.get(system)
    if not pattern:
        return []
    return [str(p) for p in sorted(pw_dir.glob(pattern), reverse=True)]


def _registry_candidates() -> list[str]:
    """从 Windows 注册表中查找已安装的 Chrome/Edge 路径。"""
    if platform.system() != "Windows":
        return []
    try:
        import winreg
    except ImportError:
        return []
    results: list[str] = []
    # Chrome/Edge 通过 App Paths 注册
    # winreg 仅 Windows 存在，typeshed 按平台裁剪导致 Darwin 下属性不可见
    for hive, subkey in [
        (
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        ),  # type: ignore[attr-defined]
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        ),  # type: ignore[attr-defined]
        (
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        ),  # type: ignore[attr-defined]
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        ),  # type: ignore[attr-defined]
    ]:
        try:
            with winreg.OpenKey(hive, subkey) as key:  # type: ignore[attr-defined]
                path, _ = winreg.QueryValueEx(key, None)  # type: ignore[attr-defined]
                if path and os.path.isfile(path):
                    results.append(path)
        except (FileNotFoundError, PermissionError):
            continue
    return results


def detect_browser() -> str | None:
    """自动检测本地已安装的 Chrome/Chromium/Edge 浏览器路径。

    按优先级依次检查：
    1. Playwright 缓存
    2. Windows 注册表
    3. 平台常见安装路径
    4. PATH 查找

    仅覆盖 Chrome / Chromium / Edge 三款浏览器的各通道版本（stable/beta/dev/canary）。
    CDP 连接和用户指定路径由 check_browser_health 处理。
    """
    system = platform.system()
    candidates: list[str] = _playwright_candidates() + _registry_candidates()

    if system == "Darwin":
        _apps = "/Applications"
        _chrome = [
            "Google Chrome",
            "Google Chrome Beta",
            "Google Chrome Dev",
            "Google Chrome Canary",
        ]
        _chromium = ["Chromium"]
        _edge = [
            "Microsoft Edge",
            "Microsoft Edge Beta",
            "Microsoft Edge Dev",
            "Microsoft Edge Canary",
        ]
        for name in _chrome + _chromium + _edge:
            candidates.append(f"{_apps}/{name}.app/Contents/MacOS/{name}")

    elif system == "Linux":
        _chrome = [
            "google-chrome",
            "google-chrome-stable",
            "google-chrome-beta",
            "google-chrome-unstable",
        ]
        _chromium = ["chromium", "chromium-browser"]
        _edge = [
            "microsoft-edge-stable",
            "microsoft-edge-beta",
            "microsoft-edge-dev",
            "microsoft-edge",
        ]
        for name in _chrome + _chromium + _edge:
            candidates.append(f"/usr/bin/{name}")
            candidates.append(f"/snap/bin/{name}")
        # /opt/ 路径（部分发行版安装位置）
        candidates += [
            "/opt/google/chrome/google-chrome",
            "/opt/google/chrome-beta/google-chrome",
            "/opt/google/chrome-unstable/google-chrome",
            "/opt/microsoft/msedge/msedge",
            "/opt/microsoft/msedge-beta/msedge",
            "/opt/microsoft/msedge-dev/msedge",
        ]

    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        # PROGRAMW6432 是 64 位进程的真实 Program Files；32 位打包（或 WOW64）
        # 下 PROGRAMFILES 会被重定向到 "... (x86)"，必须显式补上
        pf64 = os.environ.get("PROGRAMW6432", "")
        pf = os.environ.get("PROGRAMFILES", "")
        pf86 = os.environ.get("PROGRAMFILES(X86)", "")
        _chrome = ["Chrome", "Chrome Beta", "Chrome Dev", "Chrome SxS"]  # SxS = Canary
        _chromium = ["Chromium"]
        _edge = [
            "Microsoft/Edge",
            "Microsoft/Edge Beta",
            "Microsoft/Edge Dev",
            "Microsoft/Edge SxS",
        ]
        for base in dict.fromkeys((local, pf64, pf, pf86)):
            if not base:
                continue
            for name in _chrome + _chromium:
                candidates.append(str(Path(base) / name / "Application" / "chrome.exe"))
            for name in _edge:
                candidates.append(str(Path(base) / name / "Application" / "msedge.exe"))

    for p in candidates:
        if os.path.isfile(p):
            return p

    # PATH 查找（Windows 下还会匹配 chrome.exe / msedge.exe 等）
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "google-chrome-beta",
        "google-chrome-unstable",
        "chromium",
        "chromium-browser",
        "microsoft-edge-stable",
        "microsoft-edge-beta",
        "microsoft-edge-dev",
        "microsoft-edge",
        "chrome",
        "chrome.exe",
        "msedge",
        "msedge.exe",
    ):
        found = shutil.which(name)
        if found:
            return found

    return None


def _validated_browser_options(
    browser_path: str | None,
    cdp_host: str | None,
    cdp_port: int | None,
) -> tuple[str | None, str | None, int | None]:
    """规范化浏览器参数；配置不完整时禁止静默回退。"""
    path = os.fspath(browser_path).strip() if browser_path is not None else None
    path = path or None
    host = str(cdp_host).strip() if cdp_host is not None else None
    host = host or None

    if (host is None) != (cdp_port is None):
        raise RuntimeError("CDP 配置不完整：cdp_host 和 cdp_port 必须同时提供")
    if cdp_port is not None:
        if isinstance(cdp_port, bool) or not isinstance(cdp_port, int):
            raise RuntimeError("CDP 端口必须是 1 到 65535 的整数")
        if not 1 <= cdp_port <= 65535:
            raise RuntimeError("CDP 端口必须在 1 到 65535 之间")
    if host is not None and (
        "://" in host or "/" in host or any(char.isspace() for char in host)
    ):
        raise RuntimeError("cdp_host 只能填写主机名或 IP，不应包含协议、路径或空格")
    if host is not None and ":" in host and not host.startswith("["):
        host = f"[{host}]"

    # CDP 是显式的浏览器来源；此时不应因为遗留的 browser_path 失效而
    # 阻止连接既有浏览器。调用方仍保留规范化后的 CDP 主机和端口。
    if host is not None and cdp_port is not None:
        return None, host, cdp_port

    if path is not None:
        expanded = Path(os.path.expandvars(path)).expanduser()
        if not expanded.is_file():
            raise RuntimeError(f"显式指定的浏览器可执行文件不存在: {expanded}")
        if os.name != "nt" and not os.access(expanded, os.X_OK):
            raise RuntimeError(f"显式指定的浏览器文件不可执行: {expanded}")
        path = str(expanded.resolve())

    return path, host, cdp_port


def _cdp_http_host(host: str) -> str:
    """为 HTTP URL 格式化主机名（兼容 IPv6 字面量）。"""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _origin_from_url(url: str) -> str:
    """提取 RFC origin，禁止把课程路径或 fragment 当成 origin。"""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"验证码入口 URL 无效: {url!r}")
    if parsed.username or parsed.password:
        raise RuntimeError("验证码入口 URL 不应包含用户名或密码")
    return f"{parsed.scheme}://{parsed.netloc}"


def _check_cdp_health(
    host: str,
    port: int,
    *,
    timeout: float = CDP_HEALTH_TIMEOUT,
) -> None:
    """请求标准 CDP 版本端点，拒绝普通 HTTP 服务冒充调试端口。"""
    url = f"http://{_cdp_http_host(host)}:{port}/json/version"
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"CDP 健康检查失败 ({host}:{port}/json/version): {exc}"
        ) from exc

    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("Browser"), str)
        or not payload["Browser"].strip()
    ):
        raise RuntimeError(
            f"CDP 健康检查失败 ({host}:{port}/json/version): 响应缺少 Browser 字段"
        )
    websocket_url = payload.get("webSocketDebuggerUrl")
    if websocket_url is not None and not str(websocket_url).startswith(
        ("ws://", "wss://")
    ):
        raise RuntimeError(
            f"CDP 健康检查失败 ({host}:{port}/json/version): webSocketDebuggerUrl 无效"
        )


def _detect_default_cdp_endpoint() -> tuple[str, int] | None:
    """快速探测历史默认 CDP 端点，供验证码首次使用时懒发现。"""
    candidates = (
        ("127.0.0.1", 9222),
        ("127.0.0.1", 9223),
        ("host.docker.internal", 9222),
        ("host.docker.internal", 9223),
    )
    for host, port in candidates:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                pass
            _check_cdp_health(host, port, timeout=CDP_DISCOVERY_TIMEOUT)
            return host, port
        except (OSError, RuntimeError):
            continue
    return None


async def _remove_temporary_profile(instance: Any) -> None:
    config = getattr(instance, "config", None)
    if config is None or getattr(config, "uses_custom_data_dir", True):
        return
    user_data_dir = getattr(config, "user_data_dir", None)
    if not user_data_dir:
        return
    try:
        await _bounded(
            asyncio.to_thread(shutil.rmtree, user_data_dir, ignore_errors=True),
            timeout=_close_step_timeout(),
            label="清理浏览器临时目录",
        )
    except RuntimeError:
        # 删除目录失败不应阻止 unregister；系统临时目录后续仍可自行回收。
        pass


async def _close_browser_instance(instance: Any) -> None:
    """只清理传入实例，不触碰其他账号或调用方注册的浏览器。"""
    from nodriver.core import util as _nd_util

    proc = getattr(instance, "_process", None)
    try:
        with suppress(Exception):
            await _bounded(
                instance.aclose(),
                timeout=_close_step_timeout(),
                label="关闭浏览器连接",
            )
        if proc is not None:
            if getattr(proc, "returncode", None) is None:
                with suppress(ProcessLookupError, OSError):
                    proc.kill()
            with suppress(Exception):
                await _bounded(
                    proc.wait(),
                    timeout=_close_step_timeout(),
                    label="等待浏览器进程退出",
                )
    finally:
        # Process.wait() 不保证 stdout/stderr pipe transport 已关闭。
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with suppress(Exception):
                transport.close()
        _nd_util.get_registered_instances().discard(instance)
        await _remove_temporary_profile(instance)
        for _ in range(3):
            await asyncio.sleep(0)


async def kill_stray_browsers(instances: Iterable[Any] | None = None) -> None:
    """关闭指定 nodriver 实例。

    ``instances`` 留空保留旧接口语义；验证码内部始终显式传入本次创建的实例，
    避免并行账号之间互相关闭浏览器。
    """
    if instances is None:
        from nodriver.core import util as _nd_util

        instances = tuple(_nd_util.get_registered_instances())
    for instance in tuple(instances):
        await _close_browser_instance(instance)


def check_browser_health(
    browser_path: str | None = None,
    cdp_host: str | None = None,
    cdp_port: int | None = None,
) -> str:
    """按优先级检测可用的浏览器：CDP → 用户路径 → 自动检测。

    :param browser_path: 浏览器可执行文件路径（配置文件指定）
    :param cdp_host: CDP 远程调试地址
    :param cdp_port: CDP 远程调试端口
    :return: 浏览器路径或 CDP 地址
    :raises RuntimeError: 无可用浏览器时
    """
    browser_path, cdp_host, cdp_port = _validated_browser_options(
        browser_path, cdp_host, cdp_port
    )
    if cdp_host is not None and cdp_port is not None:
        _check_cdp_health(cdp_host, cdp_port)
        return f"{_cdp_http_host(cdp_host)}:{cdp_port}"

    if browser_path is not None:
        resolved = browser_path
    else:
        resolved = detect_browser()
        if not resolved:
            raise RuntimeError(
                "未找到 Chrome / Chromium / Edge 浏览器。请通过以下方式之一提供：\n"
                "  1. 在 config.toml 中配置 cdp_host 和 cdp_port 连接远程浏览器\n"
                "  2. 在 config.toml 中配置 browser_path 指定浏览器路径\n"
                "  3. 安装 Playwright: pip install playwright && playwright install chromium\n"
                "  4. 安装 Chrome、Chromium 或 Edge"
            )

    async def _probe() -> None:
        from nodriver.core import util as _nd_util

        # nodriver 启动失败时不会返回 Browser，只能通过注册表差集定位残留。
        # 串行化“快照→启动→差集清理”，确保差集只属于本次探测。
        async with _async_thread_lock(
            _NODRIVER_START_LOCK,
            timeout=ENDPOINT_LOCK_TIMEOUT,
            label="浏览器启动",
        ):
            before = set(_nd_util.get_registered_instances())
            browser = None
            try:
                browser = await _bounded(
                    nodriver.start(
                        headless=True,
                        browser_executable_path=resolved,
                    ),
                    timeout=BROWSER_START_TIMEOUT,
                    label="浏览器启动",
                )
                await _bounded(
                    browser.get("data:text/html,<h1>ok</h1>"),
                    timeout=PAGE_LOAD_TIMEOUT,
                    label="浏览器探测页加载",
                )
            finally:
                owned = set(_nd_util.get_registered_instances()) - before
                if browser is not None:
                    owned.add(browser)
                await kill_stray_browsers(owned)

    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            asyncio.run(_probe())
            return resolved
        except FileNotFoundError as e:
            raise RuntimeError(f"浏览器可执行文件不存在: {resolved}") from e
        except Exception as e:  # noqa: BLE001 -- 探测任何启动失败都要重试
            last_exc = e
        if attempt < 3:
            time.sleep(1)
    raise RuntimeError(f"浏览器启动失败: {last_exc}") from last_exc


# ── CaptchaHandler ────────────────────────────────────


class CaptchaHandler:
    """通过浏览器处理腾讯验证码"""

    _endpoint_locks: ClassVar[dict[str, Any]] = {}
    _endpoint_locks_guard: ClassVar[Any] = threading.Lock()

    def __init__(
        self,
        tenant_code: str,
        user_id: str,
        token: str,
        log,
        browser_path: str | None = None,
        cdp_host: str | None = None,
        cdp_port: int | None = None,
        debug_dir: Path | None = None,
        non_interactive: bool | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        """初始化验证码处理器。

        :param tenant_code: 租户编码
        :param user_id: 用户 ID
        :param token: 认证令牌
        :param log: 日志记录器（需支持 info/warning/success 方法）
        :param browser_path: 浏览器可执行文件路径，留空则自动查找
        :param cdp_host: CDP 远程调试地址，配合 cdp_port 使用时不启动本地浏览器
        :param cdp_port: CDP 远程调试端口，配合 cdp_host 使用时不启动本地浏览器
        :param debug_dir: 调试图片保存目录，留空则默认 logs/<user_id>/captcha
        :param non_interactive: 是否禁止人工验证码；留空则按当前运行环境判断
        :param stop_event: 进程级停止事件，用于中断浏览器和识别等待
        """
        browser_path, cdp_host, cdp_port = _validated_browser_options(
            browser_path, cdp_host, cdp_port
        )
        self._auth = {
            "userId": user_id,
            "token": token,
            "tenantCode": tenant_code,
        }
        self.log = log
        self.browser_path = browser_path
        self.cdp_host = cdp_host
        self.cdp_port = cdp_port
        self._debug_dir = debug_dir or Path("logs") / user_id / "captcha"
        self.non_interactive = (
            is_non_interactive() if non_interactive is None else non_interactive
        )
        self.stop_event = stop_event or threading.Event()
        self._closed = False
        self._browser_ready = False
        self._health_lock: Any = threading.Lock()
        self._browser_states: dict[int, dict[str, Any]] = {}

    @property
    def _endpoint_key(self) -> str | None:
        if self.cdp_host is None or self.cdp_port is None:
            return None
        host = self.cdp_host.casefold()
        if host in {"localhost", "127.0.0.1", "[::1]"}:
            host = "loopback"
        return f"{host}:{self.cdp_port}"

    @asynccontextmanager
    async def _exclusive_endpoint(self) -> AsyncIterator[None]:
        """同一共享 CDP 端点一次只允许一个验证码流程修改页面状态。"""
        self._raise_if_stopped()
        endpoint = self._endpoint_key
        if endpoint is None:
            yield
            return
        with self._endpoint_locks_guard:
            lock = self._endpoint_locks.setdefault(endpoint, threading.Lock())
        async with _async_thread_lock(
            lock,
            timeout=ENDPOINT_LOCK_TIMEOUT,
            label=f"共享 CDP 端点 {endpoint}",
            stop_event=self.stop_event,
        ):
            yield

    def _raise_if_stopped(self) -> None:
        if self._closed:
            raise RuntimeError("验证码处理器已关闭")
        if self.stop_event.is_set():
            raise InterruptedError("运行已被中断")

    async def _sleep(self, seconds: float) -> None:
        """异步等待并以较短轮询间隔响应线程停止事件。"""

        self._raise_if_stopped()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(seconds))
        while True:
            self._raise_if_stopped()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))

    async def _bound[T](
        self,
        awaitable: Awaitable[T],
        *,
        timeout: float,
        label: str,
    ) -> T:
        return await _bounded(
            awaitable,
            timeout=timeout,
            label=label,
            stop_event=self.stop_event,
        )

    async def _ensure_browser_ready(self) -> None:
        """首次真正需要验证码时再探测最终账号配置。"""
        self._raise_if_stopped()
        if self._browser_ready:
            return
        async with _async_thread_lock(
            self._health_lock,
            timeout=ENDPOINT_LOCK_TIMEOUT,
            label="浏览器健康检查",
            stop_event=self.stop_event,
        ):
            if self._browser_ready:
                return
            if (
                self.browser_path is None
                and self.cdp_host is None
                and self.cdp_port is None
            ):
                discovered = await self._bound(
                    asyncio.to_thread(_detect_default_cdp_endpoint),
                    timeout=4 * (CDP_DISCOVERY_TIMEOUT + 0.5) + 1,
                    label="默认 CDP 端点探测",
                )
                if discovered is not None:
                    self.cdp_host, self.cdp_port = discovered
                    self._browser_ready = True
                    self.log.info(
                        f"自动探测到 CDP 浏览器 {self.cdp_host}:{self.cdp_port}"
                    )
                    return
            timeout = (
                3 * (BROWSER_START_TIMEOUT + PAGE_LOAD_TIMEOUT + CLOSE_TIMEOUT + 1) + 5
            )
            resolved = await self._bound(
                asyncio.to_thread(
                    check_browser_health,
                    self.browser_path,
                    self.cdp_host,
                    self.cdp_port,
                ),
                timeout=timeout,
                label="浏览器健康检查",
            )
            if self.cdp_host is None:
                self.browser_path = resolved
            self._browser_ready = True

    # ── 浏览器 / 页面构建 ──────────────────────────────

    async def _evaluate(
        self,
        tab,
        expression: str,
        *,
        return_by_value: bool = False,
        interruptible: bool = True,
    ):
        if not interruptible:
            return await _bounded(
                tab.evaluate(expression, return_by_value=return_by_value),
                timeout=CDP_CALL_TIMEOUT,
                label="CDP 脚本执行",
            )
        return await self._bound(
            tab.evaluate(expression, return_by_value=return_by_value),
            timeout=CDP_CALL_TIMEOUT,
            label="CDP 脚本执行",
        )

    async def _eval_json(self, tab, expression: str) -> dict | None:
        """执行 JS 并将结果转为 Python dict（处理 nodriver 的 RemoteObject 反序列化）。"""
        res: Any = await self._evaluate(tab, expression, return_by_value=True)
        if isinstance(res, cdp.runtime.RemoteObject):
            if (
                res.deep_serialized_value
                and res.deep_serialized_value.type_ == "object"
            ):
                result = _dsv_to_py(res.deep_serialized_value)
                return result if isinstance(result, dict) else None
            return None
        if isinstance(res, dict):
            return res
        return None

    async def _create_browser(self, headless: bool = False) -> nodriver.Browser:
        """创建 nodriver 浏览器实例。

        :param headless: True 时以无头模式运行（无需用户交互）
        :return: 已配置的 Browser 对象

        窗口尺寸 428x818 模拟移动端以匹配腾讯验证码的移动版 UI。
        """
        from nodriver.core import util as _nd_util

        cdp_mode = self.cdp_host is not None and self.cdp_port is not None
        browser_path = None if cdp_mode else self.browser_path
        browser_args = [
            "--window-size=428,818",
            "--mute-audio",
            "--disable-extensions",
            "--disable-default-apps",
            "--no-first-run",
            "--disable-infobars",
        ]
        if headless:
            browser_args.append("--headless=new")
        # 非 CDP 模式：nodriver 启动 Chrome 的就绪窗口只有 ~2.75s，冷启动/
        # 资源紧张时可能超时（"Failed to connect to browser"），重试一次。
        # CDP 模式连已有浏览器，失败是配置问题，不重试。
        attempts = 1 if cdp_mode else 2
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with _async_thread_lock(
                    _NODRIVER_START_LOCK,
                    timeout=ENDPOINT_LOCK_TIMEOUT,
                    label="浏览器启动",
                    stop_event=self.stop_event,
                ):
                    before = set(_nd_util.get_registered_instances())
                    try:
                        return await self._bound(
                            nodriver.start(
                                headless=headless,
                                browser_executable_path=browser_path,
                                browser_args=browser_args,
                                host=self.cdp_host,
                                port=self.cdp_port,
                            ),
                            timeout=BROWSER_START_TIMEOUT,
                            label="浏览器启动",
                        )
                    except BaseException:
                        owned = set(_nd_util.get_registered_instances()) - before
                        await kill_stray_browsers(owned)
                        raise
            except Exception as e:
                last_exc = e
                connection_failed = "Failed to connect to browser" in str(
                    e
                ) or "浏览器启动超时" in str(e)
                if connection_failed:
                    if cdp_mode:
                        raise RuntimeError(
                            f"无法连接 CDP 浏览器 ({self.cdp_host}:{self.cdp_port})。"
                            "请检查：\n"
                            "  1. 远程浏览器是否已启动并开放调试端口\n"
                            "  2. config.toml 中 cdp_host 和 cdp_port 是否正确"
                        ) from e
                    if attempt < attempts:
                        await self._sleep(1)
                        continue
                    raise RuntimeError(
                        f"无法启动浏览器 ({browser_path or '自动检测'})。"
                        "请尝试以下解决方案：\n"
                        "  1. 在 config.toml 中配置 browser_path 指定浏览器路径\n"
                        "  2. 在 config.toml 中配置 cdp_host 和 cdp_port 连接远程浏览器\n"
                        "  3. 安装最新版 Chrome 或 Chromium"
                    ) from e
                # 非连接类异常：不重试，直接抛
                raise
        raise RuntimeError(f"无法启动浏览器: {last_exc}")

    async def _snapshot_local_storage(self, tab) -> dict[str, str]:
        wrapped = await self._eval_json(
            tab,
            """\
            (() => {
                const result = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    if (key !== null) result[key] = localStorage.getItem(key);
                }
                return {items: result};
            })()
            """,
        )
        if wrapped is None:
            raise RuntimeError("无法保存共享浏览器的 localStorage")
        snapshot = wrapped.get("items", wrapped)
        if not isinstance(snapshot, dict):
            raise RuntimeError(  # noqa: TRY004 -- 对外统一为浏览器生命周期失败
                "浏览器返回了无效的 localStorage 快照"
            )
        return {str(key): str(value) for key, value in snapshot.items()}

    async def _restore_local_storage(
        self,
        tab,
        snapshot: dict[str, str],
        *,
        interruptible: bool = True,
    ) -> None:
        encoded = json.dumps(snapshot, ensure_ascii=False)
        await self._evaluate(
            tab,
            f"""\
            (() => {{
                const snapshot = {encoded};
                localStorage.clear();
                for (const [key, value] of Object.entries(snapshot)) {{
                    localStorage.setItem(key, value);
                }}
            }})()
            """,
            interruptible=interruptible,
        )

    async def _inject_auth(self, tab) -> None:
        """向页面注入 localStorage 认证信息。

        :param tab: nodriver Tab
        """
        await self._evaluate(
            tab,
            f"""\
            const user = {json.dumps(self._auth)};
            localStorage.setItem('user', JSON.stringify(user));
        """,
        )

    async def _ensure_captcha_sdk(self, tab) -> None:
        """确保页面已加载腾讯验证码 SDK，轮询等待就绪。

        :param tab: nodriver Tab
        """
        try:
            async with asyncio.timeout(SDK_LOAD_TIMEOUT):
                await self._evaluate(
                    tab,
                    f"""\
                    if (typeof TencentCaptcha === 'undefined') {{
                        const script = document.createElement('script');
                        script.src = '{TCAPTCHA_SDK_URL}';
                        script.async = false;
                        document.head.appendChild(script);
                    }}
                """,
                )
                while True:
                    loaded = await self._evaluate(
                        tab,
                        "(() => typeof TencentCaptcha !== 'undefined')()",
                        return_by_value=True,
                    )
                    if loaded is True:
                        return
                    await self._sleep(0.25)
        except TimeoutError as exc:
            raise RuntimeError(
                f"腾讯验证码 SDK 加载超时（{SDK_LOAD_TIMEOUT:g} 秒）"
            ) from exc

    async def _build_page(self, entry_url: str, headless: bool = False):
        """启动浏览器，注入认证信息，加载 SDK。

        :param entry_url: 入口页面 URL（必须在腾讯验证码的域名白名单内）
        :param headless: 是否以无头模式运行
        :return: (browser, tab) 元组
        :raises: 任何页面操作异常时自动关闭浏览器，避免进程泄漏

        先打开入口 URL 的标准 origin，注入 localStorage 认证后再导航到目标页面。
        """
        await self._ensure_browser_ready()
        self.log.info("正在打开验证码入口页面")
        origin = _origin_from_url(entry_url)
        browser = await self._bound(
            self._create_browser(headless),
            timeout=BROWSER_START_TIMEOUT * 2 + CLOSE_TIMEOUT + 2,
            label="创建验证码浏览器",
        )
        cdp_mode = self.cdp_host is not None and self.cdp_port is not None
        state: dict[str, Any] = {
            "tab": None,
            "storage": None,
            "close_tab": cdp_mode,
            "origin": origin,
        }
        self._browser_states[id(browser)] = state
        try:
            # CDP 模式（连已有 headless-shell/Chrome）：浏览器可能没有默认
            # page target（headless-shell 刚启动时 /json 为空），browser.get()
            # 会因 next(filter(...)) 无 page target 抛异常，必须 new_tab 创建。
            tab = await self._bound(
                browser.get(f"{origin}/", new_tab=cdp_mode),
                timeout=PAGE_LOAD_TIMEOUT,
                label="验证码 origin 页面加载",
            )
            state["tab"] = tab
            state["storage"] = await self._snapshot_local_storage(tab)
            await self._inject_auth(tab)
            self.log.info("正在加载入口页面")
            await self._bound(
                tab.get(entry_url),
                timeout=PAGE_LOAD_TIMEOUT,
                label="验证码入口页面加载",
            )
            await self._ensure_captcha_sdk(tab)
            self.log.info("页面准备完成")
            return browser, tab
        except Exception:
            await self._quit_browser(browser, "页面构建")
            raise

    # ── 验证码触发 / 等待 ──────────────────────────────

    async def _trigger_captcha(self, tab, app_id: str) -> None:
        """调用腾讯验证码 SDK 弹出验证窗口，结果存入 window.__captchaResult。

        :param tab: 浏览器标签页对象
        :param app_id: 腾讯验证码 appId
        """
        await self._evaluate(tab, _SHOW_JS.replace("__APP_ID__", json.dumps(app_id)))

    async def _wait_captcha_result(self, tab, timeout: float = 120.0) -> dict[str, str]:
        """轮询等待验证码回调结果。

        :param tab: 浏览器标签页对象
        :param timeout: 最长等待秒数
        :return: {"randstr": str, "ticket": str}
        :raises RuntimeError: 用户关闭验证码或等待超时

        ret 值含义：0=验证通过，2=用户主动关闭，其他=验证失败。
        """
        try:
            async with asyncio.timeout(timeout):
                while True:
                    res = await self._eval_json(tab, "(() => window.__captchaResult)()")
                    if res is None:
                        await self._sleep(0.3)
                        continue
                    if res.get("ret") == 0 and res.get("ticket") and res.get("randstr"):
                        return {
                            "randstr": str(res["randstr"]),
                            "ticket": str(res["ticket"]),
                        }
                    raise RuntimeError(f"验证码未通过: ret={res.get('ret')}")
        except TimeoutError as exc:
            raise RuntimeError(f"等待验证码回调超时（{timeout:g} 秒）") from exc

    async def _run_captcha(self, tab, app_id: str) -> dict[str, str]:
        """触发验证码并阻塞等待用户手动完成。

        :param tab: 浏览器标签页对象
        :param app_id: 腾讯验证码 appId
        :return: {"randstr": str, "ticket": str}
        :raises RuntimeError: 用户关闭验证码或等待超时
        """
        await self._trigger_captcha(tab, app_id)
        return await self._wait_captcha_result(tab)

    # ── 自动识别 ────────────────────────────────────────

    async def _wait_until(
        self, predicate, timeout: float = 10.0, interval: float = 0.3
    ):
        """轮询等待条件为真。

        :param predicate: 无参异步函数，返回真值时停止等待
        :param timeout: 最长等待秒数
        :param interval: 轮询间隔秒数
        :return: predicate 的最后一次返回值
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_value = None
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            value = await self._bound(
                predicate(),
                timeout=min(CDP_CALL_TIMEOUT, remaining),
                label="验证码状态查询",
            )
            if value:
                return value
            last_value = value
            await self._sleep(interval)
        return last_value

    async def _maybe_state(self, tab):
        """检查验证码图片是否已加载就绪。

        :param tab: 浏览器标签页对象
        :return: 包含 bgUrl/ansUrl/bgRect 等字段的 dict，未就绪返回 None
        """
        s = await self._eval_json(tab, _QUERY_JS)
        if not s:
            return None
        if not (
            s.get("bgUrl", "").startswith("http")
            and s.get("ansUrl", "").startswith("http")
        ):
            return None
        if not s.get("bgRect"):
            return None
        # 弹窗有"淡入"动画：元素刚挂载时 getBoundingClientRect 可能返回
        # 负坐标（在视口外），此时点击坐标全错。视为未就绪，等动画完成。
        br = s["bgRect"]
        if br.get("x", 0) < 0 or br.get("y", 0) < 0:
            return None
        return s

    async def _btn_enabled(self, tab):
        """检查提交按钮是否已启用。

        :param tab: 浏览器标签页对象
        :return: 按钮状态 dict，未启用返回 None
        """
        s = await self._eval_json(tab, _QUERY_JS)
        if not s:
            return None
        if "--disabled" in (s.get("btnCls") or ""):
            return None
        return s

    async def _click_refresh(self, tab) -> None:
        """点击验证码刷新按钮换一组图片。

        :param tab: 浏览器标签页对象
        """
        state = await self._eval_json(tab, _QUERY_JS)
        rect = (state or {}).get("refreshRect")
        if not rect:
            return
        rx = int(rect["x"] + rect["w"] / 2)
        ry = int(rect["y"] + rect["h"] / 2)
        await self._bound(
            tab.mouse_move(rx, ry),
            timeout=CDP_CALL_TIMEOUT,
            label="移动验证码鼠标",
        )
        await self._sleep(0.15)
        await self._bound(
            tab.mouse_click(rx, ry),
            timeout=CDP_CALL_TIMEOUT,
            label="点击验证码刷新按钮",
        )
        await self._sleep(1.5)

    async def _auto_solve_once(
        self, tab, attempt: int, save_debug: bool
    ) -> dict | None:
        """单次自动识别尝试：抓图 → 识别 → 点击 → 提交。

        :param tab: 浏览器标签页对象
        :param attempt: 当前尝试次数 (用于日志和调试图片命名)
        :param save_debug: 是否保存原图和识别可视化到 debug/ 目录
        :return: 验证通过时返回 {"randstr": str, "ticket": str}，失败返回 None
        """
        self.log.info(f"自动识别: 第 {attempt} 次尝试")

        state = await self._wait_until(lambda: self._maybe_state(tab), timeout=12)
        if not state:
            self.log.warning("自动识别: 验证码图片未就绪")
            return None

        try:
            main_img, prompt_img = await self._bound(
                asyncio.gather(
                    asyncio.to_thread(fetch_image, state["bgUrl"]),
                    asyncio.to_thread(fetch_image, state["ansUrl"]),
                ),
                timeout=IMAGE_WORK_TIMEOUT,
                label="下载验证码图片",
            )
        except (OSError, RuntimeError, requests.RequestException) as exc:
            self.log.warning(f"自动识别: 抓图失败 - {exc}")
            return None

        nat_h, nat_w = main_img.shape[:2]
        bg_rect = state["bgRect"]

        ordered, candidates = await self._bound(
            asyncio.to_thread(detect_points, prompt_img, main_img),
            timeout=IMAGE_WORK_TIMEOUT,
            label="验证码图片识别",
        )

        if save_debug:
            stamp = int(time.time() * 1000)
            try:
                await self._bound(
                    asyncio.to_thread(
                        _save_debug_images,
                        self._debug_dir,
                        f"{stamp}_a{attempt}",
                        main_img,
                        prompt_img,
                        ordered,
                        candidates,
                    ),
                    timeout=IMAGE_WORK_TIMEOUT,
                    label="保存验证码调试图片",
                )
            except (OSError, RuntimeError) as exc:
                self.log.warning(f"自动识别: 调试图片保存失败 - {exc}")

        if any(p is None for p in ordered):
            self.log.warning(f"自动识别: 识别有缺失 {ordered}")
            return None

        # 将自然像素坐标转换为视口坐标
        scale_x = bg_rect["w"] / nat_w
        scale_y = bg_rect["h"] / nat_h
        viewport_points = []
        for px, py in ordered:  # type: ignore[misc]
            vx = bg_rect["x"] + px * scale_x
            vy = bg_rect["y"] + py * scale_y
            viewport_points.append((int(vx), int(vy)))

        # 按顺序点击 3 个符号
        for idx, (vx, vy) in enumerate(viewport_points, start=1):
            cx = vx + random.randint(-3, 3)
            cy = vy + random.randint(-3, 3)
            await self._bound(
                tab.mouse_move(cx, cy),
                timeout=CDP_CALL_TIMEOUT,
                label=f"移动到验证码第 {idx} 个目标",
            )
            await self._sleep(0.15 + random.random() * 0.15)
            await self._bound(
                tab.mouse_click(cx, cy),
                timeout=CDP_CALL_TIMEOUT,
                label=f"点击验证码第 {idx} 个目标",
            )
            self.log.info(f"自动识别: 点击 #{idx} at ({cx}, {cy})")
            await self._sleep(0.25 + random.random() * 0.25)

        # 等待提交按钮启用后点击
        enabled_state = await self._wait_until(
            lambda: self._btn_enabled(tab), timeout=3
        )
        if not enabled_state:
            self.log.warning("自动识别: 提交按钮未在时限内启用")
            return None

        final_state = await self._eval_json(tab, _QUERY_JS)
        btn_rect = (final_state or {}).get("btnRect") or state["btnRect"]
        if not btn_rect:
            self.log.warning("自动识别: 找不到提交按钮")
            return None

        bx = int(btn_rect["x"] + btn_rect["w"] / 2)
        by = int(btn_rect["y"] + btn_rect["h"] / 2)
        await self._bound(
            tab.mouse_move(bx, by),
            timeout=CDP_CALL_TIMEOUT,
            label="移动到验证码提交按钮",
        )
        await self._sleep(0.2)
        await self._bound(
            tab.mouse_click(bx, by),
            timeout=CDP_CALL_TIMEOUT,
            label="点击验证码提交按钮",
        )

        # 等待验证码回调
        try:
            return await self._wait_captcha_result(tab, timeout=6)
        except RuntimeError as exc:
            self.log.warning(f"自动识别: {exc}")
            return None

    async def _auto_solve_captcha(
        self, tab, app_id: str, max_retry: int = 10, save_debug: bool = False
    ) -> dict[str, str] | None:
        """尝试自动识别点选验证码，失败时自动刷新重试。

        :param tab: 浏览器标签页对象 (已加载 SDK)
        :param app_id: 腾讯验证码 appId
        :param max_retry: 最大尝试次数 (含首次)
        :param save_debug: 是否保存调试图片到 debug/ 目录
        :return: 验证通过时返回 {"randstr": str, "ticket": str}，全部失败返回 None
        """
        for attempt in range(1, max_retry + 1):
            # 清除上一轮的回调结果，避免 _wait_captcha_result 读到过期值
            await self._evaluate(tab, "window.__captchaResult = null;")

            if attempt == 1:
                await self._trigger_captcha(tab, app_id)
                await self._sleep(2)
            else:
                await self._click_refresh(tab)

            result = await self._auto_solve_once(tab, attempt, save_debug)
            if result:
                return result
        return None

    # ── 公开方法 ────────────────────────────────────────

    async def _quit_browser(self, browser: nodriver.Browser, label: str = "") -> None:
        """恢复共享状态，并只关闭当前流程拥有的标签页和连接。"""
        state = self._browser_states.pop(id(browser), None)

        async def _graceful_close() -> None:
            tab = state.get("tab") if state else None
            snapshot = state.get("storage") if state else None
            if tab is not None and isinstance(snapshot, dict):
                origin = state.get("origin") if state else None
                restore_storage = False
                try:
                    current_origin = await _bounded(
                        self._evaluate(
                            tab,
                            "(() => window.location.origin)()",
                            return_by_value=True,
                            interruptible=False,
                        ),
                        timeout=_close_step_timeout(),
                        label="确认 localStorage origin",
                    )
                    if (
                        isinstance(origin, str)
                        and isinstance(current_origin, str)
                        and current_origin == origin
                    ):
                        restore_storage = True
                    elif isinstance(origin, str) and origin:
                        await _bounded(
                            tab.get(f"{origin}/"),
                            timeout=_close_step_timeout(),
                            label="返回 localStorage origin",
                        )
                        confirmed_origin = await _bounded(
                            self._evaluate(
                                tab,
                                "(() => window.location.origin)()",
                                return_by_value=True,
                                interruptible=False,
                            ),
                            timeout=_close_step_timeout(),
                            label="再次确认 localStorage origin",
                        )
                        restore_storage = (
                            isinstance(confirmed_origin, str)
                            and confirmed_origin == origin
                        )
                        if not restore_storage:
                            self.log.warning(
                                "返回 localStorage origin 后校验不匹配，跳过恢复"
                            )
                except Exception as exc:  # noqa: BLE001 -- 仍需尝试原地恢复
                    self.log.warning(
                        f"确认浏览器 localStorage origin 失败，跳过恢复: {exc}"
                    )
                if restore_storage:
                    try:
                        await _bounded(
                            self._restore_local_storage(
                                tab,
                                snapshot,
                                interruptible=False,
                            ),
                            timeout=_close_step_timeout(),
                            label="恢复浏览器 localStorage",
                        )
                    except Exception as exc:  # noqa: BLE001 -- 清理必须继续
                        self.log.warning(f"恢复浏览器 localStorage 失败: {exc}")
            if tab is not None and state and state.get("close_tab"):
                with suppress(Exception):
                    await _bounded(
                        tab.close(),
                        timeout=_close_step_timeout(),
                        label="关闭验证码标签页",
                    )
            await _close_browser_instance(browser)

        try:
            await _bounded(
                _graceful_close(),
                timeout=CLOSE_TIMEOUT,
                label="验证码浏览器关闭流程",
            )
            if label:
                self.log.info(f"已关闭浏览器 ({label})")
        except Exception as exc:  # noqa: BLE001 -- nodriver 停止浏览器可能抛任意异常
            # 即使 websocket 不响应，也立即终止本次创建的本地进程并注销实例。
            from nodriver.core import util as _nd_util

            tab = state.get("tab") if state else None
            if tab is not None and state and state.get("close_tab"):
                with suppress(Exception):
                    await _bounded(
                        tab.close(),
                        timeout=_close_step_timeout(),
                        label="强制关闭验证码标签页",
                    )
            with suppress(Exception):
                await _bounded(
                    browser.aclose(),
                    timeout=_close_step_timeout(),
                    label="强制关闭浏览器连接",
                )
            proc = getattr(browser, "_process", None)
            if proc is not None and getattr(proc, "returncode", None) is None:
                with suppress(ProcessLookupError, OSError):
                    proc.kill()
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                with suppress(Exception):
                    transport.close()
            _nd_util.get_registered_instances().discard(browser)
            await _remove_temporary_profile(browser)
            if label:
                self.log.warning(f"关闭浏览器异常 ({label}): {exc}")

    def handle_exam_captcha(self, user_exam_plan_id: str) -> dict[str, str]:
        """处理考试前的无感验证码（同步版本）。

        无感模式：验证码在后台自动完成，无需用户交互，因此使用 headless=True。

        :param user_exam_plan_id: 考试计划 ID（预留，目前未使用）
        :return: {"randstr": str, "ticket": str} — 验证通过后的凭证
        :raises RuntimeError: 已在事件循环中调用时，使用 handle_exam_captcha_async 代替
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.handle_exam_captcha_async(user_exam_plan_id))
        raise RuntimeError(
            "handle_exam_captcha() 无法在已运行的事件循环中调用，请改用 handle_exam_captcha_async()"
        )

    async def handle_exam_captcha_async(self, user_exam_plan_id: str) -> dict[str, str]:
        """处理考试前的无感验证码（异步版本）。"""
        self._raise_if_stopped()
        await self._ensure_browser_ready()
        async with self._exclusive_endpoint():
            try:
                async with asyncio.timeout(EXAM_FLOW_TIMEOUT):
                    return await self._handle_exam_captcha_flow(user_exam_plan_id)
            except TimeoutError as exc:
                raise RuntimeError(
                    f"无感验证码处理超时（{EXAM_FLOW_TIMEOUT:g} 秒）"
                ) from exc

    async def _handle_exam_captcha_flow(self, user_exam_plan_id: str) -> dict[str, str]:
        """已取得端点互斥锁的考试验证码流程。"""
        del user_exam_plan_id
        self.log.info("正在处理无感验证码")
        browser, tab = await self._build_page(EXAM_ENTRY_URL, headless=True)
        try:
            result = await self._run_captcha(tab, EXAM_CAPTCHA_APP_ID)
            self.log.success("已获取无感验证码")
            return result
        finally:
            await self._quit_browser(browser, "无感验证码")

    def handle_course_captcha(self, course_url: str | None = None) -> dict[str, str]:
        """处理课程完成时的图片点选验证码（同步版本）。

        流程：先以无头模式自动识别（最多 3 轮 x 6 次，连接异常会重建页面），
        全部失败后再打开可见浏览器让用户手动完成。

        :param course_url: 课程入口 URL，留空则使用默认的 mcwk.mycourse.cn
        :return: {"randstr": str, "ticket": str} — 验证通过后的凭证
        :raises RuntimeError: 已在事件循环中调用时，使用 handle_course_captcha_async 代替
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.handle_course_captcha_async(course_url))
        raise RuntimeError(
            "handle_course_captcha() 无法在已运行的事件循环中调用，请改用 handle_course_captcha_async()"
        )

    async def handle_course_captcha_async(
        self, course_url: str | None = None
    ) -> dict[str, str]:
        """处理课程完成时的图片点选验证码（异步版本）。

        自动识别阶段最多 3 轮、每轮 6 次；浏览器连接异常会重建无头页面继续，
        全部失败后才转手动。
        """
        self._raise_if_stopped()
        await self._ensure_browser_ready()
        async with self._exclusive_endpoint():
            try:
                async with asyncio.timeout(COURSE_FLOW_TIMEOUT):
                    return await self._handle_course_captcha_flow(course_url)
            except TimeoutError as exc:
                raise RuntimeError(
                    f"课程验证码处理超时（{COURSE_FLOW_TIMEOUT:g} 秒）"
                ) from exc

    async def _handle_course_captcha_flow(
        self, course_url: str | None = None
    ) -> dict[str, str]:
        """已取得端点互斥锁的课程验证码流程。"""
        entry_url = course_url or COURSE_ENTRY_URL
        # 自动识别重试上限（环境变量可调，小核服务器每次尝试很慢）：
        # WB_CAPTCHA_ROUNDS 轮数、WB_CAPTCHA_ATTEMPTS 每轮次数。
        # 默认 2 轮 x 3 次（最多 6 次尝试）——实测单次尝试在 1 核机器约
        # 4 分钟，18 次全试可能 1 小时+；识别成功率高时 1-2 次即过，
        # 失败应尽快跳过该课程而不是无限重试。
        max_auto_rounds = _env_positive_int("WB_CAPTCHA_ROUNDS", 2)
        attempts_per_round = _env_positive_int("WB_CAPTCHA_ATTEMPTS", 3)

        # 第一阶段: 无头自动识别
        self.log.info("正在自动识别验证码...")
        last_exc: Exception | None = None
        for round_no in range(1, max_auto_rounds + 1):
            if round_no > 1:
                self.log.warning(
                    f"自动识别未完成，重建无头浏览器重试（第 {round_no}/{max_auto_rounds} 轮）"
                )
                await self._sleep(1)
            try:
                browser, tab = await self._build_page(entry_url, headless=True)
            except InterruptedError:
                raise
            except Exception as exc:  # noqa: BLE001 -- 浏览器启动失败也继续下一轮
                last_exc = exc
                self.log.warning(
                    f"自动识别浏览器启动失败（第 {round_no}/{max_auto_rounds} 轮）: {exc}"
                )
                continue
            try:
                result = await self._auto_solve_captcha(
                    tab,
                    COURSE_CAPTCHA_APP_ID,
                    max_retry=attempts_per_round,
                )
                if result:
                    self.log.success("验证码自动识别成功")
                    return result
            except InterruptedError:
                raise
            except Exception as exc:  # noqa: BLE001 -- 连接/页面异常后重建重试
                last_exc = exc
                self.log.warning(
                    f"自动识别异常（第 {round_no}/{max_auto_rounds} 轮）: {exc}"
                )
            finally:
                await self._quit_browser(browser, "自动识别")
        if last_exc is not None:
            self.log.warning(f"自动识别曾发生异常，将回退到手动: {last_exc}")
        await self._sleep(1)  # 等待无头浏览器进程完全退出

        # 无交互模式（Docker 等无终端环境）：不打开可见浏览器等待手动，
        # 直接抛异常让上层跳过该课程
        if self.non_interactive:
            raise RuntimeError(
                "验证码自动识别连续失败且处于无交互模式，无法手动完成验证，已跳过该课程"
            )

        # 第二阶段: 打开可见浏览器，让用户手动完成
        self.log.info("=" * 50)
        self.log.warning("自动识别失败，请手动完成验证码！")
        self.log.info("请在浏览器窗口中完成图片点选验证，完成后程序将自动继续")
        self.log.info("=" * 50)
        browser, tab = await self._build_page(entry_url, headless=False)
        try:
            result = await self._run_captcha(tab, COURSE_CAPTCHA_APP_ID)
            self.log.success("验证码手动验证完成")
            return result
        finally:
            await self._quit_browser(browser, "手动验证")

    def close(self) -> None:
        """清除敏感认证信息；每次流程持有的浏览器均在流程 finally 中关闭。"""

        if self._closed:
            return
        if self._browser_states:
            self.log.warning("关闭验证码处理器时仍存在未完成的浏览器流程")
        self._auth.clear()
        self._closed = True
