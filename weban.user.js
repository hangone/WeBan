// ==UserScript==
// @name         WeBan 浏览器版（weban.user.js）
// @namespace    https://github.com/hangone/WeBan
// @version      0.2.0
// @description  浏览器里完成安全教育课程学习与考试：复用页面登录态，自动学课、答题、交卷；不需要 Python 环境
// @author       WeBan
// @match        https://weiban.mycourse.cn/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        GM_unregisterMenuCommand
// @grant        GM_openInTab
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-start
// ==/UserScript==

(function () {
  'use strict';

  // ──────────────────────────────────────────────────────────────────────────
  // 0. 常量：与 api.py 逐项对齐（改接口先改这里的表）
  // ──────────────────────────────────────────────────────────────────────────

  const VERSION = '0.1.0';
  const ORIGIN = 'https://weiban.mycourse.cn';

  // api.py API.MAIN_HEADERS：主站请求头（X-Token 每次请求按登录态动态加）
  const MAIN_HEADERS = {
    Origin: ORIGIN,
    Referer: ORIGIN + '/',
  };

  // 登录请求体固定密钥（api.py WeBanAPI.encrypt，key = wbs512 补零）
  const LOGIN_KEY = 'wbs512';

  // client.py COURSE_CATEGORIES：(chooseType, 名称, 总数键, 完成数键)
  const COURSE_CATEGORIES = [
    [3, '必修课', 'requiredNum', 'requiredFinishedNum'],
    [1, '推送课', 'pushNum', 'pushFinishedNum'],
    [2, '自选课', 'optionalNum', 'optionalFinishedNum'],
  ];

  // client.py PROGRESS_FIELDS / simulate_home_page 的首页初始化列表
  const PROGRESS_FIELDS = [
    ...COURSE_CATEGORIES.flatMap(([, , need, done]) => [need, done]),
    'examNum',
    'examFinishedNum',
  ];

  // 接口表：与 api.py / client.py 一一对应。
  // kind:
  //   post   —— 普通 .do 接口，成功判定 code === '0'
  //   get    —— GET 接口
  //   mcwk   —— 课程域名下的请求（不带 X-Token）
  const ENDPOINTS = {
    // ── 首页 / 账号 ──────────────────────────────────────
    userGuide:      { path: '/pharos/userguide/get.do',              kind: 'post' },
    listCompletion: { path: '/pharos/index/listCompletion.do',       kind: 'post' },
    labIndex:       { path: '/pharos/lab/index.do',                  kind: 'post' },
    studyTask:      { path: '/pharos/index/listStudyTask.do',        kind: 'post' },
    myProject:      { path: '/pharos/index/listMyProject.do',        kind: 'post' },
    showProgress:   { path: '/pharos/project/showProgress.do',       kind: 'post' },
    listValve:      { path: '/pharos/index/listValve.do',            kind: 'post' },
    myInfo:         { path: '/pharos/my/getInfo.do',                 kind: 'post' },
    projectStat:    { path: '/pharos/index/getProjectStat.do',       kind: 'post' },
    carousel:       { path: '/pharos/carousel/list.do',              kind: 'post' },
    simpleConfig:   { path: '/pharos/tenantconfig/getSimpleConfig.do', kind: 'post' },
    noticeIndex:    { path: '/pharos/notice/index.do',               kind: 'post' },
    noticeList:     { path: '/pharos/notice/list.do',                kind: 'post' },
    noticeMust:     { path: '/pharos/notice/listMust.do',            kind: 'post' },
    viewNotice:     { path: '/pharos/notice/viewMust.do',            kind: 'post' },

    // ── 课程 ────────────────────────────────────────────
    listCategory:   { path: '/pharos/usercourse/listCategory.do',    kind: 'post' },
    listCourse:     { path: '/pharos/usercourse/listCourse.do',      kind: 'post' },
    study:          { path: '/pharos/usercourse/study.do',           kind: 'post' },
    courseUrl:      { path: '/pharos/usercourse/getCourseUrl.do',    kind: 'post' },
    courseCheck:    { path: '/pharos/usercourse/check.do',           kind: 'post' },
    courseRating:   { path: '/pharos/resource/getNear.do',           kind: 'post' },

    // ── 考试 ────────────────────────────────────────────
    listPlan:       { path: '/pharos/exam/listPlan.do',              kind: 'post' },
    beforePaper:    { path: '/pharos/exam/beforePaper.do',           kind: 'post' },
    preparePaper:   { path: '/pharos/exam/preparePaper.do',          kind: 'post' },
    startPaper:     { path: '/pharos/exam/startPaper.do',            kind: 'post' },
    recordQuestion: { path: '/pharos/exam/recordQuestion.do',        kind: 'post' },
    submitPaper:    { path: '/pharos/exam/submitPaper.do',           kind: 'post' },
  };

  // 课程页所在的域名（mcwk），完课 / 轨迹 / 题目都在这里
  const MCWK_ORIGIN = 'https://mcwk.mycourse.cn';
  const RESOURCE_ORIGIN = 'https://resource.mycourse.cn';

  const STORE = {
    accounts: 'weban.accounts.v1',
    profile: 'weban.loginProfile.v1',
    last: 'weban.lastSnapshot.v1',
    settings: 'weban.settings.v1',
    captchaEndpoint: 'weban.captchaEndpoint.v1',
    captchaToken: 'weban.captchaToken.v1',
  };

  // 同一个 Origin 内的相对路径 + 浏览器自动带 cookie，不需要任何跨域权限
  const url = (path, params) => {
    const u = new URL(path, ORIGIN);
    if (params) for (const [k, v] of Object.entries(params)) u.searchParams.set(k, v);
    return u.toString();
  };

  // api.py get_timestamp：秒 + '.' + 毫秒，例如 1750000000.123
  const timestamp = () => {
    const n = String(Date.now());
    return n.slice(0, 10) + '.' + n.slice(10, 13);
  };

  // ──────────────────────────────────────────────────────────────────────────
  // 3. 请求层：统一错误分类（api.py handle_response 的浏览器版）
  // ──────────────────────────────────────────────────────────────────────────

  class PermissionError extends Error {}   // 401/403/463/-105：登录态失效
  class AccountBlockedError extends Error {} // 701/10018：账号被限制

  const CODE_OK = (code) => code === '0' || code === 0 || code === '200' || code === 200;

  function classify(httpStatus, body) {
    const code = body && (body.code ?? body.status);
    if (httpStatus === 701 || String(code) === '701' || String(body?.detailCode) === '10018') {
      const msg = body?.message || body?.msg || '账号被限制';
      return new AccountBlockedError(String(msg));
    }
    if ([401, 403, 463].includes(httpStatus) || String(code) === '-105') {
      return new PermissionError(body?.message || body?.msg || '登录已失效');
    }
    return null;
  }

  // 所有主站请求统一走这里。POST 用 form-urlencoded（与页面表单提交一致，
  // 不触发额外的 CORS 预检）；返回 JSON。
  async function request(method, path, data, { allow200 = false } = {}) {
    const head = { ...MAIN_HEADERS, Accept: 'application/json, text/javascript, */*; q=0.01' };
    const token = getSession().token;
    if (token) head['X-Token'] = token;
    if (method === 'POST') head['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8';

    // _post 的公共参数：tenantCode 永远带，userId 登录后带
    const payload = { ...(data || {}) };
    if (session.tenantCode && payload.tenantCode === undefined) payload.tenantCode = session.tenantCode;
    if (session.userId && payload.userId === undefined) payload.userId = session.userId;
    const body = new URLSearchParams();
    for (const [k, v] of Object.entries(payload)) {
      if (v !== undefined && v !== null) body.set(k, String(v));
    }

    const resp = await fetch(url(path, { timestamp: timestamp() }), {
      method,
      headers: head,
      credentials: 'include',
      body: method === 'POST' ? body.toString() : undefined,
    });
    return parseResponse(resp, { allow200 });
  }

  // 课程域名（mcwk）的请求：不带 X-Token，Origin/Referer 指 mcwk
  async function mcwkRequest(path, data = {}, { method = 'POST', origin = MCWK_ORIGIN } = {}) {
    const head = { Accept: '*/*', Origin: origin, Referer: origin + '/' };
    let body;
    if (method === 'POST') {
      head['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8';
      const form = new URLSearchParams();
      for (const [k, v] of Object.entries(data)) {
        if (v !== undefined && v !== null) form.set(k, String(v));
      }
      body = form.toString();
    }
    const resp = await fetch(origin + path, { method, headers: head, credentials: 'include', body });
    return parseResponse(resp);
  }

  async function parseResponse(resp, { allow200 = false } = {}) {
    let json = null;
    try { json = await resp.json(); } catch { json = null; }
    const err = classify(resp.status, json);
    if (err) throw err;
    if (allow200) {
      if (resp.status !== 200) throw new Error('HTTP ' + resp.status);
      return json ?? {};
    }
    if (!json) throw new Error('响应不是 JSON（HTTP ' + resp.status + '）');
    return json;
  }

  // 按接口表发请求
  async function call(name, payload = {}) {
    const def = ENDPOINTS[name];
    if (!def) throw new Error('未登记的接口：' + name);
    if (def.kind === 'post') return request('POST', def.path, payload);
    if (def.kind === 'get') return request('GET', def.path, payload, { allow200: true });
    throw new Error('未知接口类型：' + def.kind);
  }

  // ── mercury 签名接口（课程题目）─────────────────────────
  // api.py _mercury_request：标准化参数 + 按 key 升序拼 secret+keyvalue+secret 做 SHA1。
  const MERCURY_URL = RESOURCE_ORIGIN + '/mercuryprovider/router';
  const MERCURY_KEY = 'vtjikcng';

  async function sha1HexUppercase(text) {
    const buf = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(text));
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('').toUpperCase();
  }

  async function mercury(params) {
    const merged = {
      appKey: '00000001',
      format: 'json',
      v: '1.0',
      timestamp: timestamp(),
      clientId: 'pharos',
      ...params,
    };
    const sortedKeys = Object.keys(merged).sort();
    let signStr = MERCURY_KEY;
    for (const k of sortedKeys) signStr += k + String(merged[k]);
    signStr += MERCURY_KEY;
    merged.sign = await sha1HexUppercase(signStr);

    const resp = await fetch(MERCURY_URL, {
      method: 'POST',
      credentials: 'include',
      headers: {
        ...MAIN_HEADERS,
        Accept: '*/*',
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
      },
      body: new URLSearchParams(merged).toString(),
    });
    return parseResponse(resp);
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 4. 登录态：cookie 由浏览器拥有；token 取页面自己的（不注入、不伪造）
  // ──────────────────────────────────────────────────────────────────────────

  function readStorageToken() {
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k || !/token|user/i.test(k)) continue;
        let v = localStorage.getItem(k);
        try { v = JSON.parse(v); } catch { /* 纯字符串 */ }
        const found = findToken(v, 0);
        if (found) return found;
      }
    } catch { /* 私有模式等 */ }
    return null;
  }

  function findToken(value, depth) {
    if (depth > 3 || value == null) return null;
    if (typeof value === 'string') {
      return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
        ? value
        : null;
    }
    if (typeof value === 'object') {
      for (const [k, v] of Object.entries(value)) {
        if (/^token$/i.test(k) && typeof v === 'string' && v) return v;
        const nested = findToken(v, depth + 1);
        if (nested) return nested;
      }
    }
    return null;
  }

  let session = { token: null, userId: null, tenantCode: null, source: 'none' };
  const getSession = () => session;
  const setSession = (patch) => { session = { ...session, ...patch }; };

  async function refreshSessionFromPage() {
    const token = readStorageToken();
    if (token) setSession({ token, source: 'localStorage' });
    return session;
  }

  // userId / tenantCode 必须来自登录态本身：主站接口要求请求体带 userId，
  // 用接口反查 userId 会变成先有鸡还是先有蛋（401）。
  // 所以除了登录响应，还从页面存储里找 userId/tenantCode。
  function readStorageIdentity() {
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k) continue;
        let v = localStorage.getItem(k);
        try { v = JSON.parse(v); } catch { /* 纯字符串 */ }
        const found = findKeys(v, 0);
        if (found) return found;
      }
    } catch { /* 私有模式等 */ }
    return null;
  }

  function findKeys(value, depth) {
    if (depth > 3 || !value || typeof value !== 'object') return null;
    const get = (name) => {
      const hit = Object.entries(value).find(([k]) => k.toLowerCase() === name);
      return hit && typeof hit[1] === 'string' && hit[1] ? hit[1] : null;
    };
    const userId = get('userid');
    if (userId) return { userId, tenantCode: get('tenantcode') };
    for (const v of Object.values(value)) {
      const nested = findKeys(v, depth + 1);
      if (nested) return nested;
    }
    return null;
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 5.5 业务动作：学习课程、考试（对应 client.py 的核心流程）
  // ──────────────────────────────────────────────────────────────────────────

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // 题库：answer.json 结构为 {"题目文本": ["A","B"]}，键做规范化后比对
  let answerBank = null;

  async function loadAnswerBank() {
    if (answerBank) return answerBank;
    try {
      const res = await fetch('https://cdn.jsdelivr.net/gh/hangone/WeBan@main/answer/answer.json', {
        credentials: 'omit',
      });
      answerBank = res.ok ? await res.json() : {};
    } catch {
      answerBank = {};
    }
    return answerBank;
  }

  const normalizeQuestion = (text) =>
    String(text || '')
      .replace(/\s+/g, '')
      .replace(/[（）()【】\[\]「」""''：:，,。.；;、!！?？]/g, '')
      .toLowerCase();

  // 题库结构：{"题目文本": {"optionList": [{"content": "...", "isCorrect": 1}], "type": 1}}
  // 返回 isCorrect 为 1 的选项内容数组；未命中返回 null。
  function lookupAnswer(question) {
    if (!answerBank) return null;
    const key = normalizeQuestion(question.title || question.content || '');
    if (!answerIndex) {
      answerIndex = new Map();
      for (const [k, v] of Object.entries(answerBank)) {
        const norm = normalizeQuestion(k);
        if (norm) answerIndex.set(norm, v);
      }
    }
    const entry = answerIndex.get(key);
    if (!entry) return null;
    const correct = (entry.optionList || [])
      .filter((o) => Number(o.isCorrect) === 1)
      .map((o) => o.content);
    return correct.length ? correct : null;
  }

  let answerIndex = null;

  // 用答案文本匹配试卷选项，返回选项 id 数组。
  // 不按字母位置猜：题库和试卷的选项顺序可能不同，按内容匹配更稳。
  function matchOptions(question, correctContents, fallbackRandom) {
    const options = question.optionList || [];
    if (!options.length) return [];
    if (correctContents && correctContents.length) {
      const ids = [];
      for (const want of correctContents) {
        const target = normalizeQuestion(want);
        const hit = options.find((o) => normalizeQuestion(o.content) === target)
          || options.find((o) => {
            const optText = normalizeQuestion(o.content);
            return optText && (optText.includes(target) || target.includes(optText));
          });
        if (hit && !ids.includes(hit.id)) ids.push(hit.id);
      }
      if (ids.length) return ids;
    }
    if (!fallbackRandom) return [];
    // 兜底：多选全选，单选选第一个
    if (Number(question.type) === 2) return options.map((o) => o.id);
    return [options[0].id];
  }

  // ── 学习单门课程 ────────────────────────────────────────
  // 流程对齐 client.py：study.do → getCourseUrl.do → 答题 → 等待 → 完课
  async function learnCourse(course, project, opts = {}) {
    const { onCancel, studySeconds = 20, randomAnswer = true } = opts;
    const name = course.resourceName || course.resourceId;
    const checkCancel = () => {
      if (onCancel?.()) throw Object.assign(new Error('已取消'), { cancelled: true });
    };

    // 1) 开始学习
    const studyRes = await call('study', {
      courseId: course.resourceId,
      userProjectId: project.userProjectId,
    });
    if (!ok(studyRes)) {
      throw new Error('study.do 失败：' + (studyRes?.message || studyRes?.code));
    }

    // 2) 取课程 URL（含 userCourseId 等参数）
    const urlRes = await call('courseUrl', {
      courseId: course.resourceId,
      userProjectId: project.userProjectId,
    });
    if (!ok(urlRes) || !urlRes.data) {
      throw new Error('getCourseUrl.do 失败：' + (urlRes?.message || urlRes?.code));
    }
    const courseUrl = String(urlRes.data);
    const query = new URL(courseUrl).searchParams;
    const userCourseId = query.get('userCourseId') || course.userCourseId || '';

    // 3) 课后习题：只有页面加载题目时才查（与 client.py 一致）
    const questions = await mercury({
      service: 'mercury.microlecture.listQuestion',
      id: course.resourceId,
    }).catch(() => null);
    checkCancel();

    const examQuestions = questions?.data?.examQuestionList || [];
    if (examQuestions.length) {
      log('  ' + name + '：课后习题 ' + examQuestions.length + ' 道');
      for (const q of examQuestions) {
        checkCancel();
        const letters = lookupAnswer(q);
        const ids = matchOptions(q, letters, randomAnswer);
        if (!ids.length) continue;
        await mercury({
          service: 'mercury.microlecture.saveExamQuestion',
          courseId: course.resourceId,
          questionId: q.id,
          answers: ids.join(','),
          userId: session.userId,
          tenantCode: session.tenantCode,
          source: 'WEIBAN',
        });
        await sleep(400);
      }
    }

    // 4) 满足最低学习时长
    checkCancel();
    if (studySeconds > 0) {
      log('  ' + name + '：等待学习时长 ' + studySeconds + 's');
      await sleep(studySeconds * 1000);
      checkCancel();
    }

    // 5) 完课（JSONP GET，token 可为空即用 userCourseId）
    if (!userCourseId) throw new Error('缺少 userCourseId，无法完课');
    const finish = await finishCourse(userCourseId, query);
    if (!ok(finish, true)) {
      throw new Error('完课失败：' + (finish?.message || finish?.code));
    }
    log('  ' + name + '：完成', 'ok');
    return true;
  }

  const ok2 = (res, strict) =>
    !!res && (strict ? (res.code === '0' || res.code === 0 || res.code === '1' || res.code === 1) : (res.code === '0' || res.code === 0));

  // 完课：pharos/usercourse/v2/<token>.do，JSONP
  async function finishCourse(userCourseId, query) {
    const token = query.get('csCapt') === 'true' ? null : userCourseId;
    const path = '/pharos/usercourse/v2/' + (token || userCourseId) + '.do';
    const ts = String(Date.now());
    const cb = 'jQuery' + Math.floor(Math.random() * 1e16) + '_' + ts;
    const params = new URLSearchParams({
      userCourseId,
      tenantCode: session.tenantCode || '',
      callback: cb,
      _: String(Number(ts) + 1),
    });
    if (query.get('uniqueNo')) params.set('uniqueNo', query.get('uniqueNo'));
    const resp = await fetch(ORIGIN + path + '?' + params.toString(), {
      credentials: 'include',
      headers: { Accept: '*/*', Referer: MCWK_ORIGIN + '/' },
    });
    let text = await resp.text();
    const start = text.indexOf('(');
    const end = text.lastIndexOf(')');
    if (start >= 0 && end > start) text = text.slice(start + 1, end);
    try {
      return JSON.parse(text);
    } catch {
      throw new Error('完课响应不是合法 JSON');
    }
  }

  // ── 考试 ───────────────────────────────────────────────
  // 流程对齐 client.py：beforePaper → preparePaper → startPaper
  //   → 逐题 recordQuestion → submitPaper
  async function takeExam(plan, project, opts = {}) {
    const { onCancel, questionSeconds = 3, randomAnswer = true } = opts;
    const checkCancel = () => {
      if (onCancel?.()) throw Object.assign(new Error('已取消'), { cancelled: true });
    };

    const planId = plan.id || plan.examPlanId;
    if (!planId) throw new Error('考试计划缺少 id');

    await call('beforePaper', { userExamPlanId: planId });
    await call('preparePaper', { userExamPlanId: planId });
    checkCancel();

    const started = await call('startPaper', { userExamPlanId: planId });
    if (!ok(started)) throw new Error('开始考试失败：' + (started?.message || started?.code));
    const paper = started.data || {};
    const questionList = paper.questionList || [];
    log('  试卷 ' + questionList.length + ' 题，限时 ' + (paper.answerTime || '?') + ' 分钟');

    let matched = 0;
    for (let i = 0; i < questionList.length; i++) {
      checkCancel();
      const q = questionList[i];
      const letters = lookupAnswer(q);
      if (letters) matched++;
      const ids = matchOptions(q, letters, randomAnswer);
      await call('recordQuestion', {
        userExamPlanId: planId,
        questionId: q.id,
        useTime: Math.max(1, Math.round(questionSeconds)),
        examPlanId: plan.examPlanId,
        answerIds: ids.join(','),
      });
      if (questionSeconds > 0) await sleep(questionSeconds * 1000);
    }

    checkCancel();
    const rate = questionList.length ? Math.round((matched / questionList.length) * 100) : 0;
    log('  题库命中 ' + matched + '/' + questionList.length + '（' + rate + '%）');
    const submitted = await call('submitPaper', { userExamPlanId: planId });
    const score = submitted?.data?.score;
    log('  交卷完成，得分 ' + (score ?? '?'), 'ok');
    return { score, matched, total: questionList.length };
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 6. 采集：项目 → 进度 → 分类 → 课程 → 考试计划（作为执行计划的数据源）
  // ──────────────────────────────────────────────────────────────────────────

  const logEl = () => document.getElementById('weban-probe-log');

  function log(msg, level = 'info') {
    const el = logEl();
    const line = document.createElement('div');
    line.className = 'weban-log-line weban-log-' + level;
    const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    line.textContent = '[' + t + '] ' + msg;
    if (el) { el.appendChild(line); el.scrollTop = el.scrollHeight; }
    console.log('[WeBan] ' + msg);
  }

  const ok = (res) => !!res && CODE_OK(res.code ?? res.status);

  // 首页各接口只留摘要：总数/开关/提示，不保留个人身份信息原文
  function summarizeHome(name, res) {
    if (!ok(res)) return { ok: false, code: res?.code, message: res?.message || res?.msg || '' };
    const d = res?.data;
    switch (name) {
      case 'myInfo': {
        const info = d || {};
        return { ok: true, tenantName: info.tenantName || '', batchName: info.batchName || '', orgName: info.orgName || '' };
      }
      case 'listCompletion':
        return { ok: true, modules: (d || []).filter((m) => m.showable === 1).map((m) => m.module) };
      case 'userGuide':
        return { ok: true, hasGuide: !!(d && d.url) };
      case 'noticeIndex':
      case 'noticeList':
        return { ok: true, count: Array.isArray(d) ? d.length : (d ? 1 : 0) };
      case 'listValve':
        return { ok: true, valves: d && typeof d === 'object' ? Object.keys(d) : [] };
      case 'studyTask':
        return { ok: true, count: Array.isArray(d) ? d.length : (d ? 1 : 0) };
      default:
        return { ok: true };
    }
  }

  async function collectSnapshot({ onCancel } = {}) {
    const snapshot = {
      generatedAt: new Date().toISOString(),
      scriptVersion: VERSION,
      account: null,
      home: {},
      projects: [],
      warnings: [],
    };
    const warn = (m) => { snapshot.warnings.push(m); log(m, 'warn'); };
    const checkCancelled = () => {
      if (onCancel?.()) throw Object.assign(new Error('已取消'), { cancelled: true });
    };

    // 6.1 首页初始化（与 client.simulate_home_page 同顺序）
    log('读取首页状态…');
    const homeSteps = [
      ['userGuide', '新手引导'],
      ['listCompletion', '模块完成情况'],
      ['myInfo', '用户信息'],
      ['projectStat', '项目统计'],
      ['noticeIndex', '公告状态'],
      ['noticeList', '公告列表'],
      ['listValve', '功能开关'],
      ['studyTask', '学习任务'],
    ];
    for (const [name, label] of homeSteps) {
      checkCancelled();
      try {
        const res = await call(name);
        snapshot.home[name] = res;
        if (!ok(res)) warn('首页「' + label + '」返回异常：' + (res?.message || res?.code));
      } catch (e) {
        if (e instanceof PermissionError || e instanceof AccountBlockedError) throw e;
        warn('首页「' + label + '」失败：' + e.message);
      }
    }

    // 新手引导未完成时官方会先跳引导页，这里只提示，不代做
    if (snapshot.home.userGuide?.data?.url) {
      warn('该账号有未完成的新手引导，官网会先跳引导页；本脚本不代替完成。');
    }

    // 6.2 项目列表（含实验室课程，与 client._get_project_list 对齐）
    log('读取项目列表…');
    const myProject = await call('myProject', { ended: 2 });
    if (!ok(myProject)) throw new Error('项目列表读取失败：' + (myProject?.message || myProject?.code));
    const projects = [...(myProject.data || [])];
    const modules = (snapshot.home.listCompletion?.data || [])
      .filter((d) => d.showable === 1)
      .map((d) => d.module);
    if (modules.includes('labProject')) {
      try {
        const lab = await call('labIndex');
        const current = lab?.data?.current;
        if (current) projects.push(current);
      } catch (e) {
        warn('实验室课程读取失败：' + e.message);
      }
    }

    // 6.3 每个项目：进度 + 三类课程的分类/课程 + 考试计划
    for (const project of projects) {
      checkCancelled();
      const upid = project.userProjectId;
      const entry = {
        projectName: project.projectName || '未命名项目',
        userProjectId: upid,
        endTime: project.endTime || '',
        studyStateLabel: project.studyStateLabel || '',
        progress: null,
        categories: {},
        examPlans: [],
      };
      snapshot.projects.push(entry);
      if (!upid) { warn('项目「' + entry.projectName + '」缺少 userProjectId，跳过'); continue; }

      log('项目：' + entry.projectName);
      try {
        const progress = await call('showProgress', { userProjectId: upid });
        if (!ok(progress)) throw new Error(progress?.message || 'code=' + progress?.code);
        const data = progress.data || {};
        const missing = PROGRESS_FIELDS.filter((f) => !(f in data));
        if (missing.length) warn('进度响应缺字段：' + missing.join(','));
        entry.progress = data;
        const fmt = (n, d) => (data[d] ?? '-') + '/' + (data[n] ?? '-');
        log('  进度：必修 ' + fmt('requiredNum', 'requiredFinishedNum') +
          '，推送 ' + fmt('pushNum', 'pushFinishedNum') +
          '，自选 ' + fmt('optionalNum', 'optionalFinishedNum') +
          '，考试 ' + fmt('examNum', 'examFinishedNum'), 'ok');
      } catch (e) {
        if (e instanceof PermissionError || e instanceof AccountBlockedError || e.cancelled) throw e;
        warn('进度读取失败：' + e.message);
      }

      for (const [chooseType, label] of COURSE_CATEGORIES) {
        checkCancelled();
        const rows = [];
        entry.categories[label] = rows;
        try {
          const cats = await call('listCategory', { userProjectId: upid, chooseType });
          if (!ok(cats)) throw new Error(cats?.message || 'code=' + cats?.code);
          for (const cat of cats.data || []) {
            checkCancelled();
            const courses = await call('listCourse', {
              userProjectId: upid,
              chooseType,
              categoryCode: cat.categoryCode,
            });
            if (!ok(courses)) throw new Error('分类「' + cat.categoryName + '」' + (courses?.message || '读取失败'));
            for (const c of courses.data || []) {
              rows.push({
                categoryCode: cat.categoryCode,
                categoryName: cat.categoryName || c.categoryName || '',
                resourceId: c.resourceId,
                userCourseId: c.userCourseId || '',
                resourceName: c.resourceName || '',
                finished: Number(c.finished) === 1,
                source: c.source,
              });
            }
          }
          const done = rows.filter((r) => r.finished).length;
          log('  ' + label + '：' + done + '/' + rows.length + ' 已完成', 'ok');
        } catch (e) {
          if (e instanceof PermissionError || e instanceof AccountBlockedError || e.cancelled) throw e;
          warn(label + '读取失败：' + e.message);
        }
      }

      try {
        const plans = await call('listPlan', { userProjectId: upid });
        if (!ok(plans)) throw new Error(plans?.message || 'code=' + plans?.code);
        entry.examPlans = (plans.data || []).map((p) => ({
          examPlanName: p.examPlanName || '',
          answerNum: p.answerNum,
          answerTime: p.answerTime,
          passScore: p.passScore,
          examFinishNum: p.examFinishNum,
          examOddNum: p.examOddNum,
          examScore: p.examScore,
          startTime: p.startTime,
          endTime: p.endTime,
        }));
        const remain = entry.examPlans.reduce(
          (n, p) => n + Math.max(0, (p.examOddNum || 0) - (p.examFinishNum || 0)), 0);
        if (entry.examPlans.length) log('  考试计划 ' + entry.examPlans.length + ' 个，剩余机会 ' + remain, 'ok');
      } catch (e) {
        if (e instanceof PermissionError || e instanceof AccountBlockedError || e.cancelled) throw e;
        warn('考试计划读取失败：' + e.message);
      }
    }

    // 只保留摘要：个人信息（身份证/手机号/考生号等）不写进快照，
    // 快照会被复制、下载、存进 GM 存储，不能带这些。
    snapshot.home = Object.fromEntries(
      Object.entries(snapshot.home).map(([k, v]) => [k, summarizeHome(k, v)]),
    );
    snapshot.account = {
      tenantName: snapshot.home.myInfo?.tenantName || '',
      batchName: snapshot.home.myInfo?.batchName || '',
      orgName: snapshot.home.myInfo?.orgName || '',
      userId: session.userId || '',
      tenantCode: session.tenantCode || '',
      loginSource: session.source,
    };
    return snapshot;
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 6.5 验证码识别服务（可选）：把图片地址交给 captcha_server.py 换坐标/字符
  //     服务端只收图片 URL，不收账号、cookie、ticket；不配置就完全跳过。
  // ──────────────────────────────────────────────────────────────────────────

  function captchaConfig() {
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
        timeout: 20000,
        onload: (res) => {
          let json = null;
          try { json = JSON.parse(res.responseText); } catch { /* 非 JSON */ }
          if (res.status === 200 || res.status === 422) return resolve(json || {});
          const msg = json && json.error ? json.error : 'HTTP ' + res.status;
          reject(new Error('验证码服务：' + msg));
        },
        onerror: () => reject(new Error('验证码服务连接失败')),
        ontimeout: () => reject(new Error('验证码服务超时')),
      });
    });
  }

  // 点选验证码：返回相对主图像素的 3 个坐标；服务不可用/识别不全时抛错或返回 null
  async function solvePoint(bgUrl, ansUrl) {
    const cfg = captchaConfig();
    if (!cfg.endpoint) return null;
    const res = await gmPost(cfg.endpoint + '/captcha/point', { bg: bgUrl, ans: ansUrl }, cfg.token);
    if (!res || !Array.isArray(res.points)) throw new Error('验证码服务返回格式异常');
    if (res.missing > 0) throw new Error('识别不全（缺 ' + res.missing + ' 个）');
    return { points: res.points, width: res.width, height: res.height };
  }

  // 登录验证码：返回 4 位字符
  async function solveText(imageUrl) {
    const cfg = captchaConfig();
    if (!cfg.endpoint) return null;
    const res = await gmPost(cfg.endpoint + '/captcha/text', { image: imageUrl }, cfg.token);
    if (!res || !res.ok || !/^[0-9a-z]{4}$/i.test(res.code || '')) {
      throw new Error('登录验证码识别失败');
    }
    return res.code;
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 7. UI：右下角面板（无交互骨架，纯 DOM）
  // ──────────────────────────────────────────────────────────────────────────

  const UI = {
    root: null,
    account: null,
    running: false,
    cancelled: false,
    config: null,
  };

  function styles() {
    return `
      #weban-probe { position: fixed; right: 18px; bottom: 18px; width: 340px; z-index: 2147483000;
        font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; color: #1f2328;
        background: #fff; border: 1px solid #d0d7de; border-radius: 12px;
        box-shadow: 0 12px 32px rgba(0,0,0,.16); overflow: hidden; }
      #weban-probe.collapsed .weban-body { display: none; }
      .weban-head { display: flex; align-items: center; justify-content: space-between;
        padding: 10px 12px; background: #f6f8fa; cursor: pointer; user-select: none; }
      .weban-head b { font-size: 13px; }
      .weban-head span { color: #656d76; font-size: 12px; }
      .weban-body { padding: 10px 12px 12px; }
      .weban-row { display: flex; gap: 6px; margin-bottom: 8px; }
      .weban-row button { flex: 1; padding: 7px 8px; border-radius: 7px; cursor: pointer;
        border: 1px solid #d0d7de; background: #f6f8fa; font-size: 12.5px; }
      .weban-row button.primary { background: #1f883d; border-color: #1a7f37; color: #fff; }
      .weban-row button.danger { background: #fff; border-color: #cf222e; color: #cf222e; }
      .weban-row button:disabled { opacity: .45; cursor: not-allowed; }
      #weban-probe-log { max-height: 190px; overflow-y: auto; font: 11.5px/1.55 ui-monospace, Menlo, monospace;
        background: #0d1117; color: #c9d1d9; border-radius: 8px; padding: 8px; }
      .weban-log-warn { color: #e3b341; }
      .weban-log-ok { color: #56d364; }
      .weban-log-err { color: #ff7b72; }
      .weban-field { margin-bottom: 7px; }
      .weban-field label { display: block; font-size: 12px; color: #656d76; margin-bottom: 3px; }
      .weban-field input, .weban-field textarea { width: 100%; box-sizing: border-box; padding: 6px 8px;
        border: 1px solid #d0d7de; border-radius: 6px; font-size: 12.5px; }
      .weban-field textarea { min-height: 84px; font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; }
      .weban-tip { font-size: 11.5px; color: #656d76; }
      .weban-tip.is-error { color: #cf222e; }
      .weban-badge { display: inline-block; padding: 1px 6px; border-radius: 99px; font-size: 11px;
        background: #ddf4ff; color: #0969da; }
    `;
  }

  function mountPanel() {
    if (document.getElementById('weban-probe')) return;
    const style = document.createElement('style');
    style.textContent = styles();
    const root = document.createElement('div');
    root.id = 'weban-probe';
    root.innerHTML =
      '<div class="weban-head"><b>WeBan 浏览器版</b><span id="weban-probe-ver">v' + VERSION + '</span></div>' +
      '<div class="weban-body">' +
        '<div id="weban-login"></div>' +
        '<div class="weban-row">' +
          '<button id="weban-btn-run" class="primary">开始</button>' +
          '<button id="weban-btn-cancel" class="danger" disabled>停止</button>' +
          '<button id="weban-btn-copy">复制</button>' +
        '</div>' +
        '<div class="weban-row">' +
          '<button id="weban-btn-json">下载报告</button>' +
          '<button id="weban-btn-clear">清空</button>' +
        '</div>' +
        '<div id="weban-settings"></div>' +
        '<div id="weban-captcha-cfg"></div>' +
        '<div id="weban-probe-log"></div>' +
      '</div>';
    document.body.appendChild(root);
    root.querySelector('.weban-head').addEventListener('click', () => root.classList.toggle('collapsed'));
    UI.root = root;

    root.querySelector('#weban-btn-run').addEventListener('click', () => runProbe());
    root.querySelector('#weban-btn-cancel').addEventListener('click', () => {
      UI.cancelled = true;
      log('已请求停止…', 'warn');
    });
    root.querySelector('#weban-btn-copy').addEventListener('click', async () => {
      const snap = GM_getValue(STORE.last, null);
      if (!snap) return log('还没有快照可复制', 'warn');
      try {
        await navigator.clipboard.writeText(JSON.stringify(snap, null, 2));
        log('快照已复制到剪贴板', 'ok');
      } catch (e) { log('复制失败：' + e.message, 'err'); }
    });
    renderSettings();
    renderCaptchaConfig();
    root.querySelector('#weban-btn-json').addEventListener('click', downloadJson);
    root.querySelector('#weban-btn-clear').addEventListener('click', () => {
      const el = logEl(); if (el) el.innerHTML = '';
    });

    renderAccount();
  }

  function renderSettings() {
    const box = document.getElementById('weban-settings');
    if (!box) return;
    const cfg = settings();
    box.innerHTML =
      '<div class="weban-field"><label>每门课最短学习时长（秒）</label>' +
      '<input id="weban-set-study" type="number" min="0" value="' + cfg.studyTime + '" /></div>' +
      '<div class="weban-field"><label>每题答题间隔（秒）</label>' +
      '<input id="weban-set-exam" type="number" min="0" value="' + cfg.examQuestionTime + '" /></div>' +
      '<div class="weban-tip" style="margin-bottom:6px">' +
      '<label><input id="weban-set-random" type="checkbox"' + (cfg.randomAnswer ? ' checked' : '') + ' /> ' +
      '题库外的题随机作答（关闭则跳过）</label></div>' +
      '<div class="weban-row"><button id="weban-btn-settings-save">保存设置</button></div>';
    box.querySelector('#weban-btn-settings-save').addEventListener('click', () => {
      GM_setValue(STORE.settings, {
        studyTime: Math.max(0, Number(box.querySelector('#weban-set-study').value) || 0),
        examQuestionTime: Math.max(0, Number(box.querySelector('#weban-set-exam').value) || 0),
        randomAnswer: box.querySelector('#weban-set-random').checked,
      });
      log('设置已保存', 'ok');
    });
  }

  function renderCaptchaConfig() {
    const box = document.getElementById('weban-captcha-cfg');
    if (!box) return;
    const cfg = captchaConfig();
    box.innerHTML =
      '<div class="weban-field"><label>验证码识别服务（可选，如 http://127.0.0.1:8765）</label>' +
      '<input id="weban-captcha-endpoint" placeholder="http://127.0.0.1:8765" value="' + esc(cfg.endpoint) + '" />' +
      '</div>' +
      '<div class="weban-field"><label>服务 Token（服务端设了才需要）</label>' +
      '<input id="weban-captcha-token" placeholder="留空表示服务端未开启鉴权" value="' + esc(cfg.token) + '" />' +
      '</div>' +
      '<div class="weban-row"><button id="weban-btn-captcha-save">保存识别服务设置</button></div>';
    box.querySelector('#weban-btn-captcha-save').addEventListener('click', () => {
      const endpoint = box.querySelector('#weban-captcha-endpoint').value.trim();
      const token = box.querySelector('#weban-captcha-token').value.trim();
      if (endpoint && !/^https?:\/\/[^\s]+$/.test(endpoint)) {
        log('识别服务地址格式不对，应以 http:// 或 https:// 开头', 'err');
        return;
      }
      GM_setValue(STORE.captchaEndpoint, endpoint);
      GM_setValue(STORE.captchaToken, token);
      log(endpoint ? '识别服务已保存：' + endpoint : '已清空识别服务（恢复手动输入）', 'ok');
    });
  }

  function renderAccount() {
    const box = document.getElementById('weban-login');
    if (!box) return;
    const token = session.token;
    box.innerHTML = token
      ? '<div class="weban-tip" style="margin-bottom:8px">' +
          '<span class="weban-badge">已登录</span> 使用页面现有登录态' +
        '</div>'
      : '<div class="weban-tip is-error" style="margin-bottom:8px">' +
          '页面未登录：请先在官网完成登录，回到首页后点「开始」' +
        '</div>';
  }

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function downloadJson() {
    const snap = GM_getValue(STORE.last, null);
    if (!snap) return log('还没有快照可下载', 'warn');
    const blob = new Blob([JSON.stringify(snap, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'weban-snapshot-' + new Date().toISOString().replace(/[:.]/g, '-') + '.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    log('已下载 ' + a.download, 'ok');
  }

  function setRunning(running) {
    UI.running = running;
    const run = document.getElementById('weban-btn-run');
    const cancel = document.getElementById('weban-btn-cancel');
    if (run) run.disabled = running;
    if (cancel) cancel.disabled = !running;
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 8. 登录态：本脚本不负责登录，只用页面自己的登录结果
  //    - 用户在官网正常登录（或页面本来就登录着），脚本读取页面存储里的
  //      token / userId / tenantCode 用于主站接口
  //    - 没有登录态就提示用户先登录，脚本不提交账号密码
  // ──────────────────────────────────────────────────────────────────────────

  async function ensureLoggedIn() {
    await refreshSessionFromPage();
    const ident = readStorageIdentity();
    if (ident) {
      setSession({ userId: ident.userId, tenantCode: ident.tenantCode || session.tenantCode });
    }
    if (session.token && session.userId) {
      log('已读取页面登录态（' + session.source + '，userId…' + session.userId.slice(-6) + '）', 'ok');
      return;
    }
    const missing = [];
    if (!session.token) missing.push('token');
    if (!session.userId) missing.push('userId');
    throw Object.assign(
      new Error(
        '页面未检测到登录态（缺 ' + missing.join(' / ') + '）。' +
        '请先在官网完成登录，回到首页后再点「读取」。',
      ),
      { needsLogin: true },
    );
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 9. 主流程：采集 → 逐门课学习 → 逐场考试
  // ──────────────────────────────────────────────────────────────────────────

  function settings() {
    const cfg = GM_getValue(STORE.settings, null) || {};
    return {
      studyTime: Number(cfg.studyTime ?? 20),
      examQuestionTime: Number(cfg.examQuestionTime ?? 3),
      randomAnswer: cfg.randomAnswer !== false,
    };
  }

  async function runProbe() {
    if (UI.running) return;
    UI.cancelled = false;
    setRunning(true);
    const cfg = settings();
    log('开始：采集 → 学习 → 考试（可在任何时候点「停止」）');
    try {
      await ensureLoggedIn();
      const snapshot = await collectSnapshot({ onCancel: () => UI.cancelled });
      GM_setValue(STORE.last, snapshot);

      await loadAnswerBank();

      // 逐项目执行：先学完所有未完成课程，再考未完成的考试
      for (const project of snapshot.projects) {
        if (UI.cancelled) break;
        log('===== 项目：' + project.projectName + ' =====');

        for (const [label, rows] of Object.entries(project.categories)) {
          const pending = rows.filter((r) => !r.finished);
          if (!pending.length) continue;
          log('  ' + label + '：待学 ' + pending.length + ' 门');
          for (let i = 0; i < pending.length; i++) {
            if (UI.cancelled) break;
            const course = pending[i];
            log('  [' + (i + 1) + '/' + pending.length + '] ' + course.resourceName);
            try {
              await learnCourse(course, project, {
                onCancel: () => UI.cancelled,
                studySeconds: cfg.studyTime,
                randomAnswer: cfg.randomAnswer,
              });
            } catch (e) {
              if (e.cancelled || e instanceof PermissionError || e instanceof AccountBlockedError) throw e;
              log('  跳过：' + e.message, 'warn');
            }
          }
        }

        // 考试：剩余次数 > 0 才考
        for (const plan of project.examPlans) {
          if (UI.cancelled) break;
          const remain = (plan.examOddNum || 0) - (plan.examFinishNum || 0);
          if (remain <= 0) continue;
          log('  考试「' + plan.examPlanName + '」剩余 ' + remain + ' 次');
          try {
            await takeExam(plan, project, {
              onCancel: () => UI.cancelled,
              questionSeconds: cfg.examQuestionTime,
              randomAnswer: cfg.randomAnswer,
            });
          } catch (e) {
            if (e.cancelled || e instanceof PermissionError || e instanceof AccountBlockedError) throw e;
            log('  考试跳过：' + e.message, 'warn');
          }
        }
      }

      if (UI.cancelled) {
        log('已停止（已完成的部分不会回滚）', 'warn');
      } else {
        log('全部完成', 'ok');
      }
    } catch (e) {
      if (e.cancelled) {
        log('已停止', 'warn');
      } else if (e instanceof AccountBlockedError) {
        log('账号被限制：' + e.message + '，已停止', 'err');
      } else if (e instanceof PermissionError) {
        log('登录态失效（' + e.message + '），请重新登录官网', 'err');
        setSession({ token: null, source: 'none' });
        renderAccount();
      } else if (e.needsLogin) {
        log(e.message, 'err');
      } else {
        log('出错：' + e.message, 'err');
      }
    } finally {
      setRunning(false);
    }
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 10. 菜单命令与启动
  // ──────────────────────────────────────────────────────────────────────────

  GM_registerMenuCommand('开始（学习+考试）', () => { mountPanel(); runProbe(); });
  GM_registerMenuCommand('清空已存报告', () => {
    GM_setValue(STORE.last, null);
    log('已清空本地快照', 'ok');
  });

  function boot() {
    // 不在登录页自动执行，避免和官网登录流程抢操作
    if (/\/(login|pharos\/login)/i.test(location.pathname)) {
      console.info('[WeBan] 登录页不自动运行；登录后回到首页再点「读取」');
    }
    mountPanel();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }

  // 调试入口：控制台里可以用 __weban.* 手动调
  window.__weban = {
    version: VERSION,
    call,
    mercury,
    collectSnapshot,
    learnCourse,
    takeExam,
    loadAnswerBank,
    lookupAnswer,
    ensureLoggedIn,
    refreshSessionFromPage,
    readStorageIdentity,
    getSession,
    setSession,
    runProbe,
  };
})();
