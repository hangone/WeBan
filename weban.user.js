// ==UserScript==
// @name         WeBan 只读探针（weban.user.js）
// @namespace    https://github.com/hangone/WeBan
// @version      0.1.0
// @description  用 config.toml 里的账号登录后，只读获取项目/进度/课程/考试信息并导出 JSON；只发 GET 和只读 POST，不写任何学习或考试数据
// @author       WeBan
// @match        https://weiban.mycourse.cn/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        GM_unregisterMenuCommand
// @grant        GM_openInTab
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

  // 只读接口白名单：POST 但不写业务数据。
  // allow-200 只允许 HTTP 200 的纯读接口；其余按业务 code 判定：
  //   .do 类接口成功为 code === '0'，mercury 类接口用 success/code 200。
  // 写接口（study.do / check.do / v2 finish / jupiter next / saveExamQuestion
  // / startPaper / submitPaper …）一律不在此表内，脚本里没有调用它们的路径。
  const ENDPOINTS = {
    userGuide:      { path: '/pharos/userguide/get.do',              kind: 'post' },
    listCompletion: { path: '/pharos/index/listCompletion.do',       kind: 'post' },
    labIndex:       { path: '/pharos/lab/index.do',                  kind: 'post' },
    studyTask:      { path: '/pharos/index/listStudyTask.do',        kind: 'post' },
    myProject:      { path: '/pharos/index/listMyProject.do',        kind: 'post' },
    showProgress:   { path: '/pharos/project/showProgress.do',       kind: 'post' },
    listValve:      { path: '/pharos/index/listValve.do',            kind: 'post' },
    listCategory:   { path: '/pharos/usercourse/listCategory.do',    kind: 'post' },
    listCourse:     { path: '/pharos/usercourse/listCourse.do',      kind: 'post' },
    listPlan:       { path: '/pharos/exam/listPlan.do',              kind: 'post' },
    noticeIndex:    { path: '/pharos/notice/index.do',               kind: 'post' },
    noticeList:     { path: '/pharos/notice/list.do',                kind: 'post' },
    myInfo:         { path: '/pharos/my/getInfo.do',                 kind: 'post' },
    projectStat:    { path: '/pharos/index/getProjectStat.do',       kind: 'post' },
    carousel:       { path: '/pharos/carousel/list.do',              kind: 'post' },
    simpleConfig:   { path: '/pharos/tenantconfig/getSimpleConfig.do', kind: 'post' },
    tenantList:     { path: '/pharos/login/getTenantListWithLetter.do', kind: 'get-200' },
  };

  const STORE = {
    accounts: 'weban.accounts.v1',
    profile: 'weban.loginProfile.v1',
    last: 'weban.lastSnapshot.v1',
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
  // 1. TOML 子集解析：只为读 config.toml 的 [[account]] / [settings]
  //    （支持字符串/数字/布尔、单行数组、# 注释、单双引号，够用为止）
  // ──────────────────────────────────────────────────────────────────────────

  function parseTomlScalar(raw) {
    const s = raw.trim();
    if (s === 'true') return true;
    if (s === 'false') return false;
    if (/^[+-]?\d+(\.\d+)?$/.test(s)) return Number(s);
    if (s.startsWith('"') || s.startsWith("'")) {
      const q = s[0];
      const end = s.indexOf(q, 1);
      return s.slice(1, end < 0 ? s.length : end);
    }
    if (s.startsWith('[')) {
      const inner = s.slice(1, s.lastIndexOf(']'));
      const items = [];
      let buf = '';
      let quote = null;
      for (const ch of inner) {
        if (quote) {
          buf += ch;
          if (ch === quote) quote = null;
        } else if (ch === '"' || ch === "'") {
          quote = ch;
          buf += ch;
        } else if (ch === ',') {
          if (buf.trim()) items.push(parseTomlScalar(buf));
          buf = '';
        } else {
          buf += ch;
        }
      }
      if (buf.trim()) items.push(parseTomlScalar(buf));
      return items;
    }
    return s;
  }

  function parseToml(text) {
    const root = { settings: {}, ai: {}, account: [] };
    let table = root;
    for (const line of String(text).split(/\r?\n/)) {
      let s = line.trim();
      if (!s || s.startsWith('#')) continue;
      if (s.startsWith('[[')) {
        const name = s.slice(2, s.indexOf(']]')).trim();
        const item = {};
        (root[name] = root[name] || []).push(item);
        table = item;
        continue;
      }
      if (s.startsWith('[')) {
        const name = s.slice(1, s.indexOf(']')).trim();
        root[name] = root[name] || {};
        table = root[name];
        continue;
      }
      // 去掉行内注释（引号内的 # 不算；标量里不会出现裸引号）
      let quote = null;
      let cut = s.length;
      for (let i = 0; i < s.length; i++) {
        const ch = s[i];
        if (quote) {
          if (ch === quote) quote = null;
        } else if (ch === '"' || ch === "'") quote = ch;
        else if (ch === '#') { cut = i; break; }
      }
      s = s.slice(0, cut);
      const eq = s.indexOf('=');
      if (eq < 0) continue;
      table[s.slice(0, eq).trim()] = parseTomlScalar(s.slice(eq + 1));
    }
    return root;
  }

  // 账号块：单账号可用 [tenant_name]/username 顶层写法，统一转成数组
  function accountsFromConfig(cfg) {
    const list = Array.isArray(cfg.account) && cfg.account.length
      ? cfg.account
      : cfg.tenant_name || cfg.username
        ? [cfg]
        : [];
    return list
      .map((a) => ({
        tenantName: String(a.tenant_name || '').trim(),
        username: String(a.username || '').trim(),
        password: String(a.password ?? a.username ?? ''),
      }))
      .filter((a) => a.tenantName && a.username);
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 2. 登录加密：AES-ECB/PKCS7，密钥为 'wbs512' 补零到 16 字节（= 原 base64
  //    d2JzNTEyAAAAAAAAAAAAAA==），结果 URL-safe Base64（api.py encrypt 同序）
  // ──────────────────────────────────────────────────────────────────────────

  // 不用 crypto.subtle：它没有 ECB 模式，逐块套 CBC(零 IV) 每块都要开一次
  // 异步调用；自带 AES 一次算完，且输出与 api.py 逐字节一致（有离线对照测试）。
  function encryptLoginPayload(payload) {
    const enc = new TextEncoder();
    const keyBytes = new Uint8Array(16);
    keyBytes.set(enc.encode(LOGIN_KEY).slice(0, 16));
    return encryptEcbBlocks(enc.encode(JSON.stringify(payload)), keyBytes);
  }

  // 纯 JS AES-128 加密（ECB，PKCS7）。约 100 行，避免依赖 CryptoJS。
  const AES = (() => {
    const sbox = new Uint8Array(256);
    const rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36];
    let p = 1, q = 1;
    do {
      p = p ^ ((p << 1) & 0xff) ^ (p & 0x80 ? 0x1b : 0);
      q ^= q << 1; q ^= q << 2; q ^= q << 4; q &= 0xff;
      if (q & 0x80) q ^= 0x09;
      sbox[p] = (q ^ ((q << 1) | (q >>> 7)) ^ ((q << 2) | (q >>> 6)) ^
        ((q << 3) | (q >>> 5)) ^ ((q << 4) | (q >>> 4)) ^ 0x63) & 0xff;
    } while (p !== 1);
    sbox[0] = 0x63;

    const xtime = (a) => ((a << 1) ^ (a & 0x80 ? 0x1b : 0)) & 0xff;

    function expandKey(key) {
      const w = new Uint8Array(176);
      w.set(key);
      for (let i = 16, r = 0; i < 176; i += 4) {
        let t0 = w[i - 4], t1 = w[i - 3], t2 = w[i - 2], t3 = w[i - 1];
        if (i % 16 === 0) {
          const tmp = t0;
          t0 = sbox[t1] ^ rcon[r++];
          t1 = sbox[t2];
          t2 = sbox[t3];
          t3 = sbox[tmp];
        }
        w[i] = w[i - 16] ^ t0;
        w[i + 1] = w[i - 15] ^ t1;
        w[i + 2] = w[i - 14] ^ t2;
        w[i + 3] = w[i - 13] ^ t3;
      }
      return w;
    }

    function encryptBlock(block, w) {
      const s = Uint8Array.from(block);
      for (let i = 0; i < 16; i++) s[i] ^= w[i];
      for (let round = 1; round <= 10; round++) {
        for (let i = 0; i < 16; i++) s[i] = sbox[s[i]];
        // ShiftRows（列主序：字节 i 对应 row=i%4, col=(i/4)|0）
        const t = s.slice();
        for (let c = 0; c < 4; c++) {
          for (let r = 1; r < 4; r++) s[c * 4 + r] = t[((c + r) % 4) * 4 + r];
        }
        if (round !== 10) {
          for (let c = 0; c < 4; c++) {
            const a = s.slice(c * 4, c * 4 + 4);
            const [a0, a1, a2, a3] = a;
            // MixColumns：b0 = 2a0 ^ 3a1 ^ a2 ^ a3 = 2(a0^a1) ^ a1 ^ a2 ^ a3
            const m = (x, y) => xtime(x ^ y) ^ y;
            s[c * 4]     = m(a0, a1) ^ a2 ^ a3;
            s[c * 4 + 1] = m(a1, a2) ^ a0 ^ a3;
            s[c * 4 + 2] = m(a2, a3) ^ a0 ^ a1;
            s[c * 4 + 3] = m(a3, a0) ^ a1 ^ a2;
          }
        }
        const off = round * 16;
        for (let i = 0; i < 16; i++) s[i] ^= w[off + i];
      }
      return s;
    }

    return { expandKey, encryptBlock };
  })();

  function encryptEcbBlocks(plain, key) {
    const padLen = 16 - (plain.length % 16);
    const padded = new Uint8Array(plain.length + padLen);
    padded.set(plain);
    padded.fill(padLen, plain.length);
    const w = AES.expandKey(key);
    const out = new Uint8Array(padded.length);
    for (let i = 0; i < padded.length; i += 16) {
      out.set(AES.encryptBlock(padded.subarray(i, i + 16), w), i);
    }
    let bin = '';
    for (const b of out) bin += String.fromCharCode(b);
    // api.py: urlsafe_b64encode(...)，保留 '=' 填充，- 和 _ 替换 + 和 /
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_');
  }

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

  // 所有请求统一走这个函数：GET 或「只读 POST」。
  // POST 走 form-urlencoded（与浏览器表单提交同签名同 cookie 语义），
  // 不用 fetch 的 JSON body，避免触发额外的 CORS 预检。
  async function request(method, path, data, { allow200 = false } = {}) {
    const head = { ...MAIN_HEADERS, Accept: 'application/json, text/javascript, */*; q=0.01' };
    const token = getSession().token;
    if (token) head['X-Token'] = token;
    // _post 的公共参数：tenantCode 永远带，userId 登录后带（顺序与 api.py 一致）
    const body = new URLSearchParams();
    const payload = { ...(data || {}) };
    if (session.tenantCode && payload.tenantCode === undefined) payload.tenantCode = session.tenantCode;
    if (session.userId && payload.userId === undefined) payload.userId = session.userId;
    for (const [k, v] of Object.entries(payload)) {
      if (v !== undefined && v !== null) body.set(k, String(v));
    }
    if (method === 'POST') head['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8';
    // 所有 .do 接口都要求 timestamp query（api.py _post 固定加）
    const qs = { timestamp: timestamp() };

    const resp = await fetch(url(path, qs), {
      method,
      headers: head,
      credentials: 'include',
      body: method === 'POST' ? body.toString() : undefined,
    });
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

  // 读接口包装：只允许白名单里的 method+path
  async function read(name, payload = {}) {
    const def = ENDPOINTS[name];
    if (!def) throw new Error('未登记的只读接口：' + name);
    if (def.kind === 'get-200') return request('GET', def.path, null, { allow200: true });
    return request('POST', def.path, payload);
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
  // 5. 登录流程（api.py：randLetterImage → login，body AES 加密）
  //    账号来自 config.toml；密码只在本机内存里，不落 GM 存储。
  // ──────────────────────────────────────────────────────────────────────────

  async function randLetterImage(verifyTime) {
    const resp = await fetch(url('/pharos/login/randLetterImage.do', { time: verifyTime }), {
      credentials: 'include',
    });
    return resp.blob();
  }

  async function login(account, verifyCode, verifyTime) {
    const payload = {
      keyNumber: account.username,
      password: account.password,
      tenantCode: account.tenantCode,
      time: verifyTime,
      verifyCode,
    };
    const encrypted = await encryptLoginPayload(payload);
    const body = new URLSearchParams({ data: encrypted });
    const resp = await fetch(url('/pharos/login/login.do', { timestamp: timestamp() }), {
      method: 'POST',
      credentials: 'include',
      headers: { ...MAIN_HEADERS, 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
      body: body.toString(),
    });
    let json = null;
    try { json = await resp.json(); } catch { json = null; }
    if (json && json.detailCode === '67') return { ok: false, reason: 'verify-wrong', raw: json };
    const user = json?.data;
    if (json?.code === '0' && user?.token) {
      setSession({ token: user.token, userId: user.userId, tenantCode: user.tenantCode, source: 'login' });
      return { ok: true, user, raw: json };
    }
    return { ok: false, reason: 'login-failed', raw: json };
  }

  async function resolveTenantCode(tenantName) {
    const res = await read('tenantList');
    const entries = (res?.data || []).flatMap((g) => g.list || []);
    const exact = entries.find((e) => String(e.name || '').trim() === tenantName.trim());
    if (exact) return exact.code;
    const fuzzy = entries.filter((e) => String(e.name || '').includes(tenantName.trim()));
    if (fuzzy.length === 1) return fuzzy[0].code;
    throw new Error(fuzzy.length
      ? '学校名不唯一，请写全称：' + fuzzy.map((e) => e.name).join(' / ')
      : '没找到学校代码，请检查 config.toml 的 tenant_name');
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 6. 只读采集：项目 → 进度 → 分类 → 课程 → 考试计划
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

    // 6.1 首页初始化（与 client.simulate_home_page 同顺序，只读子集）
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
        const res = await read(name);
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
    const myProject = await read('myProject', { ended: 2 });
    if (!ok(myProject)) throw new Error('项目列表读取失败：' + (myProject?.message || myProject?.code));
    const projects = [...(myProject.data || [])];
    const modules = (snapshot.home.listCompletion?.data || [])
      .filter((d) => d.showable === 1)
      .map((d) => d.module);
    if (modules.includes('labProject')) {
      try {
        const lab = await read('labIndex');
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
        const progress = await read('showProgress', { userProjectId: upid });
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
          const cats = await read('listCategory', { userProjectId: upid, chooseType });
          if (!ok(cats)) throw new Error(cats?.message || 'code=' + cats?.code);
          for (const cat of cats.data || []) {
            checkCancelled();
            const courses = await read('listCourse', {
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
        const plans = await read('listPlan', { userProjectId: upid });
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
      tenantName: UI.account?.tenantName || '',
      username: UI.account?.username || '',
      userId: session.userId || '',
      tenantCode: session.tenantCode || '',
      loginSource: session.source,
    };
    return snapshot;
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
      '<div class="weban-head"><b>WeBan 只读探针</b><span id="weban-probe-ver">v' + VERSION + '</span></div>' +
      '<div class="weban-body">' +
        '<div id="weban-login"></div>' +
        '<div class="weban-row">' +
          '<button id="weban-btn-run" class="primary">读取</button>' +
          '<button id="weban-btn-cancel" class="danger" disabled>停止</button>' +
          '<button id="weban-btn-copy">复制</button>' +
        '</div>' +
        '<div class="weban-row">' +
          '<button id="weban-btn-json">下载 JSON</button>' +
          '<button id="weban-btn-clear">清空</button>' +
        '</div>' +
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
    root.querySelector('#weban-btn-json').addEventListener('click', downloadJson);
    root.querySelector('#weban-btn-clear').addEventListener('click', () => {
      const el = logEl(); if (el) el.innerHTML = '';
    });

    renderAccount();
  }

  function renderAccount() {
    const box = document.getElementById('weban-login');
    if (!box) return;
    const acc = UI.account;
    if (acc) {
      box.innerHTML = '<div class="weban-tip" style="margin-bottom:8px">' +
        '<span class="weban-badge">账号</span> ' + esc(acc.tenantName) + ' / ' + esc(acc.username) +
        '</div>';
      return;
    }
    box.innerHTML =
      '<div class="weban-field"><label>config.toml 内容（粘贴进来）</label>' +
      '<textarea id="weban-login-toml" placeholder="[[account]]&#10;tenant_name = &quot;你的学校全称&quot;&#10;username = &quot;学号&quot;&#10;password = &quot;密码&quot;"></textarea></div>' +
      '<div class="weban-row"><button id="weban-btn-config">解析并读第一条账号</button></div>' +
      '<div class="weban-tip" id="weban-login-msg">' +
      '说明：配置只在本机内存解析，密码不写入 ScriptStorage；页面已登录时直接复用现成登录态。</div>';
    box.querySelector('#weban-btn-config').addEventListener('click', () => {
      const text = box.querySelector('#weban-login-toml').value;
      try {
        const cfg = parseToml(text);
        const list = accountsFromConfig(cfg);
        if (!list.length) throw new Error('没解析到有效账号（需要 tenant_name/username）');
        UI.account = list[0];
        UI.accountList = list;
        UI.config = cfg;
        renderAccount();
        log('已解析 ' + list.length + ' 个账号，使用 ' + UI.account.tenantName + ' / ' + UI.account.username, 'ok');
      } catch (e) {
        const msg = box.querySelector('#weban-login-msg');
        msg.textContent = e.message; msg.classList.add('is-error');
      }
    });
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
  // 8. 登录到目标账号：优先复用页面登录态，其次用 config.toml 的账号登录
  // ──────────────────────────────────────────────────────────────────────────

  async function ensureLoggedIn() {
    await refreshSessionFromPage();
    const ident = readStorageIdentity();
    if (ident) setSession({ userId: ident.userId, tenantCode: ident.tenantCode || session.tenantCode });
    if (session.token && session.userId) {
      log('复用页面登录态（' + session.source + '，userId…' + session.userId.slice(-6) + '）', 'ok');
      return;
    }
    if (!UI.account) {
      throw new Error('页面未登录，且尚未填写 config.toml 账号');
    }
    log('使用 config.toml 账号登录：' + UI.account.tenantName + ' / ' + UI.account.username);
    const tenantCode = await resolveTenantCode(UI.account.tenantName);
    UI.account.tenantCode = tenantCode;
    log('学校代码：' + tenantCode, 'ok');

    for (let attempt = 1; attempt <= 5; attempt++) {
      const verifyTime = String(Date.now()).slice(0, 13);
      const blob = await randLetterImage(verifyTime);
      const code = await askCaptcha(blob, attempt);
      if (code === null) throw Object.assign(new Error('已取消登录'), { cancelled: true });
      const res = await login(UI.account, code, Number(verifyTime));
      if (res.ok) {
        log('登录成功：' + (res.user.realName || res.user.userName || '') + ' userId=' + res.user.userId, 'ok');
        return res.user;
      }
      if (res.reason === 'verify-wrong') { log('验证码错误，重试…', 'warn'); continue; }
      throw new Error('登录失败：' + (res.raw?.message || res.raw?.code || '未知错误') +
        '（请检查学校全称/学号/密码）');
    }
    throw new Error('验证码连续错误，已停止');
  }

  // 登录验证码：弹小窗显示图片，用户输入。只用于登录这一次，不落盘。
  function askCaptcha(blob, attempt) {
    return new Promise((resolve) => {
      const imgUrl = URL.createObjectURL(blob);
      const wrap = document.createElement('div');
      wrap.id = 'weban-captcha';
      wrap.style.cssText =
        'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:2147483001;' +
        'display:flex;align-items:center;justify-content:center;';
      wrap.innerHTML =
        '<div style="background:#fff;border-radius:10px;padding:16px;width:260px;text-align:center">' +
          '<div style="font-size:13px;margin-bottom:8px">登录验证码（第 ' + attempt + ' 次）</div>' +
          '<img style="width:100%;border:1px solid #d0d7de;border-radius:6px" />' +
          '<input id="weban-cap-input" maxlength="8" style="width:100%;box-sizing:border-box;' +
            'margin-top:10px;padding:7px;border:1px solid #d0d7de;border-radius:6px;text-align:center;' +
            'font-size:16px;letter-spacing:3px" autocomplete="off" />' +
          '<div style="display:flex;gap:8px;margin-top:10px">' +
            '<button id="weban-cap-ok" style="flex:1;padding:7px;background:#1f883d;color:#fff;border:0;border-radius:7px">确定</button>' +
            '<button id="weban-cap-cancel" style="flex:1;padding:7px;background:#f6f8fa;border:1px solid #d0d7de;border-radius:7px">取消</button>' +
          '</div>' +
        '</div>';
      wrap.querySelector('img').src = imgUrl;
      document.body.appendChild(wrap);
      const input = wrap.querySelector('#weban-cap-input');
      input.focus();
      const done = (v) => { URL.revokeObjectURL(imgUrl); wrap.remove(); resolve(v); };
      wrap.querySelector('#weban-cap-ok').addEventListener('click', () => done(input.value.trim() || null));
      wrap.querySelector('#weban-btn-cancel');
      wrap.querySelector('#weban-cap-cancel').addEventListener('click', () => done(null));
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') done(input.value.trim() || null); });
    });
  }

  // ──────────────────────────────────────────────────────────────────────────
  // 9. 主流程
  // ──────────────────────────────────────────────────────────────────────────

  async function runProbe() {
    if (UI.running) return;
    UI.cancelled = false;
    setRunning(true);
    log('开始读取（只读，不会开始学习、答题或交卷）…');
    try {
      await ensureLoggedIn();
      const snapshot = await collectSnapshot({ onCancel: () => UI.cancelled });
      GM_setValue(STORE.last, snapshot);
      const done = snapshot.projects.reduce(
        (n, p) => n + Object.values(p.categories).flat().filter((c) => c.finished).length, 0);
      const total = snapshot.projects.reduce(
        (n, p) => n + Object.values(p.categories).flat().length, 0);
      log('完成：' + snapshot.projects.length + ' 个项目，课程 ' + done + '/' + total + ' 已完成' +
        (snapshot.warnings.length ? '，' + snapshot.warnings.length + ' 条提示' : ''), 'ok');
    } catch (e) {
      if (e.cancelled) {
        log('已停止', 'warn');
      } else if (e instanceof AccountBlockedError) {
        log('账号被限制：' + e.message + '，已停止', 'err');
      } else if (e instanceof PermissionError) {
        log('登录态失效（' + e.message + '），请先登录官网或重新填写账号', 'err');
        setSession({ token: null, source: 'none' });
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

  GM_registerMenuCommand('读取（只读）', () => { mountPanel(); runProbe(); });
  GM_registerMenuCommand('清空已存快照', () => {
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

  // 供调试/后续扩展使用（课程页只读探针会用到）
  window.__weban = {
    version: VERSION,
    parseToml,
    encryptLoginPayload,
    read,
    collectSnapshot,
    ensureLoggedIn,
    refreshSessionFromPage,
    readStorageIdentity,
    readStorageToken,
    getSession,
    setSession,
    runProbe,
  };
})();
