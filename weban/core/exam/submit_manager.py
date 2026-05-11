import re


class ExamSubmitManager:
    """提交流程管理器。"""

    def is_submit_blocked_message(self, text: str) -> bool:
        compact = re.sub(r"\s+", "", text or "")
        if not compact:
            return False
        if "请作答当前题目" in compact or "请作答" in compact:
            return True
        un_answered_match = re.search(r"还有(\d+)道未作答", compact)
        if un_answered_match and int(un_answered_match.group(1)) > 0:
            return True
        return bool(re.search(r"第[\d,，、\s]+题未作答", compact))

    def extract_score_from_result_text(self, result_text: str) -> int:
        score_match = re.search(r"(\d+)\s*分", result_text or "")
        if not score_match:
            return 0
        return int(score_match.group(1))

    def is_popup_passed(self, result_text: str) -> bool:
        text = result_text or ""
        return "通过" in text or "合格" in text
