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
     detect_captcha / _captcha_visible / _find_captcha_context /
     has_captcha / handle_click_captcha
     检测页面是否存在腾讯点选验证码，并自动完成图像识别与点击操作。

  5. 辅助工具
     _click_captcha_point
     模拟点击验证码坐标。
"""

import os
import re
import time
import random
import threading

import cv2
import numpy as np
from typing import Optional, Tuple, List, cast

# 调试模式：开启后将把验证码截图保存到 logs/ 目录
_DEBUG_SAVE = False
_DEBUG_LOG_DIR = "logs"

# 线程本地存储，用于记录当前账号名（用于分目录保存）
_thread_local = threading.local()

# ---------------------------------------------------------------------------
# DOM 元素选择器常量定义
# ---------------------------------------------------------------------------
# 主背景图 - 优先选择 img 元素，其次是容器
_SEL_CAPTCHA_BG = (
    ".tencent-captcha-dy__verify-bg-img, "  # img 元素（首选）
    ".tencent-captcha-dy__verify-img-area img, "  # 备用 img
    ".tencent-captcha-dy__verify-bg, "  # 容器 div（次选）
    ".tencent-captcha-dy__verify, "
    ".WPA3-SELECT-BG"
)
# 提示图 - 优先选择 img 元素
_SEL_CAPTCHA_PROMPT = (
    ".tencent-captcha-dy__header-answer img, "  # img 元素（首选）
    ".tencent-captcha-dy__header-answer, "  # 容器本身（次选，可能背景图）
    ".tencent-captcha-dy__prompt-img, "  # 备用选择器
    ".tcaptcha-dy-prompt, "
    ".WPA3-SELECT-HINT img, "
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
    ".tencent-captcha-dy__verify-bg-img",
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
    for angle in (-60, -45, -30, -20, -10, 10, 20, 30, 45, 60, 90, -90):
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
    if min(qh, qw) < 10:
        return -1.0, None

    best_score = -1.0
    best_center = None
    scales = np.linspace(1.1, 3.4, 16)
    angles = range(-90, 91, 10)

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


def _binarize_main(gray: np.ndarray) -> np.ndarray:
    """单策略精调二值化：自适应高斯 + 全局暗色阈值取交集。

    自适应阈值捕获局部暗色线条，全局阈值过滤中等灰度背景，
    形态学运算清理断点和噪点。
    """
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )
    global_bw = (gray < 90).astype(np.uint8) * 255
    symbol_bw = cv2.bitwise_and(adaptive, global_bw)
    symbol_bw = cv2.morphologyEx(symbol_bw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    symbol_bw = cv2.morphologyEx(symbol_bw, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return symbol_bw


def _extract_candidates(symbol_bw: np.ndarray) -> List[dict]:
    """从二值图中用连通域分析提取字符候选区域，严格过滤噪点。"""
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        symbol_bw, connectivity=8
    )
    candidates = []
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

    # 用灰度掩码定位提示条区域（提示图背景通常为浅灰色）
    gray_mask = ((top_gray > 110) & (top_gray < 220)).astype(np.uint8) * 255
    gray_mask = cv2.morphologyEx(gray_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    num_labels, _, stats, _ = cv2.connectedComponentsWithStats(
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
    _debug_save("strip_roi", strip_roi)

    # 将提示条均分为 3 格，每格对应一个目标字符
    query_cells = np.array_split(strip_roi_gray, 3, axis=1)
    query_templates = []
    query_raw_masks = []
    for cell in query_cells:
        # Otsu 二值化，去除边缘防止边框干扰
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

    # ---- 从主图中提取字符候选区域 ----
    main_gray = cv2.cvtColor(main_img, cv2.COLOR_BGR2GRAY)
    symbol_bw = _binarize_main(main_gray)

    all_candidates = _extract_candidates(symbol_bw)
    all_candidates = _merge_nearby_candidates(all_candidates, dist=15)

    if _DEBUG_SAVE:
        debug_main = main_img.copy()
        for c in all_candidates:
            cx, cy = c["center"]
            bx, by, bw_c, bh_c = c["box"]
            cv2.rectangle(
                debug_main, (bx, by), (bx + bw_c, by + bh_c), (0, 255, 255), 1
            )
            cv2.circle(debug_main, (cx, cy), 4, (0, 0, 255), -1)
        _debug_save("candidates", debug_main)

    if not all_candidates:
        return []

    # ---- 预计算所有模板-候选配对成本矩阵 ----
    n_templates = len(query_templates)
    cost_matrix = []
    for ti, template in enumerate(query_templates):
        row = []
        if template is None:
            cost_matrix.append(row)
            continue
        for ci, candidate in enumerate(all_candidates):
            score = match_cost(template, candidate["norm"], allow_rotate=True)
            row.append((score, ci))
        cost_matrix.append(row)

    # ---- 全局最优分配：枚举所有有效排列，取总成本最低者 ----
    # 对于 N 个模板和 M 个候选，枚举所有 P(M, count) 排列（即 M!/(M-count)! 种）
    # 候选数通常 ≤ 30，3 个模板，排列数 ≤ 30*29*28 = 24360，实际候选通常 < 15
    from itertools import permutations as _perms

    active_indices = [
        ti for ti in range(n_templates) if query_templates[ti] is not None
    ]
    n_active = len(active_indices)
    ordered_points: List[Optional[Tuple[int, int]]] = [None] * n_templates
    base_scores: List[float] = [float("inf")] * n_templates
    best_assignment = None

    if n_active > 0 and all_candidates:
        # 为每个活动模板排序候选（按成本升序）
        best_total = float("inf")
        ranked = []
        for ti in active_indices:
            row = cost_matrix[ti]
            row.sort(key=lambda x: x[0])
            ranked.append(row)

        # 限制候选数量：只取每个模板的前 top_k 候选
        top_k = min(len(all_candidates), max(n_active * 4, 12))
        candidate_pool = set()
        for row in ranked:
            for _, ci in row[:top_k]:
                candidate_pool.add(ci)

        # 枚举所有候选排列，找全局最优
        pool_list = list(candidate_pool)

        if len(pool_list) >= n_active:
            for perm in _perms(pool_list, n_active):
                total = 0.0
                valid = True
                for ti_idx, ci in zip(active_indices, perm):
                    # 找到这个候选在成本矩阵中的成本
                    cost_for_this = None
                    for score, mapped_ci in cost_matrix[ti_idx]:
                        if mapped_ci == ci:
                            cost_for_this = score
                            break
                    if cost_for_this is None:
                        valid = False
                        break
                    total += cost_for_this

                if valid and total < best_total:
                    best_total = total
                    best_assignment = perm

            if best_assignment is not None:
                for ti_idx, ci in zip(active_indices, best_assignment):
                    ordered_points[ti_idx] = all_candidates[ci]["center"]
                    # 找到该配对的成本
                    for score, mapped_ci in cost_matrix[ti_idx]:
                        if mapped_ci == ci:
                            base_scores[ti_idx] = score
                            break

    # 全局最优分配失败时回退到贪心
    if best_assignment is None and n_active > 0:
        used = set()
        for ti in range(n_templates):
            if query_templates[ti] is None:
                continue
            best_idx, best_score = -1, float("inf")
            for idx, candidate in enumerate(all_candidates):
                if idx in used:
                    continue
                for score, ci in cost_matrix[ti]:
                    if ci == idx:
                        if score < best_score:
                            best_score = score
                            best_idx = idx
                        break
            if best_idx >= 0:
                used.add(best_idx)
                ordered_points[ti] = all_candidates[best_idx]["center"]
                base_scores[ti] = best_score

    # ---- 对匹配分数差的项，改用多尺度模板匹配兜底 ----
    for i, raw_mask in enumerate(query_raw_masks):
        # 基础匹配较可靠时不启用模板校正
        if ordered_points[i] is not None and base_scores[i] < 280:
            continue

        score, center = locate_with_template(raw_mask, symbol_bw)
        if center is None:
            continue
        if score < 0.70:
            continue

        new_point = center
        # 避免与已有坐标过近
        too_close = False
        for j, point in enumerate(ordered_points):
            if j == i or point is None:
                continue
            if (
                (point[0] - new_point[0]) ** 2 + (point[1] - new_point[1]) ** 2
            ) ** 0.5 < 26:
                too_close = True
                break
        if too_close:
            continue

        # 有旧点时，仅在基础匹配不稳定且模板匹配置信度更高时才替换
        if ordered_points[i] is not None and base_scores[i] < 340:
            continue

        ordered_points[i] = new_point

    result: List[Tuple[int, int]] = [p for p in ordered_points if p is not None]

    if _DEBUG_SAVE:
        debug_result = main_img.copy()
        for idx, p in enumerate(result):
            cv2.circle(debug_result, p, 16, (0, 255, 0), 2)
            cv2.putText(
                debug_result,
                str(idx + 1),
                (p[0] - 6, p[1] + 6),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
            )
        _debug_save("result", debug_result)

    return result


# ---------------------------------------------------------------------------
# 验证码页面处理（Playwright 浏览器操作层）
# ---------------------------------------------------------------------------


def _captcha_visible(frame, require_cscapt: bool = True) -> bool:
    """检查验证码核心元素在 frame 内是否真正可见。

    在 mcwk iframe 中，验证码容器始终预加载存在。
    只有当 URL 含 cscapt=true 且验证码真正弹出时才需要处理。

    Args:
        frame: Playwright Frame 对象
        require_cscapt: 是否要求 URL 参数 cscapt=true（默认 True）
    """
    state = _get_captcha_url_state(frame)

    if require_cscapt and not state.get("has_cscapt_true"):
        return False

    try:
        bg_img = frame.locator(".tencent-captcha-dy__verify-bg-img").first
        if bg_img.count() > 0 and bg_img.is_visible():
            bb = bg_img.bounding_box()
            if bb and bb["width"] > 50 and bb["height"] > 50:
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
    课程完成时的验证码会在 mcwk.mycourse.cn iframe 内部弹出（URL 含 cscapt=true）。
    """
    try:
        frames = list(page.frames)

        for ctx in frames:
            state = _get_captcha_url_state(ctx)

            if _captcha_visible(ctx, require_cscapt):
                if ctx == page.main_frame:
                    return ctx, "主页面验证码 (Parent DOM)"
                else:
                    hint = f"iframe验证码(mcwk={state['is_mcwk']}, cscapt={state['cscapt']})"
                    return ctx, hint

    except Exception:
        pass
    return None, None


def _get_visible_captcha_element(
    frame, selectors, element_name, log, min_width=30, min_height=10
):
    """获取真正可见的验证码元素。

    从多个候选选择器中找到第一个真正可见（visible）的元素。
    避免选择到隐藏的容器 div。

    Args:
        frame: Playwright Frame 对象
        selectors: 选择器字符串（逗号分隔的多个选择器）
        element_name: 元素名称（用于日志）
        log: Logger 对象
        min_width: 最小宽度（像素），默认30
        min_height: 最小高度（像素），默认10

    Returns:
        可见的元素 Locator，如果没有找到则返回 None
    """
    # 拆分多个选择器
    selector_list = [s.strip() for s in selectors.split(",")]

    # 首先尝试找到所有候选元素
    for selector in selector_list:
        try:
            loc = frame.locator(selector)
            count = loc.count()
            if count == 0:
                continue

            # 检查每个匹配的元素，找到第一个真正可见的
            for i in range(count):
                try:
                    el = loc.nth(i)
                    if el.is_visible():
                        # 额外检查：确保元素有实际尺寸
                        bb = el.bounding_box()
                        if (
                            bb
                            and bb.get("width", 0) > min_width
                            and bb.get("height", 0) > min_height
                        ):
                            log.debug(
                                f"[点选验证码] 找到可见的{element_name}: {selector} (索引 {i}), bbox={bb['width']:.0f}x{bb['height']:.0f}"
                            )
                            return el
                except Exception:
                    continue
        except Exception:
            continue

    return None


def _handle_captcha_core(frame, log) -> bool:
    """统一的验证码处理核心（供 handle_click_captcha_in_frame 和 handle_click_captcha 共用）。

    Args:
        frame: Playwright Frame 对象
        log: Logger 对象
    """
    try:
        state = _get_captcha_url_state(frame)
        log.info("[点选验证码] 开始自动识别处理...")
        log.debug(
            f"[点选验证码] 验证码上下文: url={state['url']} | "
            f"mcwk={str(state['is_mcwk']).lower()} | cscapt={state['cscapt']}"
        )

        # ---- 等待验证码元素真正出现 ----
        max_wait_attempts = 15
        prompt_el = None
        main_el = None

        for attempt in range(max_wait_attempts):
            if not prompt_el:
                prompt_el = _get_visible_captcha_element(
                    frame,
                    _SEL_CAPTCHA_PROMPT,
                    "提示图",
                    log,
                    min_width=15,
                    min_height=8,
                )
            if not main_el:
                main_el = _get_visible_captcha_element(
                    frame, _SEL_CAPTCHA_BG, "主背景图", log
                )

            if main_el:
                if prompt_el:
                    log.debug(f"[点选验证码] 验证码已真正可见 (尝试 {attempt + 1})")
                    break
                else:
                    if attempt > 3:
                        try:
                            prompt_container = frame.locator(
                                ".tencent-captcha-dy__header-answer"
                            ).first
                            if (
                                prompt_container.count() > 0
                                and prompt_container.is_visible()
                            ):
                                bb = prompt_container.bounding_box()
                                if (
                                    bb
                                    and bb.get("width", 0) > 20
                                    and bb.get("height", 0) > 10
                                ):
                                    prompt_el = prompt_container
                                    break
                        except Exception:
                            pass
                    if attempt > 8:
                        log.warning(
                            "[点选验证码] 只找到主图，未找到提示图，尝试仅使用主图处理"
                        )
                        break

            time.sleep(1.5)
        else:
            if main_el:
                log.warning("[点选验证码] 只有主图，没有提示图，尝试继续处理")
            else:
                log.error("[点选验证码] 主图也未找到，无法处理")
                return False

        confirm_btn = frame.locator(_SEL_CAPTCHA_CONFIRM_BTN).first
        time.sleep(0.8)

        # ---- 重新获取元素（防止 stale element） ----
        main_el = _get_visible_captcha_element(frame, _SEL_CAPTCHA_BG, "主背景图", log)
        if main_el is None:
            log.error("[点选验证码] 重新获取主图失败")
            return False

        prompt_el = _get_visible_captcha_element(
            frame, _SEL_CAPTCHA_PROMPT, "提示图", log, min_width=15, min_height=8
        )
        if prompt_el is None:
            prompt_container = frame.locator(".tencent-captcha-dy__header-answer").first
            if prompt_container.count() > 0 and prompt_container.is_visible():
                bb = prompt_container.bounding_box()
                if bb and bb.get("width", 0) > 20:
                    prompt_el = prompt_container

        # ---- 获取提示图（截图，失败时从主图顶部裁剪） ----
        prompt_bytes = None
        if prompt_el is not None:
            for retry in range(3):
                try:
                    if prompt_el is None or not prompt_el.is_visible():
                        prompt_el = _get_visible_captcha_element(
                            frame,
                            _SEL_CAPTCHA_PROMPT,
                            "提示图",
                            log,
                            min_width=15,
                            min_height=8,
                        )
                        if prompt_el is None:
                            break
                    assert prompt_el is not None
                    prompt_bytes = prompt_el.screenshot(
                        timeout=5000, animations="disabled"
                    )
                    if prompt_bytes:
                        break
                except Exception as e:
                    log.warning(
                        f"[点选验证码] 提示图截图失败 (重试 {retry + 1}/3): {e}"
                    )
                    time.sleep(0.5)

        if prompt_bytes is None:
            try:
                main_el = _get_visible_captcha_element(
                    frame, _SEL_CAPTCHA_BG, "主背景图", log
                )
                if main_el is None:
                    raise Exception("无法获取主图")
                temp_main_bytes = main_el.screenshot(
                    timeout=5000, animations="disabled"
                )
                if temp_main_bytes:
                    temp_main_img = cv2.imdecode(
                        np.frombuffer(temp_main_bytes, np.uint8), cv2.IMREAD_COLOR
                    )
                    if temp_main_img is not None:
                        h, w = temp_main_img.shape[:2]
                        prompt_img = temp_main_img[0 : int(h * 0.25), 0:w]
                        _, prompt_bytes = cv2.imencode(".png", prompt_img)
                        prompt_bytes = prompt_bytes.tobytes()
                        log.debug(
                            f"[点选验证码] 从主图裁剪提示区域: {w}x{int(h * 0.25)}"
                        )
            except Exception as e:
                log.debug(f"[点选验证码] 裁剪提示区域失败: {e}")

        if prompt_bytes is None:
            log.error("[点选验证码] 无法获取提示图，无法识别")
            return False

        # ---- 获取主图 ----
        main_el = _get_visible_captcha_element(frame, _SEL_CAPTCHA_BG, "主背景图", log)
        if main_el is None:
            log.error("[点选验证码] 获取主图失败")
            return False

        try:
            main_el.scroll_into_view_if_needed(timeout=3000)
            time.sleep(0.3)
        except Exception:
            pass

        try:
            assert main_el is not None
            main_bytes = main_el.screenshot(timeout=5000, animations="disabled")
        except Exception as e:
            log.warning(f"[点选验证码] 主图截图失败: {e}")
            return False

        if not main_bytes:
            log.warning("[点选验证码] 截图数据为空，无法识别")
            return False

        # ---- 识别坐标 ----
        log.debug("[点选验证码] 开始识别坐标...")
        points = detect_captcha(cast(bytes, prompt_bytes), cast(bytes, main_bytes))

        if not points:
            log.warning("[点选验证码] 未识别到有效坐标")
            return False

        log.info(f"[点选验证码] 识别到 {len(points)} 个目标点: {points}")

        # ---- 坐标缩放 ----
        img_arr = cv2.imdecode(np.frombuffer(main_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img_arr is None:
            log.warning("[点选验证码] 无法解码主图")
            return False

        img_h, img_w = img_arr.shape[:2]
        main_bb = None
        try:
            main_bb = main_el.bounding_box(timeout=3000)
        except Exception:
            pass

        scale_x = 1.0
        scale_y = 1.0
        if main_bb and main_bb["width"] > 0 and main_bb["height"] > 0:
            scale_x = main_bb["width"] / img_w
            scale_y = main_bb["height"] / img_h
            log.debug(
                f"[点选验证码] 坐标缩放: 截图={img_w}x{img_h}, "
                f"元素={main_bb['width']:.0f}x{main_bb['height']:.0f}, "
                f"比例=({scale_x:.2f},{scale_y:.2f})"
            )

        # ---- 点击目标点 ----
        click_success_count = 0
        for i, (px, py) in enumerate(points):
            rel_x = px * scale_x
            rel_y = py * scale_y

            if main_bb and main_bb["width"] > 0 and main_bb["height"] > 0:
                rel_x = max(10, min(rel_x, main_bb["width"] - 10))
                rel_y = max(10, min(rel_y, main_bb["height"] - 10))

            # 重新检查可见性（防止 stale element）
            try:
                if main_el is None or not main_el.is_visible():
                    main_el = _get_visible_captcha_element(
                        frame, _SEL_CAPTCHA_BG, "主背景图", log
                    )
                    if main_el is None:
                        return False
                    main_bb = main_el.bounding_box(timeout=2000)
            except Exception:
                pass

            click_success = False
            for retry in range(2):
                try:
                    assert main_el is not None
                    main_el.click(
                        position={"x": rel_x, "y": rel_y}, force=True, timeout=5000
                    )
                    log.debug(f"[点选验证码] 第 {i + 1} 个目标点击成功")
                    click_success = True
                    click_success_count += 1
                    break
                except Exception as e:
                    if retry == 0:
                        time.sleep(0.5)
                        try:
                            main_el = _get_visible_captcha_element(
                                frame, _SEL_CAPTCHA_BG, "主背景图", log
                            )
                            if main_el:
                                main_bb = main_el.bounding_box(timeout=2000)
                        except Exception:
                            pass
                    else:
                        log.error(f"[点选验证码] 第 {i + 1} 个目标点击失败: {e}")

            if not click_success:
                continue

            time.sleep(0.4 + random.uniform(0.1, 0.3))

        if click_success_count == 0:
            log.error("[点选验证码] 所有目标点都点击失败")
            return False

        log.info(f"[点选验证码] 成功点击 {click_success_count}/{len(points)} 个目标")
        time.sleep(0.8)

        # ---- 点击确认按钮 ----
        if confirm_btn.count() > 0:
            try:
                confirm_btn.wait_for(state="visible", timeout=3000)
                if confirm_btn.is_enabled():
                    confirm_btn.click()
                    time.sleep(2.0)
            except Exception as e:
                log.debug(f"[点选验证码] 确认按钮点击失败: {e}")
        else:
            time.sleep(2.0)

        # ---- 验证结果检查 ----
        for check in range(2):
            still_has_captcha = _captcha_visible(frame, require_cscapt=False)
            if not still_has_captcha:
                log.info("[点选验证码] 验证码已消失，验证成功")
                return True

            error_tip = frame.locator(_SEL_CAPTCHA_ERROR_TIP).first
            if error_tip.count() > 0 and error_tip.is_visible():
                error_text = ""
                try:
                    error_text = error_tip.inner_text(timeout=1000)
                except Exception:
                    pass
                log.warning(f"[点选验证码] 检测到错误提示: {error_text}")
                return False

            if check == 0:
                time.sleep(1.0)

        log.warning("[点选验证码] 验证码仍在，验证可能失败")
        return False

    except Exception as e:
        log.warning(f"[点选验证码] 处理异常: {e}")
        return False


def handle_click_captcha_in_frame(frame, log) -> bool:
    """在指定 frame 内处理腾讯点选验证码（课程完成场景）。"""
    return _handle_captcha_core(frame, log)


def handle_click_captcha(page, log) -> bool:
    """自动处理腾讯点选验证码（考试/通用场景）。

    识别提示图中的目标字符，在主图上按顺序点击，再点击确认按钮等待结果。
    返回 True 表示验证通过，False 表示失败或未检测到验证码。
    """
    ctx, vendor_hint = _find_captcha_context(page)
    if ctx is None:
        log.debug("[点选验证码] 未检测到有效验证码上下文")
        _log_captcha_contexts(page, log)
        return False

    log.info(f"[{vendor_hint or '点选验证码'}] 开始自动识别...")
    return _handle_captcha_core(ctx, log)


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
    return _handle_captcha_core(ctx, log)


def has_captcha(page, require_cscapt: bool = True) -> bool:
    """检测当前页面是否弹出了点选验证码。

    Args:
        page: Playwright Page 对象
        require_cscapt: 是否要求 cscapt=true 参数（默认 True）
    """
    ctx, _ = _find_captcha_context(page, require_cscapt)
    return ctx is not None


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
