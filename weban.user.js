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
  // 2. 题库：交给 server.py，本地不再维护匹配逻辑
  //     服务端返回 { hit, indexes: [选项下标], answers: [正确文本], matchedBy }
  //     未配置服务时退化为随机/全选，不影响学习流程。
  // ────────────────────────────────────────────────────────────────────────

  function serverConfig() {
    return {
      endpoint: String(GM_getValue(STORE.captchaEndpoint, '') || '').replace(/\/+$/, ''),
      token: String(GM_getValue(STORE.captchaToken, '') || ''),
    };
  }

  function gmPost(url, body, token) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url,
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: 'Bearer ' + token } : {}),
        },
        data: JSON.stringify(body),
        timeout: 15000,
        onload: (res) => {
          let json = null;
          try { json = JSON.parse(res.responseText); } catch { /* 非 JSON */ }
          if (res.status === 200 || res.status === 404 || res.status === 422) {
            return resolve({ status: res.status, body: json || {} });
          }
          reject(new Error((json && json.error) || 'HTTP ' + res.status));
        },
        onerror: () => reject(new Error('服务连接失败')),
        ontimeout: () => reject(new Error('服务超时')),
      });
    });
  }

  async function askServerAnswer(question, options) {
    const cfg = serverConfig();
    if (!cfg.endpoint) return null;
    try {
      const res = await gmPost(
        cfg.endpoint + '/answer/query',
        { title: question.title || question.content || '', options },
        cfg.token,
      );
      if (!res.body || !res.body.hit) return null;
      return res.body;
    } catch (e) {
      log('题库服务不可用：' + e.message, 'warn');
      return null;
    }
  }

  // 把服务端返回的 indexes 映射成选项 id；没命中时按策略兜底
  function pickOptionIds(question, serverResult, fallbackRandom) {
    const options = question.optionList || [];
    if (!options.length) return [];
    if (serverResult && Array.isArray(serverResult.indexes) && serverResult.indexes.length) {
      return serverResult.indexes.map((i) => options[i]).filter(Boolean).map((o) => o.id);
    }
    if (!fallbackRandom) return [];
    if (Number(question.type) === 2) return options.map((o) => o.id);
    return [options[0].id];
  }

  // ────────────────────────────────────────────────────────────────────────
  // 2.5 动画加速：课程页的「慢」来自 CSS animation
  //     实测 src/online-*/mcwk 的 CSS 里 animation 时长 2.4~6s，且带
  //     animation-delay（单页最多等 4.8s）。翻一页要等动画播完，这是卡顿
  //     的主因。这里注入一份覆盖样式把它们压缩到极短。
  //     —— 只改「播放速度」，不跳过页面逻辑，页面该有的状态切换照常发生。
  // ────────────────────────────────────────────────────────────────────────

  const SPEED_STYLE_ID = 'weban-speedup';

  function installSpeedup(win, speed = 0.05) {
    try {
      const doc = win.document;
      if (!doc || doc.getElementById(SPEED_STYLE_ID)) return;
      const style = doc.createElement('style');
      style.id = SPEED_STYLE_ID;
      // 用 !important 覆盖页面所有 animation/transition
      style.textContent =
        '*, *::before, *::after {' +
        '  animation-duration: ' + speed + 's !important;' +
        '  animation-delay: 0s !important;' +
        '  transition-duration: ' + speed + 's !important;' +
        '  transition-delay: 0s !important;' +
        '}';
      (doc.head || doc.documentElement).appendChild(style);
    } catch { /* 跨域等情况忽略 */ }
  }

  // 加速 setTimeout：item.js 里用 setTimeout(fn, 2600) 等交互反馈，
  // 这是纯等待，压缩它不会影响正确性（页面逻辑按顺序执行）。
  function installTimerBoost(win, scale = 0.05) {
    try {
      const w = win;
      if (!w || w.__webanTimerBoost) return;
      const nativeSetTimeout = w.setTimeout.bind(w);
      w.setTimeout = function (fn, delay, ...args) {
        const d = Number(delay) || 0;
        // 只压缩 100ms 以上的等待，短延时保持原样避免竞态
        const next = d > 100 ? Math.max(20, Math.round(d * scale)) : d;
        return nativeSetTimeout(fn, next, ...args);
      };
      w.__webanTimerBoost = true;
    } catch { /* ignore */ }
  }

  // ────────────────────────────────────────────────────────────────────────
  // 2.6 静音：课程页点击时用 <audio id="click-base"> 播提示音
  //     （item.js: clickbase.play()），视频课也有声音。
  //     默认全部静音：改 muted + volume=0，并拦截 HTMLMediaElement.play()，
  //     防止页面之后重新调起有声播放。
  // ────────────────────────────────────────────────────────────────────────

  function installMute(win) {
    try {
      const w = win;
      if (!w || w.__webanMuteHook) return;
      const proto = w.HTMLMediaElement && w.HTMLMediaElement.prototype;
      if (!proto) return;

      const nativePlay = proto.play;
      proto.play = function () {
        try {
          this.muted = true;
          this.volume = 0;
        } catch { /* 某些元素不允许改属性 */ }
        return nativePlay.apply(this, arguments);
      };

      // 已有的 audio/video 也立刻静音
      const muteAll = () => {
        try {
          for (const el of w.document.querySelectorAll('audio, video')) {
            el.muted = true;
            el.volume = 0;
            if (!el.paused) el.pause();
          }
        } catch { /* ignore */ }
      };
      muteAll();
      // 页面后续动态插入的媒体元素也覆盖
      const mo = new w.MutationObserver(muteAll);
      mo.observe(w.document.documentElement, { childList: true, subtree: true });
      w.__webanMuteHook = true;
      w.__webanMuteObserver = mo;
    } catch { /* 跨域等忽略 */ }
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

  // 驱动一次翻页。返回 true 表示确实有按钮可以点。
  // 关键：起始页等没有按钮的页面，用「下一个页面」里的按钮前进。
  async function clickNext(win) {
    const doc = win.document;
    const pages = [...doc.querySelectorAll('.page-item')];
    const idx = pages.findIndex((p) => p.classList.contains('page-active'));
    if (idx < 0) return false;

    // 当前页自己的按钮优先，否则用下一个页面的
    let target = pages[idx].querySelector('.btn-next');
    if (!target && pages[idx + 1]) target = pages[idx + 1].querySelector('.btn-next');
    if (!target) return false;

    // 完课按钮由上层单独处理，这里不点
    if (target.classList.contains('btn-next-end')) return false;

    target.click();
    return true;
  }

  function currentPageNums(win) {
    try {
      const v = win.pageNums;
      return typeof v === 'number' ? v : null;
    } catch {
      return null;
    }
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

  // 课程页「交互题」自动答题。
  // 依据 src/online-*/mcwk/course-A11078/item.js：
  //   - 每个选项按钮 .p0502/.p0503/... 绑定 p5btn()，点击后记录到 Set
  //   - 提交按钮 .p0511 点击后把选项标记为 over，并 setTimeout 2600ms 后复位
  //   - 集满 5 个不同选项才显示 .btn-base（下一页按钮）
  // 所以自动答题要做的是：把页面上所有可点选项依次点一遍，再点提交。
  // 交互题真实流程（实测 A11078 page-5）：
  //   1. 初始 5 个选项都可见、提交按钮(.p0511)不可见
  //   2. 点一个选项 → 该选项加 p050Nactive、提交按钮出现
  //   3. 点提交 → 选项标记 over，2600ms 后复位
  //   4. 必须依次把所有选项都走完这一步，页面才放出下一页
  // 关键：一次只点一个，不能连点（连点会把页面状态搞乱、卡死）。
  // 交互题真实流程（在 A11078 第 6 页实测确认）：
  //   - 页面元素：img.p0502 ~ img.p0506 五个选项，img.p0511 是提交
  //   - 点选项后：该选项加 p050Nactive 类，提交按钮变可见
  //   - 点提交后：选项短暂变 over 再复位；提交按钮隐藏
  //   - 每点完一个选项都要提交一次，页面才继续放行
  // 判断「还没点过」不能只看类名含 active：必须精确比对该选项自己的
  // active 类（p0502active 只对应 p0502），否则会反复点同一个。
  const INTERACTIVE_IDS = ['p0502', 'p0503', 'p0504', 'p0505', 'p0506'];

  // 判断选项「是否还没点过」。
  // 实测：点过的选项有两个标志——
  //   1) 类名变成 p050Nover（并隐藏）
  //   2) src 从 p050N.png 变成 p050N-1.png
  // 只看类名不够（点了但还没提交时类名仍是 p050N），所以两个都看。
  function optionUntouched(el, base) {
    if (!el) return false;
    const parts = (el.className || '').split(/\s+/);
    if (parts.includes(base + 'over')) return false;
    const src = String(el.getAttribute('src') || '');
    if (/-(\d+)\.(png|jpg|jpeg|gif|svg|webp)$/i.test(src)) return false;
    return true;
  }

  async function autoAnswerInteractive(win, opts = {}) {
    const { maxSteps = 20, waitAfterSubmit = 3200 } = opts;
    const pause = opts.sleep || sleep;
    const isVisible = (node) => {
      if (!node) return false;
      const r = node.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    };

    for (let step = 0; step < maxSteps; step++) {
      let d;
      try { d = win.document; } catch { return false; }

      // 1) 有未提交的选择就先提交，等复位
      const submit = d.querySelector('.p0511');
      if (isVisible(submit)) {
        try { submit.click(); } catch { /* ignore */ }
        await pause(waitAfterSubmit);
        continue;
      }

      // 2) 找一个还没点过的选项（可见 且 未 over 且 src 没有 -N 后缀）
      let target = null;
      for (const base of INTERACTIVE_IDS) {
        const node = d.querySelector('.' + base);
        if (node && isVisible(node) && optionUntouched(node, base)) {
          target = node;
          break;
        }
      }
      if (!target) return true;   // 全都点过了，本页交互结束

      try { target.click(); } catch { return false; }
      await pause(600);
    }
    return true;
  }

  // 判断当前页是不是交互题页（有选项按钮）
  function hasInteractive(win) {
    try {
      return !!win.document.querySelector('.p0502, .p0503, .p0504, .p0505, .p0506');
    } catch {
      return false;
    }
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

      // 注入加速：CSS 动画 + 长 setTimeout
      if (opts.speedup !== false) {
        installSpeedup(win, opts.animSpeed ?? 0.05);
        installTimerBoost(win, opts.timerScale ?? 0.05);
        if (opts.mute !== false) installMute(win);
        log('  已启用动画加速' + (opts.mute !== false ? '，页面已静音' : ''));
      }

      const info = getPageNumber(win);
      log('  ' + name + '：共 ' + (info ? info.total : '?') + ' 页，开始翻页');

      // 翻页主循环。实测结论：
      //   - 内容页在 .page-item 里，按钮就在各自页面内
      //   - 起始页没有按钮，要用下一页的按钮前进
      //   - 交互题页必须先答完才放出下一页
      //   - 最后一页是 page-N，没有按钮；完课按钮藏在最后一个隐藏页里
      let guard = 0;
      let stuck = 0;
      let lastSig = '';
      while (!checkCancel()) {
        if (++guard > 400) throw new Error('翻页次数异常，已停止');

        // 1) 交互题：先答完
        if (hasInteractive(win)) {
          log('  发现交互题，自动作答');
          await autoAnswerInteractive(win);
          await sleep(400);
        }

        // 2) 下一页
        const moved = await clickNext(win);
        if (!moved) {
          // 没有可点的下一页：可能已到最后一页
          if (++stuck >= 2) break;
          await sleep(600);
          continue;
        }
        await sleep(opts.pageInterval ?? 600);

        // 3) 页面完全没变化，连续几次就认为到底
        const after = pageSignature(win);
        if (after === lastSig) {
          if (++stuck >= 3) break;
        } else {
          stuck = 0;
        }
        lastSig = after;
      }

      checkCancel();

      // 完课：btn-next-end 在最后一个隐藏页里，pageNums >= 7 时点击即完课
      const nums = currentPageNums(win);
      log('  ' + name + '：已翻到第 ' + nums + ' 页，提交完课');
      const endBtn = win.document.querySelector('.btn-next-end');
      if (!endBtn) throw new Error('找不到完课按钮');
      endBtn.click();
      await sleep(2500);

      if (!detectFinish(win)) {
        log('  ' + name + '：完课请求已发出，但未确认到成功提示', 'warn');
      }
      log('  ' + name + '：学习完成', 'ok');
      return true;
    } finally {
      closeCourseTab();
    }
  }

  // 页面状态指纹：用于判断「翻页是否真的前进了」
  function pageSignature(win) {
    try {
      const doc = win.document;
      const active = doc.querySelector('.page-active');
      if (!active) return '';
      const cls = active.className || '';
      const num = getPageNumber(win);
      return cls + '|' + (num ? num.current + '/' + num.total : '');
    } catch {
      return '';
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
      const options = (q.optionList || []).map((o) => o.content || '');
      const serverResult = await askServerAnswer(q, options);
      if (serverResult) matched++;
      const ids = pickOptionIds(q, serverResult, randomAnswer);
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

  const UI = { root: null, running: false, cancelled: false, logs: [], progress: '' };

  function settings() {
    const cfg = GM_getValue(STORE.settings, null) || {};
    return {
      pageInterval: Number(cfg.pageInterval ?? 0.8),
      examQuestionTime: Number(cfg.examQuestionTime ?? 2),
      randomAnswer: cfg.randomAnswer !== false,
      speedup: cfg.speedup !== false,
      animSpeed: Number(cfg.animSpeed ?? 0.05),
      mute: cfg.mute !== false,
      stopWhenDone: cfg.stopWhenDone !== false,
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
    UI.logs = UI.logs || [];
    UI.logs.push(line);
    if (UI.logs.length > 2000) UI.logs.shift();
    if (box) {
      const div = document.createElement('div');
      div.className = 'weban-line weban-' + level;
      div.textContent = line;
      box.appendChild(div);
      while (box.childElementCount > 500) box.removeChild(box.firstChild);
      box.scrollTop = box.scrollHeight;
    }
    console.log('[WeBan] ' + msg);
  }

  // 进度条：显示当前项目 / 第几门 / 总数
  function setProgress(text, ratio) {
    const box = el('weban-progress');
    if (!box) return;
    const pct = Math.max(0, Math.min(100, Math.round((ratio || 0) * 100)));
    box.innerHTML = text
      ? '<div class="weban-prog"><div class="weban-prog-text">' + esc(text) + '</div>' +
        '<div class="weban-prog-bar"><i style="width:' + pct + '%"></i></div></div>'
      : '';
    UI.progress = text;
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
      .weban-sec { font-size: 11px; color: #8c959f; margin: 8px 0 4px; font-weight: 600;
        border-top: 1px solid #eaeef2; padding-top: 6px; }
      .weban-sec:first-child { border-top: 0; margin-top: 0; padding-top: 0; }
      .weban-prog { margin-bottom: 8px; }
      .weban-prog-text { font-size: 11.5px; color: #656d76; margin-bottom: 3px;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .weban-prog-bar { height: 5px; background: #eaeef2; border-radius: 99px; overflow: hidden; }
      .weban-prog-bar i { display: block; height: 100%; background: #1f883d; transition: width .3s; }
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
        '<div id="weban-progress"></div>' +
        '<div class="weban-row">' +
          '<button id="btn-start" class="on">开始</button>' +
          '<button id="btn-stop" class="stop" disabled>停止</button>' +
        '</div>' +
        '<div class="weban-row">' +
          '<button id="btn-clear">清空日志</button>' +
          '<button id="btn-copy">复制日志</button>' +
        '</div>' +
        '<div id="weban-settings"></div>' +
        '<div id="weban-log"></div>' +
      '</div>';
    document.body.appendChild(panel);
    const verEl = el('weban-ver');
    if (verEl) verEl.textContent = 'v' + VERSION;

    const on = (sel, fn) => {
      const node = panel.querySelector(sel);
      if (node) node.addEventListener('click', fn);
    };
    on('.weban-head', () => panel.classList.toggle('min'));
    on('#btn-start', run);
    on('#btn-stop', () => {
      UI.cancelled = true;
      log('已请求停止…', 'warn');
    });
    on('#btn-clear', () => {
      const box = el('weban-log');
      if (box) box.innerHTML = '';
      UI.logs = [];
    });
    on('#btn-copy', async () => {
      const text = (UI.logs || []).join('\n');
      try {
        await navigator.clipboard.writeText(text);
        log('日志已复制', 'ok');
      } catch (e) {
        log('复制失败：' + e.message, 'err');
      }
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
    const srv = serverConfig();
    const row = (id, label, type, value, extra) =>
      '<div class="weban-f"><label>' + label + '</label>' +
      '<input id="' + id + '" type="' + type + '"' + (extra || '') + ' value="' + esc(String(value)) + '"></div>';
    const check = (id, label, checked) =>
      '<div class="weban-tip"><label><input id="' + id + '" type="checkbox"' +
      (checked ? ' checked' : '') + '> ' + label + '</label></div>';

    box.innerHTML =
      '<div class="weban-sec">学习</div>' +
      row('set-page', '翻页间隔（秒）', 'number', cfg.pageInterval, ' min="0.2" step="0.1"') +
      check('set-speedup', '动画加速（大幅缩短课程页等待）', cfg.speedup) +
      check('set-mute', '静音（关闭点击音效）', cfg.mute) +
      '<div class="weban-sec">考试</div>' +
      row('set-exam', '答题间隔（秒）', 'number', cfg.examQuestionTime, ' min="0" step="0.5"') +
      check('set-random', '题库外的题随机作答', cfg.randomAnswer) +
      '<div class="weban-sec">辅助服务（题库/验证码）</div>' +
      row('set-endpoint', '服务地址', 'text', srv.endpoint) +
      row('set-token', '服务 Token', 'text', srv.token);

    const save = () => {
      GM_setValue(STORE.settings, {
        pageInterval: Math.max(0.2, Number(box.querySelector('#set-page').value) || 0.8),
        examQuestionTime: Math.max(0, Number(box.querySelector('#set-exam').value) || 0),
        randomAnswer: box.querySelector('#set-random').checked,
        speedup: box.querySelector('#set-speedup').checked,
        mute: box.querySelector('#set-mute').checked,
      });
      GM_setValue(STORE.captchaEndpoint, box.querySelector('#set-endpoint').value.trim());
      GM_setValue(STORE.captchaToken, box.querySelector('#set-token').value.trim());
    };
    for (const id of ['set-page', 'set-exam', 'set-random', 'set-speedup', 'set-mute', 'set-endpoint', 'set-token']) {
      const node = box.querySelector('#' + id);
      if (node) node.addEventListener('change', save);
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
      const cfgSrv = serverConfig();
      log(cfgSrv.endpoint ? '题库服务：' + cfgSrv.endpoint : '未配置题库服务，题库外的题按随机策略作答', cfgSrv.endpoint ? 'ok' : 'warn');

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
            setProgress('学习中 ' + (i + 1) + '/' + pending.length + '：' + course.resourceName,
              (i + 1) / pending.length);
            log('(' + (i + 1) + '/' + pending.length + ') ' + course.resourceName);
            try {
              await studyCourse(course, project, {
                onCancel: () => UI.cancelled,
                pageInterval: cfg.pageInterval * 1000,
                speedup: cfg.speedup,
                animSpeed: cfg.animSpeed,
                mute: cfg.mute,
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
      setProgress('', 0);
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
      askServerAnswer,
      pickOptionIds,
      installSpeedup,
      installTimerBoost,
      installMute,
      autoAnswerInteractive,
      run,
    };
  }
})();

