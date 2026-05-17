from dataclasses import dataclass, field
from typing import List

PROJECT_STUDY_TABS = {
    "pre": [3, 2],
    "normal": [3, 1, 2],
    "special": [3, 2],
    "military": [3],
    "lab": [3],
    "foods": [3],
    "contest": [3],
}


@dataclass
class StudyRunState:
    study_tabs: List[int] = field(default_factory=list)
    active_tab_index: int = 0
    current_project_title: str = ""
    active_section_index: int = -1
    expanded_tabs: set = field(default_factory=set)
    expanded_sections: set = field(default_factory=set)
    _expand_count_map: dict = field(default_factory=dict)
    _verified_complete_sections: set = field(default_factory=set)
    _last_expanded_section_key: str = ""
