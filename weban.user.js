// ==UserScript==
// @name         安全教育自动学习助手 | 网课助手 自动刷课 自动答题 自动考试
// @name:zh-CN   安全教育自动学习助手 | 自动刷课 自动答题 自动考试
// @namespace    https://github.com/hangone/WeBan
// @version      1.0.0
// @description  安全微伴/安全教育平台全自动学习：自动学完未完成课程、自动答题、自动交卷，支持题库匹配与倍速播放。Secure Education auto study & exam assistant.
// @description:zh-CN  安全微伴/安全教育平台全自动学习：自动学完未完成课程、自动答题、自动交卷，支持题库匹配与倍速播放。
// @author       hangone
// @homepageURL  https://github.com/hangone/WeBan
// @supportURL   https://github.com/hangone/WeBan/issues
// @source       https://github.com/hangone/WeBan
// @license      GPL-3.0
// @match        https://weiban.mycourse.cn/*
// @match        https://mcwk.mycourse.cn/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        GM_info
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      cdn.jsdelivr.net
// @connect      raw.githubusercontent.com
// @connect      127.0.0.1
// @connect      localhost
// @updateURL    https://cdn.jsdelivr.net/gh/hangone/WeBan@main/weban.user.js
// @downloadURL  https://cdn.jsdelivr.net/gh/hangone/WeBan@main/weban.user.js
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  // ════════════════════════════════════════════════════════════════════════
  // 设计原则
  // ---------------------------------------------------------------------------
  // 主站（weiban）是 Vue + Vuex 应用，所有接口调用都封装在 store 的 actions
  // 里（源码见 src/online-*/main-site/src/store/services.js）。所以本脚本
  // **不自己拼请求**：通过 #app.__vue__ 拿到根实例，直接 dispatch 官方
  // action —— 签名、加密、token、公共参数全部由页面自己的代码处理，
  // 平台改加密算法也不用改脚本。
  //
  // 课程页（mcwk）是纯 jQuery 页面，学习进度靠真实点击翻页产生（见
  // src/online-*/mcwk/course-A11078/item.js）。所以课程学习采用**真实
  // 打开课程页 + 驱动页面 DOM**：点击 .btn-next 翻页、点 .btn-next-end 完课，
  // 完课请求由页面自己的 finishWxCourse() 发出。
  // ════════════════════════════════════════════════════════════════════════

  // 版本号唯一来源是头部 @version
  const VERSION = (typeof GM_info === 'object' && GM_info?.script?.version) || '1.0.0';

  const STORE = {
    settings: 'weban.settings.v1',
    lastRun: 'weban.lastRun.v1',
    captchaEndpoint: 'weban.captchaEndpoint.v1',
    captchaToken: 'weban.captchaToken.v1',
  };

  const isCoursePage = () => location.host === 'mcwk.mycourse.cn';
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ────────────────────────────────────────────────────────────────────────
  // 1. 取页面能力：Vue 根实例 → $store
  // ────────────────────────────────────────────────────────────────────────

  let appVm = null;

  function getApp() {
    if (appVm && appVm.$store) return appVm;
    const root = document.querySelector('#app');
    // Vue 2 会把根实例挂在根元素上（__vue__），取到后沿着 $root 归一
    for (const el of [root, document.body, ...document.querySelectorAll('#app *')]) {
      const vm = el && el.__vue__;
      if (vm) {
        appVm = vm.$root && vm.$root.$store ? vm.$root : vm;
        if (appVm.$store) return appVm;
      }
    }
    return null;
  }

  // 调官方 store action：dispatch('getCourseUrl', payload)
  async function dispatch(type, payload) {
    const app = getApp();
    if (!app) throw new Error('未找到页面应用实例，请刷新页面后重试');
    return app.$store.dispatch(type, payload);
  }

  // 读 store 里的登录信息（页面自己存的 user）
  function getPageUser() {
    const app = getApp();
    const fromStore = app?.$store?.state?.user;
    if (fromStore && (fromStore.userId || fromStore.token)) return fromStore;
    try {
      for (const key of ['user', 'userInfo']) {
        const raw = localStorage.getItem(key);
        if (!raw) continue;
        const obj = JSON.parse(raw);
        if (obj && (obj.userId || obj.token)) return obj;
      }
    } catch { /* ignore */ }
    return null;
  }

  function requireUser() {
    const user = getPageUser();
    if (!user || !user.userId) {
      throw Object.assign(new Error('页面未登录或登录信息未就绪，请先在官网登录，刷新后再点「开始」'), {
        needsLogin: true,
      });
    }
    return user;
  }

  // ────────────────────────────────────────────────────────────────────────
  // 2. 题库（唯一需要外部网络的资源）
  // ────────────────────────────────────────────────────────────────────────

  let answerBank = null;
  let answerIndex = null;

  async function loadAnswerBank() {
    if (answerBank) return answerBank;
    const urls = [
      'https://cdn.jsdelivr.net/gh/hangone/WeBan@main/answer/answer.json',
      'https://raw.githubusercontent.com/hangone/WeBan/main/answer/answer.json',
    ];
    for (const u of urls) {
      try {
        const data = await gmGetJson(u);
        if (data && typeof data === 'object') {
          answerBank = data;
          answerIndex = new Map();
          for (const [k, v] of Object.entries(data)) {
            const nk = normalizeQuestion(k);
            if (nk) answerIndex.set(nk, v);
          }
          log('题库已加载：' + answerIndex.size + ' 题', 'ok');
          return answerBank;
        }
      } catch (e) {
        log('题库源不可用（' + u.split('/')[2] + '）：' + e.message, 'warn');
      }
    }
    answerBank = {};
    answerIndex = new Map();
    log('题库加载失败，将按随机策略作答', 'warn');
    return answerBank;
  }

  function gmGetJson(url) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url,
        timeout: 20000,
        onload: (res) => {
          if (res.status !== 200) return reject(new Error('HTTP ' + res.status));
          try { resolve(JSON.parse(res.responseText)); } catch (e) { reject(e); }
        },
        onerror: () => reject(new Error('网络错误')),
        ontimeout: () => reject(new Error('超时')),
      });
    });
  }

  const normalizeQuestion = (text) =>
    String(text || '')
      .replace(/\s+/g, '')
      .replace(/[（）()【】\[\]「」""''：:，,。.；;、!！?？\-—_]/g, '')
      .toLowerCase();

  // 题库结构：{"题目": {"optionList": [{"content": "...", "isCorrect": 1}], "type": 1}}
  function lookupAnswer(question) {
    if (!answerIndex) return null;
    const entry = answerIndex.get(normalizeQuestion(question.title || question.content));
    if (!entry) return null;
    const correct = (entry.optionList || []).filter((o) => Number(o.isCorrect) === 1).map((o) => o.content);
    return correct.length ? correct : null;
  }

  // 用答案文本匹配选项（题库和试卷选项顺序可能不同，按内容匹配更稳）
  function matchOptions(question, correctContents, fallbackRandom) {
    const options = question.optionList || [];
    if (!options.length) return [];
    if (correctContents && correctContents.length) {
      const ids = [];
      for (const want of correctContents) {
        const target = normalizeQuestion(want);
        if (!target) continue;
        const hit = options.find((o) => normalizeQuestion(o.content) === target);
        if (hit && !ids.includes(hit.id)) ids.push(hit.id);
      }
      if (ids.length) return ids;
    }
    if (!fallbackRandom) return [];
    if (Number(question.type) === 2) return options.map((o) => o.id);
    return [options[0].id];
  }


  // ────────────────────────────────────────────────────────────────────────
  // 3. 课程学习：真实打开课程页，驱动页面 DOM
  //    依据 src/online-*/mcwk/course-A11078/item.js：
  //      .btn-next      翻页（点击后 pageNums++）
  //      .btn-next-end  完课（pageNums >= 7 才允许，内部调 finishWxCourse()）
  //    完课请求由页面自己发出，签名/参数/token 全由页面处理。
  // ────────────────────────────────────────────────────────────────────────

  const COURSE_PAGE_TIMEOUT = 30000;
  let courseTab = null;

  // 在后台标签页打开课程页；学完后关闭
  async function openCourseTab(courseUrl) {
    const win = window.open(courseUrl, '_blank');
    if (!win) {
      throw new Error('浏览器拦截了新窗口，请允许本站弹出窗口后重试');
    }
    courseTab = win;
    return win;
  }

  function closeCourseTab() {
    try { courseTab && courseTab.close(); } catch { /* ignore */ }
    courseTab = null;
  }

  // 等待课程页渲染完成（.btn-next 出现）
  async function waitCourseReady(win, timeout = COURSE_PAGE_TIMEOUT) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      try {
        const doc = win.document;
        if (doc && doc.querySelector('.btn-next')) return true;
      } catch { /* 跨域或未就绪 */ }
      await sleep(500);
    }
    throw new Error('课程页加载超时');
  }

  // 驱动一次翻页：点 .btn-next，等页面切换
  async function clickNext(win) {
    const doc = win.document;
    const buttons = [...doc.querySelectorAll('.btn-next')];
    if (!buttons.length) throw new Error('课程页没有可点击的下一页按钮');

    // 找到当前可见页里的按钮
    const bar = doc.querySelector('.page-active');
    const target = (bar && [...bar.querySelectorAll('.btn-next')][0]) || buttons[0];
    target.click();
    await sleep(1200);
    return buttons.length;
  }

  function getPageNumber(win) {
    // item.js 用 pageNums 计数，页面里还有 .page-reciprocal 显示页码
    try {
      const el = win.document.querySelector('.page-reciprocal');
      const text = el ? el.textContent : '';
      const m = String(text).match(/(\d+)\s*\/\s*(\d+)/);
      if (m) return { current: Number(m[1]), total: Number(m[2]) };
    } catch { /* ignore */ }
    return null;
  }

  // 学一门课：打开 → 翻到底 → 点完课按钮
  async function studyCourse(course, project, opts = {}) {
    const { onCancel, pageInterval = 1500 } = opts;
    const name = course.resourceName || course.resourceId;
    const checkCancel = () => {
      if (onCancel?.()) throw Object.assign(new Error('已取消'), { cancelled: true });
    };

    // 用页面自己的 action 拿课程地址（study.do + getCourseUrl.do 都在里面）
    const url = await dispatch('getCourseUrl', {
      userProjectId: project.userProjectId,
      courseId: course.resourceId,
      tenantCode: requireUser().tenantCode,
    });
    if (!url || typeof url !== 'string') {
      throw new Error('获取课程地址失败：' + JSON.stringify(url));
    }
    log('  ' + name + '：打开课程页');
    const win = await openCourseTab(url);
    try {
      await waitCourseReady(win);
      checkCancel();

      const info = getPageNumber(win);
      const totalPages = info ? info.total : null;
      log('  ' + name + '：共 ' + (totalPages ?? '?') + ' 页，开始翻页');

      let guard = 0;
      let lastPage = info ? info.current : 0;
      while (!checkCancel()) {
        const cur = getPageNumber(win);
        if (cur && cur.total && cur.current >= cur.total) break;
        if (++guard > 200) throw new Error('翻页次数异常，已停止');
        await clickNext(win);
        await sleep(pageInterval);
        const now = getPageNumber(win);
        if (now && cur && now.current === cur.current) {
          // 页码没变，可能已到最后一页
          break;
        }
        if (cur) lastPage = cur.current;
      }

      checkCancel();
      // 点完课：页面会校验 pageNums >= 7 并调用 finishWxCourse()
      const endBtn = win.document.querySelector('.btn-next-end');
      if (!endBtn) throw new Error('找不到完课按钮');
      endBtn.click();
      await sleep(3000);

      // 校验：页面上出现完成提示，或者按钮区域切换
      const finished = detectFinish(win);
      if (!finished) {
        log('  ' + name + '：完课请求已发出，但未确认到成功提示', 'warn');
      }
      log('  ' + name + '：学习完成', 'ok');
      return true;
    } finally {
      closeCourseTab();
    }
  }

  // 从课程页判断是否完课成功
  function detectFinish(win) {
    try {
      const doc = win.document;
      const endPanel = doc.querySelector('.page-end.page-active, .page-end');
      if (endPanel && endPanel.classList.contains('page-active')) return true;
      const text = doc.body ? doc.body.innerText : '';
      return /完成|已学完|学习结束/.test(text) && !/请先/.test(text);
    } catch {
      return false;
    }
  }


  // ────────────────────────────────────────────────────────────────────────
  // 4. 考试：同样走页面 action
  //    数据流见 src/online-*/main-site/src/store/services.js 与 client.py：
  //    beforePaper → preparePaper → startPaper → recordQuestion → submitPaper
  // ────────────────────────────────────────────────────────────────────────

  async function takeExam(plan, project, opts = {}) {
    const { onCancel, questionSeconds = 3, randomAnswer = true } = opts;
    const planId = plan.id || plan.examPlanId;
    if (!planId) throw new Error('考试计划缺少 id');
    const checkCancel = () => {
      if (onCancel?.()) throw Object.assign(new Error('已取消'), { cancelled: true });
    };

    await dispatch('beforePaper', { userExamPlanId: planId });
    await dispatch('preparePaper', { userExamPlanId: planId });
    checkCancel();

    const paper = await dispatch('startPaper', { userExamPlanId: planId });
    const questionList = (paper && paper.questionList) || [];
    if (!questionList.length) throw new Error('试卷没有题目');
    log('  试卷 ' + questionList.length + ' 题，限时 ' + (paper.answerTime || '?') + ' 分钟');

    let matched = 0;
    for (let i = 0; i < questionList.length; i++) {
      checkCancel();
      const q = questionList[i];
      const correct = lookupAnswer(q);
      if (correct) matched++;
      const ids = matchOptions(q, correct, randomAnswer);
      await dispatch('recordQuestion', {
        userExamPlanId: planId,
        questionId: q.id,
        useTime: Math.max(1, Math.round(questionSeconds)),
        examPlanId: plan.examPlanId,
        answerIds: ids.join(','),
      });
      if (i % 5 === 0) log('  答题 ' + (i + 1) + '/' + questionList.length);
      if (questionSeconds > 0) await sleep(questionSeconds * 1000);
    }

    checkCancel();
    const rate = Math.round((matched / questionList.length) * 100);
    log('  题库命中 ' + matched + '/' + questionList.length + '（' + rate + '%）');
    const submitted = await dispatch('submitPaper', { userExamPlanId: planId });
    const score = submitted && submitted.score;
    log('  交卷完成，得分 ' + (score ?? '?'), 'ok');
    return { score, matched, total: questionList.length };
  }

  // ────────────────────────────────────────────────────────────────────────
  // 5. 采集：全部走页面 action
  // ────────────────────────────────────────────────────────────────────────

  const COURSE_CATEGORIES = [
    [3, '必修课', 'requiredNum', 'requiredFinishedNum'],
    [1, '推送课', 'pushNum', 'pushFinishedNum'],
    [2, '自选课', 'optionalNum', 'optionalFinishedNum'],
  ];

  async function collectPlan(user, onCancel) {
    const checkCancel = () => {
      if (onCancel?.()) throw Object.assign(new Error('已取消'), { cancelled: true });
    };
    const projects = await dispatch('listMyProject', { ended: 2, tenantCode: user.tenantCode, userId: user.userId });
    const list = Array.isArray(projects) ? projects : [];
    const result = [];

    for (const project of list) {
      checkCancel();
      const entry = {
        projectName: project.projectName || '未命名项目',
        userProjectId: project.userProjectId,
        progress: null,
        categories: {},
        examPlans: [],
      };
      result.push(entry);
      log('项目：' + entry.projectName);

      try {
        entry.progress = await dispatch('showProgress', {
          userProjectId: project.userProjectId,
          tenantCode: user.tenantCode,
          userId: user.userId,
        });
        const p = entry.progress || {};
        log('  进度：必修 ' + (p.requiredFinishedNum ?? '?') + '/' + (p.requiredNum ?? '?') +
          '，考试 ' + (p.examFinishedNum ?? '?') + '/' + (p.examNum ?? '?'), 'ok');
      } catch (e) {
        log('  进度读取失败：' + e.message, 'warn');
      }

      for (const [chooseType, label] of COURSE_CATEGORIES) {
        checkCancel();
        const rows = [];
        entry.categories[label] = rows;
        try {
          const cats = await dispatch('listCategory', {
            userProjectId: project.userProjectId,
            chooseType,
            tenantCode: user.tenantCode,
            userId: user.userId,
          });
          for (const cat of cats || []) {
            checkCancel();
            const courses = await dispatch('listCourse', {
              userProjectId: project.userProjectId,
              chooseType,
              categoryCode: cat.categoryCode,
              tenantCode: user.tenantCode,
              userId: user.userId,
            });
            for (const c of courses || []) {
              rows.push({
                categoryCode: cat.categoryCode,
                resourceId: c.resourceId,
                resourceName: c.resourceName || '',
                finished: Number(c.finished) === 1,
              });
            }
          }
          const done = rows.filter((r) => r.finished).length;
          log('  ' + label + '：' + done + '/' + rows.length + ' 已完成');
        } catch (e) {
          log('  ' + label + '读取失败：' + e.message, 'warn');
        }
      }

      try {
        const plans = await dispatch('listPlan', {
          userProjectId: project.userProjectId,
          tenantCode: user.tenantCode,
          userId: user.userId,
        });
        entry.examPlans = (plans || []).map((p) => ({
          id: p.id,
          examPlanId: p.examPlanId,
          examPlanName: p.examPlanName || '考试',
          examFinishNum: p.examFinishNum || 0,
          examOddNum: p.examOddNum || 0,
          examScore: p.examScore,
          passScore: p.passScore,
        }));
      } catch (e) {
        log('  考试计划读取失败：' + e.message, 'warn');
      }
    }
    return result;
  }


  // ────────────────────────────────────────────────────────────────────────
  // 6. UI
  // ────────────────────────────────────────────────────────────────────────

  const UI = { root: null, running: false, cancelled: false };

  function settings() {
    const cfg = GM_getValue(STORE.settings, null) || {};
    return {
      pageInterval: Number(cfg.pageInterval ?? 1.5),
      examQuestionTime: Number(cfg.examQuestionTime ?? 3),
      randomAnswer: cfg.randomAnswer !== false,
    };
  }

  function captchaConfig() {
    return {
      endpoint: String(GM_getValue(STORE.captchaEndpoint, '') || '').replace(/\/+$/, ''),
      token: String(GM_getValue(STORE.captchaToken, '') || ''),
    };
  }

  const el = (id) => document.getElementById(id);

  function log(msg, level = 'info') {
    const box = el('weban-log');
    const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    const line = '[' + t + '] ' + msg;
    if (box) {
      const div = document.createElement('div');
      div.className = 'weban-line weban-' + level;
      div.textContent = line;
      box.appendChild(div);
      box.scrollTop = box.scrollHeight;
    }
    console.log('[WeBan] ' + msg);
  }

  function styles() {
    return `
      #weban-panel { position: fixed; right: 16px; bottom: 16px; width: 330px; z-index: 2147483000;
        font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; color: #1f2328;
        background: #fff; border: 1px solid #d0d7de; border-radius: 12px; overflow: hidden;
        box-shadow: 0 12px 32px rgba(0,0,0,.16); }
      #weban-panel.min .weban-body { display: none; }
      .weban-head { display: flex; justify-content: space-between; align-items: center; padding: 9px 12px;
        background: #f6f8fa; cursor: pointer; user-select: none; }
      .weban-head b { font-size: 13px; }
      .weban-head span { font-size: 11px; color: #656d76; }
      .weban-body { padding: 10px 12px 12px; }
      .weban-row { display: flex; gap: 6px; margin-bottom: 8px; }
      .weban-row button { flex: 1; padding: 7px; border-radius: 7px; cursor: pointer;
        border: 1px solid #d0d7de; background: #f6f8fa; font-size: 12.5px; }
      .weban-row button.on { background: #1f883d; border-color: #1a7f37; color: #fff; }
      .weban-row button.stop { background: #fff; border-color: #cf222e; color: #cf222e; }
      .weban-row button:disabled { opacity: .45; cursor: not-allowed; }
      #weban-log { max-height: 200px; overflow-y: auto; font: 11.5px/1.55 ui-monospace, Menlo, monospace;
        background: #0d1117; color: #c9d1d9; border-radius: 8px; padding: 8px; }
      .weban-warn { color: #e3b341; } .weban-ok { color: #56d364; } .weban-err { color: #ff7b72; }
      .weban-f { margin-bottom: 6px; }
      .weban-f label { display: block; font-size: 11.5px; color: #656d76; margin-bottom: 2px; }
      .weban-f input { width: 100%; box-sizing: border-box; padding: 5px 7px;
        border: 1px solid #d0d7de; border-radius: 6px; font-size: 12px; }
      .weban-tip { font-size: 11.5px; color: #656d76; margin-bottom: 6px; }
      .weban-tip.bad { color: #cf222e; }
    `;
  }

  function mount() {
    if (el('weban-panel')) return;
    const style = document.createElement('style');
    style.textContent = styles();
    document.head.appendChild(style);

    const panel = document.createElement('div');
    panel.id = 'weban-panel';
    panel.innerHTML =
      '<div class="weban-head"><b>安全教育助手</b><span id="weban-ver"></span></div>' +
      '<div class="weban-body">' +
        '<div id="weban-status"></div>' +
        '<div class="weban-row">' +
          '<button id="btn-start" class="on">开始</button>' +
          '<button id="btn-stop" class="stop" disabled>停止</button>' +
        '</div>' +
        '<div id="weban-settings"></div>' +
        '<div id="weban-log"></div>' +
      '</div>';
    document.body.appendChild(panel);
    const verEl = el('weban-ver');
    if (verEl) verEl.textContent = 'v' + VERSION;

    panel.querySelector('.weban-head').addEventListener('click', () => panel.classList.toggle('min'));
    el('btn-start').addEventListener('click', run);
    el('btn-stop').addEventListener('click', () => {
      UI.cancelled = true;
      log('已请求停止…', 'warn');
    });

    renderSettings();
    renderStatus();
  }

  function renderStatus() {
    const box = el('weban-status');
    if (!box) return;
    const user = getPageUser();
    box.innerHTML = user && user.userId
      ? '<div class="weban-tip"><b>已登录</b>：' + esc(user.realName || user.userName || user.userId) + '</div>'
      : '<div class="weban-tip bad">未检测到登录状态，请先在官网登录</div>';
  }

  function renderSettings() {
    const box = el('weban-settings');
    if (!box) return;
    const cfg = settings();
    box.innerHTML =
      '<div class="weban-f"><label>翻页间隔（秒）</label>' +
      '<input id="set-page" type="number" min="0.5" step="0.5" value="' + cfg.pageInterval + '"></div>' +
      '<div class="weban-f"><label>考试答题间隔（秒）</label>' +
      '<input id="set-exam" type="number" min="0" value="' + cfg.examQuestionTime + '"></div>' +
      '<div class="weban-tip"><label><input id="set-random" type="checkbox"' +
      (cfg.randomAnswer ? ' checked' : '') + '> 题库外随机作答</label></div>';
    for (const id of ['set-page', 'set-exam', 'set-random']) {
      box.querySelector('#' + id).addEventListener('change', () => {
        GM_setValue(STORE.settings, {
          pageInterval: Math.max(0.5, Number(box.querySelector('#set-page').value) || 1.5),
          examQuestionTime: Math.max(0, Number(box.querySelector('#set-exam').value) || 0),
          randomAnswer: box.querySelector('#set-random').checked,
        });
      });
    }
  }

  function setRunning(running) {
    UI.running = running;
    if (el('btn-start')) el('btn-start').disabled = running;
    if (el('btn-stop')) el('btn-stop').disabled = !running;
  }


  // ────────────────────────────────────────────────────────────────────────
  // 7. 主流程
  // ────────────────────────────────────────────────────────────────────────

  async function run() {
    if (UI.running) return;
    UI.cancelled = false;
    setRunning(true);
    renderStatus();
    const cfg = settings();
    const startedAt = Date.now();
    const summary = { studied: 0, failed: 0, exams: [] };

    try {
      const user = requireUser();
      log('使用页面登录态：' + (user.realName || user.userName || user.userId));
      await loadAnswerBank();

      const projects = await collectPlan(user, () => UI.cancelled);

      for (const project of projects) {
        if (UI.cancelled) break;
        for (const [label, rows] of Object.entries(project.categories)) {
          const pending = rows.filter((r) => !r.finished);
          if (!pending.length) continue;
          log('【' + project.projectName + '】' + label + ' 待学 ' + pending.length + ' 门');
          for (let i = 0; i < pending.length; i++) {
            if (UI.cancelled) break;
            const course = pending[i];
            log('(' + (i + 1) + '/' + pending.length + ') ' + course.resourceName);
            try {
              await studyCourse(course, project, {
                onCancel: () => UI.cancelled,
                pageInterval: cfg.pageInterval * 1000,
              });
              summary.studied++;
            } catch (e) {
              if (e.cancelled) throw e;
              summary.failed++;
              log('  跳过：' + e.message, 'warn');
            }
          }
        }

        for (const plan of project.examPlans) {
          if (UI.cancelled) break;
          if (plan.examOddNum - plan.examFinishNum <= 0) continue;
          log('【' + project.projectName + '】考试：' + plan.examPlanName);
          try {
            const r = await takeExam(plan, project, {
              onCancel: () => UI.cancelled,
              questionSeconds: cfg.examQuestionTime,
              randomAnswer: cfg.randomAnswer,
            });
            summary.exams.push({ project: project.projectName, ...r });
          } catch (e) {
            if (e.cancelled) throw e;
            log('  考试跳过：' + e.message, 'warn');
          }
        }
      }

      const mins = ((Date.now() - startedAt) / 60000).toFixed(1);
      log(UI.cancelled
        ? '已停止：学习 ' + summary.studied + ' 门，失败 ' + summary.failed + ' 门，用时 ' + mins + ' 分钟'
        : '全部完成：学习 ' + summary.studied + ' 门，失败 ' + summary.failed + ' 门，用时 ' + mins + ' 分钟',
        UI.cancelled ? 'warn' : 'ok');
      GM_setValue(STORE.lastRun, { at: new Date().toISOString(), summary });
    } catch (e) {
      if (e.cancelled) log('已停止', 'warn');
      else if (e.needsLogin) log(e.message, 'err');
      else log('出错：' + e.message, 'err');
    } finally {
      setRunning(false);
    }
  }

  // ────────────────────────────────────────────────────────────────────────
  // 8. 课程页（mcwk）内的辅助：页面自身就能完成任务，脚本只补一个状态提示
  // ────────────────────────────────────────────────────────────────────────

  function coursePageHint() {
    if (!isCoursePage()) return;
    // 课程页不需要脚本做任何事：翻页和完课都由主站窗口驱动。
    // 这里只放一个很小的提示，避免用户以为脚本没生效。
    const tip = document.createElement('div');
    tip.style.cssText =
      'position:fixed;left:12px;bottom:12px;z-index:2147483000;background:rgba(0,0,0,.75);' +
      'color:#fff;font:12px/1.5 sans-serif;padding:6px 10px;border-radius:6px;pointer-events:none';
    tip.textContent = '安全教育助手：此页面由脚本自动翻页，请勿关闭';
    document.body.appendChild(tip);
  }

  // ────────────────────────────────────────────────────────────────────────
  // 9. 启动
  // ────────────────────────────────────────────────────────────────────────

  if (isCoursePage()) {
    coursePageHint();
  } else {
    const boot = () => {
      mount();
      GM_registerMenuCommand('打开面板 / 开始', () => {
        mount();
        run();
      });
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', boot, { once: true });
    } else {
      boot();
    }

    // 调试入口
    unsafeWindow.__weban = {
      version: VERSION,
      dispatch,
      getApp,
      getPageUser,
      collectPlan,
      studyCourse,
      takeExam,
      loadAnswerBank,
      lookupAnswer,
      run,
    };
  }
})();

