import re
from difflib import SequenceMatcher
from typing import Any

from weban.app.runtime import clean_text, ignore_symbols


def _normalize_question_title(text: str) -> str:
    compact = clean_text(text or "")
    compact = re.sub(r"^\s*\d+[\.、\s]+", "", compact).strip()
    return ignore_symbols(compact)


def _normalize_option_text(text: str) -> str:
    compact = clean_text(text or "")
    compact = re.sub(r"^[A-Z][.\s、\n]+", "", compact).strip()
    return ignore_symbols(compact)


class ExamAnswerManager:
    """答题匹配管理器，集中处理题目与选项匹配。"""

    def find_answer_item(self, answer_bank: dict[str, Any], title: str) -> dict | None:
        normalized_title = _normalize_question_title(title)
        if not normalized_title:
            return None

        exact_match: dict | None = None
        best_fuzzy_match: dict | None = None
        best_score = 0.0

        for raw_title, answer_item in answer_bank.items():
            bank_title = _normalize_question_title(raw_title)
            if not bank_title:
                continue
            if bank_title == normalized_title:
                return answer_item
            if normalized_title in bank_title or bank_title in normalized_title:
                exact_match = answer_item
                continue

            score = SequenceMatcher(None, normalized_title, bank_title).ratio()
            if score > best_score:
                best_score = score
                best_fuzzy_match = answer_item

        if exact_match:
            return exact_match
        if best_score >= 0.92:
            return best_fuzzy_match
        return None

    def extract_correct_options(self, answer_item: dict | None) -> list[str]:
        if not answer_item or "optionList" not in answer_item:
            return []
        return [
            option["content"]
            for option in answer_item["optionList"]
            if option.get("isCorrect") == 1
        ]

    def find_option_index(
        self, option_text: str, page_options: Any, options_count: int
    ) -> int:
        normalized_answer = _normalize_option_text(option_text)
        if not normalized_answer:
            return -1

        best_fuzzy_index = -1
        best_score = 0.0
        for index in range(options_count):
            try:
                page_text = page_options.nth(index).inner_text()
            except Exception:
                continue
            normalized_page_option = _normalize_option_text(page_text)
            if not normalized_page_option:
                continue
            if (
                normalized_answer in normalized_page_option
                or normalized_page_option in normalized_answer
            ):
                return index
            score = SequenceMatcher(
                None, normalized_answer, normalized_page_option
            ).ratio()
            if score > best_score:
                best_score = score
                best_fuzzy_index = index

        if best_score >= 0.88:
            return best_fuzzy_index
        return -1
