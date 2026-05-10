# ---------------------------------------------------------------------------
# 登录页选择器 (Login.vue)
# ---------------------------------------------------------------------------

# 学校选择：登录页专有语义类名
SEL_LOGIN_TENANT_INPUT = "input.loginp-input.loginp-inputsl[readonly]"
SEL_LOGIN_TENANT_SEARCH = (
    "input[placeholder*='搜索'], .van-search__field input, .search-input input"
)
SEL_LOGIN_ACCOUNT = (
    "input.loginp-input:not([readonly]):not([maxlength]):not([type='password'])"
)
SEL_LOGIN_PASSWORD = "input.loginp-input-pwd, input.loginp-input[type='password']"
SEL_LOGIN_CAPTCHA_IMG = "img.loginp-label-verify, img[src*='randLetterImage']"
SEL_LOGIN_CAPTCHA_INPUT = "input.loginp-input[maxlength='6']"
SEL_LOGIN_SUBMIT_BTN_AUTH = "a.loginp-submit"
SEL_LOGIN_TOAST = (
    ".van-toast__text, .van-toast, "
    ".mint-toast, .mint-toast-text, "
    ".van-dialog__message, .el-message__content"
)
SEL_LOGIN_POPUP_CONFIRM = (
    ".van-dialog__confirm, .mint-msgbox-confirm, "
    "button:has-text('确定'), button:has-text('确认')"
)
SEL_LOGIN_SCHOOL_ITEM_TEMPLATE = (
    ".van-cell__title span:text-is('{name}'), "
    ".van-cell:has-text('{name}') .van-cell__title"
)

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
SEL_EXAM_RESULT_SCORE = ".exam-score, .result-score, .score-text"
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
# 主页面底部控制栏（答题卡/上一题/下一题，无交卷按钮）
# 使用 :visible 过滤，避免匹配到 sheet/confirm-sheet 内的隐藏控制栏
SEL_EXAM_BOTTOM_CTRLS = ".bottom-ctrls"
# Sheet 弹窗内的交卷按钮
SEL_EXAM_SHEET_SUBMIT = (
    ".sheet .bottom-ctrls button:has-text('交卷'), "
    ".sheet .bottom-ctrls .mint-button:has-text('交卷')"
)
# Confirm-sheet 内的确认/交卷按钮
SEL_EXAM_CONFIRM_SUBMIT = (
    ".confirm-sheet .bottom-ctrls button:has-text('交卷'), "
    ".confirm-sheet .bottom-ctrls button:has-text('确 认'), "
    ".confirm-sheet .bottom-ctrls .mint-button:has-text('交卷'), "
    ".confirm-sheet .bottom-ctrls .mint-button:has-text('确 认')"
)
SEL_EXAM_NEXT_BTN_IN_BOTTOM = (
    "button:has-text('下一题'), "
    ".mint-button:has-text('下一题'), "
    ".van-button:has-text('下一题')"
)
SEL_EXAM_CARD_BTN_IN_BOTTOM = (
    "button:has-text('答题卡'), .mint-button:has-text('答题卡')"
)
SEL_EXAM_QUEST_INDEX_ITEM_TEMPLATE = (
    ".sheet .quest-indexs-list li:has(span:text-is('{num}')), "
    ".sheet .quest-indexs-list li:has-text('{num}')"
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
# 课程列表项：仅限课程内容级别的元素，不包含 project 级别的 .task-block
SEL_COURSE_LIST_ITEMS = ", ".join(
    [
        ".img-texts-item",
        ".fchl-item",
    ]
)

SEL_COLLAPSE_ITEM = ".van-collapse-item"
SEL_COLLAPSE_ITEM_TITLE = ".van-cell__title"
SEL_COLLAPSE_ITEM_CONTENT = ".van-collapse-item__content"

SEL_IMG_TEXT_ITEM = ".img-texts-item"
SEL_IMG_TEXT_ITEM_NOT_PASSED = ".img-texts-item:not(.passed)"
SEL_IMG_TEXT_ITEM_NOT_PASSED_VISIBLE = ".img-texts-item:not(.passed):visible"

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

SEL_RUNTIME_NAV_BTNS = ", ".join(
    [
        ".page-active .btn-aq-21",
        ".page-active .btn-aq",
        ".page-active .btn-at",
        ".page-active .btn-af",
        ".page-active .btn-start",
        ".page-active .btn-next-end",
        ".page-active .btn-next2",
        ".page-active .btn-next-aq01",
        ".page-active .btn-next",
        ".page-active .btn-ce",
        ".page-active .btn-at-1",
        ".page-active .btn-at",
        ".page-active .pri-start-btn",
        ".page-active .page-start-btn",
        ".page-active .page-commit",
        ".page-active .page-success-button",
        ".page-active .page-fail-button",
        ".page-active .back-list",
        # A08030 等 VR/互动课程的自定义按钮
        ".page-active .changePage",
        ".page-active .page-0-button",
        ".page-active .page-1-button",
        ".page-active .page-2-button",
        ".page-active .page-3-button",
        ".page-active .page-4-button",
        ".page-active .page-5-button",
        ".page-active .page-6-button",
        ".page-active .page-finish-button",
        ".page-active .page-inspect-btn",
        # DA0416050 等课程的浮动导航栏，btn-next/btn-prev 在 .btn-next-prev 容器内
        ".btn-next-prev .btn-next",
        ".btn-next-prev .btn-prev",
        # 全局回退（不在 .page-active 内的按钮）
        ".btn-next-end",
        ".btn-start",
        ".btn-base:has-text('开始')",
        ".btn-base:has-text('下一步')",
        # A08030 全局回退
        ".changePage",
        ".page-finish-button",
        ".page-inspect-btn",
    ]
)
