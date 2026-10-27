import json
import os
import re
import sys
import threading
import time
import webbrowser
from random import randint
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from uuid import uuid4

from loguru import logger

from api import WeBanAPI
from captcha import CaptchaHandler, LoginCaptchaSolver, is_non_interactive

if getattr(sys, "frozen", False):
    base_path = os.path.dirname(os.path.abspath(sys.executable))
    bundle_path = sys._MEIPASS  # type: ignore[attr-defined]
else:
    base_path = os.path.dirname(os.path.abspath(__file__))
    bundle_path = base_path

# 无交互模式判定复用 captcha.is_non_interactive（ENVIRONMENT=docker / stdin 非 TTY）

# 数据目录模式（main.py --data-dir / WB_DATA_DIR）：answer 也放数据目录，便于挂载持久化
_data_dir = os.environ.get("WB_DATA_DIR", "")
if _data_dir:
    answer_dir = os.path.join(_data_dir, "answer")
else:
    answer_dir = os.path.join(base_path, "answer")
answer_path = os.path.join(answer_dir, "answer.json")
root_answer_path = os.path.join(_data_dir, "answer.json") if _data_dir else os.path.join(base_path, "answer.json")
bundle_answer_path = os.path.join(bundle_path, "answer", "answer.json")


def clean_text(text):
    """只保留字母、数字和汉字，自动去除所有符号和空格

    去除标点/空格后做模糊匹配，确保如「以下说法正确的是（）」能命中
    题库中「以下说法正确的是」。
    :param text: 原始文本
    :return: 仅含字母、数字和汉字的文本
    """
    return re.sub(r"[^\w一-龥]", "", text)


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------


def get_source_str(query: dict) -> str:
    """从 URL 参数推断 sourceStr，与 JS 逻辑一致
    :param query: parse_qs 解析后的 URL 查询参数
    :return: sourceStr 值，如 "LYRA"、"MOON"、"WEIBAN" 等
    """
    if query.get("weiban", [None])[0] != "weiban":
        return "LYRA" if query.get("lyra", [None])[0] == "lyra" else "PROTEUS"
    if query.get("source", [None])[0] == "moon":
        return "MOON"
    return "WEIBAN"


def read_first_existing(paths: list[str]) -> str | None:
    """按序读取第一个存在的本地文件内容（模板/题库的打包版兜底共用）。

    模板（config.example.toml）与题库（answer.json）的下载都会先尝试
    jsDelivr 远程源；失败时回退到本地候选文件（打包内置 _MEIPASS 或
    可执行文件旁），两者共用本函数读取兜底内容。
    :param paths: 候选路径，按优先级排列（如 bundle 内置优先）
    :return: 文件文本；全部不存在或不可读返回 None
    """
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return None


def _check_code_ok(data: dict, allow_200: bool = True) -> bool:
    """接口业务码是否成功（对齐官方 checkCode）

    主站请求封装（app.js request）：Boolean(data) && Number(code)∈{0,1,200}；
    完课 JSONP（sdk.js finishWxCourse）：Boolean(data) && Number(code)∈{0,1}，
    传 allow_200=False 对齐。注意 Number(null)===0，code 为 null 时官方同样视为成功。
    :param data: 接口响应 dict
    :param allow_200: 是否允许 code=200（主站接口 True，完课 JSONP False）
    :return: 业务成功返回 True
    """
    if not data:
        return False
    code = data.get("code")
    try:
        num = int(code) if code is not None else 0
    except (TypeError, ValueError):
        return False
    return num in ((0, 1, 200) if allow_200 else (0, 1))


def _extract_map(content: str) -> dict:
    """从 JS 内容中提取 nonstrMap / pageIdMap

    两阶段匹配：先按命名变量精确匹配 nonstrMap/pageIdMap，
    匹配不到再退回到任意 Map，防止误匹配其他 Map 定义。
    :param content: JS 文件内容
    :return: {step_index: nonstr_value} 映射，未找到返回空字典
    """
    for pattern in [
        r"(?:const|var|let)\s+nonstrMap\s*=\s*new\s+Map\(\[([\s\S]*?)\]\)",
        r"(?:const|var|let)\s+pageIdMap\s*=\s*new\s+Map\(\[([\s\S]*?)\]\)",
    ]:
        match = re.search(pattern, content)
        if match:
            entries = re.findall(r'\[(\d+),\s*[\'"]([^\'"]+)[\'"]\]', match.group(1))
            if entries:
                return {int(step): val for step, val in entries}
    # 退而求其次：匹配任意 Map（变量名未知）
    for m in re.finditer(r"new\s+Map\(\[([\s\S]*?)\]\)", content):
        entries = re.findall(r'\[(\d+),\s*[\'"]([^\'"]+)[\'"]\]', m.group(1))
        if entries:
            return {int(step): val for step, val in entries}
    return {}


# 课程分类：chooseType、显示名、需求数字段、已完成数字段
COURSE_CATEGORIES = (
    (3, "必修课", "requiredNum", "requiredFinishedNum"),
    (1, "推送课", "pushNum", "pushFinishedNum"),
    (2, "自选课", "optionalNum", "optionalFinishedNum"),
)
PROGRESS_FIELDS = {
    *(field for _, _, need, done in COURSE_CATEGORIES for field in (need, done)),
    "examNum", "examFinishedNum",
}

# 课程脚本里的轨迹上报调用
TRACE_NEXT = r'''callApinext\(["']next["']\s*,\s*2'''
TRACE_PREV = r'''callApinext\(["']prev["']\s*,\s*2'''
TRACE_FINISH = r'''callApinext\(["']next["']\s*,\s*1'''

CLASS_ATTR = r'''class=["']([^"']+)["']'''
PAGE_ITEM_SECTION = (
    r'''<section\b[^>]*class=["']([^"']*\bpage-item\b[^"']*)["'][^>]*>'''
    r'''([\s\S]*?)(?=<section\b[^>]*class=["'][^"']*\bpage-item\b|$)'''
)
PAGE_NAME = re.compile(r"page-(?:\d+|aq\d+|at\d+|af\d+)")


def _class_lists(fragment: str) -> list[list[str]]:
    """取片段里所有 class 属性的词列表。"""
    return [value.split() for value in re.findall(CLASS_ATTR, fragment)]


def _has_class(fragment: str, name: str) -> bool:
    return any(name in classes for classes in _class_lists(fragment))


def _page_sections(html: str) -> list[re.Match]:
    """按 page-item 切分课程页面区块。"""
    return list(re.finditer(PAGE_ITEM_SECTION, html))


def _page_names(sections: list[re.Match]) -> list[str]:
    """取每个区块的页码类名，没有则返回空串。"""
    return [next((name for name in section.group(1).split() if PAGE_NAME.fullmatch(name)), "") for section in sections]


def _js_tokens(script: str):
    """按词法区分字符串、正则与注释，避免压缩代码里的 / 被误判。"""
    pattern = re.compile(
        r'''\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[\w$]+|=>|\+\+|--|.''',
        re.DOTALL,
    )
    regex_literal = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n])+/[a-z]*")
    position, allow_regex = 0, True
    prefixes = {"return", "throw", "case", "typeof", "void", "delete", "yield", "await", "in", "of", "new"}
    while position < len(script):
        token = regex_literal.match(script, position) if allow_regex and script[position] == "/" and not script.startswith(("//", "/*"), position) else None
        is_regex = token is not None
        token = token or pattern.match(script, position)
        if token is None:
            break
        value = token.group()
        yield position, value, value.startswith(("//", "/*")) and not is_regex
        position = token.end()
        if value.isspace() or value.startswith(("//", "/*")) and not is_regex:
            continue
        allow_regex = not is_regex and (value in prefixes or value in {"(", "[", "{", "=", ",", ":", ";", "!", "?", "&", "|", "+", "-", "*", "%", "~", "^", "<", ">", "=>"})


def _strip_js_comments(content: str) -> str:
    """删除 JS 注释，保留字符串和正则表达式。"""
    return "".join(" " if comment else value for _, value, comment in _js_tokens(content))


def _js_block_end(script: str, start: int) -> int:
    """查找代码块结尾，不计字符串、正则和注释里的花括号。"""
    depth = 1
    for position, value, comment in _js_tokens(script[start:]):
        if comment:
            continue
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
        if depth == 0:
            return start + position
    return len(script)


def _expand_course_pages(html: str, script: str) -> str:
    """展开课程初始化时插入的数字内容页，不执行下载的 JavaScript。"""
    html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    for template in re.finditer(r"([\w$]+)\s*\+=\s*`([^`]+)`", script):
        fragment = template.group(2)
        if not re.search(r"<section\b[^>]*\bpage-item\b[^>]*\bpage-\$\{", fragment):
            continue  # 课后题 page-aq 等模板由题目分支处理。
        error = "无法解析动态内容页，已停止学习，请在官网完成该课程"
        loops = [
            match for match in re.finditer(r"\bfor\s*\(([^)]*)\)\s*\{", script)
            if match.end() <= template.start() < _js_block_end(script, match.end())
        ]
        if len(loops) != 1:
            raise RuntimeError(error)
        loop = loops[0]
        loop_end = _js_block_end(script, loop.end())
        header = re.fullmatch(
            r"\s*(?:let|var)\s+([\w$]+)\s*=\s*(\d+)\s*;\s*\1\s*(<=?)\s*(\d+)\s*;\s*\1\s*\+\+\s*",
            loop.group(1),
        )
        if not header or script[loop.end():loop_end].strip().rstrip(";").strip() != template.group():
            raise RuntimeError(error)
        variable, first, operator, last = header.groups()
        begin, stop = int(first), int(last) + (operator == "<=")
        if not 0 < stop - begin <= 1000:
            raise RuntimeError(error)
        functions = [
            match for match in re.finditer(r"\bfunction\s+([\w$]+)\s*\(\s*\)\s*\{", script)
            if match.end() <= loop.start() < _js_block_end(script, match.end())
        ]
        if len(functions) != 1:
            raise RuntimeError(error)
        function = functions[0]
        function_end = _js_block_end(script, function.end())
        if not re.search(
            r"\b(?:var|let|const)\s+" + re.escape(template.group(1))
            + r'''\s*=\s*(?:''|"")\s*;\s*$''', script[:function.start()],
        ):
            raise RuntimeError(error)
        if script[function.end():loop.start()].strip() or not re.match(
            r"\s*;?\s*" + re.escape(function.group(1)) + r"\s*\(\s*\)\s*;",
            script[function_end + 1:],
        ):
            raise RuntimeError(error)
        insertion = re.fullmatch(
            r'''\s*\$\(["']\.([\w-]+)["']\)\.after\(\s*'''
            + re.escape(template.group(1)) + r"\s*\)\s*;?\s*",
            script[loop_end + 1:function_end],
        )
        if not insertion:
            raise RuntimeError(error)
        expression = r"\$\{\s*" + re.escape(variable) + r"\s*\}"
        if "${" in re.sub(expression, "0", fragment):
            raise RuntimeError(error)
        anchors = [
            match for match in re.finditer(r'''<section\b[^>]*class=["']([^"']+)["'][^>]*>''', html)
            if insertion.group(1) in match.group(1).split()
        ]
        if len(anchors) != 1:
            raise RuntimeError(error)
        depth, position = 1, None
        for tag in re.finditer(r"<(/?)section\b[^>]*>", html[anchors[0].end():]):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                position = anchors[0].end() + tag.end()
                break
        if position is None:
            raise RuntimeError(error)
        pages = "".join(re.sub(expression, str(index), fragment) for index in range(begin, stop))
        html = html[:position] + pages + html[position:]
    return html


def _video_course_jumps(script: str) -> dict[str, str]:
    """解析播放器结束后不发轨迹的跳页；只接受页面明确给出的目标。"""
    jumps = {}
    hls = re.search(r"function\s+Hlsplay\s*\(([^)]*)\)\s*\{", script)
    if not hls:
        return jumps
    body = script[hls.end():_js_block_end(script, hls.end())]
    if not re.search(r'''(?:\.on|addEventListener)\(["']ended["']''', body):
        return jumps
    if re.search(r"\bcallApinext\s*\(", body):
        return jumps
    params = [value.strip() for value in hls.group(1).split(",")]
    target_arg = re.search(r"\$\(\s*([\w$]+)\s*\)\.addClass\([\"']page-active", body)
    fixed = re.search(r'''\$\(["']\.(page-\d+)["']\)\.addClass\(["']page-active''', body)
    for call in re.finditer(r'''\bHlsplay\(\s*["']([^"']+)["']([^()]*)\)''', script):
        target = fixed.group(1) if fixed else None
        if target_arg and target_arg.group(1) in params:
            args = re.findall(r'''["'][^"']*["']|[\w$]+''', call.group())
            index = params.index(target_arg.group(1)) + 1
            if index >= len(args):
                continue
            value = args[index].strip("\"'")
            if re.fullmatch(r"\.page-\d+", value):
                target = value[1:]
            else:
                assignments = list(re.finditer(r"\b" + re.escape(value) + r'''\s*=\s*["']\.(page-\d+)["']''', script))
                if assignments:
                    target = assignments[-1].group(1)
        if target:
            jumps[call.group(1)] = target
    return jumps


def _direct_course_steps(html: str, script: str) -> tuple[int, int] | None:
    """识别直接绑定按钮的课程，不按课程编号选择规则。"""
    handlers = list(re.finditer(
        r'''(?:\$\(["']\.([^"']+)["']\)\.(?:on|click)\(\s*(?:["']click["']\s*,\s*)?|document\.querySelector\(["']\.([^"']+)["']\)\.addEventListener\(["']click["']\s*,\s*)function\s*\([^)]*\)\s*\{''', script
    ))
    actions = {}
    for handler in handlers:
        selector = handler.group(1) or handler.group(2)
        body = script[handler.end():_js_block_end(script, handler.end())]
        count = len(re.findall(TRACE_NEXT, body))
        if count and selector not in {"btn-prev", "btn-next"}:
            actions[selector] = count
    has_end = bool(re.search(TRACE_FINISH, script))
    if not has_end or not actions:
        return None
    if "btn-next-prev" in html:
        return None
    if re.search(r'''\$\(["']\.btn-next["']\)\.each''', script):
        ordinary = sum(
            any("btn-next" in classes for classes in _class_lists(section.group(2)))
            for section in _page_sections(html)
            if "page-reciprocal" not in section.group(1).split()
        )
        middle = ordinary + sum(actions.values())
    else:
        # 编译模板把开始按钮保存到局部变量后注册事件。
        start = re.search(r'''([\w$]+)=["']\.page-start-btn["']''', script)
        if start:
            binding = re.search(r"document\.querySelector\(" + re.escape(start.group(1)) + r'''\)\.addEventListener\(["']click["']\s*,\s*function\([^)]*\)\s*\{''', script[start.end():])
            if binding:
                offset = start.end() + binding.end()
                body = script[offset:_js_block_end(script, offset)]
                for nested in reversed(list(re.finditer(r"function\s*\([^)]*\)\s*\{", body))):
                    end = _js_block_end(body, nested.end())
                    body = body[:nested.start()] + body[end + 1:]
                actions["page-start-btn"] = len(re.findall(TRACE_NEXT, body))
        middle = sum(actions.values())
    return middle, middle + 1


def _dynamic_course_steps(html: str, script: str, has_questions: bool, question_count: int = 2) -> tuple[int, int] | None:
    """返回动态题目分支的中间步数和完成 step。"""
    question_branch = re.search(
        r"\bif\s*\([^{};]*\bexamQuestionList\.length\s*>\s*0\s*\)\s*\{", script
    )
    selected = ""
    if question_branch:
        block_end = _js_block_end(script, question_branch.end())
        selected = script[question_branch.end():block_end] if has_questions else ""
        chain_end = block_end + 1
        other = re.match(r"\s*else\s*\{", script[chain_end:])
        if other:
            begin = chain_end + other.end()
            block_end = _js_block_end(script, begin)
            if not has_questions:
                selected = script[begin:block_end]
            chain_end = block_end + 1
        script = script[:question_branch.start()] + selected + script[chain_end:]
        # 空题分支会改掉下一页按钮的类名，委托事件随后不再匹配原按钮。
        for mutation in re.finditer(
            r'''\$\(["']\.(page-[\w-]+)\s+\.([\w-]+)["']\)\.addClass\(["']([\w -]+)["']\)\.removeClass\(["']([\w -]+)["']\)''', selected
        ):
            # 改名后若页面显式解绑旧事件，原按钮不再上报；否则旧监听仍有效。
            unbound = re.search(
                r'''\$\([^)]*\)\.off\(["']click["']\)''', script
            ) or re.search(r'''\$\(document\)\.on\(["']click["']\s*,\s*["']\.'''
                           + re.escape(mutation.group(2)) + r'''["']''', script)
            if not unbound:
                continue
            for section in re.finditer(r'''<section\b[^>]*class=["']([^"']+)["'][^>]*>[\s\S]*?</section>''', html):
                if mutation.group(1) not in section.group(1).split():
                    continue
                def update_class(match, mutation=mutation):
                    classes = match.group(1).split()
                    if mutation.group(2) in classes:
                        classes = [name for name in classes if name not in mutation.group(4).split()] + mutation.group(3).split()
                    return 'class="' + " ".join(classes) + '"'
                updated = re.sub(r'''class=["']([^"']+)["']''', update_class, section.group())
                html = html[:section.start()] + updated + html[section.end():]
                break
    # 空题分支会用选择器列表摘掉具体按钮的 btn-next，这些按钮不再上报。
    for selector_list, classes in re.findall(
        r'''\$\(["']([^"']+)["']\)\.removeClass\(["']([\w -]+)["']\)''', selected
    ) if question_branch else []:
        if "btn-next" not in classes.split():
            continue
        for selector in selector_list.split(","):
            name = selector.strip().lstrip(".")
            if not name or name.startswith("page-"):
                continue
            for section in re.finditer(r'''<section\b[^>]*class=["']([^"']+)["'][^>]*>[\s\S]*?</section>''', html):
                if f'"{name}' not in section.group(0) and f' {name} ' not in section.group(0):
                    continue
                def strip_class(match, name=name):
                    items = match.group(1).split()
                    if name not in items:
                        return match.group()
                    return 'class="' + " ".join(item for item in items if item != "btn-next") + '"'
                updated = re.sub(r'''class=["']([^"']+)["']''', strip_class, section.group())
                html = html[:section.start()] + updated + html[section.end():]
                break
    sections = _page_sections(html)
    names = _page_names(sections)
    if not re.search(r"function\s+BtnFn\s*\(", script):
        if not ("btn-next-prev" in html and re.search(r'''\$\(["']\.page-active["']\)\.next\(\)\.addClass\(["']page-active''', script)):
            return _direct_course_steps(html, script)
        numeric = [name for name in names if re.fullmatch(r"page-\d+", name)]
        endpoint = None
        if has_questions:
            inserts = list(re.finditer(r'''\$\(["']\.(page-\d+)["']\)\.after\(''', script))
            endpoint = inserts[-1].group(1) if inserts else None
        else:
            finish_page = re.search(r'''\$\(["']\.(page-\d+)["']\)\.addClass\(["']page-reciprocal["']\)''', script)
            endpoint = finish_page.group(1) if finish_page else None
        next_method = re.search(r'''key:["']_nextPage["']\s*,\s*value:function\([^)]*\)\s*\{''', script)
        unconditional_next = False
        if next_method:
            next_body = script[next_method.end():_js_block_end(script, next_method.end())]
            unconditional_next = "page-reciprocal" not in next_body and bool(re.search(TRACE_NEXT, next_body))
        if endpoint in numeric and not (unconditional_next and not has_questions):
            numeric = numeric[:numeric.index(endpoint) + 1]
        elif has_questions and endpoint not in numeric and re.search(r"getQuestions\s*\(", script):
            return None
        middle = len(numeric) + int(has_questions or unconditional_next)
        return (middle, middle + 1) if numeric else None
    start = re.search(
        r'''BtnFn\(["']\.btn-start["']\s*,\s*["']\.page-start["']\s*,\s*["']\.page-((?:aq)?\d+)["']''', script
    )
    # 题目前可能是内容页内的最后一个 box（例如 .e7），先还原所属 page。
    script = re.sub(
        r'''(BtnFn\(["']\.btn-ce["']\s*,\s*["']\.)([\w-]+)(["']\s*,\s*["']\.page-aq)''',
        lambda match: match.group(1) + next((name for name, section in zip(names, sections, strict=True)
            if any(match.group(2) in classes for classes in _class_lists(section.group(2)))), match.group(2)) + match.group(3), script,
    )
    ends = list(re.finditer(
        r'''BtnFn\(["']\.btn-ce["']\s*,\s*["']\.page-(\d+)["']\s*,\s*["']\.page-aq''', script
    ))
    end = ends[-1] if ends else None
    empty_end = re.search(
        r'''\$\(["']\.page-(\d+)["']\)\.addClass\(["']page-reciprocal["']\)''', script
    )
    if not empty_end:
        empty_end = re.search(
            r'''<section\b[^>]*class=["'][^"']*\bpage-(\d+)\b[^"']*\bpage-reciprocal\b''', html
        )
    dynamic_questions = bool(re.search(r"getQuestions\s*\(", script))
    endpoint = end if has_questions and dynamic_questions else empty_end
    continuation = None
    save_callback = re.search(r"saveExamQuestion\([^;]*?\)\s*\.then\(\s*function\s*\([^)]*\)\s*\{", script)
    if has_questions and save_callback:
        callback = script[save_callback.end():_js_block_end(script, save_callback.end())]
        terminal = re.search(r"if\s*\(\s*atsum\s*>=\s*Examnum\s*\)\s*\{", callback)
        if terminal:
            branch_body = callback[terminal.end():_js_block_end(callback, terminal.end())]
            target = re.search(r'''\$\(["']\.(page-\d+)["']\)\.addClass\(["']page-active''', branch_body)
            if target and not re.search(r"\bfinishWxCourse\s*\(", branch_body):
                continuation = target.group(1)
                endpoint = empty_end
    btnfn = re.search(r"function\s+BtnFn\([^)]*\)\s*\{", script)
    static_end = next((name for name, section in zip(names, sections, strict=True) if "page-reciprocal" in section.group(1).split()), None)
    if not start or not (endpoint or static_end) or not btnfn:
        return None
    body = script[btnfn.end():_js_block_end(script, btnfn.end())]
    next_pattern = TRACE_NEXT
    prev_pattern = TRACE_PREV
    button_test = r'''Btn\s*==+\s*["']([^"']+)["']'''
    branch_pattern = r'''if\s*\(\s*((?:Btn\s*==+\s*["'][^"']+["']\s*)(?:\|\|\s*Btn\s*==+\s*["'][^"']+["']\s*)*)\)\s*\{'''
    deltas = {}
    selectors = {".btn-start", ".btn-ce"}
    selectors.update(re.findall(r'''BtnFn\(["']([^"']+)["']\s*,''', script))
    for selector in selectors:
        selected_body = body
        while branch := re.search(branch_pattern, selected_body):
            begin = branch.end()
            block_end = _js_block_end(selected_body, begin)
            selected = selected_body[begin:block_end] if selector in re.findall(button_test, branch.group(1)) else None
            chain_end = block_end + 1
            while other := re.match(r"\s*else\s*(?:" + branch_pattern[:-2] + r")?\s*\{", selected_body[chain_end:]):
                begin = chain_end + other.end()
                block_end = _js_block_end(selected_body, begin)
                if selected is None and (other.group(1) is None or selector in re.findall(button_test, other.group(1))):
                    selected = selected_body[begin:block_end]
                chain_end = block_end + 1
            selected_body = selected_body[:branch.start()] + (selected or "") + selected_body[chain_end:]
        deltas[selector] = len(re.findall(next_pattern, selected_body)) - len(re.findall(prev_pattern, selected_body))
    start_page = f"page-{start.group(1)}"
    last_page = f"page-{endpoint.group(1)}" if endpoint else static_end
    if not dynamic_questions and static_end:
        last_page = static_end
    if start_page not in names or last_page not in names:
        return None
    first, last = names.index(start_page), names.index(last_page)
    if first > last:
        return None
    middle = deltas[".btn-start"]
    video_jumps = _video_course_jumps(script)
    skipped = set()
    for section, name in zip(sections, names, strict=True):
        for identifier, target in video_jumps.items():
            if re.search(r'''\bid=["']''' + re.escape(identifier) + r'''["']''', section.group(2)) and target in names:
                begin, stop = names.index(name), names.index(target)
                if begin < stop:
                    skipped.update(names[begin:stop])
    bindings = list(re.finditer(
        r'''BtnFn\(["']([^"']+)["']\s*,\s*["']\.([^"']+)["']\s*,\s*["']\.([^"']+)["']''', script
    ))
    merge_targets: dict[str, list[str]] = {}
    for binding in bindings:
        source, target = binding.group(2), binding.group(3)
        if source in names and target in names and binding.group(1) not in {".btn-start", ".btn-ce", ".btn-prev-start"}:
            merge_targets.setdefault(target, []).append(source)
    for sources in merge_targets.values():
        sources = list(dict.fromkeys(sources))
        if len(sources) > 1 and all(not _has_class(sections[names.index(source)].group(2), "btn-next") for source in sources):
            skipped.update(sources[1:])
    nested_selectors = set()
    for helper in re.finditer(r"function\s+([\w$]+)\s*\(\s*([\w$]+)\s*\)\s*\{", script):
        helper_body = script[helper.end():_js_block_end(script, helper.end())]
        if re.search(r'''\.parents\(["']\.box["']\)\.next\(''', helper_body) and re.search(next_pattern, helper_body):
            nested_selectors.update(re.findall(re.escape(helper.group(1)) + r'''\(["']\.([^"']+)["']\)''', script))
    nav_selectors = {"btn-next"}
    for nav in re.finditer(
        r'''\$\(["']\.([\w-]+)["']\)\.(?:each|click|on)\(\s*(?:["'][^"']+["']\s*,\s*)?function\s*\([^)]*\)\s*\{''', script
    ):
        nav_body = script[nav.end():_js_block_end(script, nav.end())]
        if nav.group(1) not in {"btn-at", "btn-at01", "btn-af"} and re.search(next_pattern, nav_body) and (
            nav.group(1).startswith("btn-next")
            or re.search(r'''\.parents\(["']\.page-item["']\)\.next\(\)\.addClass\(["']page-active''', nav_body)
        ):
            nav_selectors.add(nav.group(1))
    for section in sections[first:last + 1]:
        if (not (has_questions and dynamic_questions) or continuation) and last_page in section.group(1).split():
            if nested_selectors:
                middle += sum(bool(nested_selectors.intersection(classes)) for classes in _class_lists(section.group(2))) - 1
            continue  # 完成页按钮不再发送中间步骤。
        section_name = next((name for name in section.group(1).split() if name in names), "")
        if section_name in skipped:
            continue
        video = re.search(r'''<video\b[^>]*\bid=["']([^"']+)["']''', section.group(2))
        auto_video = False
        if video:
            player = re.search(r'''videojs\(["']''' + re.escape(video.group(1)) + r'''["'][\s\S]*?function\s*\([^)]*\)\s*\{''', script)
            if player:
                player_body = script[player.end():_js_block_end(script, player.end())]
                auto_video = bool(re.search(r'''\$\(["']\.page-\d+["']\)\.addClass\(["']page-active["']\)''', player_body))
        if not auto_video and not _has_class(section.group(2), "btn-next-end"):
            middle += int(any(nav_selectors.intersection(classes) for classes in _class_lists(section.group(2))))
        middle += sum(bool(nested_selectors.intersection(classes)) for classes in _class_lists(section.group(2)))
        page = next((name for name in section.group(1).split() if re.fullmatch(r"page-\d+", name)), "")
        for binding in re.finditer(
            r'''BtnFn\(["']([^"']+)["']\s*,\s*["']\.([^"']+)["']\s*,\s*["']\.([^"']+)["']''', script
        ):
            if (binding.group(2) == page and binding.group(1) not in ({".btn-prev-start"} | ({".btn-ce"} if dynamic_questions else set()))
                    and binding.group(3) in names and names.index(binding.group(3)) > names.index(page)):
                middle += deltas.get(binding.group(1), 0)
    # 情景按钮也会在动画回调中上报。
    reached = set(names[first:last + 1]) - skipped
    for helper in re.finditer(r"function\s+([\w$]+)\s*\(([^)]*)\)\s*\{", script):
        params = [value.strip() for value in helper.group(2).split(",")]
        if len(params) < 2:
            continue
        callback = script[helper.end():_js_block_end(script, helper.end())]
        targets = [param for param in params[1:] if re.search(
            r"\$\(\s*" + re.escape(param) + r'''\s*\)\.addClass\(["']page-active''', callback
        )]
        if not targets or not re.search(r"\$\(\s*" + re.escape(params[0]) + r"\s*\)\.click", callback):
            continue
        count = len(re.findall(next_pattern, callback))
        sources = set()
        for call in re.finditer(re.escape(helper.group(1)) + r"\(([^()]*)\)", script):
            args = [value.strip().strip("\"'") for value in call.group(1).split(",")]
            if len(args) != len(params):
                continue
            selector = args[0].lstrip(".")
            if selector in nav_selectors:
                continue
            destinations = [args[params.index(target)] for target in targets]
            if not all(re.fullmatch(r"\.page-\d+", value) for value in destinations):
                continue  # 情景按钮只在跳转到具体内容页时上报，题目页另有规则。
            if not any(value.lstrip(".") in reached for value in destinations):
                continue
            for name, section in zip(names, sections, strict=True):
                if name in reached and any(selector in classes for classes in _class_lists(section.group(2))):
                    # 同一页上不同按钮各自跳不同目标页，各算一次上报。
                    sources.add((name, tuple(destinations)))
        middle += len(sources) * count
    handlers = list(re.finditer(
        r'''\$\(["']([^"']+)["']\)\.(?:click|on)\(\s*(?:["'][^"']+["']\s*,\s*)?function\s*\([^)]*\)\s*\{''', script
    ))
    for handler in handlers:
        if handler.group(1) in {".btn-next", ".btn-prev", ".btn-af"}:
            continue
        callback = script[handler.end():_js_block_end(script, handler.end())]
        if not re.search(r"\bcallApinext\s*\(", callback) and handler.group(1).startswith("."):
            # 有些情景按钮直接转到下一内容页，不触发原来的 btn-next。
            # 逗号选择器（如 ".p1602,.p1603"）同样属于该页的情景按钮。
            selectors = {
                part.strip()[1:] for part in handler.group(1).split(",")
                if part.strip().startswith(".")
            }
            source_pages = [
                name for name, section in zip(names, sections, strict=True)
                if selectors and any(selectors.intersection(classes) for classes in _class_lists(section.group(2)))
            ]
            targets = re.findall(r'''\$\(["']\.(page-\d+)["']\)\.addClass\(["']page-active["']\)''', callback)
            if len(source_pages) == 1 and targets:
                source = source_pages[0]
                if source in reached and source != last_page and all(target in names and names.index(target) == names.index(source) + 1 for target in targets):
                    section = sections[names.index(source)]
                    if any("btn-next" in classes for classes in _class_lists(section.group(2))):
                        middle -= 1
        for call in re.finditer(next_pattern, callback):
            targets = re.findall(
                r'''\$\(["']\.(page-\d+)["']\)\.addClass\(["']page-active["']\)''',
                callback[:call.start()],
            )
            if targets and targets[-1] in reached:
                repeats = 1
                for animation in re.finditer(
                    r'''\$\(["']\.([\w-]+)["']\)\.animate\(\s*\{[^}]*\}\s*,\s*[^,]+,\s*function\s*\([^)]*\)\s*\{''',
                    callback,
                ):
                    if animation.end() <= call.start() < _js_block_end(callback, animation.end()):
                        # complete 会对每个匹配元素执行一次。
                        repeats *= sum(animation.group(1) in classes for classes in _class_lists(html))
                middle += repeats
    delta = deltas[".btn-ce"] * len(ends) if has_questions and dynamic_questions else 0
    if continuation and save_callback:
        callback = script[save_callback.end():_js_block_end(script, save_callback.end())]
        middle += question_count * len(re.findall(next_pattern, callback))
    if re.search(r'''\bslFn\(["']''', script):
        inline_questions = len(re.findall(r'''\bslFn\(["']''', script))
        slfn = re.search(r"function\s+slFn\([^)]*\)\s*\{", script)
        if slfn:
            sl_body = script[slfn.end():_js_block_end(script, slfn.end())]
            correct = re.search(r"if\s*\(\s*arr\.uniq\(\)\.sort\(\)\.toString\(\)\s*==+\s*arrItem\.sort\(\)\.toString\(\)\s*\)\s*\{", sl_body)
            if correct:
                stop = _js_block_end(sl_body, correct.end())
                other = re.match(r"\s*else\s*\{", sl_body[stop + 1:])
                if other:
                    end = stop + 1 + other.end()
                    sl_body = sl_body[:correct.start()] + sl_body[correct.end():stop] + sl_body[_js_block_end(sl_body, end) + 1:]
            middle += inline_questions * len(re.findall(next_pattern, sl_body))
        at_handler = re.search(r'''\$\(["']\.btn-at(?:01)?["']\)\.each\(function\s*\([^)]*\)\s*\{''', script)
        if at_handler:
            callback = script[at_handler.end():_js_block_end(script, at_handler.end())]
            count = inline_questions if '.btn-at01' in at_handler.group() else max(inline_questions - 1, 0)
            middle += count * len(re.findall(next_pattern, callback))
    middle += max(delta, 0)
    return middle, middle + min(delta, 0) + 1


def _question_trace_flow(html: str, script: str) -> tuple[int, int]:
    """返回每道课后题成功后及返回正文后需要发送的中间轨迹数。"""
    save = re.search(r"saveExamQuestion\([^;]*?\)\s*\.then\(\s*function\s*\([^)]*\)\s*\{", script)
    if not save:
        return 0, 0
    callback = script[save.end():_js_block_end(script, save.end())]
    per_question = len(re.findall(TRACE_NEXT, callback))
    terminal = re.search(r"if\s*\(\s*atsum\s*>=\s*Examnum\s*\)\s*\{", callback)
    if not terminal:
        return per_question, 0
    body = callback[terminal.end():_js_block_end(callback, terminal.end())]
    target = re.search(r'''\$\(["']\.(page-\d+)["']\)\.addClass\(["']page-active''', body)
    if not target or re.search(r"\bfinishWxCourse\s*\(", body):
        return per_question, 0
    after = 0
    reached = False
    for section in _page_sections(html):
        classes = section.group(1).split()
        reached = reached or target.group(1) in classes
        if reached and ("page-reciprocal" in classes or "page-end" in classes):
            return per_question, after
        if reached:
            after += int(any("btn-next" in item for item in _class_lists(section.group(2))))
    raise RuntimeError("题目返回正文后的轨迹路径无法完整解析，已停止学习")


def _course_completion_timing(script: str, has_questions: bool | None = None) -> tuple[bool, float] | None:
    """提取完课相对最终轨迹的顺序及延迟；分支不一致时不猜。"""
    finishes = list(re.finditer(r"\bfinishWxCourse\s*\(\s*\)", script))
    if not finishes:
        return None
    timers = []
    for timer in re.finditer(r"\bsetTimeout\(\s*(?:function\s*\([^)]*\)|\([^)]*\)\s*=>)\s*\{", script):
        end = _js_block_end(script, timer.end())
        delay = re.match(r"\s*,\s*(\d+(?:e\d+)?)\s*\)", script[end + 1:])
        if delay:
            timers.append((timer.end(), end, float(delay.group(1)) / 1000))
    question_callbacks = []
    for callback in re.finditer(
        r"saveExamQuestion\([^;]*?\)\s*\.then\(\s*function\s*\([^)]*\)\s*\{", script
    ):
        question_callbacks.append((callback.end(), _js_block_end(script, callback.end())))
    for callback in re.finditer(
        r'''(?:\$\(["']\.btn-at["']\)\.(?:on|click)\([^{}]*|\$\(document\)\.on\(["']click["']\s*,\s*["']\.btn-at["']\s*,\s*)function\s*\([^)]*\)\s*\{''', script
    ):
        question_callbacks.append((callback.end(), _js_block_end(script, callback.end())))
    timings = set()
    for finish in finishes:
        in_questions = any(start <= finish.start() < end for start, end in question_callbacks)
        if has_questions is not None and question_callbacks and in_questions != has_questions:
            continue
        containing = [(start, end, delay) for start, end, delay in timers if start <= finish.start() < end]
        inline = re.search(r"setTimeout\(\s*\(\s*\)\s*=>\s*$", script[max(0, finish.start() - 60):finish.start()])
        inline_delay = re.match(r"\s*,\s*(\d+(?:e\d+)?)\s*\)", script[finish.end():]) if inline else None
        if containing or inline_delay:
            delay = float(inline_delay.group(1)) / 1000 if inline_delay else min(containing, key=lambda item: item[1] - item[0])[2]
            timings.add((False, delay))
            continue
        following = script[finish.end():finish.end() + 250]
        preceding = script[max(0, finish.start() - 250):finish.start()]
        if re.search(TRACE_FINISH, following):
            timings.add((True, 0.0))
        elif re.search(TRACE_FINISH, preceding) or not re.search(r"\bcallApinext\s*\(", script):
            timings.add((False, 0.0))
        else:
            return None
    return timings.pop() if len(timings) == 1 else None


def _check_exam(content: str) -> bool:
    """检查 JS 内容中是否包含课后习题相关代码
    :param content: JS 文件内容
    :return: 包含习题相关代码返回 True
    """
    return "saveExamQuestion" in content or "listQuestions" in content


def _count_nav_pages(html: str) -> tuple[int, int]:
    """统计 HTML 中触发向前导航的页面数，以及题目页数。

    统计所有 page-item page-N 区块（排除特殊页），再加回 page-start（点击后触发导航）。
    每个题目页会触发 2 次额外 apinext 调用（提交 → 结果页 → 继续）。

    :return: (nav_pages, question_pages) 基础导航步数 和 题目页数量
    """
    # 统计所有 page-N 区块（排除特殊页面）
    content_pages = 0
    has_start_page = False
    for m in re.finditer(
        r'<section\b[^>]*class="([^"]*\bpage-item\b[^"]*)"',
        html,
    ):
        classes = m.group(1).split()
        if "btn-next-prev" in classes:
            continue  # 集中导航控件，不是内容页
        if {"page-end", "page-success", "page-fail"} & set(classes):
            continue  # 结果页由题目触发，不计入基础导航
        if "page-start" in classes:
            has_start_page = True
            continue  # 单独计数
        page_match = re.search(r"page-(\d+)", m.group(1))
        if page_match:
            content_pages += 1

    # 统计题目页（含 data-all-answer 的 page-options）
    question_pages = 0
    for m in re.finditer(
        r'<section\b[^>]*class="([^"]*\bpage-item\b[^"]*)"[^>]*>'
        r"(?:(?!</section>).)*?(?:data-all-answer|page-commit)",
        html,
        re.DOTALL,
    ):
        page_match = re.search(r"page-(\d+)", m.group(1))
        if page_match:
            question_pages += 1

    # 基础导航步数 = 内容页数 + start（如果有）
    nav_pages = content_pages + (1 if has_start_page else 0)
    return nav_pages, question_pages


def _fetch_text(session, url: str, referer: str | None = None) -> str:
    """读取计算课程路径所必需的 HTML/JS，失败时停止，不能按缺失脚本完课。
    :param session: LoggingSession 实例
    :param url: 目标 URL
    :param referer: 自定义 Referer（抓 mcwk 资源时应传课程播放页 URL，
        否则默认 Referer 为 weiban 根域，资源服务器可能拒绝）
    :return: 响应文本
    """
    headers: dict[str, str | None] = {"X-Token": None, "Origin": None, "Content-Type": None}
    if referer:
        headers["Referer"] = referer
    resp = session.get(url, timeout=10, headers=headers)
    if resp.status_code == 701:
        raise PermissionError("课程资源请求被限制，已停止学习")
    if resp.status_code != 200 or not resp.text.strip():
        raise RuntimeError(f"课程资源读取失败（HTTP {resp.status_code}），已停止学习")
    return resp.text


# ---------------------------------------------------------------------------
# WeBanClient
# ---------------------------------------------------------------------------


class WeBanClient:
    _stdin_lock = threading.Lock()

    def __init__(
        self,
        tenant_name: str,
        account: str | None = None,
        password: str | None = None,
        user: dict[str, str] | None = None,
        log=logger,
        browser_path: str | None = None,
        cdp_host: str | None = None,
        cdp_port: int | None = None,
        debug: bool = False,
        ai_config: dict[str, Any] | None = None,
        video_speed: float = 1.0,
        jupiter_fallback: bool = False,
    ) -> None:
        """
        :param tenant_name: 学校全称
        :param account: 用户名
        :param password: 密码
        :param user: 已有用户凭据 {"userId": ..., "token": ...}，提供则跳过登录
        :param log: logger 实例
        :param browser_path: 浏览器可执行文件路径，用于验证码处理
        :param cdp_host: CDP 远程调试地址
        :param cdp_port: CDP 远程调试端口
        :param debug: 是否启用调试日志
        :param ai_config: AI 搜题配置
        :param video_speed: 视频课程学习倍速，完课前按 视频时长/倍速 等待；
            0 表示不按视频时长等待，只按 study_time 学习时长
        :param jupiter_fallback: 对未加载 apicenext.js 的课程也补发 jupiter
            翻页轨迹。官方页面只有加载 apicenext.js（定义全局 uuid 并调用
            callApinext）的课程才上报轨迹，默认 False 完全对齐官方行为；
            个别学校可能要求该校所有微课都有轨迹，
            实测无轨迹会 10018 时可开启该项
        """
        self.log = log
        self.tenant_name = tenant_name.strip()
        self.study_base_time = 20
        self.study_random_upper = 10
        self.study_force = False
        self.exam_mode = "true"
        self.video_speed = video_speed
        self.jupiter_fallback = jupiter_fallback
        # 时间预估状态（按项目累计实测，样本少时渐进信任实测值）
        self._eta_course_state: dict = {}  # project_id -> {"started_at", "start_finished"}
        self._eta_exam_avg: float | None = None  # 每场考试实测平均耗时（秒）
        self.browser_path = browser_path
        self.cdp_host = cdp_host
        self.cdp_port = cdp_port
        self.ai_config = ai_config
        self._ai_key_warned = False  # api_key 未配置提醒只打一次
        if user and all([user.get("userId"), user.get("token")]):
            self.api = WeBanAPI(user=user, debug=debug, log=log)
        elif all([self.tenant_name, account, password]):
            self.api = WeBanAPI(
                account=account, password=password, debug=debug, log=log
            )
        else:
            self.api = WeBanAPI(debug=debug, log=log)
        self.tenant_code = self.get_tenant_code()
        if self.tenant_code:
            self.api.set_tenant_code(self.tenant_code)
        else:
            raise ValueError("学校代码获取失败，请检查学校全称是否正确")
        self._captcha_handler = None

    # ---- properties / helpers ------------------------------------------------

    @property
    def captcha_handler(self):
        """延迟初始化 CaptchaHandler（需要 login 后才有 token）
        :return: CaptchaHandler 实例
        """
        if self._captcha_handler is None:
            self._captcha_handler = CaptchaHandler(
                tenant_code=self.tenant_code,
                user_id=self.api.user["userId"],
                token=self.api.user["token"],
                log=self.log,
                browser_path=self.browser_path,
                cdp_host=self.cdp_host,
                cdp_port=self.cdp_port,
            )
        return self._captcha_handler

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """秒数格式化为 XhXXmXXs / XmXXs / Xs"""
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}m{sec:02d}s"
        if m:
            return f"{m}m{sec:02d}s"
        return f"{sec}s"

    def simulate_home_page(self) -> None:
        """执行首页初始化请求。"""
        guide = self.api.get_user_guide()
        if not _check_code_ok(guide):
            raise RuntimeError("获取新手引导失败，已停止当前账号，请前往官网检查")
        guide_data = guide.get("data") or {}
        if not isinstance(guide_data, dict):
            raise RuntimeError("新手引导响应格式异常，已停止当前账号")  # noqa: TRY004
        if guide_data.get("url"):
            raise RuntimeError("请先按原登录方式进入官网完成新手引导，再运行程序")

        batch_code = self.api.user.get("batchCode", "") or ""
        steps: list[tuple[str, Any]] = [
            ("协议内容", self.api.get_ebook),
            ("协议记录", self.api.ebook_record_list),
            ("轮播图", self.api.carousel_list),
            ("用户信息", self.api.my_get_info),
            ("项目统计", self.api.get_project_stat),
            ("公告状态", self.api.notice_index),
            ("公告列表", self.api.notice_list),
            ("功能阀门", self.api.list_valve),
            ("租户配置", self.api.get_simple_config),
            ("帮助文件", self.api.get_help),
            ("学习任务", self.api.list_study_task),
        ]
        for name, fn in steps:
            try:
                res = fn()
                if not _check_code_ok(res):
                    self.log.debug(f"首页{name}返回异常：{res}")
            except PermissionError:
                raise  # Token 失效（被顶号等），立即终止该账号
            except OSError as e:  # 网络异常（DNS/连接/SSL）忽略，不影响主流程
                self.log.debug(f"首页{name}请求失败（网络异常）：{e}")

        # 必读公告：官方弹窗逐条展示，阅读完成后逐条确认（与浏览器行为一致）
        try:
            must = self.api.notice_list_must(batch_code)
            notices = must.get("data") or []
            if not _check_code_ok(must) or not isinstance(notices, list):
                self.log.debug(f"必读公告返回异常：{must}")
                notices = []
            for n in notices:
                nid = n.get("id", "")
                title = n.get("title", "")
                ntype = n.get("type", "")
                file_url = n.get("fileUrl", "")
                min_read = n.get("minReadLength", 0)
                self.log.info(
                    f"必读公告：{title}（ID={nid}，类型={ntype}，"
                    f"链接={file_url}，阅读时长={min_read}秒）"
                )
                # 官方普通类型公告有阅读倒计时（minReadLength 秒），
                # 倒计时结束才可点击"下一条/关闭"确认；上限 300s 防极端值
                if ntype not in (3, 4, 5) and isinstance(min_read, (int, float)) and min_read > 0:
                    time.sleep(min(min_read, 300))
                try:
                    self.api.view_must_notice(nid)
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except OSError as e:
                    self.log.debug(f"确认必读公告失败（网络异常）：{e}")
        except PermissionError:
            raise  # Token 失效，立即终止该账号
        except OSError as e:
            self.log.debug(f"必读公告流程失败（网络异常）：{e}")

        # 问卷：官方在必读公告确认后检查待答问卷，仅拉取并提示，不自动作答
        try:
            q = self.api.questionnaire_list_by_user_id()
            qlist = q.get("data") if isinstance(q.get("data"), list) else []
            if q.get("code", "-1") == "0" and qlist:
                self.log.info(f"存在 {len(qlist)} 个待答问卷（官方会弹窗提示，请前往网页完成）")
        except PermissionError:
            raise  # Token 失效，立即终止该账号
        except OSError as e:
            self.log.debug(f"问卷列表请求失败（网络异常）：{e}")

    def _prompt(self, message: str) -> str:
        """线程安全的 input 封装，多线程下避免 input 输出交错
        :param message: 提示信息
        :return: 去除首尾空白的用户输入
        """
        with self._stdin_lock:
            return input(message).strip()

    def _load_answers_json(self, warn_on_fail: bool = False) -> dict:
        """加载题库，返回 {clean_text(题目): {clean_text(正确选项), ...}}

        :param warn_on_fail: True 时加载失败只警告不抛异常（学习模式容错），
            False 时抛出异常（考试模式必须要有题库）
        :return: 清洗后的题目标题 → 正确答案内容集合的映射
        """
        answers: dict = {}
        # 优先级: 根目录 answer.json > answer/answer.json > 打包内置
        if os.path.exists(root_answer_path):
            load_path = root_answer_path
        elif os.path.exists(answer_path):
            load_path = answer_path
        else:
            load_path = bundle_answer_path
        try:
            with open(load_path, encoding="utf-8") as f:
                for title, options in json.load(f).items():
                    title = clean_text(title)
                    answers.setdefault(title, set()).update(
                        clean_text(a["content"])
                        for a in options.get("optionList", [])
                        if a["isCorrect"] == 1
                    )
        except Exception:
            if warn_on_fail:
                self.log.warning("题库加载失败，课后习题将随机作答")
            else:
                raise
        return answers

    @staticmethod
    def get_project_type(project_category: int) -> str:
        """获取项目类型
        :param project_category: 1.新生安全教育 2.安全课程 3.专题学习 4.军事理论 9.实验室
        :return: 官网项目路由类型，未识别时返回空字符串
        """
        return {1: "pre", 2: "normal", 3: "special", 4: "military", 9: "lab"}.get(project_category, "")

    @staticmethod
    def _project_startable(task: dict) -> tuple[bool, str]:
        """判断项目当前是否可学习（对齐官方 H5 项目入口拦截逻辑）

        官方 H5 首页 navToProject：
        - completion.grey 用 Number(grey)===1 判定（字符串 "1" 同样拦截），
          命中则 alert(completion.message) 并禁止进入；
        - 各分类导航（pre/normal/military）用 active===1 严格相等判定，
          active 不为数字 1 时 alert(message) 并禁止进入；
        - 学习任务列表过滤 grey!==1 && active===1 才纳入可进入列表。
        :param task: listMyProject/listStudyTask 返回的项目 dict
        :return: (是否可学, 服务端提示信息 message，未开始时非空)
        """
        completion = task.get("completion") or {}
        grey = completion.get("grey", 2)  # 1=灰色不可用（未开放/未开始），2=正常
        active = completion.get("active", 1)  # 1=可进入，2=不可进入
        # Number(grey)===1：数字 1 或字符串 "1" 都视为灰色拦截
        try:
            grey_blocked = int(grey) == 1
        except (TypeError, ValueError):
            grey_blocked = False
        active_ok = active == 1  # 官方 === 严格相等，字符串 "1" 不放行
        if not grey_blocked and active_ok:
            return True, ""
        # 官方仅弹 completion.message；studyStateLabel 只是 message 为空时的兜底
        message = completion.get("message") or task.get("studyStateLabel") or ""
        return False, message

    def _build_course_url(self, course: dict, task: dict) -> str:
        """补充播放参数，保留服务端 URL 中已有的值。"""
        url = self.api.get_course_url(course["resourceId"], task["userProjectId"])[
            "data"
        ]
        if int(course.get("source", 1)) == 2:
            return url  # 官网 source=2 直接跳转服务端地址，不拼 iframe 参数。
        parts = urlparse(url)
        params = {
            "userProjectId": task["userProjectId"],
            "userId": self.api.user["userId"],
            "courseId": course["resourceId"],
            "userName": self.api.user.get("userName", self.api.user.get("realName", "")),
            "projectType": task.get("projectType") or self.get_project_type(task.get("projectCategory", 3)),
            "projectId": task.get("projectId", "undefined"), "protocol": "true",
            "link": course.get("praiseNum", ""), "weiban": "weiban",
            "certificateId": "undefined", "userActivityState": "undefined",
            "step": "undefined", "index": "undefined", "viewStep": "undefined",
        }
        existing = parse_qs(parts.query, keep_blank_values=True)
        if existing.get("lyra", [None])[0] == "lyra":
            params.pop("weiban")
        extra = urlencode({key: value for key, value in params.items() if key not in existing})
        query = "&".join(value for value in (parts.query, extra) if value)
        return urlunparse(parts._replace(query=query))

    # ---- tenant / progress --------------------------------------------------

    def get_tenant_code(self) -> str:
        """获取学校代码
        :return: 学校代码（tenant_code），找不到返回空字符串
        """
        if not self.tenant_name:
            self.log.error("学校全称不能为空")
            return ""
        tenant_list = self.api.get_tenant_list_with_letter()
        if tenant_list.get("code", -1) == "0":
            self.log.info("获取学校列表成功")
        tenant_names = []
        maybe_names = []
        for item in tenant_list.get("data", []):
            for entry in item.get("list", []):
                name = entry.get("name", "")
                tenant_names.append(name)
                if self.tenant_name == name.strip():
                    self.log.success(f"找到学校代码: {entry['code']}")
                    return entry["code"]
                if self.tenant_name in name:
                    maybe_names.append(name)
        self.log.error(f"{tenant_names}")
        self.log.error(
            f"没找到你的学校代码，请检查学校全称是否正确"
            f"（上面是有效的学校名称）: {self.tenant_name}"
        )
        if maybe_names:
            self.log.error(f"可能的学校名称: {maybe_names}")
        return ""

    def get_progress(
        self, user_project_id: str, project_prefix: str | None, output: bool = True
    ) -> dict[str, Any]:
        """获取学习进度
        :param user_project_id: 项目 ID
        :param project_prefix: 日志前缀（如项目名）
        :param output: 是否输出进度日志
        :return: show_progress API 原始响应
        """
        progress = self.api.show_progress(user_project_id)
        if not _check_code_ok(progress):
            raise RuntimeError(f"{project_prefix} 获取进度失败，已停止当前账号")
        data = progress.get("data")
        if not isinstance(data, dict) or not PROGRESS_FIELDS.issubset(data):
            raise RuntimeError(f"{project_prefix} 进度响应缺少必要字段，已停止当前账号")
        if self.study_force:
            # force 模式会重新学习所有课程，剩余量按总数计算
            required = data["requiredNum"]
            optional = data["optionalNum"]
            push = data["pushNum"]
        else:
            required = data["requiredNum"] - data["requiredFinishedNum"]
            optional = data["optionalNum"] - data["optionalFinishedNum"]
            push = data["pushNum"] - data["pushFinishedNum"]
        exam_left = data["examNum"] - data["examFinishedNum"]

        # 每门课耗时：项目内累计实测均值，样本少时与理论值渐进混合，
        # 避免首门课带验证码或单次网络波动让 ETA 大幅跳变。
        finished = sum(data[done] for _, _, _, done in COURSE_CATEGORIES)
        now = time.time()
        state = self._eta_course_state.setdefault(
            user_project_id, {"started_at": now, "start_finished": finished}
        )
        completed = max(0, int(finished) - int(state["start_finished"]))
        measured_avg = None
        if completed > 0:
            elapsed = now - float(state["started_at"])
            if elapsed > 900:
                # 长时间中断/卡死后从当前进度重新起算，避免污染后续估算
                state.update(started_at=now, start_finished=finished)
                completed = 0
            else:
                measured_avg = elapsed / completed

        # 每门课：等待时长理论均值 + 固定开销（翻页 step 发送/课后习题/完课 API/验证码等）
        theoretical_est = self.study_base_time + self.study_random_upper / 2 + 6
        if measured_avg is None:
            course_est = theoretical_est
        else:
            trust = completed / (completed + 10)
            course_est = theoretical_est + (measured_avg - theoretical_est) * trust
            course_est = max(theoretical_est, course_est)
        eta = course_est * (required + optional + push)
        # 每场考试：默认 50 题 × 每题 4.5s + 固定开销 ≈ 4 分钟（有实测后自动替换）
        if exam_left > 0 and self.exam_mode != "false":
            exam_est = self._eta_exam_avg or (50 * 4.5 + 15)
            eta += exam_est * exam_left
        eta = max(0, int(eta))
        if output:
            eta_str = self._format_duration(eta)
            self.log.info(
                f"{project_prefix} 进度：必修课 {data['requiredFinishedNum']}/{data['requiredNum']}，"
                f"推送课 {data['pushFinishedNum']}/{data['pushNum']}，"
                f"自选课 {data['optionalFinishedNum']}/{data['optionalNum']}，"
                f"考试 {data['examFinishedNum']}/{data['examNum']}，预计剩余 {eta_str}"
            )
        return progress

    # ---- login --------------------------------------------------------------

    def login(self) -> dict | None:
        """登录并获取 token

        重试策略：前 10 次尝试用 CNN 模型自动识别验证码，
        失败 10 次后转为手动输入（打开图片浏览器），再额外给 3 次机会。
        :return: 成功返回 self.api.user，失败返回 None
        """
        if self.api.user.get("userId"):
            return self.api.user
        retry_limit = 10
        # 前 10 次 OCR 自动识别，后 3 次手动输入
        for i in range(retry_limit + 3):
            if i > 0:
                self.log.warning(f"登录失败，正在重试 {i}/{retry_limit + 2} 次")
            verify_time = self.api.get_timestamp(13, 0)
            verify_image = self.api.rand_letter_image(verify_time)
            if i < retry_limit:
                verify_code = LoginCaptchaSolver.recognize(verify_image, self.log)
                if not verify_code:
                    continue
            elif is_non_interactive():
                # 无交互模式：不阻塞等待手动输入，直接判定失败
                self.log.error(
                    "验证码 OCR 连续失败且处于无交互模式，无法手动输入验证码，"
                    "登录失败（可在宿主机浏览器配合 CDP 或稍后重试）"
                )
                break
            else:
                account_id = (
                    self.api.account or self.api.user.get("userId") or "unknown"
                )
                captcha_dir = os.path.join(base_path, "logs", account_id)
                os.makedirs(captcha_dir, exist_ok=True)
                captcha_path = os.path.join(captcha_dir, "verify_code.png")
                with open(captcha_path, "wb") as f:
                    f.write(verify_image)
                webbrowser.open(f"file://{captcha_path}")
                verify_code = self._prompt(
                    f"[{account_id}] 请在 {captcha_path} 查看验证码图片输入验证码："
                )
                try:
                    os.remove(captcha_path)
                except OSError:
                    pass
            res = self.api.login(verify_code, int(verify_time))
            if res.get("detailCode") == "67":
                self.log.warning("验证码识别失败，正在重试")
                continue
            if self.api.user.get("userId"):
                return self.api.user
            self.log.error(
                f"登录出错，请检查 config.toml 内账号密码，或删除文件后重试: {res}"
            )
            break
        return None

    # ---- project list & per-project cycle ----------------------------------

    def _get_project_list(self) -> list[dict]:
        """获取账号全部进行中的项目列表（含实验室课程合并）"""
        my_project = self.api.list_my_project()
        if not _check_code_ok(my_project):
            self.log.error(f"获取任务列表失败：{my_project}")
            return []
        my_project = my_project.get("data", [])

        completion = self.api.list_completion()
        if not _check_code_ok(completion):
            self.log.error(f"获取模块完成情况失败：{completion}")
        else:
            showable_modules = [
                d["module"] for d in completion.get("data", []) if d["showable"] == 1
            ]
            if "labProject" in showable_modules:
                self.log.info("加载实验室课程")
                lab_project = self.api.lab_index()
                if not _check_code_ok(lab_project):
                    self.log.error(f"获取实验室课程失败：{lab_project}")
                current = lab_project.get("data", {}).get("current") or {}
                if current:
                    my_project.append(current)
        return my_project

    def run_project_cycle(
        self,
        study_time: str | int,
        study_mode: str,
        exam_mode: str,
        random_answer: bool,
        exam_question_time: str,
        exam_submit_match_rate: int,
    ) -> None:
        """按项目交替执行：每个项目先完成课程学习，再完成考试，然后
        切换到下一个项目（用户要求的顺序：项目 A 学习+考试 → 项目 B 学习+考试）。

        考试模式/学习模式为 "false" 时对应阶段整体跳过。
        """
        study = study_mode != "false"
        exam = exam_mode != "false"
        if not study and not exam:
            self.log.info("学习与考试均未开启，跳过")
            return

        if study:
            mode_desc = {"true": "正常", "force": "强制重新学习"}.get(
                study_mode, study_mode
            )
            self.log.info(f"学习模式: {mode_desc}")
        if exam:
            mode_desc = {
                "true": "正常",
                "perfect": "追求满分",
                "force": "强制重考",
            }.get(exam_mode, exam_mode)
            self.log.info(f"考试模式: {mode_desc}")

        projects = self._get_project_list()
        if not projects:
            self.log.warning("当前没有进行中的项目。")
            return

        for project in projects:
            project_name = project.get("projectName", "未知项目")
            user_project_id = project.get("userProjectId", "")
            self.log.info(f"===== 开始处理项目：{project_name} =====")
            if not user_project_id:
                self.log.warning(f"{project_name}：缺少 userProjectId，跳过")
                continue
            if study:
                self.run_study(study_time, study_mode, only_project=project)
            if exam:
                self.run_exam(
                    exam_mode=exam_mode,
                    random_answer=random_answer,
                    exam_question_time=exam_question_time,
                    exam_submit_match_rate=exam_submit_match_rate,
                    only_project=project,
                )

    # ---- study --------------------------------------------------------------

    def run_study(
        self,
        study_time: str | int,
        study_mode: str = "true",
        only_project: dict | None = None,
    ) -> None:
        """主学习流程入口：遍历所有项目 → 分类 → 课程，逐门学习
        :param study_time: 每门课学习时长 "基础时间,随机上限"（秒），如 "20,10"
        :param study_mode: 学习模式，"force" 时忽略完成状态全部重新学习
        :param only_project: 只学习指定项目（按项目交替调用时传入，含
            projectName/userProjectId 等字段）；None 时学习全部项目
        """
        # 解析学习时长
        try:
            parts = str(study_time).split(",")
            self.study_base_time = max(0, int(parts[0]))
            self.study_random_upper = max(0, int(parts[1])) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            self.study_base_time = 20
            self.study_random_upper = 10

        self.log.info(
            f"每门课学习时长: {self._format_duration(self.study_base_time)}~{self._format_duration(self.study_base_time + self.study_random_upper)}"
        )

        force_restudy = study_mode == "force"
        self.study_force = force_restudy

        answers_json = self._load_answers_json(warn_on_fail=True)

        if only_project is not None:
            my_project = [only_project]
        else:
            my_project = self._get_project_list()
        if not my_project:
            self.log.warning("当前没有进行中的学习项目。")

        for task in my_project:
            project_prefix = task["projectName"]
            # 项目未开始（未到开课时间等）：官方 H5 弹 message 并禁止进入，同样提示后跳过
            startable, notice = self._project_startable(task)
            if not startable:
                self.log.warning(
                    f"{project_prefix}：{notice or '项目尚未开放，暂不可学习'}，跳过"
                )
                continue
            self.log.info(f"开始处理任务：{project_prefix}")
            # 对齐官方 H5：进入学习项目页即发 initIndex（项目详情初始化），
            # 不依赖课程是否加载 apicenext.js
            try:
                self.api.init_index(task["userProjectId"])
            except PermissionError:
                raise  # Token 失效，立即终止该账号
            except OSError as e:  # 网络异常不阻断学习
                self.log.warning(f"初始化学习索引失败（网络异常）：{e}")
            progress = self.get_progress(task["userProjectId"], project_prefix)
            progress_data = (
                progress.get("data", {}) if _check_code_ok(progress) else {}
            )

            for choose_type in COURSE_CATEGORIES:
                # 只跳过"项目无该类型需求"（需求数=0）的类型：
                # - need > 0（如项目确实要完成 5 门自选课）→ 正常学习该类型；
                # - need == 0（未配置该类型，如本例自选课 optionalNum=0）→
                #   整体跳过，避免把可选课池里未报名的课程当任务学
                #   （v3.9.8 #147 让自选课可真学，此前会把整个课池学完）
                need = int(progress_data.get(choose_type[2], 0) or 0)
                if need <= 0:
                    self.log.info(
                        f"{project_prefix} 无{choose_type[1]}需求"
                        f"（{choose_type[2]}={need}），跳过该类型"
                    )
                    continue
                # 官方 H5 课程主页按项目 projectMode 分流课程列表：
                # mode==1 折叠分类（listCategory+listCourse）；mode≠1 扁平分页
                # （listFlatCourse.do）。取不到 mode 时按折叠路径（原行为）兜底。
                try:
                    simple = self.api.get_project_simple(task["userProjectId"])
                    project_mode = int(
                        simple.get("data", {}).get("projectMode", 1) or 1
                    )
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except OSError as e:
                    self.log.debug(f"获取项目模式失败（网络异常）：{e}")
                    project_mode = 1
                if project_mode != 1:
                    self._study_flat_courses(
                        task,
                        choose_type,
                        project_prefix,
                        answers_json,
                        force_restudy,
                    )
                    continue

                try:
                    categories = self.api.list_category(
                        task["userProjectId"], choose_type[0]
                    )
                except PermissionError:
                    raise  # Token 失效，立即终止该账号
                except OSError as e:  # 网络异常（DNS/连接/SSL）跳过本分类，不中断整个账号
                    self.log.error(f"获取 {choose_type[1]} 分类失败（网络异常）：{e}")
                    continue
                if not _check_code_ok(categories):
                    self.log.error(f"获取 {choose_type[1]} 分类失败：{categories}")
                    continue

                for category in categories.get("data", []):
                    category_prefix = (
                        f"{choose_type[1]} {project_prefix}/{category['categoryName']}"
                    )
                    if (
                        not force_restudy
                        and category["finishedNum"] >= category["totalNum"]
                    ):
                        continue

                    courses = self.api.list_course(
                        task["userProjectId"], category["categoryCode"], choose_type[0]
                    )
                    for course in courses.get("data", []):
                        if not force_restudy and int(course.get("finished", 0)) == 1:
                            continue
                        if not self._learn_course(
                            course,
                            task,
                            category_prefix,
                            project_prefix,
                            answers_json,
                            force_restudy,
                        ):
                            return

            self.log.success(f"{project_prefix} 课程学习完成")
            self._check_project_course_done(task, project_prefix)

    def _check_project_course_done(
        self, task: dict, project_prefix: str
    ) -> None:
        """校验项目各类型课程完成数是否达到需求数，不足时告警。

        服务端进度更新可能有延迟，因此只告警不重试；覆盖折叠/扁平两种
        列表路径学完后的盲区（如扁平分页提前结束导致漏学）。
        """
        try:
            progress = self.get_progress(
                task["userProjectId"], project_prefix, output=False
            )
            if not _check_code_ok(progress):
                return
            data = progress.get("data", {})
            for _, label, need_key, finished_key in COURSE_CATEGORIES:
                need = int(data.get(need_key, 0) or 0)
                finished = int(data.get(finished_key, 0) or 0)
                if need > 0 and finished < need:
                    self.log.warning(
                        f"{project_prefix} {label}完成 {finished}/{need}，"
                        f"未达到需求数，请检查是否漏学"
                    )
        except PermissionError:
            raise  # Token 失效，立即终止该账号
        except OSError as e:
            self.log.debug(f"校验学习完成进度失败（网络异常）：{e}")

    def _learn_course(
        self,
        course: dict,
        task: dict,
        category_prefix: str,
        project_prefix: str,
        answers_json: dict,
        force_restudy: bool,
    ) -> bool:
        """学习单门课程并校验进度是否更新（折叠/扁平两条列表路径共用）。

        :return: True 可继续下一门；False 表示账号异常/锁定，应停止本账号
        """
        course_prefix = f"{category_prefix}/{course['resourceName']}"
        try:
            progress_before = self.get_progress(
                task["userProjectId"], project_prefix, output=False
            )
            finished_before = 0
            if progress_before.get("code", -1) == "0":
                d = progress_before["data"]
                finished_before = sum(d[done] for _, _, _, done in COURSE_CATEGORIES)
            ok = self._study_one_course(
                course,
                task,
                category_prefix,
                project_prefix,
                answers_json,
                force_restudy,
            )
            if not ok:
                raise PermissionError("检测到行为异常或账号锁定，已停止当前账号")
            progress_after = self.get_progress(task["userProjectId"], project_prefix)
            if progress_after.get("code", -1) == "0":
                d = progress_after["data"]
                finished_after = (
                    d["requiredFinishedNum"]
                    + d["pushFinishedNum"]
                    + d["optionalFinishedNum"]
                )
                if finished_after <= finished_before:
                    self.log.warning(
                        f"{course_prefix}：完课成功但进度未更新，请手动检查"
                    )
        except PermissionError:
            raise  # Token 失效，立即终止该账号
        except OSError as e:
            # 网络异常（DNS/连接/SSL）跳过本门课程，不中断整个账号；
            # 未完成的课程下次运行会自动重学
            self.log.warning(f"{course_prefix}：网络异常，跳过本门课程（{e}）")
        return True

    def _study_flat_courses(
        self,
        task: dict,
        choose_type: tuple,
        project_prefix: str,
        answers_json: dict,
        force_restudy: bool,
    ) -> None:
        """官方 projectMode≠1 的扁平分页课程列表路径（listFlatCourse.do）

        官方 H5 课程主页按 project/getSimple.do 的 projectMode 分流：
        mode==1 走折叠分类（listCategory + listCourse），mode≠1 全部 tab
        走 listFlatCourse.do 分页列表（平铺渲染，无分类层级）。日志前缀的
        分类名取课程对象的 categoryName（若服务端返回该字段），否则回退到
        tab 名（如"自选课"）；仅用于日志展示，不影响学习逻辑。

        对齐官方前端（app.js loadCourseDataByPage）：pageSize=12、pageNo
        从 1 递增、`finished = totalPages <= pageNo` 翻到最后一页为止，
        把全部页的 paginateData 拼接成完整课程列表。**先翻完所有页收集
        完整列表，再逐门学习**——不能在"边学边翻页"时翻页：listFlatCourse
        的排序会随课程完成状态变化（未完成优先），学完一页后排序漂移会让
        原本靠后的未完成课程沉到已翻过的页里，导致漏学（实测 25 门按
        12/页边学边翻只学到 13 门）。先收集再学则列表在一次翻页窗口内
        稳定，不会漏。
        """
        label = choose_type[1]
        page_size = 12  # 与官方前端一致
        page_no = 1
        courses_all: list[dict] = []
        while True:
            try:
                res = self.api.list_flat_course(
                    task["userProjectId"], choose_type[0], page_no, page_size
                )
            except PermissionError:
                raise  # Token 失效，立即终止该账号
            except OSError as e:  # 网络异常跳过本类型，不中断整个账号
                self.log.error(f"获取 {label} 课程失败（网络异常）：{e}")
                return
            if not _check_code_ok(res):
                self.log.error(f"获取 {label} 课程失败：{res}")
                return
            data = res.get("data") or {}
            courses_all.extend(data.get("paginateData") or [])
            total_pages = int(data.get("totalPages", 1) or 1)
            # 官方结束条件：totalPages <= pageNo
            if total_pages <= page_no:
                break
            page_no += 1

        for course in courses_all:
            if not force_restudy and int(course.get("finished", 0)) == 1:
                continue
            category_name = course.get("categoryName") or label
            category_prefix = f"{label} {project_prefix}/{category_name}"
            if not self._learn_course(
                course,
                task,
                category_prefix,
                project_prefix,
                answers_json,
                force_restudy,
            ):
                return

    @staticmethod
    def _is_account_blocked(res: dict) -> bool:
        """完课/接口返回是否表示行为异常或账号锁定，应立即停跑。"""
        if not res:
            return False
        detail = str(res.get("detailCode", ""))
        msg = str(res.get("msg", ""))
        raw = str(res.get("raw", ""))
        if detail in {"10018", "701"}:
            return True
        return ("行为存在异常" in msg or "Account locked" in raw or "Account locked" in msg)

    def _study_one_course(
        self,
        course: dict,
        task: dict,
        category_prefix: str,
        project_prefix: str,
        answers_json: dict,
        force_restudy: bool,
    ) -> bool:
        """处理单门课程：加载 apicenext.js 的走 jupiter 翻页轨迹；
无 apicenext 的默认只答题+完课（对齐官方页面行为），配置
jupiter_fallback=true 时也补翻页轨迹。再答题，最后完课。

        :return: True 可继续下一门；False 表示账号异常/锁定，应停止本账号
        """
        course_prefix = f"{category_prefix}/{course['resourceName']}"

        if not force_restudy and int(course.get("finished", 0)) == 1:
            return True

        self.log.info(f"学习： {course_prefix}")
        # 官方 navToDetail 先 study.do，成功才进入课程页/取课程 URL；
        # 失败（含课程未开始/未开放）toast 服务端 msg 并跳过本门课
        study_res = self.api.study(course["resourceId"], task["userProjectId"])
        if not _check_code_ok(study_res):
            msg = study_res.get("message") or study_res.get("msg") or "课程暂时无法学习"
            self.log.warning(f"{course_prefix}：{msg}，跳过")
            return True
        study_start = time.time()

        # 官方 H5 完课不依赖列表对象的 userCourseId：课程页 URL 由
        # getCourseUrl.do 返回（CourseDetail/navToDetail 均走它），userCourseId
        # 由服务端填入 URL query，sdk.js finishWxCourse 从页面 URL 读取。
        # 列表对象是否带 userCourseId 取决于服务端：实测本租户
        # listCourse.do（chooseType=2 自选课）对象不含该字段（80/80 日志实证，
        # 必修课 chooseType=3 含）。照官方逻辑从课程 URL 提取，取不到才跳过
        # （不再假报"完成"）。
        course_url = self._build_course_url(course, task)
        self.log.info(f"{course_prefix}：{course_url.split('?')[0]}")
        query = parse_qs(urlparse(course_url).query)
        source_str = get_source_str(query)
        is_lyra = query.get("weiban", [None])[0] != "weiban" and query.get("lyra", [None])[0] == "lyra"
        if "userCourseId" not in course and not is_lyra:
            uid = query.get("userCourseId")
            if uid and uid[0]:
                course["userCourseId"] = uid[0]
            else:
                self.log.warning(f"{course_prefix}：未获取到学习记录（userCourseId 为空），跳过")
                return True

        course_code = ""
        url_path = urlparse(course_url).path
        # 三级路径课程（/course/DAGJAQ/DAGJAQ001/DAGJAQ001.html）取文件名
        code_match = re.search(r"/course/(?:[^/]+/)*([^/]+)\.html$", url_path)
        if not code_match:
            code_match = re.search(r"/course/([^/]+)/", url_path)
        if code_match:
            course_code = code_match.group(1)
        item_info = (
            self.parse_item_js(course_code, course_url=course_url)
            if course_code
            else {
                "uses_apinext": False,
                "nonstr_map": {},
                "has_exam": False,
                "total_step": 0,
            }
        )

        nonstr_map = item_info.get("nonstr_map", {})
        total_step = item_info.get("total_step", 0)
        total_step_source = item_info.get("total_step_source", "")
        uses_apinext = item_info.get("uses_apinext", False)
        # 翻页和完课共用 UUID。
        apinext_no = str(uuid4())
        unique_no = apinext_no if uses_apinext else None

        # 仅在页面实际加载题目时请求；响应决定后续翻页路径。
        loads_questions = item_info.get("loads_questions", False)
        question_data = self.api.list_question(course["resourceId"]) if loads_questions else {}
        if loads_questions and not _check_code_ok(question_data):
            raise RuntimeError("获取课程题目失败，无法确定学习路径")
        questions = (question_data.get("data") or {}) if _check_code_ok(question_data) else {}
        dynamic_steps = item_info.get("dynamic_steps") or {}
        branch = "with_questions" if questions.get("examQuestionList") else "without_questions"
        steps = dynamic_steps.get(branch)
        if uses_apinext and "dynamic_steps" in item_info and not steps:
            raise RuntimeError("课程轨迹路径未能完整解析，已停止学习，不使用页数推测")
        finish_step = None
        question_trace, after_question_trace = item_info.get("question_trace_flow", (0, 0))
        if steps:
            total_step, finish_step = steps
            if questions.get("examQuestionList") and question_trace:
                adjustment = (len(questions["examQuestionList"]) - 2) * question_trace
                total_step += adjustment
                finish_step += adjustment
            total_step_source = "题目响应路径"
            self.log.info(f"按题目响应选择翻页路径：中间步骤 {total_step}，完成 step={finish_step}")

        trace_enabled = (
            (uses_apinext or self.jupiter_fallback)
            and query.get("weiban", [None])[0] == "weiban"
        )
        def send_trace(total: int, finish: int = 2, start: int = 1) -> None:
            """按本次课程上下文上报一段轨迹。"""
            self.handle_apinext(
                course["userCourseId"],
                course["resourceId"],
                task["userProjectId"],
                nonstr_map,
                total,
                unique_no=apinext_no,
                finish=finish,
                start_step=start,
                step_delay=0,
                **({"finish_step": finish_step} if finish == 1 else {}),
            )

        completion_timing = (item_info.get("completion_timings") or {}).get(branch) or item_info.get("completion_timing")
        if trace_enabled and "completion_timing" in item_info and completion_timing is None:
            raise RuntimeError("课程完课顺序未能完整解析，已停止学习")
        exam_questions = questions.get("examQuestionList", [])
        deferred_steps = len(exam_questions) * question_trace + after_question_trace if exam_questions else 0
        if total_step and trace_enabled:
            self.log.info(
                f"total_step={total_step} ({total_step_source})"
            )
            if not uses_apinext:
                self.log.info("  课程未加载 apicenext.js，补充翻页轨迹上报")
            send_trace(total_step - deferred_steps)

        if _check_code_ok(question_data):
            data = questions
            for qlist, label, save_func in [
                (
                    data.get("viewpointQuestionList", []),
                    "观点题",
                    self.api.save_question,
                ),
                (
                    data.get("examQuestionList", []),
                    "课后习题",
                    self.api.save_exam_question,
                ),
            ]:
                if qlist:
                    self.log.info(f"  {label} {len(qlist)} 道")
                    for i, q in enumerate(qlist):
                        # 无论题库命中还是 fallback 都已提交答案，
                        # 对用户而言课程题目作答流程已完成
                        self._answer_question(
                            q,
                            answers_json,
                            course["resourceId"],
                            save_func,
                            source_str,
                        )
                        self.log.info(f"    {i + 1}/{len(qlist)} 已完成")
                        if label == "课后习题" and question_trace and trace_enabled:
                            begin = total_step - deferred_steps + i * question_trace + 1
                            send_trace(begin + question_trace - 1, start=begin)
                        time.sleep(0.5)
        elif question_data:
            self.log.info(f"  list_question: code={question_data.get('code')}")
        if item_info.get("has_exam") and not questions.get("examQuestionList"):
            self.log.info("  检测到题目标记但 list_question 无课后习题，可能为内联题目")
        if exam_questions and after_question_trace and trace_enabled:
            send_trace(total_step, start=total_step - after_question_trace + 1)

        # 3. 确保满足最低学习时长（服务端要求 study 后至少学习 study_time 秒才接受完课）
        elapsed = time.time() - study_start
        study_time = self.study_base_time + randint(0, self.study_random_upper)
        # 视频课程按配置倍速播放对齐（video_speed=0 表示不按视频时长等待）
        video_duration = item_info.get("video_duration", 0)
        if self.video_speed > 0 and video_duration > 0:
            video_need = video_duration / self.video_speed
            if video_need > study_time:
                self.log.info(
                    f"视频课程按 {self.video_speed:g} 倍速对齐：等待 "
                    f"{self._format_duration(video_need)}"
                    f"（视频时长 {self._format_duration(video_duration)}）"
                )
                study_time = video_need
        remaining = study_time - elapsed
        if remaining > 0:
            self.log.info(
                f"等待学习时长 {self._format_duration(remaining)} (已用 {self._format_duration(elapsed)}/{self._format_duration(study_time)})"
            )
            if self.video_speed > 0 and video_duration > 0:
                deadline = time.monotonic() + remaining
                while remaining > 0:
                    time.sleep(min(30, remaining))
                    remaining = max(0, deadline - time.monotonic())
                    if remaining > 0:
                        self.log.info(f"视频剩余 {self._format_duration(remaining)}")
            else:
                time.sleep(remaining)

        # 按课程脚本执行最终轨迹与完课，不能统一先轨迹再等待 2 秒。
        finish_first, finish_delay = completion_timing if completion_timing is not None else (False, 0.0)
        res: dict = {}
        if finish_first:
            res = self._finish_course(course, task, query, course_url, unique_no)
            if not _check_code_ok(res, allow_200=False):
                raise RuntimeError(f"{course_prefix} 完成失败，已停止学习：{res}")
        if total_step and trace_enabled:
            send_trace(total_step, finish=1)
        if item_info.get("trace_after_finish") and total_step and trace_enabled:
            # 完成步（total_step+1）之后仍会补发一次翻页上报。
            send_trace(total_step + 2, start=total_step + 2)
        if not finish_first:
            if finish_delay:
                time.sleep(finish_delay)
            res = self._finish_course(course, task, query, course_url, unique_no)
        # 完课走 JSONP（sdk.js finishWxCourse）：checkCode 只认 code∈{0,1}
        if not _check_code_ok(res, allow_200=False):
            raise RuntimeError(f"{course_prefix} 完成失败，已停止学习：{res}")

        if query.get("csComm", [None])[0] == "true" and query.get("weiban", [None])[0] == "weiban":
            for label, load in (
                ("相邻课程", lambda: self.api.get_course_neighbors(course["userCourseId"])),
                ("课程评分", lambda: self.api.get_course_rating(course["resourceId"])),
            ):
                response = load()
                if not _check_code_ok(response):
                    raise RuntimeError(f"完课评价页{label}读取失败，已停止学习")
        self.log.success(f"{course_prefix} 完成")
        return True

    def _finish_course(
        self,
        course: dict,
        task: dict,
        query: dict,
        course_url: str,
        unique_no: str | None,
    ) -> dict:
        """调用正确的完课接口并返回响应

        四种完课模式按 URL 参数分发：
        - lyra → finish_lyra（LYRA 平台）
        - weiban 不存在 → finish_by_token(course_type="open")（PROTEUS 平台）
        - source=moon → finish_by_token(course_type="moon")
        - weiban 标准 → finish_by_token（WEIBAN，含可选 captcha 校验）
        :param course: 课程数据
        :param task: 任务数据
        :param query: URL 查询参数（parse_qs 格式）
        :param course_url: 完整课程 URL（用于 captcha）
        :param unique_no: 仅 apinext 课程传入；None 表示不传 uniqueNo
        :return: 完课 API 响应
        """
        finish_kwargs: dict = {"referer": "https://mcwk.mycourse.cn/"}
        if unique_no:
            finish_kwargs["unique_no"] = unique_no
        if query.get("weiban", [None])[0] != "weiban":
            if query.get("lyra", [None])[0] == "lyra":
                return self.api.finish_lyra(
                    query.get("userActivityId", [None])[0], unique_no=unique_no
                )
            return self.api.finish_by_token(
                course["userCourseId"], course_type="open", **finish_kwargs
            )
        if query.get("source", [None])[0] == "moon":
            finish_kwargs["course_type"] = "moon"

        if query.get("csCapt", [None])[0] == "true":
            try:
                captcha_result = self.captcha_handler.handle_course_captcha(
                    course_url=course_url
                )
            except PermissionError:
                raise
            except Exception as e:  # noqa: BLE001 -- 浏览器异常类型不固定
                self.log.error(f"课程验证码处理异常: {e}")
                return {"code": "-1"}
            check_res = self.api.course_check(
                course["userCourseId"],
                task["userProjectId"],
                course["resourceId"],
                captcha_result["randstr"],
                captcha_result["ticket"],
            )
            if not _check_code_ok(check_res):
                self.log.error(f"课程验证码校验失败：{check_res}")
                return check_res
            self.log.success("课程验证码校验通过")
            finish_kwargs["token"] = check_res.get("data", "")
            finish_kwargs.pop("course_type", None)  # 验证后改用 weiban token URL。
        return self.api.finish_by_token(course["userCourseId"], **finish_kwargs)

    # ---- exam ---------------------------------------------------------------

    def run_exam(
        self,
        exam_mode: str = "true",
        random_answer: bool = True,
        exam_question_time: str = "3,3",
        exam_submit_match_rate: int = 90,
        only_project: dict | None = None,
    ):
        """执行项目考试。

        :param exam_mode: 考试模式
            - "false": 跳过所有考试
            - "true": 正常考试，已及格/已完成的考试默认跳过
            - "perfect": 达到满分为止，只剩一次机会且题库无法完全匹配则停止
            - "force": 强制考试，即使已及格也继续，除非没有考试机会
        :param random_answer: True=单选随机多选全选，False=终端手动输入
        :param exam_question_time: 每道题答题等待时长 "基础时间,随机上限"（秒）
        :param exam_submit_match_rate: 允许提交的最低题库匹配率（%）
        :param only_project: 只考试指定项目（按项目交替调用时传入）；
            None 时考试全部项目
        """
        # 解析每题等待时间
        try:
            parts = exam_question_time.split(",")
            question_base_time = int(parts[0])
            question_random_upper = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            question_base_time = 3
            question_random_upper = 3
        self.exam_mode = exam_mode

        answers_json = self._load_answers_json()

        if only_project is not None:
            projects = [only_project]
        else:
            projects = self._get_project_list()
        if not projects:
            self.log.warning("当前没有进行中的项目可考试。")

        for project in projects:
            # 项目未开始（未到开课时间等）：官方 H5 弹 message 并禁止进入，同样提示后跳过
            startable, notice = self._project_startable(project)
            if not startable:
                self.log.warning(
                    f"{project['projectName']}：{notice or '项目尚未开放，暂不可考试'}，跳过"
                )
                continue
            self.log.info(f"开始考试项目 {project['projectName']}")
            user_project_id = project["userProjectId"]

            exam_plans = self.api.exam_list_plan(user_project_id)
            if not _check_code_ok(exam_plans):
                self.log.error(f"获取考试计划失败：{exam_plans}")
                return
            exam_plans = exam_plans["data"]

            for plan in exam_plans:
                plan_name = f"{project['projectName']}/{plan['examPlanName']}"
                exam_odd_num = plan.get("examOddNum", 0)
                exam_finish_num = plan.get("examFinishNum", 0)
                exam_score = plan.get("examScore", 0)
                pass_score = plan.get("passScore", 0)

                # ── 已考过的考试，显示历史成绩 ──
                full_score = 100
                if exam_finish_num > 0:
                    try:
                        pp = self.api.exam_prepare_paper(plan["id"])
                        full_score = pp.get("data", {}).get("paperScore", 100)
                    except PermissionError:
                        raise  # Token 失效，立即终止该账号
                    except OSError:
                        pass
                    self.log.info(
                        f"{plan_name} 已考过 {exam_finish_num}/{exam_odd_num} 次，"
                        f"最高 {exam_score}/{full_score}（及格线 {pass_score}）"
                    )
                elif exam_odd_num > 0:
                    self.log.info(
                        f"{plan_name} 未考试，可考 {exam_odd_num} 次"
                    )

                # ── 根据 exam_mode 判断是否跳过 ──
                if exam_odd_num <= 0:
                    self.log.info(f"{plan_name} 无剩余考试机会，跳过")
                    continue

                if (
                    exam_mode == "true"
                    and exam_finish_num > 0
                    and exam_score >= pass_score
                ):
                    self.log.info(f"{plan_name} 已及格 ({exam_score}分 >= {pass_score}分)，跳过")
                    continue

                if (
                    exam_mode == "perfect"
                    and exam_finish_num > 0
                    and exam_score >= full_score
                ):
                    self.log.info(f"{plan_name} 已满分 ({exam_score}分)，跳过")
                    continue

                # perfect 模式：只剩 1 次机会时，检查题库是否能全覆盖
                if exam_mode == "perfect" and exam_odd_num <= 1:
                    # 先获取题目列表检查匹配率
                    warning_msg = (
                        f"{plan_name} 只剩 {exam_odd_num} 次考试机会，"
                        f"但 perfect 模式需要满分"
                    )
                    self.log.warning(warning_msg)

                if exam_mode == "true" and exam_finish_num > 0:
                    self.log.info(f"{plan_name} 已完成 {exam_finish_num} 次，{plan_name} 继续考试以争取更好成绩")

                user_exam_plan_id = plan["id"]
                exam_plan_id = plan["examPlanId"]

                before_paper = self.api.exam_before_paper(plan["id"])
                if not _check_code_ok(before_paper):
                    self.log.error(
                        f"考试项目 {plan_name} 获取考试记录失败：{before_paper}"
                    )

                prepare_paper = self.api.exam_prepare_paper(user_exam_plan_id)
                if not _check_code_ok(prepare_paper):
                    if prepare_paper.get("detailCode") == "14":
                        self.log.warning(
                            f"{plan_name} 课程学习未完成，无法考试；"
                            f"请先完成该项目的课程学习"
                        )
                    else:
                        self.log.error(f"获取考试信息失败：{prepare_paper}")
                    continue
                prepare_paper = prepare_paper["data"]
                question_num = prepare_paper["questionNum"]
                self.log.info(
                    f"考试信息：用户：{prepare_paper['realName']}，ID：{prepare_paper['userIDLabel']}，"
                    f"题目数：{question_num}，试卷总分：{prepare_paper['paperScore']}，"
                    f"限时 {prepare_paper['answerTime']} 分钟"
                )

                plan_start_ts = time.time()
                exam_paper = self.api.exam_start_paper(user_exam_plan_id)
                if not _check_code_ok(exam_paper):
                    self.log.error(f"获取考试题目失败：{exam_paper}")
                    if exam_paper.get("detailCode") == "10018":
                        self.log.warning(
                            f"考试项目 {plan_name} 需要手动处理，"
                            f"请在网站上开启一次考试后重试"
                        )
                    continue

                exam_paper = exam_paper.get("data", {})
                question_list = exam_paper.get("questionList", [])
                have_answer, no_answer = [], []
                for question in question_list:
                    target = (
                        have_answer
                        if clean_text(question["title"]) in answers_json
                        else no_answer
                    )
                    target.append(question)

                match_rate = (
                    len(have_answer) / len(question_list) * 100 if question_list else 0
                )
                self.log.info(
                    f"题目总数：{question_num}，有答案的题目数：{len(have_answer)}，"
                    f"无答案的题目数：{len(no_answer)}，题库匹配率：{match_rate:.1f}%"
                )

                # perfect 模式：匹配率不足且 random_answer=False 时警告
                if exam_mode == "perfect" and match_rate < 100 and not random_answer:
                    self.log.warning(
                        f"题库匹配率 {match_rate:.1f}% 不足 100%，"
                        f"perfect 模式下手动作答可能存在风险"
                    )

                # 检查提交匹配率
                if match_rate < exam_submit_match_rate and not random_answer:
                    self.log.error(
                        f"题库匹配率 {match_rate:.1f}% 低于阈值 {exam_submit_match_rate}%，"
                        f"且 random_answer=false，放弃交卷"
                    )
                    continue

                # ── 处理无答案题目 ──
                for i, question in enumerate(no_answer):
                    type_label = question.get("typeLabel", "未知")

                    # 优先尝试 AI 搜题
                    ai_answers_ids = []
                    if self.ai_config and self.ai_config.get("enable"):
                        ai_answers_ids = self._ai_search_question(question)

                    if ai_answers_ids:
                        answers_ids = ai_answers_ids
                        use_time = question_base_time + randint(
                            0, question_random_upper
                        )
                        self.log.info(
                            f"[{i + 1}/{len(no_answer)}] AI 搜题作答成功 "
                            f"({type_label})，等待 {self._format_duration(use_time)}: "
                            f"{question['title'][:40]}..."
                        )
                        time.sleep(use_time)
                    elif random_answer or is_non_interactive():
                        # 自动随机作答：单选随机选一个，多选全选
                        # （无交互模式即使配置 random_answer=false 也走随机，
                        #   避免阻塞等待终端输入）
                        answers_ids = self._auto_select_answer(question)
                        use_time = question_base_time + randint(
                            0, question_random_upper
                        )
                        self.log.info(
                            f"[{i + 1}/{len(no_answer)}] 随机作答 "
                            f"({type_label})，等待 {self._format_duration(use_time)}: "
                            f"{question['title'][:40]}..."
                        )
                        time.sleep(use_time)
                    else:
                        # 手动输入
                        self.log.info(
                            f"[{i + 1}/{len(no_answer)}] 题目不在题库中，请手动选择答案"
                        )
                        print(f"题目类型：{type_label}，题目标题：{question['title']}")
                        for j, opt in enumerate(question["optionList"]):
                            print(f"{j + 1}. {opt['content']}")

                        opt_count = len(question["optionList"])
                        start_time = time.time()
                        answers_ids = []

                        while not answers_ids:
                            answer = (
                                self._prompt(
                                    f"[{self.api.user.get('realName', '未知')}] "
                                    "请输入答案序号（多个选项用英文逗号分隔，如 1,2,3,4）："
                                )
                                .replace(" ", "")
                                .replace("，", ",")
                            )
                            candidates = [
                                ans.strip() for ans in answer.split(",") if ans.strip()
                            ]
                            if all(
                                ans.isdigit() and 1 <= int(ans) <= opt_count
                                for ans in candidates
                            ):
                                answers_ids = [
                                    question["optionList"][int(ans) - 1]["id"]
                                    for ans in candidates
                                ]
                                for ans in candidates:
                                    self.log.info(
                                        f"选择答案：{ans}，"
                                        f"内容：{question['optionList'][int(ans) - 1]['content']}"
                                    )
                            else:
                                self.log.error(
                                    "输入无效，请重新输入（序号需为数字且在选项范围内）"
                                )

                        use_time = round(time.time() - start_time)

                    self.log.info("正在提交当前答案")
                    if not self.record_answer(
                        user_exam_plan_id,
                        question["id"],
                        use_time,
                        answers_ids,
                        exam_plan_id,
                    ):
                        raise RuntimeError(f"答题失败，请重新考试：{question}")

                # ── 题库作答 ──
                if have_answer:
                    self.log.info(f"开始答题库中的题目，共 {len(have_answer)} 道题目")
                for i, question in enumerate(have_answer):
                    self.log.info(
                        f"[{i + 1}/{len(have_answer)}] 题目在题库中，开始答题"
                    )
                    self.log.info(
                        f"题目类型：{question['typeLabel']}，"
                        f"题目标题：{question['title']}"
                    )
                    answers = answers_json[clean_text(question["title"])]
                    answers_ids = [
                        opt["id"]
                        for opt in question["optionList"]
                        if clean_text(opt["content"]) in answers
                    ]
                    use_time = question_base_time + randint(0, question_random_upper)
                    self.log.info(
                        f"等待 {self._format_duration(use_time)}，模拟答题中..."
                    )
                    time.sleep(use_time)
                    if not self.record_answer(
                        user_exam_plan_id,
                        question["id"],
                        use_time,
                        answers_ids,
                        exam_plan_id,
                    ):
                        raise RuntimeError(f"答题失败，请重新考试：{question}")

                self.log.info("完成考试，正在提交试卷...")
                submit_res = self.api.exam_submit_paper(user_exam_plan_id)
                if not _check_code_ok(submit_res):
                    raise RuntimeError(f"提交试卷失败，请重新考试：{submit_res}")
                self.log.success(
                    f"试卷提交成功，考试完成，成绩：{submit_res['data']['score']} 分"
                )
                self._update_exam_eta(time.time() - plan_start_ts)

    def _update_exam_eta(self, elapsed: float) -> None:
        """用实测考试耗时更新每场考试的自适应估计（EMA）"""
        if elapsed <= 0:
            return
        if self._eta_exam_avg is None:
            self._eta_exam_avg = elapsed
        else:
            self._eta_exam_avg = 0.7 * self._eta_exam_avg + 0.3 * elapsed

    # ---- item.js parsing ----------------------------------------------------

    def parse_item_js(
        self, course_code: str, course_url: str | None = None
    ) -> dict[str, Any]:
        """从课程页面和引用脚本中提取轨迹、题目分支和视频源。"""
        result = {
            "uses_apinext": False,
            "nonstr_map": {},
            "has_exam": False,
            "loads_questions": False,
            "total_step": 0,
            "total_step_source": "",
            "has_video": False,
            "video_duration": 0.0,
        }

        try:
            # 直接复用播放 URL 的路径，兼容二级/三级路径课程
            # （/course/A25005/A25005.html 与 /course/DAGJAQ/DAGJAQ001/DAGJAQ001.html）
            url_path = urlparse(course_url).path if course_url else ""
            html_url = (
                course_url.split("#", 1)[0]
                if course_url and "/course/" in url_path
                else f"https://mcwk.mycourse.cn/course/{course_code}/{course_code}.html"
            )
            html = _fetch_text(self.api.session, html_url, referer=course_url)
            html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
            script_urls = list(dict.fromkeys(
                urljoin(html_url, src)
                for src in re.findall(r'''<script\b[^>]*\bsrc=["']([^"']+)["']''', html)
            ))
            inline_scripts = "\n".join(re.findall(r"<script\b[^>]*>([\s\S]*?)</script>", html))
            scripts = [inline_scripts]
            course_path = urlparse(html_url).path.rsplit("/", 1)[0] + "/"
            for script_url in script_urls:
                if urlparse(script_url).path.startswith(course_path):
                    scripts.append(_fetch_text(self.api.session, script_url, referer=html_url))
            script = _strip_js_comments("\n".join(scripts))
            html = _expand_course_pages(html, script)
            result["has_exam"] = _check_exam(script)
            result["loads_questions"] = bool(re.search(r"getQuestions\s*\(", script))
            result["completion_timing"] = _course_completion_timing(script)
            result["completion_timings"] = {
                "with_questions": _course_completion_timing(script, True),
                "without_questions": _course_completion_timing(script, False),
            }
            result["nonstr_map"] = _extract_map(script)
            result["question_trace_flow"] = _question_trace_flow(html, script)
            # 尾页点击先完成、随后无条件再发一次翻页上报。
            result["trace_after_finish"] = bool(re.search(
                r'''\$\(["']\.page-end["']\)\.hasClass\(["']page-active["']\)\?[\s\S]{0,220}?''' + TRACE_NEXT,
                script,
            ))
            result["dynamic_steps"] = {
                "with_questions": _dynamic_course_steps(html, script, True),
                "without_questions": _dynamic_course_steps(html, script, False),
            }

            # 播放器可能在脚本中设置视频地址。
            video_match = None
            video_block = re.search(r"<video\b[^>]*>(.*?)</video>", html, re.DOTALL)
            if video_block:
                # 去掉注释（被注释掉的备选 m3u8 源不算数），再找 <source>
                clean = re.sub(
                    r"<!--.*?-->", "", video_block.group(1), flags=re.DOTALL
                )
                video_match = re.search(
                    r"<source\b[^>]*\bsrc=[\"']([^\"']+)[\"']", clean
                )
            if not video_match:
                video_match = re.search(
                    r"<video\b[^>]*\bsrc=[\"']([^\"']+)[\"']", html
                )
            video_urls = [urljoin(html_url, src) for src in re.findall(
                r'''<(?:video|source)\b[^>]*\bsrc=["']([^"']+\.(?:mp4|m3u8)(?:\?[^"']*)?)["']''', html
            )]
            if video_match:
                video_urls.append(urljoin(html_url, video_match.group(1)))
            video_urls.extend(urljoin(html_url, src) for src in re.findall(
                r'''["']([^"'\s<>]+\.(?:mp4|m3u8)(?:\?[^"'\s<>]*)?)["']''', script
            ))
            # 备用 CDN 不重复计时，多段视频累计时长。
            video_sources = {}
            for video_url in video_urls:
                name = urlparse(video_url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                video_sources.setdefault(name, video_url)
            durations = []
            for video_url in video_sources.values():
                result["has_video"] = True
                video_duration = 0.0
                try:
                    if urlparse(video_url).path.endswith(".m3u8"):
                        # m3u8：累加 EXTINF 时长
                        playlist = self.api.session.get(video_url, timeout=10)
                        if playlist.status_code == 200:
                            segments = re.findall(
                                r"#EXTINF:\s*([0-9]+(?:\.[0-9]+)?)",
                                playlist.text,
                            )
                            if not segments:
                                variant = re.search(
                                    r"#EXT-X-STREAM-INF:[^\n]*\n\s*(\S+)",
                                    playlist.text,
                                )
                                if variant:
                                    playlist = self.api.session.get(
                                        urljoin(video_url, variant.group(1)),
                                        timeout=10,
                                    )
                                    if playlist.status_code == 200:
                                        segments = re.findall(
                                            r"#EXTINF:\s*([0-9]+(?:\.[0-9]+)?)",
                                            playlist.text,
                                        )
                            video_duration = sum(float(s) for s in segments)
                    else:
                        # moov 可能在 mdat 后，按 box 长度跳转定位。
                        offset = 0
                        movie_end = None
                        full_content = None
                        for _ in range(128):
                            if full_content is None:
                                metadata = self.api.session.get(
                                    video_url,
                                    headers={"Range": f"bytes={offset}-{offset + 63}"},
                                    timeout=10,
                                )
                                if metadata.status_code not in (200, 206):
                                    break
                                if metadata.status_code == 200:
                                    full_content = metadata.content
                                buf = metadata.content if full_content is None else full_content[offset:offset + 64]
                            else:
                                buf = full_content[offset:offset + 64]
                            if len(buf) < 8:
                                break
                            size = int.from_bytes(buf[:4], "big")
                            kind = buf[4:8]
                            header = 16 if size == 1 else 8
                            if size == 1:
                                size = int.from_bytes(buf[8:16], "big")
                            if size < header:
                                break
                            if kind == b"moov":
                                movie_end = offset + size
                                offset += header
                                continue
                            if kind == b"mvhd" and movie_end is not None:
                                version = buf[header]
                                scale_pos = header + (20 if version == 1 else 12)
                                duration_pos = scale_pos + 4
                                duration_size = 8 if version == 1 else 4
                                if version not in (0, 1) or len(buf) < duration_pos + duration_size:
                                    break
                                timescale = int.from_bytes(buf[scale_pos:duration_pos], "big")
                                duration = int.from_bytes(buf[duration_pos:duration_pos + duration_size], "big")
                                if timescale:
                                    video_duration = duration / timescale
                                break
                            offset += size
                            if movie_end is not None and offset >= movie_end:
                                break
                except OSError:
                    pass
                durations.append(video_duration)
            if durations:
                if not all(durations) and self.video_speed > 0:
                    raise RuntimeError("视频时长获取失败")
                result["video_duration"] = sum(durations) if all(durations) else 0.0
                self.log.info(
                    "视频课程，视频时长 "
                    f"{self._format_duration(sum(durations)) if all(durations) else '未知'}"
                )

            # 不加载 apicenext.js 的课程：JS 无 nonstrMap。仍解析 nav 步数供
            # jupiter_fallback=true 时使用（默认关闭，完全对齐官方不发轨迹）
            if not any("apicenext.js" in urlparse(url).path for url in script_urls):
                nav_pages, _ = _count_nav_pages(html)
                if nav_pages:
                    result["total_step"] = nav_pages
                    result["total_step_source"] = f"html nav={nav_pages}"
                return result

            result["uses_apinext"] = True

            # 轨迹只能由页面实际事件确定，不能用 section 数或校验表最大 key 猜测。
            result["total_step_source"] = "课程事件路径"

        except (PermissionError, RuntimeError):
            raise
        except Exception as e:  # 解析边界转为明确失败，不能降级为无轨迹完课
            raise RuntimeError(f"课程资源或脚本解析失败（{type(e).__name__}），已停止学习") from e
        return result

    # ---- apinext / answer helpers -------------------------------------------

    def handle_apinext(
        self,
        user_course_id: str,
        course_id: str,
        user_project_id: str,
        nonstr_map: dict[int, str],
        total_step: int,
        unique_no: str = "",
        finish: int = 2,
        step_delay: float = 1,
        finish_step: int | None = None,
        start_step: int = 1,
    ) -> str:
        """上报翻页和完课步骤。

        :param user_course_id: 用户课程 ID
        :param course_id: 课程 ID
        :param user_project_id: 用户项目 ID
        :param nonstr_map: step 对应的 nonstr 值
        :param total_step: finish=2 的调用次数
        :param unique_no: 本次学习的唯一标识
        :param finish: 2=中间步骤, 1=完成标记
        :param step_delay: 每步之间的延迟（秒）
        :param finish_step: 完成步骤，默认 total_step+1
        :param start_step: 分段发送时的起始步骤
        :return: unique_no
        """
        if unique_no == "":
            unique_no = str(uuid4())
        if not total_step:
            return unique_no

        def _send_step(step: int, finish: int, nonstr: str, label: str) -> None:
            """轨迹失败立即停止，不能带着断步继续完课。"""
            resp = self.api.apinext(
                user_course_id,
                course_id,
                user_project_id,
                step=step,
                finish=finish,
                nonstr=nonstr,
                unique_no=unique_no,
            )
            if not resp.get("success"):
                raise RuntimeError(f"apinext [{label}] 返回失败，已停止学习")
            self.log.info(f"apinext [{label}] finish={finish} 已发送")

        if finish == 2:
            self.log.info(f"apinext 发送中间步骤，共 {total_step} 步")
            for step in range(start_step, total_step + 1):
                if step_delay:
                    time.sleep(step_delay)
                # nonstr_map 的 key 对应 finish=2 的 step，完成步 (finish=1) 不在 map 中
                _send_step(step, 2, nonstr_map.get(step, ""), f"{step}/{total_step}")
        else:
            step = finish_step if finish_step is not None else total_step + 1
            # nextnummax 会过滤回退后重复的步骤。
            if step <= total_step:
                return unique_no
            if step_delay:
                time.sleep(step_delay)
            _send_step(step, 1, nonstr_map.get(step, ""), f"完成标记 step={step}")
        return unique_no

    @staticmethod
    def _auto_select_answer(question: dict) -> list:
        """自动选择答案：单选随机选一个，多选全选

        :param question: 题目数据（含 type 和 optionList）
        :return: 选中选项的 ID 列表
        """
        option_list = question.get("optionList", [])
        if not option_list:
            return []
        question_type = question.get("type", 1)
        if question_type == 2:
            # 多选题 → 全选
            return [opt["id"] for opt in option_list]
        # 单选题 → 随机选一个
        return [option_list[randint(0, len(option_list) - 1)]["id"]]

    def _answer_question(
        self,
        question: dict,
        answers_json: dict,
        course_id: str,
        save_func,
        source: str,
    ) -> bool:
        """答题通用逻辑，返回是否通过题库命中。

        题库未命中时先提交第一个选项，从响应中提取 answerLabel，
        再据此提交正确答案；观点题返回投票统计列表，无需修正。
        :param question: 题目数据（含 title、optionList）
        :param answers_json: 题库映射
        :param course_id: 课程 ID
        :param save_func: 提交函数（save_question 或 save_exam_question）
        :param source: sourceStr 值
        :return: 首次题库答案通过返回 True，其余成功路径返回 False
        """
        title = clean_text(question.get("title", ""))
        option_list = question.get("optionList", [])
        if not option_list:
            raise RuntimeError("课程题目缺少选项，已停止学习")

        answer_ids = []
        if title in answers_json:
            answer_ids = [
                opt["id"]
                for opt in option_list
                if clean_text(opt["content"]) in answers_json[title]
            ]
        matched = bool(answer_ids)
        if not answer_ids:
            answer_ids = [option_list[0]["id"]]

        res = save_func(course_id, question["id"], json.dumps(answer_ids), source)
        if not _check_code_ok(res):
            raise RuntimeError("课程答案保存失败，已停止学习")
        data = res.get("data")
        if isinstance(data, list):
            return matched
        if not isinstance(data, dict):
            raise TypeError("课程答案响应格式异常，已停止学习")
        if data.get("isRight") in (1, "1"):
            return matched

        # 题库错答也复用本次反馈，不再额外试答或遍历选项。
        answer_label = data.get("answerLabel", "")
        if not answer_label:
            raise RuntimeError("课后题未通过且缺少反馈答案，已停止学习")
        if not isinstance(answer_label, str):
            raise TypeError("课后题反馈答案格式异常，已停止学习")
        correct_letters = {ch for ch in answer_label.replace("-", "") if ch.isalpha()}
        if not correct_letters:
            raise RuntimeError("课后题反馈答案格式异常，已停止学习")
        letter_to_opt = {chr(65 + idx): opt for idx, opt in enumerate(option_list)}
        if not correct_letters.issubset(letter_to_opt):
            raise RuntimeError("课后题反馈无法匹配选项，已停止学习")
        answer_ids = [opt["id"] for letter, opt in letter_to_opt.items() if letter in correct_letters]
        corrected = save_func(course_id, question["id"], json.dumps(answer_ids), source)
        corrected_data = corrected.get("data")
        if not _check_code_ok(corrected) or not isinstance(corrected_data, dict) or corrected_data.get("isRight") not in (1, "1"):
            raise RuntimeError("课后题修正后仍未通过，已停止学习")
        return False

    def record_answer(
        self,
        user_exam_plan_id: str,
        question_id: str,
        per_time: int,
        answers_ids: list,
        exam_plan_id: str,
    ) -> bool:
        """记录答题
        :param user_exam_plan_id: 用户考试计划 ID
        :param question_id: 题目 ID
        :param per_time: 答题耗时（秒，用于模拟真实答题行为）
        :param answers_ids: 选中选项的 ID 列表
        :param exam_plan_id: 考试计划 ID
        :return: 成功返回 True，失败返回 False
        """
        res = self.api.exam_record_question(
            user_exam_plan_id,
            question_id,
            per_time,
            answers_ids,
            exam_plan_id,
        )
        if not _check_code_ok(res):
            self.log.error(f"答题失败，请重新开启考试：{res}")
            return False
        self.log.info("保存答案成功")
        return True

    def _ai_search_question(self, question: dict) -> list:
        """使用 AI 服务获取题目答案

        :param question: 题目字典
        :return: 选中的选项 ID 列表
        """
        if not self.ai_config or not self.ai_config.get("enable"):
            return []

        api_key = self.ai_config.get("api_key", "").strip()
        base_url = self.ai_config.get("base_url", "https://api.deepseek.com").strip()
        model = self.ai_config.get("model", "deepseek-v4-pro").strip()
        timeout = int(self.ai_config.get("timeout", 60))
        max_retries = int(self.ai_config.get("max_retries", 2))

        if not api_key and not self._ai_key_warned:
            self.log.warning("AI 搜题已启用，但未配置 api_key")
            self._ai_key_warned = True

        title = question.get("title", "")
        type_label = question.get("typeLabel", "未知")
        options = question.get("optionList", [])
        opt_count = len(options)

        options_str = "\n".join(
            [f"{i + 1}. {opt['content']}" for i, opt in enumerate(options)]
        )

        prompt = f"""你是一个在线教育考试答题助手。请根据题目和选项，给出正确答案。

【题目类型】{type_label}

【题目】{title}

【选项】
{options_str}

【规则】
1. 单选题/判断题：answers 只能包含 1 个选项序号
2. 多选题：answers 包含所有正确选项的序号
3. 选项序号为 1-based，有效范围：1~{opt_count}

【输出格式】严格输出一个 JSON 对象，不要输出任何其他内容：
{{"answers":[1],"reason":"理由"}}"""

        self.log.info(f"AI 搜题：{title[:40]}...")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0
        }

        # 用户自定义 body：兼容单行 JSON 字符串（config.toml）与 dict（TOML 内联表）
        user_payload = self.ai_config.get("custom_body") or {}
        if isinstance(user_payload, str):
            try:
                user_payload = json.loads(user_payload)
            except json.JSONDecodeError as e:
                self.log.error(f"AI 搜题 - [ai].custom_body 不是合法 JSON（{e}），已忽略")
                user_payload = {}
        if not isinstance(user_payload, dict):
            self.log.error("AI 搜题 - [ai].custom_body 必须是 JSON 对象（{...}），已忽略")
            user_payload = {}

        reserved_keys = ("model", "messages", "temperature") # 禁止用户设置这些body，因为这会和系统内置的产生冲突

        url = f"{base_url.rstrip('/')}/chat/completions"
        content = None
        resp = None

        for k,v in user_payload.items():
            if k in reserved_keys:
                self.log.warning(f"AI 搜题 - {k} 为系统保留字段，已忽略")
                continue
            payload[k] = v

        for attempt in range(1, max_retries + 1):
            try:
                resp = self.api.session.post(
                    url, headers=headers, json=payload, timeout=timeout
                )
                resp.raise_for_status()
                res_data = resp.json()

                usage = res_data.get("usage", {})
                if usage:
                    self.log.debug(
                        f"AI token 用量 — prompt: {usage.get('prompt_tokens', '?')}, "
                        f"completion: {usage.get('completion_tokens', '?')}, "
                        f"total: {usage.get('total_tokens', '?')}"
                    )

                content = res_data["choices"][0]["message"]["content"].strip()
                break

            except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
                api_detail = (
                    f"，响应体：{resp.text[:300]}" if resp is not None else ""
                )
                if attempt < max_retries:
                    wait = attempt * 2
                    self.log.warning(
                        f"AI 搜题第 {attempt} 次请求失败，{wait}s 后重试：{e}{api_detail}"
                    )
                    time.sleep(wait)
                else:
                    self.log.error(
                        f"AI 搜题请求失败（已重试 {max_retries} 次）：{e}{api_detail}"
                    )
                    return []

        if not content:
            return []

        # 解析 AI 返回的 JSON
        raw_indices = self._parse_ai_answer(content)
        if raw_indices is None:
            self.log.warning(f"AI 返回内容解析失败，原始内容：{content[:200]}")
            return []

        # 校验并映射为选项 ID
        valid_ids = []
        for idx in raw_indices:
            try:
                val = int(idx)
            except (ValueError, TypeError):
                continue
            if 1 <= val <= opt_count:
                valid_ids.append(options[val - 1]["id"])
                self.log.info(f"AI 推荐：{val}. {options[val - 1]['content']}")
            else:
                self.log.warning(
                    f"AI 返回的选项序号 {val} 超出范围 1~{opt_count}，忽略"
                )

        if not valid_ids:
            self.log.warning("AI 返回的答案未能匹配任何有效选项")

        return valid_ids

    @staticmethod
    def _parse_ai_answer(content: str) -> list | None:
        """从 AI 返回内容中提取 answers 序号列表

        :param content: AI 返回的原始文本
        :return: 答案序号列表，解析失败返回 None
        """
        # 去除 markdown 代码块包裹
        json_str = content
        if "```" in json_str:
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", json_str, re.DOTALL)
            if match:
                json_str = match.group(1).strip()

        # 方式一：直接解析 JSON
        try:
            data = json.loads(json_str)
            answers = data.get("answers", [])
            if isinstance(answers, list) and answers:
                return answers
        except (json.JSONDecodeError, TypeError):
            pass

        # 方式二：用正则从文本中提取第一个 {"answers": [...]} 结构
        match = re.search(r'"answers"\s*:\s*\[([\d\s,]+)\]', json_str)
        if match:
            try:
                return [int(x) for x in match.group(1).split(",") if x.strip()]
            except (ValueError, TypeError):
                pass

        return None

    # ---- sync answers -------------------------------------------------------

    @staticmethod
    def _is_valid_answers(answers_json: Any) -> bool:
        """校验题库是否为有效字典且非空"""
        return isinstance(answers_json, dict) and bool(answers_json)

    @staticmethod
    def _normalize_answers(answers_json: dict) -> dict:
        """合并 clean_text 后相同的题目与选项条目，保留原始标点。

        标题与选项文本取各组内最长（标点最完整）的原文，选项标记取并集
        （任一变体标 1 则保留 1），合并前后经 clean_text 匹配的运行时
        行为不变。
        """
        merged: dict = {}
        for title, question in answers_json.items():
            clean_title = clean_text(title)
            entry = merged.get(clean_title)
            if entry is None:
                entry = merged[clean_title] = {
                    "title": title,
                    "type": question.get("type"),
                    "options": {},
                }
            elif len(title) > len(entry["title"]):
                entry["title"] = title
            for option in question.get("optionList", []):
                content = clean_text(option["content"])
                old = entry["options"].get(content)
                if old is None:
                    entry["options"][content] = {
                        "content": option["content"],
                        "isCorrect": option["isCorrect"],
                    }
                else:
                    if len(option["content"]) > len(old["content"]):
                        old["content"] = option["content"]
                    if option["isCorrect"] == 1:
                        old["isCorrect"] = 1
        return {
            entry["title"]: {
                "type": entry["type"],
                "optionList": list(entry["options"].values()),
            }
            for entry in merged.values()
        }

    def sync_answers(self) -> None:
        """同步答案
        :return: 无返回值
        """
        os.makedirs(answer_dir, exist_ok=True)
        # 按优先级查找已有题库: 根目录 > answer/ > 打包内置
        existing_path: str | None = None
        for p in [root_answer_path, answer_path, bundle_answer_path]:
            if os.path.exists(p):
                existing_path = p
                break
        need_download = existing_path is None

        answers_json: dict | None = None
        if not need_download:
            assert existing_path is not None
            try:
                with open(existing_path, encoding="utf-8") as f:
                    answers_json = json.load(f)
                if not self._is_valid_answers(answers_json):
                    need_download = True
            except (json.JSONDecodeError, OSError):
                need_download = True

        if need_download:
            # 与模板下载同构：远程 jsDelivr 失败后回退打包内置/本地文件兜底
            self.log.info("题库不存在或格式错误，正在下载...")
            try:
                remote = self.api.download_answer()
                self.log.success("题库已从远程下载")
            except Exception as e:  # noqa: BLE001 -- 网络失败不应中断整个账号流程
                self.log.warning(f"题库下载失败：{e}，回退本地内置题库")
                remote = read_first_existing(
                    [bundle_answer_path, answer_path, root_answer_path]
                )
                if remote is None:
                    self.log.warning("本地无可用题库，本次跳过题库同步")
                    return
            with open(answer_path, "w", encoding="utf-8") as f:
                f.write(remote)
            try:
                with open(answer_path, encoding="utf-8") as f:
                    answers_json = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                self.log.error(f"读取题库失败：{e}")
                return
            if not self._is_valid_answers(answers_json):
                self.log.error("下载的题库格式无效，应为非空 JSON 对象")
                return

        if answers_json is None:
            self.log.error("题库加载失败")
            return

        # 合并变体：clean_text 相同的题目/选项仅保留一条，保留原始标点
        answers_json = self._normalize_answers(answers_json)
        # clean 标题 → 原始标题索引，服务器标题按 clean 语义匹配
        key_by_clean = {clean_text(k): k for k in answers_json}

        user_project_ids = [
            p["userProjectId"] for p in self.api.list_my_project().get("data", [])
        ]
        user_project_ids.extend(
            p["userProjectId"]
            for p in self.api.list_my_project(ended=1).get("data", [])
        )
        completion = self.api.list_completion()
        if not _check_code_ok(completion):
            self.log.error(f"获取模块完成情况失败：{completion}")

        showable_modules = [
            d["module"] for d in completion.get("data", []) if d["showable"] == 1
        ]
        if "labProject" in showable_modules:
            self.log.info("加载实验室课程")
            lab_project = self.api.lab_index()
            if not _check_code_ok(lab_project):
                self.log.error(f"获取实验室课程失败：{lab_project}")
            user_project_ids.append(
                lab_project.get("data", {}).get("current", {}).get("userProjectId")
            )
        for user_project_id in user_project_ids:
            for plan in self.api.exam_list_plan(user_project_id).get("data", []):
                for history in self.api.exam_list_history(
                    plan["examPlanId"], plan["examType"]
                ).get("data", []):
                    questions = self.api.exam_review_paper(
                        history["id"], history["isRetake"]
                    )["data"].get("questions", [])
                    for answer in questions:
                        server_title = answer["title"]
                        clean_title = clean_text(server_title)
                        old_key = key_by_clean.get(clean_title)
                        if old_key is None:
                            # 新题：直接以服务器原文入库，并提醒用户
                            answers_json[server_title] = {
                                "type": answer["type"],
                                "optionList": [
                                    {
                                        "content": o["content"],
                                        "isCorrect": o["isCorrect"],
                                    }
                                    for o in answer.get("optionList", [])
                                ],
                            }
                            key_by_clean[clean_title] = server_title
                            self.log.info(f"发现新题：{server_title}")
                            for option in answer.get("optionList", []):
                                self.log.info(
                                    f"发现题目：{server_title} 新选项：{option['content']}"
                                )
                            continue
                        entry = answers_json[old_key]
                        # 标题有变化则以服务器原文更新
                        if old_key != server_title:
                            del answers_json[old_key]
                            answers_json[server_title] = entry
                            key_by_clean[clean_title] = server_title
                        # 选项追加合并：新选项追加；已有选项标记取并集
                        # （任一变体标 1 则保留 1），文本保留较长原文，同题
                        # 不同答案的变体互不覆盖（与 _normalize_answers 一致）
                        options = {
                            clean_text(o["content"]): o for o in entry["optionList"]
                        }
                        for option in answer.get("optionList", []):
                            content = clean_text(option["content"])
                            old = options.get(content)
                            if old is None:
                                options[content] = {
                                    "content": option["content"],
                                    "isCorrect": option["isCorrect"],
                                }
                                self.log.info(
                                    f"发现题目：{server_title} 新选项：{option['content']}"
                                )
                                continue
                            merged = False
                            if option["isCorrect"] == 1 and old["isCorrect"] != 1:
                                old["isCorrect"] = 1
                                merged = True
                            if len(option["content"]) > len(old["content"]):
                                old["content"] = option["content"]
                                merged = True
                            if merged:
                                self.log.info(
                                    f"发现题目：{server_title} 答案合并：{old['content']}"
                                )
                        entry["optionList"] = list(options.values())
                        entry["type"] = answer["type"]

        # 所有入库路径统一走规范化，确保只保留 content/isCorrect
        answers_json = self._normalize_answers(answers_json)

        # 写回读取来源（打包内置路径只读，退回可写的 answer/ 目录），
        # 与 _load_answers_json 的加载优先级保持一致，避免同步结果不被加载
        write_path = (
            existing_path
            if existing_path is not None and existing_path != bundle_answer_path
            else answer_path
        )
        os.makedirs(os.path.dirname(write_path), exist_ok=True)
        with open(write_path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(answers_json, indent=2, ensure_ascii=False, sort_keys=True)
            )
            f.write("\n")
