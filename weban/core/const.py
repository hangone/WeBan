"""WeBan 全局选择器与常量定义。"""

# ---------------------------------------------------------------------------
# 业务流程通用选择器
# ---------------------------------------------------------------------------
SEL_DIALOG = ".van-dialog, .van-toast, .mint-msgbox, .mint-toast"
SEL_BROADCAST_MODAL = ".broadcast-modal"
SEL_BROADCAST_CLOSE_BTN = ".broadcast-modal button"

# 流程控制
SEL_JOIN_BTN = 'button.exam-button:has-text("参加考试")'
SEL_START_BTN = 'a.popup-btn:has-text("开始考试")'
SEL_SUBMIT_BTN = (
    ".sheet .bottom-ctrls .mint-button:has-text('交卷'), "
    ".sheet .bottom-ctrls button:has-text('交卷'), "
    ".sheet .bottom-ctrls a:has-text('交卷'), "
    ".confirm-sheet .bottom-ctrls .mint-button:has-text('交卷'), "
    ".confirm-sheet .bottom-ctrls button:has-text('交卷'), "
    ".confirm-sheet .bottom-ctrls a:has-text('交卷'), "
    ".sheet .bottom-ctrls button:text-matches('^\\s*交\\s*卷\\s*$', 'i'), "
    ".confirm-sheet .bottom-ctrls button:text-matches('^\\s*交\\s*卷\\s*$', 'i'), "
    "button:text-matches('^\\s*交\\s*卷\\s*$', 'i'), "
    ".mint-button:text-matches('^\\s*交\\s*卷\\s*$', 'i')"
)

# 任务/项目列表
SEL_TASK_BLOCK = ".task-block"
SEL_TASK_BLOCK_TITLE = ".task-block-title"
SEL_IMG_TEXT_BLOCK = ".img-text-block"
SEL_TASK_OR_IMG_BLOCK = ".task-block, .img-text-block"

# ---------------------------------------------------------------------------
# 按钮与交互
# ---------------------------------------------------------------------------
SUBMIT_CONFIRM_LABELS = [
    "确 认",
    "确认",
    "确定",
    "确认交卷",
    "提交",
    "立即交卷",
    "交卷",
    "提交试卷",
]
SUBMIT_IGNORE_LABELS = ["取消", "退出", "暂不", "返回", "继续考试"]

SEL_CONFIRM_BTN = (
    ".van-dialog__confirm, .mint-msgbox-confirm, "
    "button:text-is('确认'), button:text-is('确定'), button:text-is('确 认')"
)

SEL_SUBMIT_CONFIRM = (
    ", ".join(
        [
            f"button:text-matches('^\\s*{lb.replace(' ', '\\s*')}\\s*$', 'i')"
            for lb in SUBMIT_CONFIRM_LABELS
        ]
    )
    + f", {SEL_CONFIRM_BTN}"
)

# 常用功能按钮
SEL_COURSE_READY = ".van-tab, .van-collapse-item, .img-texts-item, .fchl-item"
SEL_INTERMEDIATE_WAIT_TARGETS = (
    ".van-collapse-item, .img-texts-item, .fchl-item, #agree, "
    ".agree-checkbox input, input[type='checkbox'], .img-text-block, .task-block"
)
SEL_COURSE_LIST_MARKERS = ".van-collapse-item, .img-texts-item, .fchl-item"

SEL_NEXT_BTN = (
    ".bottom-ctrls .mint-button:has-text('下一题'), "
    ".bottom-ctrls button:has-text('下一题'), "
    ".bottom-ctrls a:has-text('下一题'), "
    ".bottom-ctrls button:text-matches('^\\s*下\\s*一\\s*题\\s*$', 'i'), "
    ".bottom-ctrls span:text-matches('^\\s*下\\s*一\\s*题\\s*$', 'i'), "
    ".bottom-ctrls div:text-matches('^\\s*下\\s*一\\s*题\\s*$', 'i'), "
    "button:has-text('下一题'), a:has-text('下一题'), "
    ".btn-next, .btn-primary-next"
)

SEL_ANSWER_CARD_BTN = (
    ".bottom-ctrls .mint-button:has-text('答题卡'), "
    ".bottom-ctrls button:has-text('答题卡'), "
    ".bottom-ctrls a:has-text('答题卡'), "
    ".bottom-ctrls button:text-matches('^\\s*答\\s*题\\s*卡\\s*$', 'i'), "
    ".bottom-ctrls span:text-matches('^\\s*答\\s*题\\s*卡\\s*$', 'i'), "
    ".bottom-ctrls div:text-matches('^\\s*答\\s*题\\s*卡\\s*$', 'i')"
)

SEL_BTN_SUBMIT_SIGN = "button:has-text('提交'), button:has-text('确认提交')"
SEL_TASK_DONE_LABEL = ".task-block-done"

# ---------------------------------------------------------------------------
# 页面状态检测
# ---------------------------------------------------------------------------
SEL_EXAM_TAB = '.van-tab:has-text("在线考试")'
SEL_QUESTION_TITLE = ".quest-stem"
SEL_OPTIONS = ".quest-option-item"
SEL_QUEST_CATEGORY = ".quest-category"
SEL_QUEST_INDICATOR = ".quest-indicator"

# ---------------------------------------------------------------------------
# 课程学习核心选择器
# ---------------------------------------------------------------------------
SEL_AGREE_CHECKBOX = "#agree, input[type='checkbox']"
SEL_BTN_NEXT_STEP = (
    "button:has-text('下一步'), a:has-text('下一步'), "
    "button:has-text('同意'), a:has-text('同意')"
)
SEL_COURSE_LIST_MARKERS = ".van-collapse-item, .img-texts-item, .fchl-item"
SEL_COURSE_LIST_WAIT_TARGETS = ".van-collapse-item, .img-texts-item, .fchl-item, .task-block, .img-text-block, #agree, .van-cell"
SEL_IMG_TEXT_ITEM = ".img-texts-item, .img-text-item, .img-text-block-item, .course-item, .lesson-item, .list-item, .van-cell"
SEL_IMG_TEXT_ITEM_VISIBLE = ".img-texts-item:visible, .img-text-item:visible, .img-text-block-item:visible, .course-item:visible, .lesson-item:visible, .list-item:visible, .van-cell:visible"
SEL_IMG_TEXT_ITEM_NOT_PASSED = ".img-texts-item:not(.passed), .img-text-item:not(.passed), .img-text-block-item:not(.passed), .course-item:not(.passed), .lesson-item:not(.passed), .list-item:not(.passed), .van-cell:not(.passed)"
SEL_IMG_TEXT_ITEM_NOT_PASSED_VISIBLE = ".img-texts-item:not(.passed):visible, .img-text-item:not(.passed):visible, .img-text-block-item:not(.passed):visible, .course-item:not(.passed):visible, .lesson-item:not(.passed):visible, .list-item:not(.passed):visible, .van-cell:not(.passed):visible"

SEL_FCHL_ITEM = ".fchl-item, .fchl-items, .course-item-fchl"
SEL_FCHL_ITEM_VISIBLE = ".fchl-item:visible, .fchl-items:visible"
SEL_FCHL_ITEM_NOT_PASSED = (
    ".fchl-item:not(.fchl-item-active), .fchl-items:not(.fchl-item-active)"
)
SEL_FCHL_ITEM_NOT_PASSED_VISIBLE = ".fchl-item:not(.fchl-item-active):visible, .fchl-items:not(.fchl-item-active):visible"
SEL_COLLAPSE_ITEM = ".van-collapse-item, .course-chapter, .chapter-list-item"
SEL_COLLAPSE_ITEM_TITLE = ".van-collapse-item__title, .van-cell__title, .chapter-title"
SEL_COLLAPSE_CELL_TITLE = ".van-cell__title"
SEL_RUNTIME_BTN_BACKLIST = ".back-list"
SEL_COMMENT_BACK_BTN = ".comment-footer-button:has-text('返回')"
SEL_NAV_BAR_LEFT = ".van-nav-bar__left"
SEL_DIALOG_PREV_BTN = ".pop-jsv-prev"
SEL_DIALOG_POP = ".pop-jsv, .pop-jsv-prev"
SEL_ITEM_TITLE_TEXT = ".title, .fchl-item-content-title, .van-cell__title, .img-texts-item-title, .course-name, .name, .lesson-name"
SEL_TASK_OR_IMG_BLOCK = (
    ".task-block, .img-text-block, .img-texts-item-block, .project-item, .van-card"
)
SEL_RUNTIME_MARKERS = (
    ".back-list, .btn-start, .btn-next, .btn-prev, .btn-at, .btn-af, .page-WH"
)
SEL_RUNTIME_FRAME_SKELETON = ".page-container, .page-item, .btn-next, .back-list"
SEL_COURSE_JS_ITEMS_VISIBLE = ".img-texts-item:visible, .fchl-item:visible"
