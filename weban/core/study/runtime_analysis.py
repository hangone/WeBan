import re
from typing import Iterable

_CONTROL_PAGE_MARKERS = (
    "btn-next-prev",
    "btn-next2",
    "page-loading",
    "loading-box",
)
_MAP_BLOCK_RE = re.compile(r"new\s+Map\s*\(\s*\[([\s\S]*?)\]\s*\)")
_MAP_PAIR_RE = re.compile(r"""\[\s*(\d+)\s*,\s*['"]([^'"]+)['"]\s*\]""")


def normalize_page_item_count(page_classes: Iterable[str]) -> int:
    """Return an effective mcwk page count, excluding control-only nodes."""
    raw: list[str] = []
    filtered: list[str] = []

    for cls in page_classes:
        text = (cls or "").strip().lower()
        if not text or "page-item" not in text:
            continue
        raw.append(text)
        if any(marker in text for marker in _CONTROL_PAGE_MARKERS):
            continue
        filtered.append(text)

    if filtered:
        return len(filtered)
    return len(raw)


def extract_nonstr_map_from_text(content: str) -> dict[int, str]:
    """Extract mcwk nonstr tokens from script source."""
    if not content:
        return {}

    best_entries: list[tuple[int, str]] = []

    for match in _MAP_BLOCK_RE.finditer(content):
        body = match.group(1)
        entries: list[tuple[int, str]] = []
        for num, token in _MAP_PAIR_RE.findall(body):
            try:
                entries.append((int(num), token))
            except ValueError:
                continue
        if len(entries) > len(best_entries):
            best_entries = entries

    if not best_entries and (
        "nonstrMap" in content or "callApinext" in content or "new Map" in content
    ):
        entries = []
        for num, token in _MAP_PAIR_RE.findall(content):
            try:
                entries.append((int(num), token))
            except ValueError:
                continue
        best_entries = entries

    if not best_entries:
        return {}

    return dict(best_entries)


def resolve_course_archetype(
    *,
    has_call_apinext: bool,
    has_global_nonstr_map: bool,
    has_page_controller: bool,
    has_animate_public: bool,
    has_map_literal_hint: bool,
) -> str:
    """Choose runtime strategy for the current mcwk course."""
    if has_call_apinext and (has_global_nonstr_map or has_map_literal_hint):
        return "standard"
    if has_page_controller:
        return "webpack"
    if has_animate_public:
        return "animate"
    if has_call_apinext:
        return "standard"
    return "simple"
