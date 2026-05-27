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
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import requests
import nodriver
from PIL import Image

# 腾讯验证码 SDK 地址
TCAPTCHA_SDK_URL = "https://turing.captcha.qcloud.com/TJCaptcha.js"

# 验证码 appId
EXAM_CAPTCHA_APP_ID = "190330343"    # 无感验证码（考试）
COURSE_CAPTCHA_APP_ID = "195119536"  # 图片点选验证码（课程完成）

# 默认入口页面
EXAM_ENTRY_URL = "https://weiban.mycourse.cn/#/course"
COURSE_ENTRY_URL = "https://mcwk.mycourse.cn/"

# ── JS 片段（自动识别用）──────────────────────────────

_SHOW_JS = r"""
    return (function(){
      window.__captchaResult = null;
      try {
        const captcha = new TencentCaptcha(__APP_ID__, function(res){
            window.__captchaResult = res;
        }, { userLanguage: 'zh-cn' });
        captcha.show();
        window.__captcha = captcha;
        return 'ok';
      } catch(e) { return 'ERROR: ' + String(e); }
    })();
"""

_QUERY_JS = r"""
    return (function(){
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


def normalize_mask(binary_mask: np.ndarray, canvas_size: int = 48,
                   symbol_size: int = 34) -> Optional[np.ndarray]:
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
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    resized = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_NEAREST)

    canvas = np.zeros((canvas_size, canvas_size), dtype=np.uint8)
    oy = (canvas_size - new_h) // 2
    ox = (canvas_size - new_w) // 2
    canvas[oy:oy + new_h, ox:ox + new_w] = resized
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
    return cv2.warpAffine(mask, matrix, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)


def crop_foreground(mask: np.ndarray) -> Optional[np.ndarray]:
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


def match_cost(query: np.ndarray, candidate: np.ndarray,
               allow_rotate: bool = True) -> float:
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

    for angle in (-60, -45, -30, -20, -10, 10, 20, 30, 45, 60, 90, -90):
        rotated = rotate_mask(query, angle)
        score = float(np.sum(cv2.absdiff(rotated, candidate)) / 255.0)
        if score < best:
            best = score
    return best


def locate_with_template(query_mask: np.ndarray,
                         main_mask: np.ndarray) -> Tuple[float, Optional[Tuple[int, int]]]:
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
    scales = np.linspace(1.1, 3.4, 16)
    angles = range(-90, 91, 10)

    for scale in scales:
        new_w = max(8, int(round(qw * scale)))
        new_h = max(8, int(round(qh * scale)))
        base = cv2.resize(query_crop, (new_w, new_h), interpolation=cv2.INTER_NEAREST)

        for angle in angles:
            rotated = rotate_mask(base, angle)
            rotated = crop_foreground(rotated)
            if rotated is None:
                continue
            if rotated.shape[0] >= main_mask.shape[0] or rotated.shape[1] >= main_mask.shape[1]:
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


def _extract_query_templates(prompt_img: np.ndarray) -> Tuple[List[Optional[np.ndarray]], List[np.ndarray]]:
    """从提示图 (顶部灰色 3 格题目条) 提取 3 个模板和原始 mask。

    :param prompt_img: BGR 格式的提示图 (包含顶部灰色条和下方箭头等)
    :return: (query_templates, query_raw_masks)
        - query_templates: 长度 3 的列表，每个元素是 48x48 归一化模板或 None
        - query_raw_masks: 长度 3 的列表，每个元素是原始二值 mask (用于精匹配)
    """
    top_gray = cv2.cvtColor(prompt_img, cv2.COLOR_BGR2GRAY)
    gray_mask = ((top_gray > 110) & (top_gray < 220)).astype(np.uint8) * 255
    gray_mask = cv2.morphologyEx(gray_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(gray_mask, connectivity=8)
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
    strip_roi = prompt_img[sy:sy + sh, sx:sx + sw]
    strip_roi_gray = cv2.cvtColor(strip_roi, cv2.COLOR_BGR2GRAY)
    query_cells = np.array_split(strip_roi_gray, 3, axis=1)

    query_templates: List[Optional[np.ndarray]] = []
    query_raw_masks: List[np.ndarray] = []
    for cell in query_cells:
        _, cell_bw = cv2.threshold(cell, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        cell_bw[:2, :] = 0
        cell_bw[-2:, :] = 0
        cell_bw[:, :2] = 0
        cell_bw[:, -2:] = 0
        cell_bw = cv2.morphologyEx(cell_bw, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        query_raw_masks.append(cell_bw)
        query_templates.append(normalize_mask(cell_bw))

    return query_templates, query_raw_masks


def _extract_main_candidates(main_img: np.ndarray) -> Tuple[List[dict], np.ndarray]:
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

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(symbol_bw, connectivity=8)
    candidates: List[dict] = []
    for i in range(1, num_labels):
        x, y, w, h, area = stats[i]
        if area < 150 or area > 6000:
            continue
        if w < 20 or h < 20:
            continue
        if w / max(h, 1) > 3.0 or h / max(w, 1) > 3.0:
            continue

        component_mask = np.where(labels[y:y + h, x:x + w] == i, 255, 0).astype(np.uint8)
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


def detect_points(prompt_img: np.ndarray,
                  main_img: np.ndarray) -> Tuple[List[Optional[Tuple[int, int]]], List[dict]]:
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
    ordered_points: List[Optional[Tuple[int, int]]] = []
    base_scores: List[float] = []

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
            distance = ((point[0] - new_point[0]) ** 2 + (point[1] - new_point[1]) ** 2) ** 0.5
            if distance < 26:
                too_close = True
                break
        if too_close:
            continue
        if old_point is not None and base_scores[i] < 340:
            continue

        ordered_points[i] = new_point

    return ordered_points, candidates


def render_debug(main_img: np.ndarray,
                 ordered_points: List[Optional[Tuple[int, int]]],
                 candidates: List[dict]) -> np.ndarray:
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
        cv2.putText(vis, str(idx), (point[0] - 6, point[1] + 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    return vis


def fetch_image(url: str) -> np.ndarray:
    """通过 HTTP 下载验证码图片并解码为 BGR 数组。

    :param url: 图片 URL (腾讯验证码 CDN 地址)
    :return: BGR 格式的 numpy 数组
    :raises RuntimeError: 图片无法解码时
    :raises requests.HTTPError: HTTP 请求失败时
    """
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
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

    _ocr: Any = None       # cv2.dnn.Net 或 False (不可用)
    _initialized: bool = False
    _lock = threading.Lock()
    _charset = "0123456789abcdefghijklmnopqrstuvwxyz"
    _idx_to_char = {i: c for c, i in {c: i for i, c in enumerate(_charset)}.items()}
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
                        exe_path = os.environ.get("PYFUZE_EXECUTABLE_PATH")
                        if exe_path:
                            base_path = os.path.dirname(os.path.abspath(exe_path))
                            model_path = Path(base_path) / "captcha_model.onnx"
                        else:
                            model_path = Path(__file__).parent / "captcha_model.onnx"

                        if not model_path.exists():
                            log.warning(f"验证码模型文件不存在: {model_path}")
                            cls._ocr = False
                        else:
                            cls._ocr = cv2.dnn.readNetFromONNX(str(model_path))
                    except Exception:
                        log.warning("OpenCV DNN 初始化失败，自动验证码识别功能将不可用")
                        cls._ocr = False
                    cls._initialized = True
        return cls._ocr if cls._ocr is not False else None

    @classmethod
    def recognize(cls, image: bytes, log) -> Optional[str]:
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
            img = Image.open(BytesIO(image)).convert("L")
            arr = np.array(img, dtype=np.uint8)
            h, w = arr.shape
            seg_w = w // 4

            result = []
            for i in range(4):
                char_img = arr[:, i * seg_w:(i + 1) * seg_w if i < 3 else w]
                resized = np.array(Image.fromarray(char_img).resize(
                    (cls._char_size, cls._char_size), Image.BILINEAR))
                inp = (resized.astype(np.float32) / 255.0).reshape(
                    1, 1, cls._char_size, cls._char_size)
                with cls._lock:
                    ocr.setInput(inp)
                    out = ocr.forward()
                result.append(cls._idx_to_char[int(out[0].argmax())])

            code = "".join(result)
            log.info(f"自动验证码识别结果: {code}")
            if len(code) == 4:
                return code
            log.warning("验证码识别结果长度不正确，正在重试")
        except Exception as e:
            log.error(f"验证码识别异常: {e}")
        return None


# ── 浏览器自动检测 ──────────────────────────────────────


def detect_browser() -> Optional[str]:
    """自动检测系统中已安装的 Chrome/Chromium 浏览器路径。

    检查顺序：环境变量 CHROMIUM_BINARY / CHROME_BINARY → 平台默认路径
    """
    # 环境变量优先（Docker 等场景）
    for env_var in ("CHROMIUM_BINARY", "CHROME_BINARY"):
        path = os.environ.get(env_var, "")
        if path and os.path.isfile(path):
            return path

    system = platform.system()
    candidates: list[str] = []
    if system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    elif system == "Linux":
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("PROGRAMFILES", "")
        pf86 = os.environ.get("PROGRAMFILES(X86)", "")
        candidates = [
            *[str(Path(p) / "Google/Chrome/Application/chrome.exe")
              for p in (local, pf, pf86) if p],
            *[str(Path(p) / "Chromium/Application/chrome.exe")
              for p in (local, pf, pf86) if p],
        ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


# ── CaptchaHandler ────────────────────────────────────

class CaptchaHandler:
    """通过浏览器处理腾讯验证码"""

    def __init__(self, tenant_code: str, user_id: str, token: str, log,
                 browser_path: Optional[str] = None,
                 debug_dir: Optional[Path] = None) -> None:
        """初始化验证码处理器。

        :param tenant_code: 租户编码
        :param user_id: 用户 ID
        :param token: 认证令牌
        :param log: 日志记录器（需支持 info/warning/success 方法）
        :param browser_path: 浏览器可执行文件路径，留空则自动查找
        :param debug_dir: 调试图片保存目录，留空则默认 logs/<user_id>/captcha
        """
        self._auth = {
            "userId": user_id,
            "token": token,
            "tenantCode": tenant_code,
        }
        self.log = log
        self.browser_path = browser_path
        self._debug_dir = debug_dir or Path("logs") / user_id / "captcha"

    # ── 浏览器 / 页面构建 ──────────────────────────────

    async def _create_browser(self, headless: bool = False) -> nodriver.Browser:
        """创建 nodriver 浏览器实例。

        :param headless: True 时以无头模式运行（无需用户交互）
        :return: 已配置的 Browser 对象

        窗口尺寸 428x818 模拟移动端以匹配腾讯验证码的移动版 UI。
        """
        browser_path = self.browser_path or detect_browser()
        browser_args = ["--window-size=428,818", "--mute-audio"]
        return await nodriver.start(
            headless=headless,
            browser_executable_path=browser_path or None,
            browser_args=browser_args,
            sandbox=False,
        )

    async def _inject_auth(self, tab) -> None:
        """向页面注入 localStorage 认证信息。

        :param tab: nodriver Tab

        .. note:: json.dumps 对含特殊字符的 token 做安全编码，
           避免 JS 代码注入（例如 token 中出现引号或反斜杠时）。
        """
        await tab.evaluate(f"""\
            const user = {json.dumps(self._auth)};
            localStorage.setItem('user', JSON.stringify(user));
        """)

    async def _ensure_captcha_sdk(self, tab) -> None:
        """确保页面已加载腾讯验证码 SDK。

        :param tab: nodriver Tab
        """
        await tab.evaluate(f"""\
            if (typeof TencentCaptcha === 'undefined') {{
                const script = document.createElement('script');
                script.src = '{TCAPTCHA_SDK_URL}';
                script.async = false;
                document.head.appendChild(script);
            }}
        """)

    async def _build_page(self, entry_url: str, headless: bool = False):
        """启动浏览器，注入认证信息，加载 SDK。

        :param entry_url: 入口页面 URL（必须在腾讯验证码的域名白名单内）
        :param headless: 是否以无头模式运行
        :return: (browser, tab) 元组
        :raises: 任何页面操作异常时自动关闭浏览器，避免进程泄漏

        页面加载两次：第一次建立域名（localStorage 按域名隔离）；
        注入认证后重新加载，使页面能读取到 localStorage 中的登录态。
        """
        self.log.info("正在打开验证码入口页面")
        browser = await self._create_browser(headless)
        try:
            tab = await browser.get(entry_url)                 # 第一次：建立域名
            await self._inject_auth(tab)
            await tab.get(entry_url)                           # 第二次：读取注入的认证
            await tab.sleep(3)
            await self._ensure_captcha_sdk(tab)
            await tab.sleep(2)
            return browser, tab
        except Exception:
            browser.stop()
            raise

    # ── 验证码触发 / 等待 ──────────────────────────────

    async def _trigger_captcha(self, tab, app_id: str) -> None:
        """调用腾讯验证码 SDK 弹出验证窗口，结果存入 window.__captchaResult。

        :param tab: 浏览器标签页对象
        :param app_id: 腾讯验证码 appId
        """
        await tab.evaluate(_SHOW_JS.replace("__APP_ID__", json.dumps(app_id)))

    async def _wait_captcha_result(self, tab, timeout: float = 120.0) -> Dict[str, str]:
        """轮询等待验证码回调结果。

        :param tab: 浏览器标签页对象
        :param timeout: 最长等待秒数
        :return: {"randstr": str, "ticket": str}
        :raises RuntimeError: 用户关闭验证码或等待超时

        ret 值含义：0=验证通过，2=用户主动关闭，其他=验证失败。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = await tab.evaluate("return window.__captchaResult;", return_by_value=True)
            if res:
                if isinstance(res, dict) and res.get("ret") == 0 and res.get("ticket"):
                    return {"randstr": res["randstr"], "ticket": res["ticket"]}
                raise RuntimeError(f"验证码未通过: ret={res.get('ret') if isinstance(res, dict) else res}")
            await asyncio.sleep(0.3)
        raise RuntimeError("等待验证码回调超时")

    async def _run_captcha(self, tab, app_id: str) -> Dict[str, str]:
        """触发验证码并阻塞等待用户手动完成。

        :param tab: 浏览器标签页对象
        :param app_id: 腾讯验证码 appId
        :return: {"randstr": str, "ticket": str}
        :raises RuntimeError: 用户关闭验证码或等待超时
        """
        await self._trigger_captcha(tab, app_id)
        return await self._wait_captcha_result(tab)

    # ── 自动识别 ────────────────────────────────────────

    @staticmethod
    async def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.3):
        """轮询等待条件为真。

        :param predicate: 无参异步函数，返回真值时停止等待
        :param timeout: 最长等待秒数
        :param interval: 轮询间隔秒数
        :return: predicate 的最后一次返回值
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(interval)
        return await predicate()

    @staticmethod
    async def _maybe_state(tab):
        """检查验证码图片是否已加载就绪。

        :param tab: 浏览器标签页对象
        :return: 包含 bgUrl/ansUrl/bgRect 等字段的 dict，未就绪返回 None
        """
        s = await tab.evaluate(_QUERY_JS, return_by_value=True)
        if not s:
            return None
        if not (s.get("bgUrl", "").startswith("http") and s.get("ansUrl", "").startswith("http")):
            return None
        if not s.get("bgRect"):
            return None
        return s

    @staticmethod
    async def _btn_enabled(tab):
        """检查提交按钮是否已启用。

        :param tab: 浏览器标签页对象
        :return: 按钮状态 dict，未启用返回 None
        """
        s = await tab.evaluate(_QUERY_JS, return_by_value=True)
        if not s:
            return None
        if "--disabled" in (s.get("btnCls") or ""):
            return None
        return s

    @staticmethod
    async def _click_refresh(tab) -> None:
        """点击验证码刷新按钮换一组图片。

        :param tab: 浏览器标签页对象
        """
        state = await tab.evaluate(_QUERY_JS, return_by_value=True)
        rect = (state or {}).get("refreshRect")
        if not rect:
            return
        rx = int(rect["x"] + rect["w"] / 2)
        ry = int(rect["y"] + rect["h"] / 2)
        await tab.mouse_move(rx, ry)
        await asyncio.sleep(0.15)
        await tab.mouse_click(rx, ry)
        await asyncio.sleep(1.5)

    async def _auto_solve_once(self, tab, attempt: int, save_debug: bool) -> Optional[Dict]:
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
            main_img = fetch_image(state["bgUrl"])
            prompt_img = fetch_image(state["ansUrl"])
        except Exception as exc:
            self.log.warning(f"自动识别: 抓图失败 - {exc}")
            return None

        nat_h, nat_w = main_img.shape[:2]
        bg_rect = state["bgRect"]

        ordered, candidates = detect_points(prompt_img, main_img)

        if save_debug:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = int(time.time() * 1000)
            cv2.imwrite(str(self._debug_dir / f"{stamp}_a{attempt}_main.png"), main_img)
            cv2.imwrite(str(self._debug_dir / f"{stamp}_a{attempt}_prompt.png"), prompt_img)
            cv2.imwrite(str(self._debug_dir / f"{stamp}_a{attempt}_debug.png"),
                        render_debug(main_img, ordered, candidates))

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
            await tab.mouse_move(cx, cy)
            await asyncio.sleep(0.15 + random.random() * 0.15)
            await tab.mouse_click(cx, cy)
            self.log.info(f"自动识别: 点击 #{idx} at ({cx}, {cy})")
            await asyncio.sleep(0.25 + random.random() * 0.25)

        # 等待提交按钮启用后点击
        await self._wait_until(lambda: self._btn_enabled(tab), timeout=3)

        final_state = await tab.evaluate(_QUERY_JS, return_by_value=True)
        btn_rect = (final_state or {}).get("btnRect") or state["btnRect"]
        if not btn_rect:
            self.log.warning("自动识别: 找不到提交按钮")
            return None

        bx = int(btn_rect["x"] + btn_rect["w"] / 2)
        by = int(btn_rect["y"] + btn_rect["h"] / 2)
        await tab.mouse_move(bx, by)
        await asyncio.sleep(0.2)
        await tab.mouse_click(bx, by)

        # 等待验证码回调
        try:
            return await self._wait_captcha_result(tab, timeout=6)
        except RuntimeError as exc:
            self.log.warning(f"自动识别: {exc}")
            return None

    async def _auto_solve_captcha(self, tab, app_id: str, max_retry: int = 10,
                            save_debug: bool = False) -> Optional[Dict[str, str]]:
        """尝试自动识别点选验证码，失败时自动刷新重试。

        :param tab: 浏览器标签页对象 (已加载 SDK)
        :param app_id: 腾讯验证码 appId
        :param max_retry: 最大尝试次数 (含首次)
        :param save_debug: 是否保存调试图片到 debug/ 目录
        :return: 验证通过时返回 {"randstr": str, "ticket": str}，全部失败返回 None
        """
        for attempt in range(1, max_retry + 1):
            # 清除上一轮的回调结果，避免 _wait_captcha_result 读到过期值
            await tab.evaluate("window.__captchaResult = null;")

            if attempt == 1:
                await self._trigger_captcha(tab, app_id)
                await asyncio.sleep(2)
            else:
                await self._click_refresh(tab)

            result = await self._auto_solve_once(tab, attempt, save_debug)
            if result:
                return result
        return None

    # ── 公开方法 ────────────────────────────────────────

    def _quit_browser(self, browser: nodriver.Browser, label: str = "") -> None:
        """安全关闭浏览器，捕获退出异常避免掩盖原始错误。

        :param browser: nodriver Browser 实例
        :param label: 日志标签 (如 "无感验证码"、"自动识别"、"手动验证")
        """
        try:
            browser.stop()
            if label:
                self.log.info(f"已关闭浏览器 ({label})")
        except Exception as exc:
            if label:
                self.log.warning(f"关闭浏览器异常 ({label}): {exc}")

    def handle_exam_captcha(self, user_exam_plan_id: str) -> Dict[str, str]:
        """处理考试前的无感验证码。

        无感模式：验证码在后台自动完成，无需用户交互，因此使用 headless=True。

        :param user_exam_plan_id: 考试计划 ID（预留，目前未使用）
        :return: {"randstr": str, "ticket": str} — 验证通过后的凭证
        """
        return asyncio.run(self._handle_exam_captcha(user_exam_plan_id))

    async def _handle_exam_captcha(self, user_exam_plan_id: str) -> Dict[str, str]:
        self.log.info("正在处理无感验证码")
        browser, tab = await self._build_page(EXAM_ENTRY_URL, headless=True)
        try:
            result = await self._run_captcha(tab, EXAM_CAPTCHA_APP_ID)
            self.log.success("已获取无感验证码")
            return result
        finally:
            self._quit_browser(browser, "无感验证码")

    def handle_course_captcha(self, course_url: Optional[str] = None) -> Dict[str, str]:
        """处理课程完成时的图片点选验证码。

        流程：先以无头模式自动识别 (10 次重试)，全部失败后再打开可见浏览器让用户手动完成。

        :param course_url: 课程入口 URL，留空则使用默认的 mcwk.mycourse.cn
        :return: {"randstr": str, "ticket": str} — 验证通过后的凭证
        """
        return asyncio.run(self._handle_course_captcha(course_url))

    async def _handle_course_captcha(self, course_url: Optional[str] = None) -> Dict[str, str]:
        entry_url = course_url or COURSE_ENTRY_URL

        # 第一阶段: 无头自动识别
        self.log.info("正在自动识别验证码...")
        browser, tab = await self._build_page(entry_url, headless=True)
        try:
            result = await self._auto_solve_captcha(tab, COURSE_CAPTCHA_APP_ID)
            if result:
                self.log.success("验证码自动识别成功")
                return result
        except Exception as exc:
            self.log.warning(f"自动识别异常，将回退到手动: {exc}")
        finally:
            self._quit_browser(browser, "自动识别")
        await asyncio.sleep(1)  # 等待无头浏览器进程完全退出

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
            self._quit_browser(browser, "手动验证")
