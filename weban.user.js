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
// @connect      127.0.0.1
// @connect      localhost
// @connect      cdn.jsdelivr.net
// @connect      raw.githubusercontent.com
// @connect      gh-proxy.com
// @connect      ghfast.top
// @connect      api.deepseek.com
// @updateURL    https://cdn.jsdelivr.net/gh/hangone/WeBan@main/weban.user.js
// @downloadURL  https://cdn.jsdelivr.net/gh/hangone/WeBan@main/weban.user.js
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';


  const VERSION = (typeof GM_info === 'object' && GM_info?.script?.version) || '1.0.0';

  const STORE = {
    settings: 'weban.settings.v1',
    ai: 'weban.ai.v1',
    lastRun: 'weban.lastRun.v1',
    captchaEndpoint: 'weban.captchaEndpoint.v1',
    captchaToken: 'weban.captchaToken.v1',
  };

  const isCoursePage = () => location.host === 'mcwk.mycourse.cn';
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));


  let appVm = null;

  function getApp() {
    if (appVm && appVm.$store) return appVm;
    const root = document.querySelector('#app');
    for (const el of [root, document.body, ...document.querySelectorAll('#app *')]) {
      const vm = el && el.__vue__;
      if (vm) {
        appVm = vm.$root && vm.$root.$store ? vm.$root : vm;
        if (appVm.$store) return appVm;
      }
    }
    return null;
  }

  async function dispatch(type, payload) {
    const app = getApp();
    if (!app) throw new Error('未找到页面应用实例，请刷新页面后重试');
    return app.$store.dispatch(type, payload);
  }

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

  let bankIndex = null;
  let bankLoading = null;

  function normText(t) {
    return String(t || '')
      .replace(/[\s（）()【】\[\]「」『』《》〈〉｛｝{}＜＞<>：:，,。.；;、!！?？\-—－_~～·…"'“”‘’]+/g, '')
      .toLowerCase();
  }

  async function loadBank() {
    if (bankIndex) return bankIndex;
    if (bankLoading) return bankLoading;
    bankLoading = (async () => {
      for (const url of ANSWER_URLS) {
        try {
          const data = await gmGetJson(url);
          const idx = new Map();
          for (const [title, entry] of Object.entries(data)) {
            const k = normText(title);
            if (k && entry && typeof entry === 'object') idx.set(k, entry);
          }
          if (idx.size) {
            bankIndex = idx;
            log('题库已加载：' + idx.size + ' 题', 'ok');
            return idx;
          }
        } catch (e) {
          log('题库源不可用（' + url.split('/')[2] + '）：' + e.message, 'warn');
        }
      }
      bankIndex = new Map();
      return bankIndex;
    })();
    return bankLoading;
  }

  function gmGetJson(url) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url,
        timeout: 30000,
        onload: (res) => {
          if (res.status !== 200) return reject(new Error('HTTP ' + res.status));
          try { resolve(JSON.parse(res.responseText)); } catch (e) { reject(e); }
        },
        onerror: () => reject(new Error('网络错误')),
        ontimeout: () => reject(new Error('超时')),
      });
    });
  }

  async function askServerBatch(questions) {
    const cfg = serverConfig();
    if (!cfg.endpoint || !questions.length) return null;
    try {
      const map = new Map();
      // 服务端单次上限 200 题，超过就分块
      for (let i = 0; i < questions.length; i += 200) {
        const chunk = questions.slice(i, i + 200);
        const res = await gmPost(
          cfg.endpoint + '/answer/match',
          { items: chunk.map((q) => ({ id: q.id, title: q.title || q.content || '', optionList: q.optionList || [] })) },
          cfg.token,
        );
        const body = res && res.body;
        if (!body || !Array.isArray(body.results)) continue;
        for (const r of body.results) {
          if (r.hit && Array.isArray(r.indexes) && r.indexes.length) map.set(r.id, r);
        }
      }
      return map.size ? map : null;
    } catch (e) {
      log('  题库服务不可用：' + e.message, 'warn');
      return null;
    }
  }

  async function askBank(question, options) {
    const idx = await loadBank();
    if (!idx.size) return null;
    const entry = idx.get(normText(question.title || question.content));
    if (!entry) return null;
    const correct = (entry.optionList || [])
      .filter((o) => Number(o.isCorrect) === 1)
      .map((o) => String(o.content || ''));
    if (!correct.length) return null;
    const normOpts = options.map(normText);
    const indexes = [];
    for (const want of correct) {
      const target = normText(want);
      if (!target) continue;
      for (let i = 0; i < normOpts.length; i++) {
        const opt = normOpts[i];
        if (!opt) continue;
        if (opt === target || (target.length >= 4 && (opt.includes(target) || target.includes(opt)))) {
          if (!indexes.includes(i)) indexes.push(i);
          break;
        }
      }
    }
    return indexes.length ? { hit: true, indexes, matchedBy: 'bank' } : null;
  }

  async function askAI(question, options) {
    const cfg = settings();
    if (!cfg.aiEnable || !cfg.aiApiKey) return null;
    const prompt = '题目：' + (question.title || question.content || '') +
      '\n选项：' + options.map((o, i) => String.fromCharCode(65 + i) + '. ' + o).join('\n') +
      '\n只回复正确选项字母，多选用逗号分隔。';
    try {
      const res = await gmPostJson(
        cfg.aiBaseUrl.replace(/\/+$/, '') + '/chat/completions',
        { model: cfg.aiModel, messages: [{ role: 'user', content: prompt }], temperature: 0 },
        { Authorization: 'Bearer ' + cfg.aiApiKey },
      );
      const text = res?.choices?.[0]?.message?.content || '';
      const idx = [...text.toUpperCase().matchAll(/\b([A-Z])\b/g)]
        .map((m) => m[1].charCodeAt(0) - 65)
        .filter((i) => i >= 0 && i < options.length);
      return idx.length ? { hit: true, indexes: idx, matchedBy: 'ai' } : null;
    } catch (e) {
      log('  AI 搜题失败：' + e.message, 'warn');
      return null;
    }
  }

  function gmPostJson(url, body, headers) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url,
        headers: { 'Content-Type': 'application/json', ...(headers || {}) },
        data: JSON.stringify(body),
        timeout: 60000,
        onload: (res) => {
          let body = null;
          try { body = JSON.parse(res.responseText); } catch (e) { body = null; }
          if (res.status >= 200 && res.status < 300 && body) { resolve(body); return; }
          const msg = (body && body.error && body.error.message) || ('HTTP ' + res.status);
          reject(new Error(msg));
        },
        onerror: () => reject(new Error('网络错误')),
        ontimeout: () => reject(new Error('超时')),
      });
    });
  }

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


  const SPEED_STYLE_ID = 'weban-speedup';

  function installSpeedup(win, speed = 0.05) {
    try {
      const doc = win.document;
      if (!doc || doc.getElementById(SPEED_STYLE_ID)) return;
      const style = doc.createElement('style');
      style.id = SPEED_STYLE_ID;
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

  function installTimerBoost(win, scale = 0.05) {
    try {
      const w = win;
      if (!w || w.__webanTimerBoost) return;
      const nativeSetTimeout = w.setTimeout.bind(w);
      w.setTimeout = function (fn, delay, ...args) {
        const d = Number(delay) || 0;
        const next = d > 100 ? Math.max(20, Math.round(d * scale)) : d;
        return nativeSetTimeout(fn, next, ...args);
      };
      w.__webanTimerBoost = true;
    } catch { /* ignore */ }
  }


  function installDialogGuard(win, sink) {
    try {
      const w = win;
      if (!w || w.__webanDialogGuard) return;
      const nativeAlert = w.alert;
      w.alert = function (msg) {
        try { sink.push(String(msg)); } catch { /* ignore */ }
        return undefined;
      };
      w.confirm = function (msg) {
        try { sink.push('confirm: ' + String(msg)); } catch { /* ignore */ }
        return true;
      };
      w.__webanDialogGuard = true;
      w.__webanNativeAlert = nativeAlert;
    } catch { /* ignore */ }
  }


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
      const mo = new w.MutationObserver(muteAll);
      mo.observe(w.document.documentElement, { childList: true, subtree: true });
      w.__webanMuteHook = true;
      w.__webanMuteObserver = mo;
    } catch { /* 跨域等忽略 */ }
  }


  const COURSE_PAGE_TIMEOUT = 30000;
  let courseTab = null;

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

  async function waitCourseReady(win, timeout = COURSE_PAGE_TIMEOUT) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      try {
        const doc = win.document;
        if (doc && doc.querySelector('.btn-next, .btn-start, .page-start-btn')) return true;
      } catch { /* 跨域或未就绪 */ }
      await sleep(500);
    }
    throw new Error('课程页加载超时');
  }


  // 前进控件的语义分类：一次定义，选择器 / 兜底正则 / 分支页判断共用
  const ANSWER_URLS = [
    'https://cdn.jsdelivr.net/gh/hangone/WeBan@main/answer/answer.json',
    'https://gh-proxy.com/https://raw.githubusercontent.com/hangone/WeBan/main/answer/answer.json',
    'https://ghfast.top/https://raw.githubusercontent.com/hangone/WeBan/main/answer/answer.json',
    'https://raw.githubusercontent.com/hangone/WeBan/main/answer/answer.json',
  ];

  const AI_DEFAULT = {
    baseUrl: 'https://api.deepseek.com/v1',
    model: 'deepseek-chat',
  };

  const FORWARD_PATTERNS = {
    start: ['btn-start', 'page-start-btn', 'pri-start-btn', 'pub-test-btn'],
    continue: ['btn-ce', 'btn-at', 'btn-ti', 'question-continue', 'p14btn', 'p15btn', 'p16btn'],
    next: ['btn-next', 'page-next-btn', 'answer-btn-next', '_next', 'next10'],
    test: ['test-btn'],
    success: ['success-button'],
    finish: ['btn-next-end', 'page-end-button'],
  };

  const QUIZ_SELECTORS = {
    aq: '.aq-item-label',
    quest: '.ques-list-item',
    radio: '.page-radio',
    aqSubmit: '[class*="btn-aq-"]',
    questSubmit: '[class*="btn-ti"]',
    interactiveSubmit: '.p0511',
  };

  const FORWARD_RE = new RegExp(
    Object.values(FORWARD_PATTERNS).flat().join('|'),
  );

  const BACK_RE = /prev|back-list|icon-back/;

  function forwardSelectors() {
    const inPage = (t) => '.page-active [class*="' + t + '"]';
    return [
      ...FORWARD_PATTERNS.start.map(inPage),
      ...FORWARD_PATTERNS.continue.map(inPage),
      ...FORWARD_PATTERNS.test.map(inPage),
      ...FORWARD_PATTERNS.success.map(inPage),
      ...FORWARD_PATTERNS.next.map(inPage),
      '.page-active [class*="-btn"]:not([class*="prev"])',
      '.btn-next-prev .btn-next',
    ];
  }

  const FORWARD_SELECTORS = forwardSelectors();

  // 可见性统一判断：用 computedStyle，不用 getBoundingClientRect
  // （入场动画期间尺寸为 0，但元素可点）
  function visibleIn(win, n) {
    if (!n) return false;
    try {
      const cs = win.getComputedStyle(n);
      return cs.display !== 'none' && cs.visibility !== 'hidden' && Number(cs.opacity) !== 0;
    } catch {
      return false;
    }
  }

  function collectForwardButtons(win) {
    const doc = win.document;
    const skip = (n) => n.classList.contains('btn-next-end')
      || n.classList.contains('btn-prev')
      || n.classList.contains('btn-next-prev');

    const out = [];
    const seen = new Set();
    const push = (n, sel) => {
      if (!n || seen.has(n) || skip(n) || !visibleIn(win, n)) return;
      seen.add(n);
      out.push({ el: n, sel });
    };

    const pages = [...doc.querySelectorAll('.page-item')];
    const activeIdx = pages.findIndex((p) => p.classList.contains('page-active'));
    const scope = [];
    if (activeIdx >= 0) {
      scope.push(pages[activeIdx]);
      if (pages[activeIdx + 1]) scope.push(pages[activeIdx + 1]);
    }

    const navNext = doc.querySelector('.btn-next-prev .btn-next');
    if (navNext) push(navNext, 'global-nav');

    for (const sel of FORWARD_SELECTORS) {
      let nodes;
      try { nodes = [...doc.querySelectorAll(sel)]; } catch { continue; }
      for (const n of nodes) {
        const inNav = n.closest('.btn-next-prev');
        if (!inNav && scope.length && /btn|next|answer/i.test(sel)) {
          if (!scope.some((p) => p.contains(n))) continue;
        }
        push(n, sel);
      }
    }
    for (const n of doc.querySelectorAll('a, button, img, div, span')) {
      const cls = String(n.className || '');
      if (!/next|btn-start|start-btn|btn-ce|btn-at\b|btn-ti|success-button/.test(cls)) continue;
      if (/prev|back-list|icon-back/.test(cls)) continue;
      push(n, '(fallback)');
    }
    return out;
  }

  function findForwardButton(win) {
    const list = collectForwardButtons(win);
    return list.length ? list[0] : null;
  }

  function isBranchPage(win) {
    try {
      const d = win.document;
      const act = d.querySelector('.page-item.page-active');
      if (!act) return false;
      const visible = (n) => {
        const cs = win.getComputedStyle(n);
        return cs.display !== 'none' && cs.visibility !== 'hidden';
      };
      const hasForward = [...act.querySelectorAll('*')]
        .some((n) => visible(n) && FORWARD_RE.test(String(n.className)) && !BACK_RE.test(String(n.className)));
      const hasBack = [...act.querySelectorAll('*')]
        .some((n) => visible(n) && /prev/.test(String(n.className)));
      return !hasForward && hasBack;
    } catch {
      return false;
    }
  }

  function reachedEnd(win) {
    try {
      const doc = win.document;
      if (doc.querySelector('.page-end.page-active')) return true;
      const endPage = doc.querySelector('.page-end');
      if (endPage) {
        const disp = win.getComputedStyle(endPage).display;
        if (disp !== 'none' && endPage.getAttribute('aria-hidden') !== 'true') return true;
      }
      return false;
    } catch {
      return false;
    }
  }

  async function clickForward(win) {
    const btn = findForwardButton(win);
    if (!btn) return { clicked: false, found: false };
    try {
      btn.click();
      return { clicked: true, found: true, cls: btn.className };
    } catch {
      return { clicked: false, found: true };
    }
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
    try {
      const el = win.document.querySelector('.page-reciprocal');
      const text = el ? el.textContent : '';
      const m = String(text).match(/(\d+)\s*\/\s*(\d+)/);
      if (m) return { current: Number(m[1]), total: Number(m[2]) };
    } catch { /* ignore */ }
    return null;
  }

  const INTERACTIVE_IDS = [2, 3, 4, 5, 6].map((i) => 'p050' + i);

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

    for (let step = 0; step < maxSteps; step++) {
      let d;
      try { d = win.document; } catch { return false; }

      const submit = d.querySelector(QUIZ_SELECTORS.interactiveSubmit);
      if (visibleIn(win, submit)) {
        try { submit.click(); } catch { /* ignore */ }
        await pause(waitAfterSubmit);
        continue;
      }

      let target = null;
      for (const base of INTERACTIVE_IDS) {
        const node = d.querySelector('.' + base);
        if (node && visibleIn(win, node) && optionUntouched(node, base)) {
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

  function hasVideo(win) {
    try {
      const v = win.document.querySelector('video');
      if (!v) return false;
      return v.duration > 0 || !v.paused || v.readyState > 0;
    } catch {
      return false;
    }
  }

  async function handleVideo(win, opts = {}) {
    const {
      videoMode = 'duration',
      studyTime = 20,
      videoRate = 2,
      onCancel,
      maxWaitMin = 120,
    } = opts;

    const deadline = Date.now() + maxWaitMin * 60000;
    let applied = false;

    const kick = () => {
      let video;
      try { video = win.document.querySelector('video'); } catch { return null; }
      if (!video) return null;
      try {
        video.muted = true;
        video.volume = 0;
        if (!applied && videoRate > 1 && video.playbackRate !== videoRate) {
          video.playbackRate = videoRate;
          log('  视频倍速设为 ' + videoRate + 'x');
          applied = true;
        }
        if (video.paused) {
          const pr = video.play();
          if (pr && pr.catch) pr.catch(() => { /* 需手势时忽略 */ });
        }
      } catch { /* ignore */ }
      return video;
    };

    if (videoMode === 'wait') {
      log('  视频模式：等待播放结束');
      while (Date.now() < deadline) {
        if (onCancel && onCancel()) return false;
        const video = kick();
        if (!video) return false;
        if (video.ended) return true;
        const dur = video.duration || 0;
        if (dur > 0 && (video.currentTime || 0) >= dur - 0.5) return true;
        if (reachedEnd(win)) return true;
        await sleep(1000);
      }
      return false;
    }

    const need = studyTime * 1000;
    log('  视频模式：播放 ' + studyTime + ' 秒后直接提交完课');
    const startedAt = Date.now();
    while (Date.now() - startedAt < need) {
      if (onCancel && onCancel()) return false;
      kick();
      await sleep(Math.min(1000, need - (Date.now() - startedAt)));
    }

    const ok = callFinishFn(win);
    if (ok) {
      log('  已调用 finishWxCourse() 提交完课', 'ok');
      await sleep(2000);
      return true;
    }
    log('  页面没有 finishWxCourse，退回等待播放结束', 'warn');
    while (Date.now() < deadline) {
      if (onCancel && onCancel()) return false;
      const video = kick();
      if (!video) return false;
      if (video.ended) return true;
      if (reachedEnd(win)) return true;
      await sleep(1000);
    }
    return false;
  }

  function collectPageScripts(win) {
    const texts = [];
    try {
      for (const sc of win.document.querySelectorAll('script')) {
        if (sc.src) texts.push({ src: sc.src, inline: false });
        else if (sc.textContent) texts.push({ text: sc.textContent, inline: true });
      }
    } catch { /* ignore */ }
    return texts;
  }

  function parseAnswerTable(source) {
    const table = {};
    const marker = 'slFn(';
    let from = 0;
    for (;;) {
      const at = source.indexOf(marker, from);
      if (at < 0) break;
      from = at + marker.length;

      let depth = 0;
      let i = at + marker.length - 1;
      let end = -1;
      for (; i < source.length; i++) {
        const ch = source[i];
        if (ch === '(') depth++;
        else if (ch === ')') {
          depth--;
          if (depth === 0) { end = i; break; }
        }
      }
      if (end < 0) break;
      const argStr = source.slice(at + marker.length, end);

      const args = [];
      let buf = '';
      let quote = null;
      let bracket = 0;
      for (const ch of argStr) {
        if (quote) {
          buf += ch;
          if (ch === quote) quote = null;
          continue;
        }
        if (ch === '"' || ch === "'") { quote = ch; buf += ch; continue; }
        if (ch === '[' || ch === '(' || ch === '{') { bracket++; buf += ch; continue; }
        if (ch === ']' || ch === ')' || ch === '}') { bracket--; buf += ch; continue; }
        if (ch === ',' && bracket === 0) { args.push(buf.trim()); buf = ''; continue; }
        buf += ch;
      }
      if (buf.trim()) args.push(buf.trim());

      if (args.length < 5) continue;

      const nameMatch = /name\s*=\s*['"]([^'"]+)['"]/.exec(args[1]);
      if (!nameMatch) continue;

      const listRaw = args[4];
      const answers = [...listRaw.matchAll(/['"]([^'"]+)['"]/g)].map((x) => x[1]);
      if (!answers.length) continue;

      table[nameMatch[1].trim()] = {
        answers,
        atPage: args[2].replace(/['"]/g, '').trim(),
        afPage: args[3].replace(/['"]/g, '').trim(),
      };
    }
    return table;
  }

  let cachedAnswerTable = null;

  async function getAnswerTable(win) {
    if (cachedAnswerTable) return cachedAnswerTable;
    const table = {};
    for (const sc of collectPageScripts(win)) {
      let text = '';
      if (sc.inline) {
        text = sc.text;
      } else if (sc.src) {
        try {
          const res = await win.fetch(sc.src, { credentials: 'include' });
          if (res.ok) text = await res.text();
        } catch { /* 忽略单个脚本 */ }
      }
      Object.assign(table, parseAnswerTable(text));
    }
    cachedAnswerTable = table;
    return table;
  }

  function hasQuiz(win) {
    try {
      const d = win.document;
      const labels = [...d.querySelectorAll(QUIZ_SELECTORS.aq)];
      return labels.some((n) => visibleIn(win, n));
    } catch {
      return false;
    }
  }

  async function autoAnswerQuiz(win, opts = {}) {
    const { onCancel, maxRounds = 4 } = opts;
    const table = await getAnswerTable(win);

    for (let round = 0; round < maxRounds; round++) {
      if (onCancel && onCancel()) return false;
      let d;
      try { d = win.document; } catch { return false; }

      if (![...d.querySelectorAll(QUIZ_SELECTORS.aq)].some((n) => visibleIn(win, n))) return true;

      const submit = [...d.querySelectorAll('[class*="btn-aq"]')].find((n) => visibleIn(win, n));
      if (!submit) return true;

      const labels = [...d.querySelectorAll(QUIZ_SELECTORS.aq)];
      const boxes = [...d.querySelectorAll('input[type=checkbox], input[type=radio]')];

      let groupName = null;
      for (const l of labels) {
        if (!visibleIn(win, l)) continue;
        const b = boxes.find((x) => l.contains(x));
        if (b) { groupName = b.name; break; }
      }
      if (!groupName) return true;

      const mine = boxes.filter((b) => b.name === groupName);
      for (const b of mine) {
        if (!b.checked) continue;
        try {
          b.checked = false;
          b.dispatchEvent(new win.Event('change', { bubbles: true }));
        } catch { /* ignore */ }
      }
      await sleep(120);

      const entry = table[groupName];
      if (entry && entry.answers.length) {
        for (const letter of entry.answers) {
          const box = mine.find((b) => String(b.value).toUpperCase() === letter.toUpperCase());
          if (!box) continue;
          const label = labels.find((l) => l.contains(box));
          try {
            if (label) label.click(); else box.click();
            await sleep(150);
          } catch { /* ignore */ }
        }
        log('  选择题 ' + groupName + '：按页面答案 ' + entry.answers.join(',') + ' 勾选');
      } else {
        for (const box of mine) {
          const label = labels.find((l) => l.contains(box));
          try {
            if (label) label.click(); else box.click();
            await sleep(120);
          } catch { /* ignore */ }
        }
        log('  选择题 ' + groupName + '：页面无答案表，全选尝试', 'warn');
      }

      const btn = [...d.querySelectorAll('[class*="btn-aq"]')].find((n) => visibleIn(win, n));
      if (!btn) return false;
      try { btn.click(); await sleep(1000); } catch { return false; }

      const atOk = entry ? d.querySelector(entry.atPage) : null;
      const afHit = entry ? d.querySelector(entry.afPage) : null;
      const atActive = atOk && atOk.classList.contains('page-active');
      const afActive = afHit && afHit.classList.contains('page-active');

      if (atActive) return true;          // 答对
      if (afActive) {
        const back = d.querySelector('.' + FORWARD_PATTERNS.continue[1]);
        if (back) { try { back.click(); } catch { /* ignore */ } await sleep(700); }
        continue;
      }
      await sleep(600);
    }
    return true;
  }

  async function clickAllCollectibles(win, opts = {}) {
    const { onCancel, limit = 15 } = opts;
    const d = win.document;
    const page = d.querySelector('.page-item.page-active');
    if (!page) return 0;

    const nodes = [...page.querySelectorAll('*')].filter((n) => {
      if (!visibleIn(win, n)) return false;
      const cls = String(n.className || '');
      if (/btn-|prev|next|back-list|icon-back/i.test(cls)) return false;
      return /\bp\d{4}\b/.test(cls);
    });

    let clicked = 0;
    for (const n of nodes.slice(0, limit)) {
      if (onCancel && onCancel()) break;
      try {
        n.click();
        clicked++;
        await sleep(250);
      } catch { /* ignore */ }

      const now = d.querySelector('.page-item.page-active');
      if (now && now !== page) {
        const back = [...now.querySelectorAll('[class*="prev"]')]
          .find((b) => {
            try {
              const cs = win.getComputedStyle(b);
              return cs.display !== 'none' && cs.visibility !== 'hidden';
            } catch { return false; }
          });
        if (back) { try { back.click(); await sleep(400); } catch { /* ignore */ } }
      }
    }
    return clicked;
  }

  function onAnswerFeedbackPage(win) {
    try {
      const act = win.document.querySelector('.page-item.page-active');
      const cls = act ? String(act.className) : '';
      return /page-(at|af)\d/.test(cls);
    } catch {
      return false;
    }
  }

  function hasQuestList(win) {
    try {
      const d = win.document;
      const active = d.querySelector('.page-item.page-active');
      const cls = active ? String(active.className) : '';
      if (/page-(at|af)\d/.test(cls)) return false;
      const box = active || d;
      return [...box.querySelectorAll(QUIZ_SELECTORS.quest)].some((n) => {
        try {
          const cs = win.getComputedStyle(n);
          return cs.display !== 'none' && cs.visibility !== 'hidden';
        } catch { return false; }
      });
    } catch {
      return false;
    }
  }

  function parseQuestAnswers(source) {
    const map = {};
    const re = /\$\("\.btn-ti-(\d+)"\)\.click\(function\(\)\s*\{[\s\S]*?if\s*\(\s*\w+\s*==\s*(\d+)\s*\)/g;
    let m;
    while ((m = re.exec(source))) {
      map[m[1]] = Number(m[2]);
    }
    return map;
  }

  let cachedQuestAnswers = {};
  async function getQuestAnswers(win) {
    if (Object.keys(cachedQuestAnswers).length) return cachedQuestAnswers;
    const merged = {};
    for (const sc of collectPageScripts(win)) {
      let text = '';
      if (sc.inline) text = sc.text;
      else if (sc.src) {
        try {
          const res = await win.fetch(sc.src, { credentials: 'include' });
          if (res.ok) text = await res.text();
        } catch { /* ignore */ }
      }
      Object.assign(merged, parseQuestAnswers(text));
    }
    cachedQuestAnswers = merged;
    return merged;
  }

  async function answerQuestList(win, opts = {}) {
    const { onCancel } = opts;
    const d = win.document;

    const active = d.querySelector('.page-item.page-active');
    if (!active) return false;
    const qsMatch = /page-qs(\d+)/.exec(String(active.className));
    if (!qsMatch) return false;

    const items = [...active.querySelectorAll(QUIZ_SELECTORS.quest)].filter((n) => visibleIn(win, n));
    if (!items.length) return false;

    const btn = [...active.querySelectorAll(QUIZ_SELECTORS.questSubmit)].find((n) => visibleIn(win, n));
    if (!btn) return false;

    const noMatch = /btn-ti-(\d+)/.exec(String(btn.className));
    const no = noMatch ? noMatch[1] : String(Number(qsMatch[1]) + 1).padStart(2, '0');

    const answers = await getQuestAnswers(win);
    const idx = answers[no] !== undefined
      ? Math.min(answers[no], items.length - 1)
      : 0;

    if (onCancel && onCancel()) return false;

    try {
      items[idx].click();
      await sleep(250);
      const again = [...active.querySelectorAll(QUIZ_SELECTORS.questSubmit)].find((n) => visibleIn(win, n));
      if (again) again.click();
      await sleep(900);
    } catch { return false; }

    const activeCls = (d.querySelector('.page-item.page-active') || {}).className || '';
    if (/page-at/.test(activeCls)) {
      log('  列表题 ' + no + '：选第 ' + (idx + 1) + ' 项答对', 'ok');
    } else if (/page-af/.test(activeCls)) {
      log('  列表题 ' + no + '：第 ' + (idx + 1) + ' 项答错', 'warn');
    }
    return true;
  }

  function hasCustomRadio(win) {
    try {
      const d = win.document;
      return [...d.querySelectorAll(QUIZ_SELECTORS.radio)].some((n) => {
        const r = n.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
    } catch {
      return false;
    }
  }

  async function answerCustomRadio(win, opts = {}) {
    const { onCancel } = opts;
    const d = win.document;

    const radios = [...d.querySelectorAll(QUIZ_SELECTORS.radio)].filter((n) => visibleIn(win, n));
    if (!radios.length) return false;

    const first = radios.find((n) => /radio1\b|-radio1$/.test(n.className)) || radios[0];
    try { first.click(); await sleep(250); } catch { return false; }

    const page = first.closest('.page-item');
    const pageCls = page ? page.className : '';
    const m = /\bpage-(\d+)\b/.exec(pageCls);
    let submit = m ? d.querySelector('.page-' + m[1] + '-btn') : null;
    if (!submit) submit = [...d.querySelectorAll('[class*="-btn"]')].find((n) => visibleIn(win, n));
    if (!submit) return false;

    try { submit.click(); await sleep(900); } catch { return false; }
    log('  单选题：选择第一个选项并提交');
    return true;
  }

  // ── 兜底完课 ────────────────────────────────────────────
  function callFinishFn(win) {
    try {
      if (typeof win.finishWxCourse === 'function') {
        win.finishWxCourse();
        return true;
      }
    } catch { /* ignore */ }
    return false;
  }

  function hasInteractive(win) {
    try {
      return INTERACTIVE_IDS.some((id) => win.document.querySelector('.' + id));
    } catch {
      return false;
    }
  }

  async function studyCourse(course, project, opts = {}) {
    const { onCancel, pageInterval = 1500 } = opts;
    const name = course.resourceName || course.resourceId;
    const checkCancel = () => {
      if (onCancel?.()) throw Object.assign(new Error('已取消'), { cancelled: true });
    };

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

      const dialogs = [];
      installDialogGuard(win, dialogs);

      if (opts.speedup !== false) {
        installSpeedup(win, opts.animSpeed ?? 0.05);
        installTimerBoost(win, opts.timerScale ?? 0.05);
        if (opts.mute !== false) installMute(win);
        log('  已启用动画加速' + (opts.mute !== false ? '，页面已静音' : ''));
      }

      const info = getPageNumber(win);
      log('  ' + name + '：共 ' + (info ? info.total : '?') + ' 页，开始翻页');

      const studyStartedAt = Date.now();

      // ── 学习主状态机 ──────────────────────────────────────────────
      let guard = 0;
      let idle = 0;
      let lastSig = '';
      let finished = false;

      for (;;) {
        if (checkCancel()) break;
        if (++guard > 600) throw new Error('页面推进次数异常，已停止');

        if (hasVideo(win)) {
          finished = await handleVideo(win, opts);
          break;
        }

        if (hasInteractive(win)) {
          log('  发现交互题，自动作答');
          await autoAnswerInteractive(win);
          await sleep(400);
          continue;
        }
        const feedbackPage = onAnswerFeedbackPage(win);
        if (!feedbackPage && hasQuiz(win)) {
          log('  发现选择题，自动作答');
          await autoAnswerQuiz(win, opts);
          await sleep(400);
          continue;
        }
        if (hasCustomRadio(win)) {
          await answerCustomRadio(win, opts);
          await sleep(400);
          continue;
        }
        if (!feedbackPage && hasQuestList(win)) {
          log('  发现列表型选择题，自动作答');
          await answerQuestList(win, opts);
          await sleep(400);
          continue;
        }

        if (reachedEnd(win)) {
          log('  已到结束页');
          break;
        }

        if (isBranchPage(win)) {
          const back = [...win.document.querySelectorAll('.page-item.page-active [class*="prev"]')]
            .find((b) => {
              try {
                const cs = win.getComputedStyle(b);
                return cs.display !== 'none' && cs.visibility !== 'hidden';
              } catch { return false; }
            });
          if (back) {
            log('  分支页，返回主链');
            try { back.click(); } catch { /* ignore */ }
            await sleep(600);
            continue;
          }
        }

        const before = pageSignature(win);
        const candidates = collectForwardButtons(win);
        let advanced = false;
        for (const cand of candidates) {
          if (checkCancel()) break;
          try { cand.el.click(); } catch { continue; }
          await sleep(400);
          if (pageSignature(win) !== before || reachedEnd(win)) {
            advanced = true;
            debugLog('前进成功：' + String(cand.sel || ''));
            break;
          }
        }
        if (advanced) {
          await sleep(opts.pageInterval ?? 400);
          idle = 0;
          lastSig = pageSignature(win);
          continue;
        }

        if (idle === 0) {
          const n = await clickAllCollectibles(win, opts);
          if (n > 0) {
            log('  本页需要点击 ' + n + ' 处元素，已全部点击');
            await sleep(800);
            continue;
          }
        }
        if (idle === 0) log('  未找到可点元素，等待页面放出继续按钮…');
        await sleep(opts.hiddenNavWait ?? 5000);
        if (++idle >= 4) break;
        continue;

      }

      checkCancel();

      // ── 收尾完课 ────────────────────────────────────────────────
      const minStudy = Number(opts.studyTime ?? 20);
      if (minStudy > 0) {
        const need = (minStudy + Math.floor(Math.random() * 5)) * 1000;
        const elapsed = Date.now() - studyStartedAt;
        const remain = need - elapsed;
        if (remain > 0) {
          log('  补足学习时长：还需 ' + Math.ceil(remain / 1000) + ' 秒');
          const until = Date.now() + remain;
          while (Date.now() < until) {
            if (checkCancel()) break;
            await sleep(Math.min(1000, until - Date.now()));
          }
        } else {
          log('  学习时长已满足（翻页用了 ' + Math.round(elapsed / 1000) + ' 秒）');
        }
      }

      const nums = currentPageNums(win);
      log('  ' + name + '：页面推进到第 ' + (nums ?? '?') + ' 页，尝试完课');

      if (!finished) {
        const endBtn = win.document.querySelector(
          FORWARD_PATTERNS.finish.map((t) => '.' + t).join(', ') + ', .page-success-button',
        );
        if (endBtn) {
          try { endBtn.click(); } catch { /* ignore */ }
          await sleep(2500);
        }
      }

      if (!detectFinish(win)) {
        const called = callFinishFn(win);
        if (called) {
          log('  已直接调用 finishWxCourse()', 'warn');
          await sleep(2500);
        }
      }

      if (dialogs.length) {
        const known = dialogs.find((m) => /完成失败|已学过|已学完/.test(m));
        if (known) {
          log('  ' + name + '：页面提示「' + known + '」，可能已完成或需人工处理', 'warn');
        } else {
          log('  ' + name + '：页面弹窗：' + dialogs.slice(0, 3).join(' / '), 'warn');
        }
      }

      if (!detectFinish(win)) {
        log('  ' + name + '：未完课成功（未确认到结束状态）', 'warn');
        return false;
      }
      log('  ' + name + '：学习完成', 'ok');
      return true;
    } finally {
      closeCourseTab();
    }
  }

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

    const nextDelayMs = typeof opts.examDelayMs === 'function'
      ? opts.examDelayMs
      : () => (opts.examDelayMs != null ? opts.examDelayMs : 3000);

    // startPaper 已返回全部题目，先批量查一遍，再逐题提交
    const answers = new Map();
    const serverMap = await askServerBatch(questionList);
    for (const q of questionList) {
      const hit = serverMap && serverMap.get(q.id);
      if (hit) { answers.set(q.id, hit); continue; }
      const options = (q.optionList || []).map((o) => o.content || '');
      const local = await askBank(q, options) || await askAI(q, options);
      if (local) answers.set(q.id, local);
    }
    const matched = answers.size;
    log('  预查完成：' + matched + '/' + questionList.length + ' 题有答案');

    for (let i = 0; i < questionList.length; i++) {
      checkCancel();
      const q = questionList[i];
      const delayMs = nextDelayMs();
      const serverResult = answers.get(q.id) || null;
      const ids = pickOptionIds(q, serverResult, randomAnswer);
      await dispatch('recordQuestion', {
        userExamPlanId: planId,
        questionId: q.id,
        useTime: Math.max(1, Math.round(delayMs / 1000)),
        examPlanId: plan.examPlanId,
        answerIds: ids.join(','),
      });
      if (i % 5 === 0) log('  答题 ' + (i + 1) + '/' + questionList.length);
      if (delayMs > 0) await sleep(delayMs);
    }

    checkCancel();
    const rate = Math.round((matched / questionList.length) * 100);
    log('  题库命中 ' + matched + '/' + questionList.length + '（' + rate + '%）');
    // exam_submit_match_rate：匹配率不足且不随机作答时放弃交卷
    const minRate = Number(opts.minMatchRate ?? 90);
    if (!randomAnswer && rate < minRate) {
      log('  匹配率 ' + rate + '% 低于设定 ' + minRate + '%，放弃交卷', 'warn');
      return { score: null, matched, total: questionList.length, skipped: true };
    }
    const submitted = await dispatch('submitPaper', { userExamPlanId: planId });
    const score = submitted && submitted.score;
    log('  交卷完成，得分 ' + (score ?? '?'), 'ok');
    return { score, matched, total: questionList.length };
  }


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



  const UI = { root: null, running: false, cancelled: false, logs: [], progress: '' };

  function settings() {
    const cfg = GM_getValue(STORE.settings, null) || {};
    const ai = GM_getValue(STORE.ai, null) || {};
    const num = (v, def, min) => {
      const n = Number(v);
      return Number.isFinite(n) && n >= min ? n : def;
    };
    return {
      // 学习
      studyMode: ['false', 'true', 'force'].includes(cfg.studyMode) ? cfg.studyMode : 'true',
      studyTime: num(cfg.studyTime, 20, 0),
      pageInterval: num(cfg.pageInterval, 0.8, 0.2),
      speedup: cfg.speedup !== false,
      animSpeed: Math.min(1, Math.max(0.01, num(cfg.animSpeed, 0.05, 0.01))),
      mute: cfg.mute !== false,
      jupiterFallback: cfg.jupiterFallback === true,
      // 视频
      videoRate: Math.min(4, Math.max(0, num(cfg.videoRate, 2, 0))),
      videoMode: cfg.videoMode === 'wait' ? 'wait' : 'duration',
      // 考试
      examMode: ['false', 'true', 'perfect', 'force'].includes(cfg.examMode) ? cfg.examMode : 'perfect',
      examQuestionTime: cfg.examQuestionTime ?? '3,3',
      examSubmitMatchRate: num(cfg.examSubmitMatchRate, 90, 0),
      randomAnswer: cfg.randomAnswer !== false,
      // AI 搜题
      aiEnable: ai.enable === true,
      aiBaseUrl: String(ai.baseUrl || AI_DEFAULT.baseUrl),
      aiApiKey: String(ai.apiKey || ''),
      aiModel: String(ai.model || AI_DEFAULT.model),
      // 调试
      debug: cfg.debug === true,
    };
  }

  // exam_question_time 支持 "3,3" 形式（基础,随机上限），返回毫秒
  function examDelayMs(cfg) {
    const raw = String(cfg.examQuestionTime ?? '3,3');
    const parts = raw.split(',').map((x) => Number(x.trim()));
    const base = Number.isFinite(parts[0]) && parts[0] >= 0 ? parts[0] : 3;
    const extra = Number.isFinite(parts[1]) && parts[1] >= 0 ? parts[1] : 0;
    return (base + Math.floor(Math.random() * (extra + 1))) * 1000;
  }

  function captchaConfig() {
    return {
      endpoint: String(GM_getValue(STORE.captchaEndpoint, '') || '').replace(/\/+$/, ''),
      token: String(GM_getValue(STORE.captchaToken, '') || ''),
    };
  }

  const el = (id) => document.getElementById(id);

  function debugLog(msg) {
    if (!settings().debug) return;
    log('[debug] ' + msg, 'info');
  }

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
      .weban-f input, .weban-f select { width: 100%; box-sizing: border-box; padding: 5px 7px;
        border: 1px solid #d0d7de; border-radius: 6px; font-size: 12px; background: #fff; }
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
      '<input id="' + id + '" type="' + type + '"' + (extra || '') +
      ' value="' + esc(String(value)) + '"></div>';
    const check = (id, label, checked) =>
      '<div class="weban-tip"><label><input id="' + id + '" type="checkbox"' +
      (checked ? ' checked' : '') + '> ' + label + '</label></div>';
    const select = (id, label, options, value) =>
      '<div class="weban-f"><label>' + label + '</label><select id="' + id + '">' +
      options.map((o) => '<option value="' + o[0] + '"' +
        (o[0] === value ? ' selected' : '') + '>' + o[1] + '</option>').join('') +
      '</select></div>';

    box.innerHTML =
      '<div class="weban-sec">学习</div>' +
      select('set-study-mode', '学习模式', [
        ['false', '关闭'], ['true', '正常'], ['force', '强制重学'],
      ], cfg.studyMode) +
      row('set-study', '最少停留时长（秒）', 'number', cfg.studyTime, ' min="0" step="1"') +
      row('set-page', '翻页间隔（秒）', 'number', cfg.pageInterval, ' min="0.2" step="0.1"') +
      check('set-speedup', '动画加速', cfg.speedup) +
      row('set-anim', '动画时长（秒）', 'number', cfg.animSpeed, ' min="0.01" max="1" step="0.01"') +
      check('set-mute', '静音', cfg.mute) +
      check('set-jupiter', '补发 jupiter 轨迹（课程未加载 apicenext 时）', cfg.jupiterFallback) +

      '<div class="weban-sec">视频课</div>' +
      row('set-vrate', '播放倍速（0=不等视频时长）', 'number', cfg.videoRate, ' min="0" max="4" step="0.5"') +
      check('set-vmode', '等视频播完再完课（默认等够时长就提交）', cfg.videoMode === 'wait') +

      '<div class="weban-sec">考试</div>' +
      select('set-exam-mode', '考试模式', [
        ['false', '关闭'], ['true', '正常'], ['perfect', '追求满分'], ['force', '强制重考'],
      ], cfg.examMode) +
      row('set-exam-time', '每题等待（基础,随机）', 'text', cfg.examQuestionTime) +
      row('set-match-rate', '最低匹配率（%）', 'number', cfg.examSubmitMatchRate, ' min="0" max="100"') +
      check('set-random', '题库外随机作答', cfg.randomAnswer) +

      '<div class="weban-sec">AI 搜题</div>' +
      check('set-ai-enable', '题库未命中时用 AI 作答', cfg.aiEnable) +
      row('set-ai-key', 'DeepSeek API Key', 'password', cfg.aiApiKey) +
      row('set-ai-url', 'API 地址', 'text', cfg.aiBaseUrl) +
      row('set-ai-model', '模型', 'text', cfg.aiModel) +

      '<div class="weban-sec">辅助服务（验证码）</div>' +
      row('set-endpoint', '服务地址', 'text', srv.endpoint) +
      row('set-token', '服务 Token', 'text', srv.token) +

      '<div class="weban-sec">调试</div>' +
      check('set-debug', '输出调试日志', cfg.debug);

    const val = (id) => {
      const n = box.querySelector('#' + id);
      return n ? n.value : '';
    };
    const on = (id) => {
      const n = box.querySelector('#' + id);
      return n ? n.checked : false;
    };
    const num = (id, def, min) => {
      const v = Number(val(id));
      return Number.isFinite(v) && v >= min ? v : def;
    };

    const save = () => {
      GM_setValue(STORE.settings, {
        studyMode: val('set-study-mode') || 'true',
        studyTime: num('set-study', 20, 0),
        pageInterval: num('set-page', 0.8, 0.2),
        speedup: on('set-speedup'),
        animSpeed: Math.min(1, Math.max(0.01, num('set-anim', 0.05, 0.01))),
        mute: on('set-mute'),
        jupiterFallback: on('set-jupiter'),
        videoRate: Math.min(4, Math.max(0, num('set-vrate', 2, 0))),
        videoMode: on('set-vmode') ? 'wait' : 'duration',
        examMode: val('set-exam-mode') || 'perfect',
        examQuestionTime: val('set-exam-time') || '3,3',
        examSubmitMatchRate: Math.min(100, Math.max(0, num('set-match-rate', 90, 0))),
        randomAnswer: on('set-random'),
        debug: on('set-debug'),
      });
      GM_setValue(STORE.ai, {
        enable: on('set-ai-enable'),
        apiKey: val('set-ai-key').trim(),
        baseUrl: val('set-ai-url').trim() || AI_DEFAULT.baseUrl,
        model: val('set-ai-model').trim() || AI_DEFAULT.model,
      });
      GM_setValue(STORE.captchaEndpoint, val('set-endpoint').trim());
      GM_setValue(STORE.captchaToken, val('set-token').trim());
      log('设置已保存', 'ok');
    };
    const ids = ['set-study-mode','set-study','set-page','set-speedup','set-anim','set-mute',
      'set-jupiter','set-vrate','set-vmode','set-exam-mode','set-exam-time','set-match-rate',
      'set-random','set-ai-enable','set-ai-key','set-ai-url','set-ai-model',
      'set-endpoint','set-token','set-debug'];
    for (const id of ids) {
      const node = box.querySelector('#' + id);
      if (node) node.addEventListener('change', save);
    }
  }

  function setRunning(running) {
    UI.running = running;
    if (el('btn-start')) el('btn-start').disabled = running;
    if (el('btn-stop')) el('btn-stop').disabled = !running;
  }



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

      const doStudy = cfg.studyMode !== 'false';
      const forceStudy = cfg.studyMode === 'force';
      const doExam = cfg.examMode !== 'false';
      const forceExam = cfg.examMode === 'force';
      if (!doStudy) log('学习模式：关闭（跳过所有课程）', 'warn');
      if (!doExam) log('考试模式：关闭（跳过所有考试）', 'warn');
      if (forceStudy) log('学习模式：强制（已完成课程也重学）', 'warn');
      if (forceExam) log('考试模式：强制（已及格也重考）', 'warn');

      for (const project of projects) {
        if (UI.cancelled) break;
        const categories = doStudy ? Object.entries(project.categories) : [];
        for (const [label, rows] of categories) {
          const pending = forceStudy ? rows : rows.filter((r) => !r.finished);
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
                videoRate: cfg.videoRate,
                videoMode: cfg.videoMode,
                studyTime: cfg.studyTime,
              });
              summary.studied++;
            } catch (e) {
              if (e.cancelled) throw e;
              summary.failed++;
              log('  跳过：' + e.message, 'warn');
            }
          }
        }

        if (!doExam) continue;
        for (const plan of project.examPlans) {
          if (UI.cancelled) break;
          if (plan.examOddNum - plan.examFinishNum <= 0) continue;
          // perfect 模式：已满分则跳过；force 模式：已及格也重考
          if (!forceExam && plan.examMode !== 'force') {
            const passScore = Number(plan.passScore ?? 80);
            const got = Number(plan.examScore ?? 0);
            if (cfg.examMode === 'perfect' && got >= 100) continue;
            if (cfg.examMode === 'true' && got >= passScore) continue;
          }
          log('【' + project.projectName + '】考试：' + plan.examPlanName);
          try {
            const r = await takeExam(plan, project, {
              onCancel: () => UI.cancelled,
              examDelayMs: () => examDelayMs(cfg),
              minMatchRate: cfg.examSubmitMatchRate,
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


  function coursePageHint() {
    if (!isCoursePage()) return;
    const tip = document.createElement('div');
    tip.style.cssText =
      'position:fixed;left:12px;bottom:12px;z-index:2147483000;background:rgba(0,0,0,.75);' +
      'color:#fff;font:12px/1.5 sans-serif;padding:6px 10px;border-radius:6px;pointer-events:none';
    tip.textContent = '安全教育助手：此页面由脚本自动翻页，请勿关闭';
    document.body.appendChild(tip);
  }


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

  }
})();

