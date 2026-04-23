"""
captcha.py —— 验证码识别与处理模块

功能分区：
  1. 调试工具
     set_debug / get_debug_enabled / set_debug_account / _get_debug_dir / _debug_save
     控制是否将验证码截图保存到 logs/{account}/ 目录，便于排查识别失败问题。

  2. 文字图片验证码（ddddocr）
     _get_ocr / _debug_save_ocr / _ocr_captcha_with_retry
     懒加载 ddddocr 单例，对登录页的字母/数字图形验证码进行 OCR 识别，支持自动重试。

  3. 图像处理工具
     normalize_mask / rotate_mask / crop_foreground / match_cost / locate_with_template /
     _binarize_main / _extract_candidates / _merge_nearby_candidates
     基于 OpenCV 的图像预处理与模板匹配，用于定位点选验证码中各汉字的坐标。

  4. 点选验证码主流程
     detect_captcha / _captcha_visible / _find_captcha_context / _find_captcha_frame /
     has_captcha / handle_click_captcha
     检测页面是否存在腾讯点选验证码，并自动完成图像识别与点击操作。

  5. 辅助工具
     _derive_main_url / _get_main_render_size / _fetch_frame_bg_image /
     _fetch_element_image / _click_captcha_point
     负责从页面/iframe 中提取验证码背景图、前景字图，以及模拟点击验证码坐标。
"""

import os
import re
import time
import random
import threading
import urllib.request
import cv2
import numpy as np
from typing import Optional, Tuple, List

# 调试模式：开启后将把验证码截图保存到 logs/ 目录
_DEBUG_SAVE = False
_DEBUG_LOG_DIR = "logs"

# 线程本地存储，用于记录当前账号名（用于分目录保存）
_thread_local = threading.local()

# ---------------------------------------------------------------------------
# DOM 元素选择器常量定义
# ---------------------------------------------------------------------------
_SEL_CAPTCHA_BG = (
    ".tencent-captcha-dy__verify-bg-img, .tencent-captcha-dy__verify-bg, "
    ".tencent-captcha-dy__verify-img-area, .tencent-captcha-dy__verify, "
    ".WPA3-SELECT-BG"
)
_SEL_CAPTCHA_PROMPT = (
    ".tencent-captcha-dy__header-answer img, .tencent-captcha-dy__header-answer, "
    ".WPA3-SELECT-HINT"
)
_SEL_CAPTCHA_CONFIRM_BTN = (
    ".tencent-captcha-dy__verify-confirm-btn:not("
    ".tencent-captcha-dy__verify-confirm-btn--disabled), "
    ".tencent-captcha-dy__verify-confirm-btn"
)
_SEL_CAPTCHA_ERROR_TIP = (
    ".tencent-captcha-dy__verify-error-text, .tencent-captcha-dy__verify-error-tip"
)
_SEL_CAPTCHA_REFRESH_BTN = ".tencent-captcha-dy__header-refresh, .tencent-captcha-dy__verify-refresh, #tCaptchaDyRefresh"
_SEL_CAPTCHA_VISIBILITY_MARKERS = (
    "iframe[src*='captcha.qq.com']",
    "iframe[id*='tcaptcha']",
    ".tcaptcha-transform",  # 腾讯验证码外层容器
    ".t-mask, .t-captcha-mask",  # 遮罩层特征
    ".tencent-captcha-dy__verify-bg-img",
    "#tCaptchaDyContent",
    ".WPA3-SELECT-PANEL",
)


def set_debug(enabled: bool, log_dir: str = "logs") -> None:
    """由外部配置调用，开启/关闭验证码截图调试保存。"""
    global _DEBUG_SAVE, _DEBUG_LOG_DIR
    _DEBUG_SAVE = enabled
    _DEBUG_LOG_DIR = log_dir


def get_debug_enabled() -> bool:
    """返回当前调试模式状态（供其他模块实时查询，避免值拷贝过期问题）。"""
    return _DEBUG_SAVE


def set_debug_account(account: str) -> None:
    """设置当前线程的账号名，用于分目录保存调试截图。"""
    _thread_local.account = account


def _get_debug_dir() -> str:
    """返回当前线程对应的调试截图/日志保存目录（logs/{account}/）。"""
    account = getattr(_thread_local, "account", "") or "unknown"
    # 账号名中可能含特殊字符，做简单清理
    safe_account = re.sub(r'[\\/:*?"<>|]', "_", account)
    return os.path.join(_DEBUG_LOG_DIR, safe_account)


def _debug_save(name: str, img: np.ndarray) -> None:
    """调试模式下将图像保存为 PNG 文件，文件名带毫秒时间戳。"""
    if not _DEBUG_SAVE:
        return
    try:
        d = _get_debug_dir()
        os.makedirs(d, exist_ok=True)
        ts = int(time.time() * 1000)
        path = os.path.join(d, f"{name}_{ts}.png")
        cv2.imwrite(path, img)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 文字图片验证码（ddddocr）相关函数
# ---------------------------------------------------------------------------

_ocr_instance = None
_ocr_lock = threading.Lock()


def _get_ocr():
    """懒加载 ddddocr 单例，仅在首次使用时初始化，线程安全。"""
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_lock:
            if _ocr_instance is None:
                import ddddocr

                _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance


def _debug_save_ocr(label: str, img_bytes: bytes, code: str) -> None:
    """调试模式下将文字验证码截图保存到 logs/{account}/ 目录。"""
    if not get_debug_enabled():
        return
    try:
        d = _get_debug_dir()
        os.makedirs(d, exist_ok=True)
        ts = int(time.time() * 1000)
        path = os.path.join(d, f"{label}_{ts}_{code}.png")
        with open(path, "wb") as f:
            f.write(img_bytes)
    except Exception:
        pass


def _ocr_captcha_with_retry(capt_img, ocr, log, max_retries: int = 3) -> Optional[str]:
    """识别文字图片验证码（ddddocr），刷新后等待图片 src 变化再截图，最多重试 max_retries 次。

    参数：
        capt_img   - Playwright Locator，指向验证码 <img> 元素
        ocr        - ddddocr.DdddOcr 实例
        log        - logger 对象
        max_retries - 最大刷新重试次数
    返回 4 位验证码字符串，识别失败则返回 None。
    """
    try:
        img_bytes = capt_img.screenshot(timeout=5000, animations="disabled")
    except Exception as e:
        log.warning(f"[文字验证码] 截图失败: {e}")
        return None
    code = ocr.classification(img_bytes)
    _debug_save_ocr("attempt0", img_bytes, code)

    for attempt in range(max_retries):
        if len(code) == 4:
            return code
        log.warning(
            f"[文字验证码] 识别结果 '{code}' 长度不为4，尝试刷新（{attempt + 1}/{max_retries}）..."
        )
        try:
            old_src = capt_img.get_attribute("src") or ""
            capt_img.click(force=True, timeout=5000)
            for _ in range(10):
                time.sleep(0.2)
                new_src = capt_img.get_attribute("src") or ""
                if new_src and new_src != old_src:
                    break
            else:
                log.warning("[文字验证码] 图片 src 未变化，可能刷新失败")
        except Exception as e:
            log.warning(f"[文字验证码] 刷新失败: {e}")
            time.sleep(1)
        try:
            img_bytes = capt_img.screenshot(timeout=5000, animations="disabled")
        except Exception:
            break
        code = ocr.classification(img_bytes)
        _debug_save_ocr(f"attempt{attempt + 1}", img_bytes, code)

    if len(code) == 4:
        return code
    log.warning(f"[文字验证码] 多次识别均失败（最终结果: '{code}'），跳过自动填写")
    return None


# ---------------------------------------------------------------------------
# 点选验证码图像识别工具函数
# ---------------------------------------------------------------------------


def normalize_mask(
    binary_mask: np.ndarray, canvas_size: int = 48, symbol_size: int = 34
) -> Optional[np.ndarray]:
    """将二值掩码缩放并居中到固定大小画布，用于归一化字符形状以供匹配。"""
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
    canvas[oy : oy + new_h, ox : ox + new_w] = resized
    return canvas


def rotate_mask(mask: np.ndarray, angle: float) -> np.ndarray:
    """将二值掩码按指定角度旋转，用于旋转不变匹配。"""
    h, w = mask.shape
    center = (w / 2, h / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(mask, matrix, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)


def crop_foreground(mask: np.ndarray) -> Optional[np.ndarray]:
    """裁剪掉二值掩码的空白边缘，返回前景紧凑区域。"""
    ys, xs = np.where(mask > 0)
    if xs.size == 0:
        return None
    x1, x2 = xs.min(), xs.max() + 1
    y1, y2 = ys.min(), ys.max() + 1
    return mask[y1:y2, x1:x2]


def match_cost(
    query: np.ndarray, candidate: np.ndarray, allow_rotate: bool = True
) -> float:
    """计算两个归一化掩码之间的像素差距离，支持多角度旋转取最小值。

    返回值越小表示越相似（0 = 完全一致）。
    """
    diff = cv2.absdiff(query, candidate)
    best = float(np.sum(diff) / 255.0)
    if not allow_rotate:
        return best
    for angle in (-90, -60, -45, -30, -20, -10, 10, 20, 30, 45, 60, 90):
        rotated = rotate_mask(query, angle)
        score = float(np.sum(cv2.absdiff(rotated, candidate)) / 255.0)
        if score < best:
            best = score
    return best


def locate_with_template(
    query_mask: np.ndarray, main_mask: np.ndarray
) -> Tuple[float, Optional[Tuple[int, int]]]:
    """用模板匹配在主图二值图上搜索查询字符，支持多尺度和多角度。

    返回 (最佳得分, 中心坐标) 或 (-1.0, None)。
    """
    query_crop = crop_foreground(query_mask)
    if query_crop is None:
        return -1.0, None
    qh, qw = query_crop.shape
    if min(qh, qw) < 8:
        return -1.0, None

    best_score = -1.0
    best_center = None
    scales = np.linspace(0.8, 4.0, 20)
    angles = range(-90, 91, 15)

    for scale in scales:
        new_w = max(6, int(round(qw * scale)))
        new_h = max(6, int(round(qh * scale)))
        base = cv2.resize(query_crop, (new_w, new_h), interpolation=cv2.INTER_NEAREST)
        for angle in angles:
            rotated = rotate_mask(base, angle)
            rotated = crop_foreground(rotated)
            if (
                rotated is None
                or rotated.shape[0] >= main_mask.shape[0]
                or rotated.shape[1] >= main_mask.shape[1]
            ):
                continue
            if np.count_nonzero(rotated) < 20:
                continue
            result = cv2.matchTemplate(main_mask, rotated, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(result)
            if score > best_score:
                cx = loc[0] + rotated.shape[1] // 2
                cy = loc[1] + rotated.shape[0] // 2
                best_score = float(score)
                best_center = (cx, cy)

    return best_score, best_center


def _binarize_main(gray: np.ndarray) -> List[np.ndarray]:
    """多种策略二值化主图，返回多个候选二值图，提高字符提取的覆盖率。"""
    results = []

    # 策略1：自适应阈值 + 多档全局阈值叠加（AND），兼顾局部对比度和全局亮度范围
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )
    for threshold in (60, 80, 100, 120):
        global_bw = (gray < threshold).astype(np.uint8) * 255
        combined = cv2.bitwise_and(adaptive, global_bw)
        combined = cv2.morphologyEx(
            combined, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)
        )
        combined = cv2.morphologyEx(combined, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        results.append(combined)

    # 策略2：纯自适应均值阈值（不叠加全局），适合低对比度背景
    adaptive2 = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 21, 10
    )
    results.append(adaptive2)

    # 策略3：Otsu 全局最优阈值，适合双峰分布背景
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    results.append(otsu)

    return results


def _extract_candidates(symbol_bw: np.ndarray) -> List[dict]:
    """从二值图中用连通域分析提取字符候选区域，过滤掉噪点和超大连通域。"""
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        symbol_bw, connectivity=8
    )
    candidates = []
    for i in range(1, num_labels):
        x, y, w, h, area = stats[i]
        # 过滤条件：面积太小是噪点，太大是背景；宽高比过于极端也排除
        if area < 60 or area > 12000:
            continue
        if w < 10 or h < 10:
            continue
        if w / max(h, 1) > 5.0 or h / max(w, 1) > 5.0:
            continue
        component_mask = np.where(labels[y : y + h, x : x + w] == i, 255, 0).astype(
            np.uint8
        )
        normalized = normalize_mask(component_mask)
        if normalized is not None:
            candidates.append(
                {
                    "center": (int(centroids[i][0]), int(centroids[i][1])),
                    "norm": normalized,
                    "raw": component_mask,
                    "box": (x, y, w, h),
                }
            )
    return candidates


def _merge_nearby_candidates(candidates: List[dict], dist: int = 10) -> List[dict]:
    """合并中心距离过近的候选区域（去重），保留其中面积最大的一个。"""
    merged = []
    used = set()
    for i, c in enumerate(candidates):
        if i in used:
            continue
        group = [c]
        for j, d in enumerate(candidates):
            if j <= i or j in used:
                continue
            dx = c["center"][0] - d["center"][0]
            dy = c["center"][1] - d["center"][1]
            if (dx * dx + dy * dy) ** 0.5 < dist:
                group.append(d)
                used.add(j)
        # 保留 box 面积最大的候选
        best = max(group, key=lambda x: x["box"][2] * x["box"][3])
        merged.append(best)
        used.add(i)
    return merged


def detect_captcha(
    prompt_img_bytes: bytes, main_img_bytes: bytes
) -> List[Tuple[int, int]]:
    """点选验证码主识别函数。

    输入提示图（含3个目标字符）和主图（含散布字符），
    返回按提示顺序排列的最多3个点击坐标列表。
    提示图和主图由调用方直接提供，此函数不做任何截图操作。
    """
    prompt_img = cv2.imdecode(
        np.frombuffer(prompt_img_bytes, np.uint8), cv2.IMREAD_COLOR
    )
    main_img = cv2.imdecode(np.frombuffer(main_img_bytes, np.uint8), cv2.IMREAD_COLOR)
    if prompt_img is None or main_img is None:
        return []

    _debug_save("prompt", prompt_img)
    _debug_save("main", main_img)

    # ---- 从提示图中提取 3 个字符模板 ----
    top_gray = cv2.cvtColor(prompt_img, cv2.COLOR_BGR2GRAY)

    # 用灰度范围掩码定位提示条区域（提示图背景通常为中等灰度区域）
    gray_mask = ((top_gray > 90) & (top_gray < 230)).astype(np.uint8) * 255
    gray_mask = cv2.morphologyEx(gray_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    num_labels, _, stats, _ = cv2.connectedComponentsWithStats(
        gray_mask, connectivity=8
    )
    strip_box = None
    best_area = -1
    for i in range(1, num_labels):
        x, y, w, h, area = stats[i]
        if area > best_area and area > 50:
            best_area = area
            strip_box = (x, y, w, h)

    if strip_box is None:
        top_h, top_w = prompt_img.shape[:2]
        strip_box = (0, 0, top_w, top_h)

    sx, sy, sw, sh = strip_box
    strip_roi = prompt_img[sy : sy + sh, sx : sx + sw]
    strip_roi_gray = cv2.cvtColor(strip_roi, cv2.COLOR_BGR2GRAY)
    _debug_save("strip_roi", strip_roi)

    # 将提示条均分为 3 格，每格对应一个目标字符
    query_cells = np.array_split(strip_roi_gray, 3, axis=1)
    query_templates = []
    query_raw_masks = []
    for cell in query_cells:
        # 用多种阈值尝试二值化，选择像素数量最合理的结果（不太少也不太多）
        best_mask = None
        best_pixel_count = 0
        for thresh_val in (0, 60, 80, 100):
            if thresh_val == 0:
                _, bw = cv2.threshold(
                    cell, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
                )
            else:
                _, bw = cv2.threshold(cell, thresh_val, 255, cv2.THRESH_BINARY_INV)
            # 去除边缘像素，防止边框干扰
            bw[:2, :] = 0
            bw[-2:, :] = 0
            bw[:, :2] = 0
            bw[:, -2:] = 0
            bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
            cnt = np.count_nonzero(bw)
            if 30 < cnt < cell.size * 0.6 and cnt > best_pixel_count:
                best_pixel_count = cnt
                best_mask = bw
        if best_mask is None:
            _, best_mask = cv2.threshold(
                cell, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
            )
        query_raw_masks.append(best_mask)
        query_templates.append(normalize_mask(best_mask))

    # ---- 从主图中提取字符候选区域 ----
    main_gray = cv2.cvtColor(main_img, cv2.COLOR_BGR2GRAY)
    binarizations = _binarize_main(main_gray)

    # 合并所有二值化策略的候选，取并集
    all_candidates: List[dict] = []
    for bw in binarizations:
        all_candidates.extend(_extract_candidates(bw))

    # 去重：合并中心距离过近的候选
    all_candidates = _merge_nearby_candidates(all_candidates, dist=15)

    if _DEBUG_SAVE:
        debug_main = main_img.copy()
        for c in all_candidates:
            cx, cy = c["center"]
            bx, by, bw_c, bh_c = c["box"]
            cv2.rectangle(debug_main, (bx, by), (bx + bw_c, by + bh_c), (0, 255, 0), 1)
            cv2.circle(debug_main, (cx, cy), 4, (0, 0, 255), -1)
        _debug_save("candidates", debug_main)

    if not all_candidates:
        # 候选为空时，直接用模板匹配在全图搜索
        main_bw_fallback = binarizations[0] if binarizations else None
        if main_bw_fallback is None:
            return []
        ordered_points = []
        for raw_mask in query_raw_masks:
            score, center = locate_with_template(raw_mask, main_bw_fallback)
            if center and score >= 0.50:
                ordered_points.append(center)
            else:
                ordered_points.append(None)
        return [p for p in ordered_points if p is not None]

    # ---- 贪心匹配：按提示顺序为每个模板找最相似候选 ----
    # 用第一种二值化结果做模板匹配兜底打分
    main_bw_for_template = binarizations[0]

    used: set = set()
    ordered_points = []
    base_scores = []

    for template in query_templates:
        if template is None:
            ordered_points.append(None)
            base_scores.append(float("inf"))
            continue

        best_idx, best_score = -1, float("inf")
        for idx, candidate in enumerate(all_candidates):
            if idx in used:
                continue
            score = match_cost(template, candidate["norm"], allow_rotate=True)
            if score < best_score:
                best_score = score
                best_idx = idx

        if best_idx >= 0:
            used.add(best_idx)
            ordered_points.append(all_candidates[best_idx]["center"])
            base_scores.append(best_score)
        else:
            ordered_points.append(None)
            base_scores.append(float("inf"))

    # ---- 对匹配分数差的项，改用多尺度模板匹配兜底 ----
    # 分数超过阈值说明归一化匹配不可靠，尝试直接在主图搜索
    for i, raw_mask in enumerate(query_raw_masks):
        if ordered_points[i] is not None and base_scores[i] < 320:
            continue
        score, center = locate_with_template(raw_mask, main_bw_for_template)
        if center is None or score < 0.55:
            # 依次尝试其余二值化策略
            for bw in binarizations[1:]:
                score2, center2 = locate_with_template(raw_mask, bw)
                if center2 and score2 > score:
                    score, center = score2, center2
            if center is None or score < 0.50:
                continue

        new_point = center
        # 避免与已有坐标过近（同一字符不可能在两处同时匹配）
        too_close = False
        for j, point in enumerate(ordered_points):
            if j == i or point is None:
                continue
            if (
                (point[0] - new_point[0]) ** 2 + (point[1] - new_point[1]) ** 2
            ) ** 0.5 < 20:
                too_close = True
                break
        if not too_close:
            ordered_points[i] = new_point

    result = [p for p in ordered_points if p is not None]

    if _DEBUG_SAVE:
        debug_result = main_img.copy()
        for idx, p in enumerate(result):
            cv2.circle(debug_result, p, 12, (0, 255, 0), 2)
            cv2.putText(
                debug_result,
                str(idx + 1),
                (p[0] - 5, p[1] + 5),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 0, 255),
                2,
            )
        _debug_save("result", debug_result)

    return result


# ---------------------------------------------------------------------------
# 验证码页面处理（Playwright 浏览器操作层）
# ---------------------------------------------------------------------------


def _captcha_visible(frame, require_cscapt: bool = True) -> bool:
    """检查验证码核心元素在 frame 内是否真正可见。

    用 Playwright 原生 is_visible() 检测，避免脚本注入。
    验证码弹出时核心容器必然可见，隐藏预加载时则不可见。

    Args:
        frame: Playwright Frame 对象
        require_cscapt: 是否要求 URL 参数 cscapt=true（默认 True）
                       课程完成场景可能需要设为 False 以放宽检测
    """
    if require_cscapt:
        state = _get_captcha_url_state(frame)
        if not state.get("has_cscapt_true"):
            return False

    url = (frame.url or "").lower()
    # 课程页面本身绝不是验证码框架，验证码应在弹出层或独立 iframe 中
    if "mcwk.mycourse.cn" in url and (".html" in url or "/course/" in url):
        return False

    state = _get_captcha_url_state(frame)
    for sel in _SEL_CAPTCHA_VISIBILITY_MARKERS:
        try:
            el = frame.locator(sel)
            if el.count() > 0:
                item = el.first
                if item.is_visible():
                    # 1. 基础尺寸检查
                    bb = item.bounding_box()
                    if not bb or bb["width"] < 10 or bb["height"] < 10:
                        continue

                    # 2. 深度属性检查 (z-index, opacity, pointer-events)
                    try:
                        props = item.evaluate("""el => {
                            const s = window.getComputedStyle(el);
                            return {
                                zIndex: parseInt(s.zIndex) || 0,
                                opacity: parseFloat(s.opacity),
                                pointerEvents: s.pointerEvents
                            };
                        }""")
                        
                        # 腾讯验证码 z-index 通常极高，且透明度不能为 0
                        if props["opacity"] < 0.1 or props["pointerEvents"] == 'none':
                            continue
                            
                        # 如果是 iframe，z-index 可能不在元素本身而在容器上，这里做宽松处理
                        # 但如果 z-index 明确为负数，则肯定不可见
                        if props["zIndex"] < 0:
                            continue
                    except Exception:
                        pass

                    # 3. 启发式过滤：课程页面本身很大，验证码面板通常在固定范围内
                    # 如果面板尺寸过大（如超过视口的 90%），通常是误报（可能是整个页面的背景）
                    try:
                        vw = frame.evaluate("window.innerWidth")
                        vh = frame.evaluate("window.innerHeight")
                        if bb["width"] > vw * 0.9 and bb["height"] > vh * 0.9:
                            continue
                    except Exception:
                        if bb["width"] > 600 or bb["height"] > 800:
                            continue

                    # 诊断日志：只有在调试模式下才记录匹配信息
                    # (由于此函数调用极其频繁，平时不输出日志)
                    return True
        except Exception:
            pass
    return False


def _get_captcha_url_state(ctx) -> dict:
    """提取 iframe URL 的验证码状态，供判定与诊断共用。"""
    try:
        ctx_url = (ctx.url or "").strip()
    except Exception:
        ctx_url = ""

    ctx_url_lower = ctx_url.lower()
    has_url = bool(ctx_url)
    is_mcwk = "mcwk.mycourse.cn" in ctx_url_lower
    has_cscapt = "cscapt=" in ctx_url_lower
    has_cscapt_true = "cscapt=true" in ctx_url_lower

    if has_cscapt_true:
        cscapt_state = "true"
    elif has_cscapt:
        cscapt_state = "false"
    else:
        cscapt_state = "missing"

    return {
        "url": ctx_url,
        "url_lower": ctx_url_lower,
        "has_url": has_url,
        "is_mcwk": is_mcwk,
        "has_cscapt": has_cscapt,
        "has_cscapt_true": has_cscapt_true,
        "cscapt": cscapt_state,
    }


def _log_captcha_contexts(page, log) -> None:
    """输出验证码 iframe 诊断信息。

    仅记录真正存在 URL 的 iframe，并仅保留验证码判定所需信息：
    - mcwk 页面状态；
    - cscapt 状态；
    - 验证码核心元素可见性。
    """
    try:
        try:
            contexts = list(page.frames)
        except Exception:
            contexts = []

        seen = set()
        for idx, ctx in enumerate(contexts):
            try:
                if ctx == page.main_frame:
                    continue

                ctx_id = id(ctx)
                if ctx_id in seen:
                    continue
                seen.add(ctx_id)

                state = _get_captcha_url_state(ctx)
                if not state["has_url"]:
                    continue
                if "mycourse.cn" not in state["url_lower"]:
                    continue

                visible = _captcha_visible(ctx)
                if not (state["is_mcwk"] or state["has_cscapt"] or visible):
                    continue

                prompt_visible = False
                bg_visible = False
                confirm_visible = False

                try:
                    prompt_loc = ctx.locator(_SEL_CAPTCHA_PROMPT)
                    prompt_visible = (
                        prompt_loc.count() > 0 and prompt_loc.first.is_visible()
                    )
                except Exception:
                    pass

                try:
                    bg_loc = ctx.locator(_SEL_CAPTCHA_BG)
                    bg_visible = bg_loc.count() > 0 and bg_loc.first.is_visible()
                except Exception:
                    pass

                try:
                    confirm_loc = ctx.locator(_SEL_CAPTCHA_CONFIRM_BTN)
                    confirm_visible = (
                        confirm_loc.count() > 0 and confirm_loc.first.is_visible()
                    )
                except Exception:
                    pass

                log.debug(
                    f"[点选验证码][诊断] frame[{idx}] "
                    f"url={state['url']} | mcwk={str(state['is_mcwk']).lower()} | "
                    f"cscapt={state['cscapt']} | visible={str(visible).lower()} | "
                    f"prompt={str(prompt_visible).lower()} | "
                    f"bg={str(bg_visible).lower()} | "
                    f"confirm={str(confirm_visible).lower()}"
                )
            except Exception as e:
                log.debug(f"[点选验证码][诊断] frame[{idx}] 枚举异常: {e}")
    except Exception as e:
        log.debug(f"[点选验证码][诊断] 输出上下文信息失败: {e}")


def _find_captcha_context(page, require_cscapt: bool = True):
    """查找验证码上下文（支持 main_frame 和 iframe）。
    
    验证码可能出现在子框架（iframe）中，也可能出现在父级 DOM（main_frame）中。
    """
    try:
        # 1. 优先检查主页面（父级 DOM）
        if _captcha_visible(page.main_frame, require_cscapt):
            return page.main_frame, "主页面验证码 (Parent DOM)"

        # 2. 遍历所有子框架
        frames = list(page.frames)
        for ctx in frames:
            if ctx == page.main_frame:
                continue
            
            # 如果是微课课件帧，跳过其作为验证码容器的直接判定
            url = (ctx.url or "").lower()
            if "mcwk.mycourse.cn" in url and (".html" in url or "/course/" in url):
                continue

            if _captcha_visible(ctx, require_cscapt):
                state = _get_captcha_url_state(ctx)
                hint = f"iframe验证码(mcwk={state['is_mcwk']})"
                return ctx, hint
    except Exception:
        pass
    return None, None


def _find_captcha_frame(page, require_cscapt: bool = True):
    """兼容旧调用的别名，返回验证码 frame（不含 vendor_hint）。"""
    return _find_captcha_context(page, require_cscapt)


def handle_tencent_captcha(page, log, require_cscapt: bool = True) -> bool:
    """自动处理腾讯系验证码（统一入口）。

    Args:
        page: Playwright Page 对象
        log: Logger 对象
        require_cscapt: 是否要求 cscapt=true 参数（默认 True）
    """
    ctx, hint = _find_captcha_context(page, require_cscapt)
    if ctx is None:
        return False

    state = _get_captcha_url_state(ctx)
    if require_cscapt and not state.get("has_cscapt_true"):
        return False

    log.info(f"[{hint}] 检测到腾讯点选验证码，开始自动识别处理...")
    return handle_click_captcha(page, log)


def handle_slider_captcha(page, ctx, log) -> bool:
    """暂时保留滑块占位。"""
    log.warning("[验证码] 暂未启用滑块处理逻辑")
    return False


def has_captcha(page, require_cscapt: bool = True) -> bool:
    """检测当前页面是否弹出了点选验证码。

    Args:
        page: Playwright Page 对象
        require_cscapt: 是否要求 cscapt=true 参数（默认 True）
    """
    ctx, _ = _find_captcha_context(page, require_cscapt)
    return ctx is not None


def handle_click_captcha(page, log) -> bool:
    """自动处理腾讯点选验证码。

    识别提示图中的目标字符，在主图上按顺序点击，再点击确认按钮等待结果。
    返回 True 表示验证通过，False 表示失败或未检测到验证码。
    """
    try:
        ctx, vendor_hint = _find_captcha_context(page)
        if ctx is None:
            log.debug("[点选验证码] 未检测到有效验证码上下文")
            _log_captcha_contexts(page, log)
            return False

        state = _get_captcha_url_state(ctx)
        log.info(f"[{vendor_hint or '点选验证码'}] 开始自动识别...")
        log.debug(
            f"[点选验证码] 验证码上下文: url={state['url']} | "
            f"mcwk={str(state['is_mcwk']).lower()} | cscapt={state['cscapt']}"
        )

        # 定位提示图和主背景图元素
        prompt_el = ctx.locator(_SEL_CAPTCHA_PROMPT)
        main_el = ctx.locator(_SEL_CAPTCHA_BG)

        if prompt_el.count() == 0:
            log.warning("[点选验证码] 未找到提示图元素，无法自动识别")
            _log_captcha_contexts(page, log)
            return False
        if main_el.count() == 0:
            log.warning("[点选验证码] 未找到主背景图元素，无法自动识别")
            _log_captcha_contexts(page, log)
            return False

        prompt_el = prompt_el.first
        main_el = main_el.first

        prompt_visible = False
        main_visible = False

        try:
            prompt_el.wait_for(state="visible", timeout=5000)
            prompt_visible = True
        except Exception:
            log.warning("[点选验证码] 提示图等待超时，尝试继续...")
        try:
            main_el.wait_for(state="visible", timeout=5000)
            main_visible = True
        except Exception:
            log.warning("[点选验证码] 主背景图等待超时，尝试继续...")

        if not prompt_visible and not main_visible:
            log.warning("[点选验证码] 提示图和主背景图均不可见，跳过验证码处理")
            return False

        time.sleep(0.5)

        if ctx.page.is_closed() if hasattr(ctx, "page") else False:
            log.warning("[点选验证码] 页面已关闭，跳过验证码处理")
            return False

        prompt_bytes, prompt_url = _fetch_element_image(ctx, prompt_el, log, "提示图")

        # 从提示图 URL 推导主图 URL（img_index=0 → img_index=1）
        main_bytes: Optional[bytes] = None
        if prompt_url:
            derived_url = _derive_main_url(prompt_url)
            if derived_url:
                log.debug(f"[点选验证码] 主图从提示图 URL 推导: {derived_url[:80]}...")
                try:
                    req = urllib.request.Request(
                        derived_url, headers={"User-Agent": "Mozilla/5.0"}
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        main_bytes = resp.read()
                        log.debug(
                            f"[点选验证码] 主图推导下载成功，{len(main_bytes or b'')} B"
                        )
                except Exception as e:
                    log.warning(f"[点选验证码] 主图推导下载失败: {e}")

        # 推导失败时走元素 URL 提取 / 截图流程
        if not main_bytes:
            main_bytes, _ = _fetch_element_image(ctx, main_el, log, "主图")

        # 仍为空时，JS 全局扫描 frame 内背景图（仅此处保留 evaluate，无替代方案）
        if not main_bytes:
            main_bytes = _fetch_frame_bg_image(ctx, log)

        # 获取主图渲染尺寸，用于坐标映射
        main_render_size: Optional[Tuple[float, float]] = None
        try:
            main_render_size = _get_main_render_size(ctx, main_el, log)
        except Exception:
            pass

        if not prompt_bytes or not main_bytes:
            log.warning(
                f"[点选验证码] 图片数据为空（提示图={bool(prompt_bytes)}, "
                f"主图={bool(main_bytes)}），无法识别"
            )
            return False

        log.debug(
            f"[点选验证码] 图片获取完成（提示图 {len(prompt_bytes)} B，"
            f"主图 {len(main_bytes)} B），开始识别坐标..."
        )
        points = detect_captcha(prompt_bytes, main_bytes)

        # 若渲染尺寸与图片像素尺寸不一致，按比例缩放坐标
        if main_render_size and points:
            try:
                img_arr = cv2.imdecode(
                    np.frombuffer(main_bytes, np.uint8), cv2.IMREAD_COLOR
                )
                if img_arr is not None:
                    img_h, img_w = img_arr.shape[:2]
                    scale_x = main_render_size[0] / img_w
                    scale_y = main_render_size[1] / img_h
                    if abs(scale_x - 1.0) > 0.05 or abs(scale_y - 1.0) > 0.05:
                        points = [
                            (int(x * scale_x), int(y * scale_y)) for x, y in points
                        ]
                        log.debug(
                            f"[点选验证码] 坐标缩放比例: x={scale_x:.3f}, y={scale_y:.3f}，"
                            f"缩放后坐标: {points}"
                        )
            except Exception as e:
                log.debug(f"[点选验证码] 坐标缩放失败（忽略）: {e}")

        if len(points) == 0:
            log.warning("[点选验证码] 未识别到任何坐标，破解失败")
            return False
        if len(points) < 3:
            log.warning(
                f"[点选验证码] 坐标识别不足 3 个（识别到 {len(points)} 个），仍尝试点击"
            )

        log.info(
            f"[点选验证码] 识别到 {len(points)} 个坐标: {points[:3]}，开始模拟点击"
        )
        for idx, p in enumerate(points[:3]):
            try:
                # 修复：传入 page 实例以支持 page.mouse 操作
                _click_captcha_point(page, ctx, main_el, p, log, idx)
                time.sleep(0.5)
            except Exception as e:
                log.warning(f"[点选验证码] 点击第 {idx + 1} 个坐标 {p} 失败: {e}")

        # 等待确认按钮变为可用（新版验证码在点击后才解除 disabled 状态）
        time.sleep(0.5)

        # 点击确认按钮（优先匹配非禁用态）
        try:
            confirm_btn = ctx.locator(_SEL_CAPTCHA_CONFIRM_BTN)
            if confirm_btn.count() > 0:
                confirm_btn.first.click(force=True)
                log.debug("[点选验证码] 已点击确认按钮")
            else:
                log.warning("[点选验证码] 未找到确认按钮，验证可能无法完成")
        except Exception as e:
            log.warning(f"[点选验证码] 点击确认按钮失败: {e}")

        time.sleep(3)

        # 检查是否出现错误提示（验证失败）
        try:
            error_tip = ctx.locator(_SEL_CAPTCHA_ERROR_TIP)
            if error_tip.count() > 0 and error_tip.first.is_visible():
                err_text = ""
                try:
                    err_text = error_tip.first.inner_text()
                except Exception:
                    pass
                log.warning(
                    f"[点选验证码] 验证未通过，错误提示: {err_text!r}，尝试点击刷新按钮..."
                )

                # 尝试点击刷新按钮
                try:
                    refresh_btn = ctx.locator(_SEL_CAPTCHA_REFRESH_BTN)
                    if refresh_btn.count() > 0:
                        refresh_btn.first.click(force=True)
                        time.sleep(1)
                except Exception:
                    pass
                return False
        except Exception:
            pass

        log.info("[点选验证码] 点击完成，验证通过")
        return True

    except Exception as e:
        log.error(f"[点选验证码] 处理异常: {e}", exc_info=True)
        return False


def _derive_main_url(prompt_url: str) -> Optional[str]:
    """从提示图 URL 推导主图 URL（img_index=0 → img_index=1）。"""
    if "img_index=0" in prompt_url:
        return prompt_url.replace("img_index=0", "img_index=1")
    # 兜底：尝试通用索引模式
    m = re.search(r"img_index=(\d+)", prompt_url)
    if m:
        idx = int(m.group(1))
        return prompt_url[: m.start(1)] + str(idx + 1) + prompt_url[m.end(1) :]
    return None


def _get_main_render_size(ctx, main_el, log) -> Optional[Tuple[float, float]]:
    """获取主图容器的渲染尺寸，优先使用 Playwright 原生 bounding_box()。"""
    try:
        bb = main_el.bounding_box(timeout=3000)
        if bb and bb["width"] > 0 and bb["height"] > 0:
            log.debug(
                f"[点选验证码] 主图渲染尺寸(bounding_box): {bb['width']:.0f}x{bb['height']:.0f}"
            )
            return (bb["width"], bb["height"])
    except Exception:
        pass

    try:
        size = ctx.evaluate(
            """() => {
            const sels = [
                '.tencent-captcha-dy__verify-img-area',
                '.tencent-captcha-dy__verify',
                '.tencent-captcha-dy__verify-bg',
                '.tencent-captcha-dy__verify-bg-img',
                '#tCaptchaDyContent',
            ];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return [r.width, r.height];
                }
            }
            return null;
        }"""
        )
        if size and len(size) == 2 and size[0] > 0 and size[1] > 0:
            log.debug(
                f"[点选验证码] 主图渲染尺寸(evaluate fallback): {size[0]:.0f}x{size[1]:.0f}"
            )
            return (float(size[0]), float(size[1]))
    except Exception:
        pass

    log.debug("[点选验证码] 主图渲染尺寸: 无法获取")
    return None


def _fetch_frame_bg_image(ctx, log) -> Optional[bytes]:
    """在 frame 内扫描所有含 background-image 的元素，下载体积最大的一张作为主图。

    此函数需要 evaluate 读取 CSS 属性，无 Playwright 原生替代方案。
    """
    try:
        urls = ctx.evaluate(
            """() => {
            const found = [];
            for (const el of document.querySelectorAll('*')) {
                const style = el.getAttribute('style') || '';
                let m = style.match(/background-image\\s*:\\s*url\\([\"']?(https?:\\/\\/[^\"')\\s]+)[\"']?\\)/);
                if (m) { found.push(m[1]); continue; }
                const cs = window.getComputedStyle(el).backgroundImage;
                m = cs.match(/url\\([\"']?(https?:\\/\\/[^\"')\\s]+)[\"']?\\)/);
                if (m) found.push(m[1]);
            }
            return [...new Set(found)];
        }"""
        )
        if not urls:
            return None
        log.debug(f"[点选验证码] 全局扫描找到 {len(urls)} 个背景图 URL")
        best_data: Optional[bytes] = None
        for u in urls:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read()
                if best_data is None or len(data) > len(best_data):
                    best_data = data
            except Exception:
                pass
        if best_data:
            log.debug(f"[点选验证码] 全局扫描下载最大背景图 {len(best_data)} B")
        return best_data
    except Exception as e:
        log.debug(f"[点选验证码] 全局扫描失败: {e}")
        return None


def _fetch_element_image(
    ctx, el, log, label: str
) -> Tuple[Optional[bytes], Optional[str]]:
    """从元素提取图片 URL 并通过 HTTP 下载，返回 (bytes, url)。

    按优先级依次尝试：
    1. <img src="..."> 的 src 属性（Playwright get_attribute）
    2. inline style background-image（Playwright get_attribute）
    3. JS computed style 兜底（evaluate，无原生替代）
    4. 子元素 <img>（Playwright locator）
    5. 元素截图（Playwright screenshot，最后手段）
    """
    url: Optional[str] = None

    # 1. <img src="...">
    try:
        src = el.get_attribute("src")
        if src and src.startswith("http"):
            url = src
    except Exception:
        pass

    # 2. inline style background-image
    if not url:
        try:
            style = el.get_attribute("style") or ""
            m = re.search(
                r'background-image\s*:\s*url\(["\']?(https?://[^"\')\s]+)["\']?\)',
                style,
            )
            if m:
                url = m.group(1)
        except Exception:
            pass

    if not url:
        try:
            result = el.evaluate(
                """el => {
                if (el.tagName === 'IMG') {
                    const src = el.src || el.getAttribute('src');
                    if (src && src.startsWith('http')) return src;
                }
                const style = el.getAttribute('style') || '';
                let m = style.match(/background-image\\s*:\\s*url\\([\"']?(https?:\\/\\/[^\"')\\s]+)[\"']?\\)/);
                if (m) return m[1];
                const cs = window.getComputedStyle(el).backgroundImage;
                m = cs.match(/url\\([\"']?(https?:\\/\\/[^\"')\\s]+)[\"']?\\)/);
                return m ? m[1] : null;
            }"""
            )
            if result and result.startswith("http"):
                url = result
        except Exception:
            pass

    # 4. 子元素 <img>
    if not url:
        try:
            for img in el.locator("img").all():
                src = img.get_attribute("src")
                if src and src.startswith("http"):
                    url = src
                    break
        except Exception:
            pass

    # 找到 URL 后 HTTP 下载
    if url:
        log.debug(f"[点选验证码] {label} URL: {url[:80]}...")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            log.debug(f"[点选验证码] {label} HTTP 下载成功，{len(data)} B")
            return data, url
        except Exception as e:
            log.warning(f"[点选验证码] {label} HTTP 下载失败: {e}，回退截图...")

    # 5. 截图兜底（元素不可见或尺寸异常时跳过，避免 29.9s 长挂超时）
    log.debug(f"[点选验证码] {label} 未提取到 URL，使用截图...")
    try:
        try:
            is_visible = el.is_visible()
        except Exception:
            is_visible = False

        if not is_visible:
            log.debug(f"[点选验证码] {label} 元素不可见，跳过截图")
            return None, None

        bb = None
        try:
            bb = el.bounding_box(timeout=3000)
        except Exception:
            pass

        if not bb or bb["width"] < 5 or bb["height"] < 5:
            log.debug(f"[点选验证码] {label} 元素尺寸过小({bb})，跳过截图")
            return None, None

        data = el.screenshot(timeout=5000, animations="disabled")
        return data, None
    except Exception as e:
        log.warning(f"[点选验证码] {label} 截图失败: {e}")
        return None, None


def _human_mouse_move(page, x1: float, y1: float, x2: float, y2: float, log) -> None:
    """用三次贝塞尔曲线模拟人类鼠标从 (x1,y1) 移动到 (x2,y2)。

    控制点在起止点附近随机偏移，步数根据距离自适应，每步间随机微延迟。
    """
    import math

    dx = x2 - x1
    dy = y2 - y1
    dist = math.hypot(dx, dy)
    steps = max(10, int(dist / 8))  # 每 8px 一步，至少 10 步

    # 随机生成两个贝塞尔控制点（在起止连线两侧随机偏移）
    rand = random.Random()
    cp1x = x1 + dx * 0.25 + rand.uniform(-dist * 0.15, dist * 0.15)
    cp1y = y1 + dy * 0.25 + rand.uniform(-dist * 0.15, dist * 0.15)
    cp2x = x1 + dx * 0.75 + rand.uniform(-dist * 0.15, dist * 0.15)
    cp2y = y1 + dy * 0.75 + rand.uniform(-dist * 0.15, dist * 0.15)

    prev_px, prev_py = x1, y1
    try:
        for i in range(1, steps + 1):
            t = i / steps
            u = 1 - t
            # 三次贝塞尔公式
            px = u**3 * x1 + 3 * u**2 * t * cp1x + 3 * u * t**2 * cp2x + t**3 * x2
            py = u**3 * y1 + 3 * u**2 * t * cp1y + 3 * u * t**2 * cp2y + t**3 * y2

            # 只在坐标变化超过 1px 时才实际移动，减少 IPC 调用
            if abs(px - prev_px) >= 1 or abs(py - prev_py) >= 1:
                # 修复：使用传入的 page.mouse 而非 Frame.mouse
                page.mouse.move(px, py)
                prev_px, prev_py = px, py

            # 每步随机延迟 5~18ms，模拟人类手速
            time.sleep(rand.uniform(0.005, 0.018))
    except Exception as e:
        log.debug(f"[鼠标移动] 贝塞尔移动中出错（忽略）: {e}")


def _click_captcha_point(page, ctx, main_el, point, log, idx: int) -> None:
    """在主图上的指定相对坐标处模拟人类鼠标移动后点击。

    流程：
      1. 通过 bounding_box() 计算绝对坐标
      2. 用贝塞尔曲线从当前鼠标位置移动到目标点（模拟人类轨迹）
      3. 使用 Playwright 推荐的 locator.click(position=...) 完成点击
      4. 失败时 fallback 到 mouse.click()
    """
    x, y = float(point[0]), float(point[1])

    try:
        if page.is_closed():
            log.warning(f"[点选验证码] 页面已关闭，无法点击第 {idx + 1} 个坐标")
            return
    except Exception:
        pass

    try:
        bb = main_el.bounding_box(timeout=3000)
        if bb:
            abs_x = bb["x"] + x
            abs_y = bb["y"] + y

            start_x = bb["x"] + bb["width"] * random.uniform(0.05, 0.2)
            start_y = bb["y"] + bb["height"] * random.uniform(0.05, 0.2)
            _human_mouse_move(page, start_x, start_y, abs_x, abs_y, log)

            jitter_x = abs_x + random.uniform(-2, 2)
            jitter_y = abs_y + random.uniform(-2, 2)
            page.mouse.move(jitter_x, jitter_y)
            time.sleep(random.uniform(0.05, 0.15))
    except Exception as e:
        log.debug(f"[点选验证码] 贝塞尔移动失败（忽略）: {e}")

    try:
        main_el.click(position={"x": x, "y": y}, force=True, timeout=5000)
        log.debug(f"[点选验证码] 已点击第 {idx + 1} 个坐标 ({x:.0f}, {y:.0f})")
        return
    except Exception as e:
        log.debug(f"[点选验证码] locator.click 失败，尝试 mouse.click 兜底: {e}")

    try:
        bb = main_el.bounding_box(timeout=3000)
        if bb:
            abs_x = bb["x"] + x
            abs_y = bb["y"] + y
            page.mouse.click(abs_x, abs_y)
            log.debug(
                f"[点选验证码] mouse.click 第 {idx + 1} 个坐标 "
                f"({x:.0f}, {y:.0f}) → 绝对 ({abs_x:.0f}, {abs_y:.0f})"
            )
            return
    except Exception as e:
        log.warning(
            f"[点选验证码] 点击第 {idx + 1} 个坐标 ({x:.0f}, {y:.0f}) 全部方式均失败: {e}"
        )
