# ---------------------------------------------------------------------------
# 业务流程通用选择器
# ---------------------------------------------------------------------------

# 对话框与弹窗 (ExamPage.vue: van-dialog, mint-msgbox)
SEL_DIALOG = ".van-dialog, .mint-msgbox, .mint-toast"
SEL_BROADCAST_MODAL = ".broadcast-modal"

# ---------------------------------------------------------------------------
# 流程控制按钮 (ExamPopup.vue, ExamPage.vue)
# ---------------------------------------------------------------------------

SEL_JOIN_BTN = '.exam-button:has-text("参加考试")'
SEL_START_BTN = '.popup-btn:has-text("开始考试")'
SEL_SUBMIT_BTN = (
    ".sheet .bottom-ctrls button:has-text('交卷'), "
    ".confirm-sheet .bottom-ctrls button:has-text('交卷')"
)

SUBMIT_CONFIRM_LABELS = ["确 认", "确认", "确定", "提交", "交卷"]
SUBMIT_IGNORE_LABELS = ["取消", "退出", "暂不", "返回", "继续考试"]
SEL_CONFIRM_BTN = "button:has-text('确认'), button:has-text('确定')"
SEL_SUBMIT_CONFIRM = (
    ", ".join(
        [
            f"button:text-matches('^\\s*{lb.replace(' ', '\\s*')}\\s*$', 'i')"
            for lb in SUBMIT_CONFIRM_LABELS
        ]
    )
    + f", {SEL_CONFIRM_BTN}"
)

# ---------------------------------------------------------------------------
# 任务/项目列表 (CourseIndex.vue: .task-block, .img-text-block)
# ---------------------------------------------------------------------------

SEL_TASK_BLOCK = ".task-block"
SEL_TASK_BLOCK_TITLE = ".task-block-title"
SEL_TASK_DONE_LABEL = ".task-block-done"
SEL_IMG_TEXT_BLOCK = ".img-text-block"
SEL_TASK_OR_IMG_BLOCK = ".task-block, .img-text-block"

# ---------------------------------------------------------------------------
# 考试页面 (ExamPage.vue)
# ---------------------------------------------------------------------------

SEL_EXAM_TAB = '.van-tab:has-text("在线考试")'
SEL_EXAM_ITEM = ".exam-item"
SEL_EXAM_ITEM_TITLE = ".exam-item-title, .exam-info h3"
SEL_EXAM_ITEM_PASS = ".exam-pass, .exam-result"
SEL_EXAM_RESULT_SCORE = ".score-num, .score, .exam-score, .result-score, .score-text"
SEL_EXAM_SUBMIT_AREA = "button:has-text('交卷'), button:has-text('完成')"

SEL_EXAM_PREPARE_POPUPS = (
    ".van-popup, .mint-popup, .confirm-sheet, .sheet, .mint-msgbox"
)
SEL_EXAM_PREPARE_NEXT = "button:has-text('下一步'), a:has-text('下一步')"
SEL_EXAM_PREPARE_CONFIRM = (
    "button:has-text('确认'), button:has-text('完成'), button:has-text('提交')"
)
SEL_EXAM_INTERMEDIATE_PROJECT = ".img-text-block, .task-block"

SEL_EXAM_SHEET = ".sheet"
SEL_EXAM_CONFIRM_SHEET = ".confirm-sheet"
SEL_EXAM_SHEET_BOTTOM_CTRLS = ".sheet .bottom-ctrls"
SEL_EXAM_CONFIRM_SHEET_BOTTOM_CTRLS = ".confirm-sheet .bottom-ctrls"
SEL_EXAM_BOTTOM_CTRLS = ".bottom-ctrls"
SEL_EXAM_NEXT_BTN_IN_BOTTOM = "button:has-text('下一题'), .mint-button:has-text('下一题'), .van-button:has-text('下一题')"
SEL_EXAM_CARD_BTN_IN_BOTTOM = "text=答题卡"
SEL_EXAM_QUEST_INDEX_ITEM_TEMPLATE = (
    ".sheet .quest-indexs-list li:has(span:text-is('{num}')), "
    ".sheet .quest-indexs-list li:has-text('{num}')"
)
SEL_EXAM_QUEST_INDEX_TEXT_TEMPLATE = (
    ".sheet span:text-is('{num}'), .sheet div:text-is('{num}')"
)

# ---------------------------------------------------------------------------
# 答题页面 (ExamPage.vue: .quest-*)
# ---------------------------------------------------------------------------

SEL_QUEST_STEM = ".quest-stem"
SEL_QUEST_STEM_SUB = ".title, .quest-title, .stem-text"
SEL_QUEST_OPTION = ".quest-option-item"
SEL_QUEST_OPTIONS = ".quest-option-item, .answerPg-container-item"
SEL_QUEST_CATEGORY = ".quest-category"
SEL_QUEST_INDICATOR = ".quest-indicator, .answerPg-header-no"

SEL_NEXT_BTN = (
    ".btn-start, .btn-next, .btn-ce, .btn-aq, .btn-at, .btn-af, .btn-base, .back-list, "
    "button:has-text('下一题'), .mint-button:has-text('下一题'), .van-button:has-text('下一题')"
)
SEL_ANSWER_CARD_BTN = (
    ".bottom-ctrls button:has-text('答题卡'), "
    ".bottom-ctrls button:has-text('查看答题卡')"
)

# ---------------------------------------------------------------------------
# 课程列表 (CourseIndex.vue)
# ---------------------------------------------------------------------------

SEL_COURSE_TAB = ".van-tab"
SEL_COURSE_LIST_MARKERS = ".van-collapse-item, .img-texts-item, .fchl-item"
SEL_COURSE_LIST_WAIT_TARGETS = (
    ".van-collapse-item, .img-texts-item, .fchl-item, .task-block, .img-text-block, "
    "#agree, .van-cell"
)
SEL_COURSE_LIST_ITEMS = ", ".join(
    [
        ".img-texts-item",
        ".fchl-item",
        ".task-block",
        ".van-collapse-item__content .van-cell",
        ".van-collapse-item__content .course-item",
        ".van-collapse-item__content .lesson-item",
        ".list-item-content",
    ]
)

SEL_COLLAPSE_ITEM = ".van-collapse-item"
SEL_COLLAPSE_ITEM_TITLE = ".van-cell__title"
SEL_COLLAPSE_ITEM_CONTENT = ".van-collapse-item__content"

SEL_IMG_TEXT_ITEM = ".img-texts-item, .van-collapse-item__content .van-cell"
SEL_IMG_TEXT_ITEM_VISIBLE = (
    ".img-texts-item:visible, .van-collapse-item__content .van-cell:visible"
)
SEL_IMG_TEXT_ITEM_NOT_PASSED = (
    ".img-texts-item:not(.passed), .van-collapse-item__content .van-cell:not(.passed)"
)
SEL_IMG_TEXT_ITEM_NOT_PASSED_VISIBLE = (
    ".img-texts-item:not(.passed):visible, "
    ".van-collapse-item__content .van-cell:not(.passed):visible"
)

SEL_FCHL_ITEM = ".fchl-item"
SEL_FCHL_ITEM_VISIBLE = ".fchl-item:visible"
SEL_FCHL_ITEM_NOT_PASSED = ".fchl-item:not(.fchl-item-active)"
SEL_FCHL_ITEM_NOT_PASSED_VISIBLE = ".fchl-item:not(.fchl-item-active):visible"

SEL_ITEM_TITLE_TEXT = ".title, .fchl-item-content-title, .van-cell__title, .name"
SEL_ITEM_COMPLETED_ICON = ".van-icon-success, .van-icon-passed, .icon-finish"

# ---------------------------------------------------------------------------
# 协议与承诺书
# ---------------------------------------------------------------------------

SEL_AGREE_CHECKBOX = "#agree, input[type='checkbox']"
SEL_BTN_NEXT_STEP = "button:has-text('下一步'), button:has-text('同意')"
SEL_BTN_SUBMIT_SIGN = "button:has-text('保 存'), button:has-text('确认')"

# ---------------------------------------------------------------------------
# 导航与返回
# ---------------------------------------------------------------------------

SEL_NAV_BAR_LEFT = ".van-nav-bar__left"
SEL_NAV_BAR_TITLE = ".van-nav-bar__title"
SEL_COMMENT_BACK_BTN = ".comment-footer-button:has-text('返回')"

# ---------------------------------------------------------------------------
# 课程运行时 (mcwk.mycourse.cn/item.js: .page-active, .btn-*, video)
# ---------------------------------------------------------------------------

SEL_RUNTIME_ACTIVE_VIDEO = ".page-active video"
SEL_RUNTIME_VIDEO_PLAY_BTN = (
    ".page-active .vjs-big-play-button, .vjs-big-play-button:visible"
)
SEL_RUNTIME_CHOICE = ".page-active [class*='p12'], .page-active [class*='choice']"
SEL_RUNTIME_INTERACTIVE_ITEMS = (
    ".page-active [class*='p17'], .page-active .interactive-item"
)
SEL_RUNTIME_INTERACTIVE_CLOSE = ".page-active .p1712"
SEL_RUNTIME_QUIZ_LABELS = ".page-active .aq-item-label"
SEL_RUNTIME_QUIZ_CHECKED = ".page-active input:checked"
SEL_RUNTIME_DIALOG_POP = ".pop-jsv, .page-end.page-active"
SEL_RUNTIME_DIALOG_PREV_BTN = (
    ".pop-jsv-prev, .back-list, .btn-back, button:has-text('返回列表')"
)

SEL_RUNTIME_NAV_BTNS = ", ".join(
    [
        ".page-active .btn-aq-21",
        ".page-active .btn-aq",
        ".page-active .btn-at",
        ".page-active .btn-af",
        ".page-active .btn-start",
        ".page-active .btn-next",
        ".page-active .btn-ce",
        ".page-active .back-list",
        ".btn-start:visible",
        ".btn-base:has-text('开始'):visible",
        ".btn-base:has-text('下一步'):visible",
    ]
)

SEL_RUNTIME_PROBE_CANDIDATES = (
    ".page-active img, .page-active div, .page-active a, .page-active label"
)

# ---------------------------------------------------------------------------
# 兼容性别名 (保持向后兼容)
# ---------------------------------------------------------------------------

SEL_QUESTION_TITLE = SEL_QUEST_STEM
SEL_QUESTION_TITLE_SUB = SEL_QUEST_STEM_SUB
SEL_OPTIONS = SEL_QUEST_OPTIONS
