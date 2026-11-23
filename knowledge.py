"""参考文档知识库加载与自适应检索模块。

支持解析常见文档格式（PDF、Word .docx、PowerPoint .pptx、Excel .xlsx、
Markdown、TXT、CSV、JSON、HTML 等），并提供自适应上下文注入策略：
- 文档总字符数 <= max_chars：直接全文注入上下文；
- 文档总字符数 > max_chars：自动按段落/标题切块，使用本地中英文混合分词
  BM25 检索（综合题干与选项）召回最相关的 Top-K 片段。
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

PDF_EXTENSIONS = frozenset({".pdf"})
DOCX_EXTENSIONS = frozenset({".docx"})
PPTX_EXTENSIONS = frozenset({".pptx"})
XLSX_EXTENSIONS = frozenset({".xlsx", ".xlsm"})
HTML_EXTENSIONS = frozenset({".html", ".htm", ".xml"})
CSV_EXTENSIONS = frozenset({".csv", ".tsv"})
JSON_EXTENSIONS = frozenset({".json"})
PLAIN_TEXT_EXTENSIONS = frozenset(
    {".md", ".markdown", ".txt", ".rst", ".log", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
)
LEGACY_OFFICE_EXTENSIONS = frozenset({".doc", ".ppt", ".xls"})

SUPPORTED_EXTENSIONS = (
    PDF_EXTENSIONS
    | DOCX_EXTENSIONS
    | PPTX_EXTENSIONS
    | XLSX_EXTENSIONS
    | HTML_EXTENSIONS
    | CSV_EXTENSIONS
    | JSON_EXTENSIONS
    | PLAIN_TEXT_EXTENSIONS
)

# 常见中文单字虚词，用于过滤 BM25 单字噪声（保留二元组以维持短语精度）
_ZH_STOPWORDS = frozenset(
    "的了是在和有不这那为之与及或等可对中上下大小也就要都而以于到从把被让向把很最更如若但因所"
)

_EN_STOPWORDS = frozenset(
    {
        "the", "and", "for", "that", "this", "with", "from", "are", "was",
        "were", "have", "has", "had", "not", "but", "you", "all", "can",
        "her", "his", "its", "our", "out", "use", "any", "into", "than",
    }
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；\n.!?;])\s*")
_TOKEN_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN_ASCII_RE = re.compile(r"[a-z0-9]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


class _HTMLTextExtractor(HTMLParser):
    """轻量 HTML/XML 文本提取器，忽略 script/style 内容并保留段落换行。"""

    _BLOCK_TAGS = frozenset(
        {
            "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
            "section", "article", "header", "footer", "blockquote", "pre", "dt", "dd",
        }
    )
    _SKIP_TAGS = frozenset({"script", "style", "noscript", "svg"})

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        if low in self._SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and low in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and low in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


@dataclass(frozen=True)
class KnowledgeDocument:
    """单篇解析后的参考文档。"""

    source: str
    path: str
    text: str


@dataclass(frozen=True)
class KnowledgeChunk:
    """文档切片及预计算词频，用于 BM25 检索。"""

    source: str
    index: int
    text: str
    tf: Counter[str]
    length: int


@dataclass(frozen=True)
class RetrievalResult:
    """知识库检索结果。"""

    context: str = ""
    mode: str = "empty"  # empty | full | bm25 | bm25_empty
    sources: list[str] = field(default_factory=list)
    total_docs: int = 0
    total_chars: int = 0
    matched_chunks: int = 0


def parse_knowledge_paths(raw: Any) -> list[str]:
    """将配置/CLI/环境变量中的参考文档路径规范化为字符串列表。

    支持 list/tuple 或以分号/逗号/换行分隔的字符串（避免按冒号切割以兼容 Windows 盘符）。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(item).strip() for item in raw]
    elif isinstance(raw, str):
        items = [part.strip() for part in re.split(r"[;,\n]+", raw)]
    else:
        items = [str(raw).strip()]
    return [item for item in items if item]


def default_knowledge_dir(base_path: str, data_dir: str | None = None) -> str:
    """默认参考文档目录：data_dir/knowledge 或 base_path/knowledge。"""
    root = data_dir if data_dir else base_path
    return os.path.join(root, "knowledge")


def _normalize_text(text: str) -> str:
    """统一换行并清理多余空白。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    cleaned = "\n".join(lines).strip()
    return _MULTI_NEWLINE_RE.sub("\n\n", cleaned)


def _read_text_auto_encoding(path: str) -> str:
    """按 UTF-8-SIG → UTF-8 → GB18030 → UTF-16 顺序自动解码文本文件。"""
    with open(path, "rb") as f:
        raw = f.read()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_pdf(path: str, display_name: str, log: Any = None) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    pages: list[str] = []
    total_pages = len(reader.pages)
    for idx, page in enumerate(reader.pages, start=1):
        try:
            page_text = (page.extract_text() or "").strip()
        except Exception as e:  # noqa: BLE001 -- 单页异常不中断整本 PDF 解析
            if log is not None:
                log.warning(f"PDF 文档 {display_name} 第 {idx} 页提取失败：{e}")
            continue
        if page_text:
            prefix = f"[第 {idx} 页]\n" if total_pages > 1 else ""
            pages.append(f"{prefix}{page_text}")

    combined = _normalize_text("\n\n".join(pages))
    if total_pages > 0 and len(re.sub(r"\s+", "", combined)) < 20 and log is not None:
        log.warning(
            f"PDF 文档「{display_name}」（共 {total_pages} 页）几乎未提取到文本，"
            "可能是扫描图片版 PDF，建议先进行 OCR 后再放入知识库"
        )
    return combined


def _parse_docx(path: str) -> str:
    from docx import Document as DocxDocument
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = DocxDocument(path)
    blocks: list[str] = []

    body = doc.element.body
    if body is not None:
        for child in body.iterchildren():
            if isinstance(child, CT_P):
                text = Paragraph(child, doc).text.strip()
                if text:
                    blocks.append(text)
            elif isinstance(child, CT_Tbl):
                table = Table(child, doc)
                rows_text: list[str] = []
                for row in table.rows:
                    cells: list[str] = []
                    prev_cell: str | None = None
                    for cell in row.cells:
                        cell_text = re.sub(r"\s+", " ", cell.text).strip()
                        # 合并单元格在 python-docx 中会连续重复出现，去重相邻重复值
                        if cell_text and cell_text != prev_cell:
                            cells.append(cell_text)
                        prev_cell = cell_text
                    if cells:
                        rows_text.append(" | ".join(cells))
                if rows_text:
                    blocks.append("\n".join(rows_text))
    else:
        for para in doc.paragraphs:
            if para.text.strip():
                blocks.append(para.text.strip())

    return _normalize_text("\n\n".join(blocks))


def _parse_pptx(path: str) -> str:
    from pptx import Presentation

    prs = Presentation(path)
    slides_text: list[str] = []
    for idx, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                text_frame: Any = getattr(shape, "text_frame", None)
                if text_frame is not None:
                    tf_text = "\n".join(
                        p.text.strip() for p in text_frame.paragraphs if p.text.strip()
                    )
                    if tf_text:
                        parts.append(tf_text)
            if shape.has_table:
                table: Any = getattr(shape, "table", None)
                if table is not None:
                    for row in table.rows:
                        cells = [
                            re.sub(r"\s+", " ", cell.text).strip()
                            for cell in row.cells
                            if cell.text.strip()
                        ]
                        if cells:
                            parts.append(" | ".join(cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"[备注] {notes}")
        if parts:
            slides_text.append(f"[幻灯片 {idx}]\n" + "\n".join(parts))

    return _normalize_text("\n\n".join(slides_text))


def _parse_xlsx(path: str) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    sheets_text: list[str] = []
    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows_text: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = [
                    str(v).strip()
                    for v in row
                    if v is not None and str(v).strip()
                ]
                if cells:
                    rows_text.append(" | ".join(cells))
            if rows_text:
                sheets_text.append(f"[工作表: {sheet_name}]\n" + "\n".join(rows_text))
    finally:
        wb.close()

    return _normalize_text("\n\n".join(sheets_text))


def _parse_csv(path: str, ext: str) -> str:
    raw = _read_text_auto_encoding(path)
    delimiter = "\t" if ext == ".tsv" else ","
    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    rows: list[str] = []
    for row in reader:
        cells = [cell.strip() for cell in row if cell.strip()]
        if cells:
            rows.append(" | ".join(cells))
    return _normalize_text("\n".join(rows))


def _parse_json(path: str) -> str:
    raw = _read_text_auto_encoding(path)
    try:
        parsed = json.loads(raw)
        return _normalize_text(json.dumps(parsed, ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        return _normalize_text(raw)


def _parse_html(path: str) -> str:
    raw = _read_text_auto_encoding(path)
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    parser.close()
    return _normalize_text(parser.get_text())


def extract_document_text(path: str, display_name: str, log: Any = None) -> str:
    """根据扩展名提取单个文档的纯文本内容。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTENSIONS:
        return _parse_pdf(path, display_name, log=log)
    if ext in DOCX_EXTENSIONS:
        return _parse_docx(path)
    if ext in PPTX_EXTENSIONS:
        return _parse_pptx(path)
    if ext in XLSX_EXTENSIONS:
        return _parse_xlsx(path)
    if ext in HTML_EXTENSIONS:
        return _parse_html(path)
    if ext in CSV_EXTENSIONS:
        return _parse_csv(path, ext)
    if ext in JSON_EXTENSIONS:
        return _parse_json(path)
    if ext in PLAIN_TEXT_EXTENSIONS:
        return _normalize_text(_read_text_auto_encoding(path))
    return ""


def tokenize_text(text: str) -> list[str]:
    """中英文混合分词器（纯 Python、零词典依赖）。

    - 英文与数字：按连续字母数字提取小写词元；
    - 中文：提取连续汉字的二元组（高区分度短语）及非停用单字（提高召回）。
    """
    low = text.lower()
    tokens: list[str] = []

    for word in _TOKEN_ASCII_RE.findall(low):
        if (len(word) >= 2 and word not in _EN_STOPWORDS) or word.isdigit():
            tokens.append(word)

    for run in _TOKEN_CJK_RE.findall(low):
        n = len(run)
        if n == 1:
            if run not in _ZH_STOPWORDS:
                tokens.append(run)
            continue
        for i in range(n - 1):
            tokens.append(run[i : i + 2])
        for ch in run:
            if ch not in _ZH_STOPWORDS:
                tokens.append(ch)

    return tokens


def chunk_text(text: str, chunk_size: int = 700, overlap: int = 120) -> list[str]:
    """按段落优先、句子兜底的方式将长文本切分为带重叠的片段。"""
    cleaned = _normalize_text(text)
    if not cleaned:
        return []
    if len(cleaned) <= chunk_size:
        return [cleaned]

    paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= chunk_size:
            units.append(para)
            continue
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(para) if s.strip()]
        if len(sentences) <= 1:
            # 超长无标点段落：按定长滑窗切分
            step = max(1, chunk_size - overlap)
            for start in range(0, len(para), step):
                piece = para[start : start + chunk_size].strip()
                if piece:
                    units.append(piece)
        else:
            for sent in sentences:
                if len(sent) <= chunk_size:
                    units.append(sent)
                else:
                    step = max(1, chunk_size - overlap)
                    for start in range(0, len(sent), step):
                        piece = sent[start : start + chunk_size].strip()
                        if piece:
                            units.append(piece)

    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for unit in units:
        add_len = len(unit) + (2 if current_parts else 0)
        if current_parts and current_len + add_len > chunk_size:
            chunk_str = "\n\n".join(current_parts).strip()
            if chunk_str:
                chunks.append(chunk_str)
            # 保留末尾约 overlap 字符作为下一块的重叠上下文
            if overlap > 0 and chunk_str:
                tail = chunk_str[-overlap:].strip()
                current_parts = [tail] if tail else []
                current_len = len(tail)
            else:
                current_parts = []
                current_len = 0

        current_parts.append(unit)
        current_len += len(unit) + (2 if len(current_parts) > 1 else 0)

    if current_parts:
        last_chunk = "\n\n".join(current_parts).strip()
        if last_chunk and (not chunks or last_chunk != chunks[-1]):
            chunks.append(last_chunk)

    return chunks


class KnowledgeBase:
    """参考文档知识库：支持自适应全文注入与本地 BM25 分块检索。"""

    def __init__(
        self,
        documents: list[KnowledgeDocument],
        *,
        chunk_size: int = 700,
        chunk_overlap: int = 120,
    ) -> None:
        self.documents = documents
        self.total_chars = sum(len(doc.text) for doc in documents)
        self.chunks: list[KnowledgeChunk] = []
        self._idf: dict[str, float] = {}
        self._avgdl: float = 0.0

        self._build_index(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    def _build_index(self, *, chunk_size: int, chunk_overlap: int) -> None:
        chunks: list[KnowledgeChunk] = []
        df: Counter[str] = Counter()

        for doc in self.documents:
            pieces = chunk_text(doc.text, chunk_size=chunk_size, overlap=chunk_overlap)
            for idx, piece in enumerate(pieces, start=1):
                tokens = tokenize_text(piece)
                tf = Counter(tokens)
                chunks.append(
                    KnowledgeChunk(
                        source=doc.source,
                        index=idx,
                        text=piece,
                        tf=tf,
                        length=len(tokens),
                    )
                )
                for token in tf:
                    df[token] += 1

        self.chunks = chunks
        n_chunks = len(chunks)
        if n_chunks == 0:
            self._avgdl = 0.0
            self._idf = {}
            return

        self._avgdl = sum(c.length for c in chunks) / n_chunks
        self._idf = {
            token: math.log((n_chunks - freq + 0.5) / (freq + 0.5) + 1.0)
            for token, freq in df.items()
        }

    @property
    def is_empty(self) -> bool:
        return not self.documents

    def _score_chunk(
        self,
        chunk: KnowledgeChunk,
        title_tokens: list[str],
        option_tokens: list[str],
        title_phrases: list[str],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> float:
        if chunk.length == 0 or self._avgdl <= 0:
            return 0.0

        norm = k1 * (1.0 - b + b * (chunk.length / self._avgdl))
        score = 0.0

        # 题干权重高于选项（1.5x vs 1.0x）
        weighted_terms: dict[str, float] = {}
        for tok in option_tokens:
            weighted_terms[tok] = max(weighted_terms.get(tok, 0.0), 1.0)
        for tok in title_tokens:
            weighted_terms[tok] = weighted_terms.get(tok, 0.0) + 1.5

        for tok, weight in weighted_terms.items():
            tf = chunk.tf.get(tok, 0)
            if tf <= 0:
                continue
            idf = self._idf.get(tok, 0.0)
            score += weight * idf * (tf * (k1 + 1.0)) / (tf + norm)

        # 题干连续关键短语精确命中加分（利于匹配复习提纲/法规原文）
        if score > 0 and title_phrases:
            chunk_compact = re.sub(r"\s+", "", chunk.text.lower())
            for phrase in title_phrases:
                if phrase in chunk_compact:
                    score += 2.0

        return score

    def retrieve(
        self,
        question_title: str,
        options: list[str] | None = None,
        *,
        max_chars: int = 12000,
        top_k: int = 5,
    ) -> RetrievalResult:
        """根据题目与选项检索参考知识库上下文。"""
        if not self.documents:
            return RetrievalResult()

        effective_max = max(500, int(max_chars))
        effective_top_k = max(1, int(top_k))

        # 策略一：总文档长度在阈值以内，直接全文注入
        if self.total_chars <= effective_max:
            blocks = [
                f"【参考文档：{doc.source}】\n{doc.text}" for doc in self.documents
            ]
            return RetrievalResult(
                context="\n\n---\n\n".join(blocks),
                mode="full",
                sources=[doc.source for doc in self.documents],
                total_docs=len(self.documents),
                total_chars=self.total_chars,
                matched_chunks=len(self.documents),
            )

        # 策略二：文档较长，使用 BM25 按题干 + 选项召回 Top-K 片段
        title_tokens = tokenize_text(question_title)
        option_tokens = tokenize_text(" ".join(options or []))
        title_phrases = [
            seq
            for seq in _TOKEN_CJK_RE.findall(question_title.lower())
            if len(seq) >= 4
        ]

        scored: list[tuple[float, KnowledgeChunk]] = []
        for chunk in self.chunks:
            s = self._score_chunk(chunk, title_tokens, option_tokens, title_phrases)
            if s > 0:
                scored.append((s, chunk))

        if not scored:
            return RetrievalResult(
                context="",
                mode="bm25_empty",
                sources=[],
                total_docs=len(self.documents),
                total_chars=self.total_chars,
                matched_chunks=0,
            )

        scored.sort(key=lambda item: item[0], reverse=True)

        selected_blocks: list[str] = []
        selected_sources: list[str] = []
        used_chars = 0

        for rank, (_, chunk) in enumerate(scored[:effective_top_k], start=1):
            header = f"【参考片段 {rank}｜来源：{chunk.source}】\n"
            block = header + chunk.text
            sep_len = 7 if selected_blocks else 0  # "\n\n---\n\n"
            if used_chars + sep_len + len(block) > effective_max:
                remaining = effective_max - used_chars - sep_len - len(header)
                if not selected_blocks and remaining > 100:
                    block = header + chunk.text[:remaining].rstrip() + "..."
                else:
                    break
            selected_blocks.append(block)
            if chunk.source not in selected_sources:
                selected_sources.append(chunk.source)
            used_chars += sep_len + len(block)

        return RetrievalResult(
            context="\n\n---\n\n".join(selected_blocks),
            mode="bm25",
            sources=selected_sources,
            total_docs=len(self.documents),
            total_chars=self.total_chars,
            matched_chunks=len(selected_blocks),
        )


def _discover_files(
    paths: list[str],
    *,
    base_root: str,
    log: Any = None,
) -> list[tuple[str, str]]:
    """展开文件与目录列表，返回去重且有序的 (realpath, display_name) 列表。"""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for raw_path in paths:
        if not raw_path:
            continue
        expanded = os.path.expanduser(raw_path)
        candidate = expanded
        if not os.path.isabs(candidate):
            rel_to_root = os.path.join(base_root, candidate)
            if os.path.exists(rel_to_root):
                candidate = rel_to_root

        if not os.path.exists(candidate):
            if log is not None:
                log.warning(f"参考文档路径不存在，已跳过：{raw_path}")
            continue

        if os.path.isfile(candidate):
            real = os.path.realpath(candidate)
            if real in seen:
                continue
            ext = os.path.splitext(candidate)[1].lower()
            if ext in LEGACY_OFFICE_EXTENSIONS:
                if log is not None:
                    log.warning(
                        f"暂不支持旧版二进制 Office 格式「{os.path.basename(candidate)}」，"
                        "请另存为 .docx/.pptx/.xlsx 或 .pdf 后重试"
                    )
                continue
            if ext not in SUPPORTED_EXTENSIONS:
                if log is not None:
                    log.warning(f"不支持的参考文档格式，已跳过：{candidate}")
                continue
            seen.add(real)
            found.append((real, os.path.basename(candidate)))
        elif os.path.isdir(candidate):
            dir_real = os.path.realpath(candidate)
            for cur_root, dirs, files in os.walk(dir_real):
                dirs[:] = sorted(
                    d for d in dirs if not d.startswith(".") and d != "__pycache__"
                )
                for fname in sorted(files):
                    if fname.startswith((".", "~$")):
                        continue
                    full = os.path.join(cur_root, fname)
                    real = os.path.realpath(full)
                    if real in seen:
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in LEGACY_OFFICE_EXTENSIONS:
                        if log is not None:
                            log.warning(
                                f"暂不支持旧版二进制 Office 格式「{fname}」，"
                                "请另存为 .docx/.pptx/.xlsx 或 .pdf 后重试"
                            )
                        continue
                    if ext not in SUPPORTED_EXTENSIONS:
                        continue
                    seen.add(real)
                    rel_display = os.path.relpath(real, dir_real).replace("\\", "/")
                    found.append((real, rel_display))

    found.sort(key=lambda item: item[1].lower())
    return found


_KB_CACHE_LOCK = threading.Lock()
_KB_CACHE: dict[tuple[Any, ...], KnowledgeBase] = {}


def load_knowledge_base(
    extra_paths: Any = None,
    *,
    base_path: str,
    data_dir: str | None = None,
    include_default_dir: bool = True,
    chunk_size: int = 700,
    chunk_overlap: int = 120,
    log: Any = None,
) -> KnowledgeBase:
    """加载并缓存参考文档知识库（线程安全，按文件 mtime/size 自动失效）。"""
    root = data_dir if data_dir else base_path
    search_paths: list[str] = []

    if include_default_dir:
        def_dir = default_knowledge_dir(base_path, data_dir)
        if os.path.isdir(def_dir):
            search_paths.append(def_dir)

    search_paths.extend(parse_knowledge_paths(extra_paths))

    discovered = _discover_files(search_paths, base_root=root, log=log)
    if not discovered:
        return KnowledgeBase([])

    # 构建基于文件元数据的缓存键
    file_sig: list[tuple[str, int, int]] = []
    for real_path, _ in discovered:
        try:
            st = os.stat(real_path)
            file_sig.append((real_path, st.st_mtime_ns, st.st_size))
        except OSError:
            continue

    cache_key = (tuple(file_sig), chunk_size, chunk_overlap)
    with _KB_CACHE_LOCK:
        cached = _KB_CACHE.get(cache_key)
        if cached is not None:
            return cached

    documents: list[KnowledgeDocument] = []
    for real_path, display_name in discovered:
        try:
            text = extract_document_text(real_path, display_name, log=log)
        except Exception as e:  # noqa: BLE001 -- 单篇文档解析失败不阻断其余文档
            if log is not None:
                log.warning(f"解析参考文档「{display_name}」失败，已跳过：{e}")
            continue
        if text:
            documents.append(
                KnowledgeDocument(source=display_name, path=real_path, text=text)
            )
        elif log is not None:
            log.warning(f"参考文档「{display_name}」内容为空，已跳过")

    kb = KnowledgeBase(
        documents, chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )

    if log is not None and not kb.is_empty:
        log.info(
            f"已加载参考文档知识库：{len(kb.documents)} 篇文档，"
            f"共 {kb.total_chars} 字符，切分 {len(kb.chunks)} 个检索片段"
        )

    with _KB_CACHE_LOCK:
        # 避免缓存无限增长（通常仅 1 份配置生效）
        if len(_KB_CACHE) >= 8:
            _KB_CACHE.clear()
        _KB_CACHE[cache_key] = kb

    return kb
