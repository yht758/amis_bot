/* ============================================================
   IM Bridge · 本地管理台
   原生 JS · 零依赖 · 支持 ?demo=1 演示模式
   ============================================================ */
(function () {
  'use strict';

  /* ---------------- 常量与工具 ---------------- */
  const APP_VERSION = '2.1.0';
  const MAX_LOG_ROWS = 2000;
  const POLL_MS = 2000;
  const RECONNECT_MS = 6000;
  const qs = new URLSearchParams(location.search);
  const DEMO = qs.get('demo') === '1';
  const API_BASE = resolveApiBase();
  const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)');
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const ROUTES = {
    overview: { title: '概览', eyebrow: 'DASHBOARD', subtitle: '一眼掌握 Bot 的运行状态与关键指标' },
    settings: { title: '配置', eyebrow: 'CONFIGURATION', subtitle: '模型、权限、上下文与拟真输出节奏' },
    logs: { title: '日志', eyebrow: 'LIVE LOGS', subtitle: '实时追踪 Bot 与 OneBot 链路的输出' },
    env: { title: '环境', eyebrow: 'ENVIRONMENT', subtitle: '依赖、端口与运行环境的自检结果' },
    about: { title: '关于', eyebrow: 'ABOUT', subtitle: '版本信息、常用命令与免责声明' }
  };

  const CONFIG_KEYS = [
    'base_url', 'model', 'bot_qq', 'respond_to_groups', 'allowed_users', 'admin_users',
    'napcat_ws_url', 'max_history', 'summary_enabled', 'summary_threshold', 'summary_keep_recent',
    'summary_async', 'summary_token_threshold',
    'streaming_enabled', 'chunking_enabled', 'chunk_min_delay', 'chunk_max_delay',
    'chunk_max_segments', 'message_max_chars',
    'proactive_enabled', 'proactive_idle_hours', 'proactive_max_per_day', 'proactive_quiet_hours',
    'voice_input_enabled', 'voice_reply_enabled', 'asr_endpoint', 'tts_endpoint',
    'voice_max_seconds', 'voice_path_map',
    'persona'
  ];
  const SETTINGS_KEYS = ['napcat_command', 'auto_open', 'auto_restart'];
  const SNAPSHOT_KEYS = CONFIG_KEYS.concat(SETTINGS_KEYS);

  const STATUS_META = {
    running: { text: '运行中', chip: '在线', tone: 'ok' },
    stopped: { text: '已停止', chip: '空闲', tone: 'neutral' },
    starting: { text: '启动中', chip: '正在启动', tone: 'info' },
    stopping: { text: '停止中', chip: '正在停止', tone: 'warn' },
    error: { text: '异常退出', chip: '需要处理', tone: 'danger' },
    unknown: { text: '未知状态', chip: '等待数据', tone: 'neutral' }
  };

  const FIELD_MAP = {
    api_key: { input: '#f-api-key', error: '#err-api-key' },
    base_url: { input: '#f-base-url', error: '#err-base-url' },
    model: { input: ['#f-model', '#f-model-custom'], error: '#err-model' },
    bot_qq: { input: '#f-bot-qq', error: '#err-bot-qq' },
    napcat_ws_url: { input: '#f-napcat-ws', error: '#err-napcat-ws' },
    allowed_users: { input: '#tags-allowed', error: '#err-allowed_users', wrap: true },
    admin_users: { input: '#tags-admin', error: '#err-admin_users', wrap: true }
  };

  const ENV_ICONS = {
    python: 'code', deps: 'box', project: 'folder', onebot_port: 'plug',
    napcat: 'chat', edge: 'globe', config: 'file'
  };
  const ENV_STATUS_TEXT = { ok: '通过', warn: '提醒', fail: '失败' };
  const OPEN_TARGET_NAMES = { project: '项目目录', data: '数据目录', logs: '日志目录', env: '.env 文件', config: '配置文件' };

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const nowSec = () => Date.now() / 1000;
  const pad2 = (n) => String(n).padStart(2, '0');
  const clamp = (n, min, max) => Math.min(max, Math.max(min, n));
  const asInt = (value, fallback) => {
    const n = parseInt(value, 10);
    return Number.isFinite(n) ? n : fallback;
  };
  const asNum = (value, fallback) => {
    const n = parseFloat(value);
    return Number.isFinite(n) ? n : fallback;
  };
  const asBool = (value) => value === true || value === 'true' || value === 1 || value === '1';
  const asStr = (value, fallback) => (value === undefined || value === null ? (fallback || '') : String(value));
  const asArr = (value) => {
    if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
    if (typeof value === 'string') return value.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean);
    return [];
  };
  const formatClock = (ts) => {
    const date = new Date(ts * 1000);
    return Number.isNaN(date.getTime()) ? '--:--' : `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  };
  const formatLogTime = (ts) => {
    const date = new Date(ts * 1000);
    return Number.isNaN(date.getTime()) ? '--:--:--' : `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
  };
  const formatFullTime = () => {
    const d = new Date();
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  };
  const debounce = (fn, wait) => {
    let timer = 0;
    return function debounced() {
      const args = arguments;
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(null, args), wait);
    };
  };
  const iconMarkup = (name, extraClass) =>
    `<svg class="icon ${extraClass || ''}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

  function resolveApiBase() {
    const override = qs.get('api');
    if (override) return override.replace(/\/+$/, '');
    if (location.protocol === 'http:' || location.protocol === 'https:') return location.origin;
    const port = qs.get('port') || '8765';
    return 'http://127.0.0.1:' + port;
  }

  function readStoredTheme() {
    try {
      const value = localStorage.getItem('aemeath.theme');
      return value === 'light' || value === 'dark' ? value : null;
    } catch (err) { return null; }
  }

  function storeTheme(theme) {
    try { localStorage.setItem('aemeath.theme', theme); } catch (err) { /* 隐私模式下忽略 */ }
  }

  /* ---------------- 应用状态 ---------------- */
  const app = {
    demo: DEMO,
    conn: 'connecting',
    offline: false,
    route: 'overview',
    loggerRendered: false,
    state: null,
    stateAt: 0,
    stateFailures: 0,
    ping: null,
    configLoaded: false,
    settingsLoaded: false,
    logsLoaded: false,
    keySet: false,
    keyMasked: '',
    savedConfig: null,
    defaults: {},
    form: {
      base_url: '',
      model: '',
      bot_qq: '',
      respond_to_groups: false,
      allowed_users: [],
      admin_users: [],
      napcat_ws_url: '',
      max_history: 40,
      summary_enabled: true,
      summary_threshold: 24,
      summary_keep_recent: 8,
      summary_async: true,
      summary_token_threshold: 1800,
      streaming_enabled: true,
      chunking_enabled: true,
      chunk_min_delay: 1,
      chunk_max_delay: 2,
      chunk_max_segments: 8,
      message_max_chars: 1000,
      proactive_enabled: false,
      proactive_idle_hours: 24,
      proactive_max_per_day: 1,
      proactive_quiet_hours: '23:00-08:00',
      voice_input_enabled: false,
      voice_reply_enabled: false,
      asr_endpoint: '',
      tts_endpoint: '',
      voice_max_seconds: 60,
      voice_path_map: '',
      persona: 'aemeath',
      napcat_command: '',
      auto_open: true,
      auto_restart: false
    },
    baseline: null,
    dirty: [],
    saving: false,
    botBusy: false,
    action: null,
    actionAt: 0,
    napcatBusy: false,
    napcatRunning: false,
    logs: [],
    logFilter: 'all',
    logQuery: '',
    autoScroll: true,
    logNewCount: 0,
    sseState: 'connecting',
    env: null,
    envAt: null,
    envLoading: false,
    timers: { poll: 0, clock: 0, reconnect: 0, demoLogs: 0 },
    es: null,
    connecting: false,
    lastFocus: null,
    firstRoute: true
  };

  /* ---------------- 网络层 ---------------- */
  async function api(path, options) {
    const opts = options || {};
    if (DEMO) return mockApi(path, opts);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeout || 12000);
    const init = {
      method: opts.method || 'GET',
      cache: 'no-store',
      signal: controller.signal,
      headers: { Accept: 'application/json' }
    };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json; charset=utf-8';
      init.body = JSON.stringify(opts.body);
    }
    try {
      const response = await fetch(API_BASE + path, init);
      let data = null;
      try { data = await response.json(); } catch (err) { data = null; }
      if (!response.ok || !data || data.ok === false) {
        const message = (data && data.error) || (response.status ? `请求失败（HTTP ${response.status}）` : '管理台返回了无效数据');
        const error = new Error(message);
        error.details = data && Array.isArray(data.details) ? data.details : [];
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('请求超时，管理台没有响应');
      if (error instanceof TypeError) throw new Error('无法连接管理台（服务未启动或请求被拦截）');
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  /* ---------------- 演示模式 Mock ---------------- */
  const demo = {
    logs: [],
    pid: 24816,
    startTimer: 0,
    stopTimer: 0
  };

  const DEMO_LOG_POOL = [
    { level: 'INFO', source: 'bot', text: 'Bot 客户端已连接：ws://127.0.0.1:3001' },
    { level: 'DEBUG', source: 'bot', text: '收到私聊消息 uid=10001，长度 12，已写入上下文窗口（40 条）' },
    { level: 'INFO', source: 'llm', text: 'LLM 响应完成 1.8s / 使用 356 tokens / 摘要尚未触发' },
    { level: 'INFO', source: 'bot', text: '分片发送 3/3 完成，间隔 1.4s' },
    { level: 'DEBUG', source: 'bot', text: '动态白名单校验通过：uid=10001' },
    { level: 'WARN', source: 'llm', text: '上游响应较慢（2.9s），已自动重试 0 次' },
    { level: 'INFO', source: 'bot', text: '上下文摘要完成：24 条压缩为 412 字，保留最近 8 条原文' },
    { level: 'DEBUG', source: 'manager', text: '管理台状态轮询成功：running=true' },
    { level: 'ERROR', source: 'llm', text: '单次请求失败：上游返回 429，已回退为滑动窗口截断' },
    { level: 'INFO', source: 'bot', text: '收到群聊消息 group=887766，未 @Bot，已忽略' },
    { level: 'DEBUG', source: 'bot', text: '用户 10002 触发 /reset，已清空该用户上下文（5 条）' },
    { level: 'INFO', source: 'manager', text: '配置已保存：model=deepseek-chat' },
    { level: 'WARN', source: 'bot', text: '消息长度超过单条上限，已拆分为 2 条发送' },
    { level: 'INFO', source: 'bot', text: '人设 aemeath 已加载（aemeath.txt，2140 字）' }
  ];

  function seedDemoLogs() {
    const list = [];
    const count = 20;
    const start = nowSec() - count * 15;
    for (let i = 0; i < count; i += 1) {
      const item = DEMO_LOG_POOL[(i * 5 + 3) % DEMO_LOG_POOL.length];
      list.push({ ts: start + i * 15 + (i % 4), level: item.level, text: item.text, source: item.source });
    }
    return list;
  }

  function demoPushLog(level, text, source) {
    const line = { ts: nowSec(), level: level, text: text, source: source || 'manager' };
    if (DEMO) appendLog(line);
  }

  function demoStateSnapshot() {
    const bot = demo.state.bot;
    if (bot.running && bot.started_at) bot.uptime = Math.max(0, nowSec() - bot.started_at);
    return clone(demo.state);
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function initDemoWorld() {
    demo.logs = seedDemoLogs();
    demo.state = {
      ok: true,
      version: APP_VERSION,
      bot: {
        running: true,
        pid: demo.pid,
        started_at: nowSec() - 4372,
        uptime: 4372,
        exit_code: null,
        auto_restart: false
      },
      server: { port: 8765, host: '127.0.0.1' },
      config: {
        loaded: true,
        api_key_set: true,
        api_key_masked: 'sk-***（已配置，长度 35）',
        model: 'deepseek-chat',
        base_url: 'http://127.0.0.1:8080/v1',
        valid: true,
        errors: []
      }
    };
    demo.config = {
      base_url: 'http://127.0.0.1:8080/v1',
      model: 'deepseek-chat',
      bot_qq: '123456789',
      respond_to_groups: false,
      allowed_users: ['10001', '10002'],
      admin_users: ['123456789'],
      napcat_ws_url: 'ws://127.0.0.1:3001',
      max_history: 40,
      summary_enabled: true,
      summary_threshold: 24,
      summary_keep_recent: 8,
      streaming_enabled: true,
      chunking_enabled: true,
      chunk_min_delay: 1.0,
      chunk_max_delay: 2.0,
      persona: 'aemeath'
    };
    demo.settings = { napcat_command: 'start_napcat.bat', auto_restart: false, auto_open: true };
    demo.envChecks = [
      { id: 'python', title: 'Python 运行时', status: 'ok', detail: 'Python 3.14.0（64 位）', hint: '' },
      { id: 'deps', title: 'Python 依赖', status: 'ok', detail: 'websockets 17.0.1 · httpx 0.28.1 已安装', hint: '' },
      { id: 'project', title: '项目结构', status: 'ok', detail: 'run.py / src / launcher 均存在，目录完整', hint: '' },
      { id: 'onebot_port', title: 'OneBot 端口', status: 'warn', detail: '端口 3001 当前未监听（NapCat 未启动）', hint: '启动 NapCat 后会自动监听；若已启动，请检查反向 WebSocket 配置是否一致。' },
      { id: 'napcat', title: 'NapCat 进程', status: 'fail', detail: '未检测到 NapCat 进程', hint: '在概览页点击「启动 NapCat」，或在「管理台设置」中填写 NapCat 启动命令后重试。' },
      { id: 'edge', title: 'Edge 浏览器', status: 'ok', detail: 'Microsoft Edge 已安装，可用于截图与网页登录', hint: '' },
      { id: 'config', title: '配置文件', status: 'ok', detail: '.env 已加载，API Key 已配置（长度 35）', hint: '' }
    ];
    const fields = {};
    Object.keys(demo.config).forEach((key) => {
      fields[key] = { value: demo.config[key] };
    });
    fields.api_key = { value: '', set: true, masked: 'sk-***（已配置，长度 35）' };
    demo.configResponse = {
      ok: true,
      fields: fields,
      defaults: {
        model: 'deepseek-chat', max_history: 40, summary_threshold: 24, summary_keep_recent: 8,
        streaming_enabled: true, chunking_enabled: true, chunk_min_delay: 1.0, chunk_max_delay: 2.0
      },
      sources: { api_key: '.env' },
      errors: []
    };
  }

  async function mockApi(path, opts) {
    await sleep(90 + Math.random() * 170);
    const options = opts || {};
    const cleanPath = String(path).split('?')[0];
    const body = options.body || {};

    if (cleanPath === '/api/ping' || cleanPath === '/api/version') {
      return { ok: true, app: 'im-bridge-manager', version: APP_VERSION };
    }
    if (cleanPath === '/api/state') return demoStateSnapshot();
    if (cleanPath === '/api/config' && (!options.method || options.method === 'GET')) {
      return clone(demo.configResponse);
    }
    if (cleanPath === '/api/config' && options.method === 'POST') {
      const next = Object.assign({}, demo.config);
      CONFIG_KEYS.forEach((key) => {
        if (body[key] !== undefined) next[key] = body[key];
      });
      demo.config = next;
      const fields = demo.configResponse.fields;
      CONFIG_KEYS.forEach((key) => {
        if (fields[key]) fields[key].value = next[key];
      });
      if (typeof body.api_key === 'string' && body.api_key.length > 0) {
        fields.api_key.set = true;
        fields.api_key.masked = 'sk-***（已配置，长度 ' + body.api_key.length + '）';
      }
      demo.state.config.model = next.model;
      demo.state.config.base_url = next.base_url;
      demo.state.config.api_key_set = fields.api_key.set;
      demo.state.config.api_key_masked = fields.api_key.masked;
      const warnings = demo.state.bot.running ? ['部分配置需重启 Bot 生效'] : [];
      return { ok: true, saved: true, warnings: warnings };
    }
    if (cleanPath === '/api/config/test') {
      return {
        ok: true,
        latency_ms: 280 + Math.floor(Math.random() * 160),
        models: ['deepseek-chat', 'deepseek-reasoner', 'gpt-4o', 'gpt-4o-mini']
      };
    }
    if (cleanPath === '/api/bot/start') {
      if (demo.state.bot.running) return { ok: true, pid: demo.state.bot.pid, already: true };
      clearTimeout(demo.startTimer);
      demo.startTimer = setTimeout(() => {
        demo.pid = 24000 + Math.floor(Math.random() * 900);
        demo.state.bot = {
          running: true, pid: demo.pid, started_at: nowSec(), uptime: 0,
          exit_code: null, auto_restart: demo.state.bot.auto_restart
        };
        demoPushLog('INFO', 'Bot 进程已启动（PID ' + demo.pid + '）', 'manager');
        demoPushLog('INFO', 'Bot 客户端已连接：ws://127.0.0.1:3001', 'bot');
      }, 1500);
      return { ok: true, pid: null };
    }
    if (cleanPath === '/api/bot/stop') {
      clearTimeout(demo.stopTimer);
      demo.stopTimer = setTimeout(() => {
        demo.state.bot = {
          running: false, pid: null, started_at: null, uptime: 0,
          exit_code: 0, auto_restart: demo.state.bot.auto_restart
        };
        demoPushLog('INFO', 'Bot 进程已停止（exit code 0）', 'manager');
      }, 950);
      return { ok: true, stopped: true };
    }
    if (cleanPath === '/api/bot/restart') {
      clearTimeout(demo.startTimer);
      clearTimeout(demo.stopTimer);
      demo.startTimer = setTimeout(() => {
        demo.pid = 24000 + Math.floor(Math.random() * 900);
        demo.state.bot = {
          running: true, pid: demo.pid, started_at: nowSec(), uptime: 0,
          exit_code: null, auto_restart: demo.state.bot.auto_restart
        };
        demoPushLog('INFO', 'Bot 已重启（PID ' + demo.pid + '）', 'manager');
      }, 900);
      return { ok: true, pid: demo.state.bot.pid };
    }
    if (cleanPath === '/api/bot/auto-restart') {
      demo.state.bot.auto_restart = asBool(body.enabled);
      return { ok: true };
    }
    if (cleanPath === '/api/logs') {
      const tail = asInt(qs.get('tail'), 300);
      return { ok: true, lines: clone(demo.logs.slice(-tail)) };
    }
    if (cleanPath === '/api/logs/clear') {
      demo.logs = [];
      return { ok: true };
    }
    if (cleanPath === '/api/env') {
      return { ok: true, checks: clone(demo.envChecks) };
    }
    if (cleanPath === '/api/settings' && (!options.method || options.method === 'GET')) {
      return { ok: true, napcat_command: demo.settings.napcat_command, auto_restart: demo.settings.auto_restart, auto_open: demo.settings.auto_open };
    }
    if (cleanPath === '/api/settings' && options.method === 'POST') {
      demo.settings = {
        napcat_command: asStr(body.napcat_command, demo.settings.napcat_command),
        auto_restart: body.auto_restart === undefined ? demo.settings.auto_restart : asBool(body.auto_restart),
        auto_open: body.auto_open === undefined ? demo.settings.auto_open : asBool(body.auto_open)
      };
      return { ok: true };
    }
    if (cleanPath === '/api/action/open') return { ok: true };
    if (cleanPath === '/api/napcat/start') {
      return { ok: true, pid: 3300 + Math.floor(Math.random() * 200) };
    }
    if (cleanPath === '/api/napcat/stop') return { ok: true };
    if (cleanPath === '/api/napcat/status') return { ok: true, running: false };
    if (cleanPath === '/api/napcat/update/status') {
      return {
        ok: true,
        state: {
          running: false, stage: 'idle', message: '', downloaded: 0, total: 0,
          percent: 0, error: '', result: null
        },
        check: {
          ok: true,
          local: { installed: true, tag: 'v4.18.41', mtime: nowSec() - 86400, path: 'D:\\NapCatQQ\\bootmain\\napcat' },
          latest_tag: 'v4.18.41',
          update_available: false,
          asset: { name: 'NapCat.Framework.zip', size: 36738019, digest: 'sha256:demo', url: '' },
          error: ''
        },
        mirror: ''
      };
    }
    if (cleanPath === '/api/napcat/update') return { ok: true, started: true };
    if (cleanPath === '/api/shutdown') return { ok: true };
    throw new Error('演示模式未覆盖该接口：' + cleanPath);
  }

  /* ---------------- 主题 ---------------- */
  function applyTheme(theme, persist) {
    document.documentElement.dataset.theme = theme;
    if (persist !== false) storeTheme(theme);
    $$('.theme-opt').forEach((btn) => {
      btn.setAttribute('aria-checked', String(btn.dataset.setTheme === theme));
    });
    const toggle = $('#btn-theme');
    if (toggle) toggle.setAttribute('aria-label', theme === 'dark' ? '切换到浅色主题' : '切换到深色主题');
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'dark' ? '#080c14' : '#f4f7fb');
  }

  function toggleTheme() {
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark', true);
  }

  function initTheme() {
    const stored = readStoredTheme();
    const param = qs.get('theme');
    const prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
    const theme = (param === 'light' || param === 'dark') ? param : (stored || (prefersLight ? 'light' : 'dark'));
    applyTheme(theme, false);
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (event) => {
      if (!readStoredTheme() && !qs.get('theme')) applyTheme(event.matches ? 'light' : 'dark', false);
    });
  }

  /* ---------------- 通用 UI：Toast ---------------- */
  const TOAST_ICONS = { success: 'check', error: 'alert-circle', warn: 'alert-triangle', info: 'info' };

  function toast(options) {
    const opts = options || {};
    const type = opts.type || 'info';
    const duration = typeof opts.duration === 'number' ? opts.duration : (type === 'error' ? 5600 : 4200);
    const host = $('#toasts');
    if (!host) return;
    while (host.children.length >= 4) host.removeChild(host.firstElementChild);

    const node = el('div', 'toast');
    node.dataset.type = type;
    node.setAttribute('role', type === 'error' ? 'alert' : 'status');

    const icon = el('span', 'toast-icon');
    icon.innerHTML = iconMarkup(TOAST_ICONS[type] || 'info');

    const body = el('div', 'toast-body');
    if (opts.title) body.appendChild(el('div', 'toast-title', opts.title));
    const messages = [];
    if (opts.message) messages.push(String(opts.message));
    if (Array.isArray(opts.details)) opts.details.forEach((item) => { if (item) messages.push(String(item)); });
    if (messages.length) {
      if (messages.length === 1) {
        body.appendChild(el('div', 'toast-message', messages[0]));
      } else {
        const wrap = el('div', 'toast-message');
        const list = el('ul');
        messages.forEach((message) => list.appendChild(el('li', null, message)));
        wrap.appendChild(list);
        body.appendChild(wrap);
      }
    }

    const close = el('button', 'toast-close');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭提示');
    close.innerHTML = iconMarkup('x');

    const progress = el('span', 'toast-progress');
    progress.style.animationDuration = duration + 'ms';

    node.append(icon, body, close, progress);
    host.appendChild(node);

    let timer = 0;
    let dismissed = false;
    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      clearTimeout(timer);
      node.classList.add('is-leaving');
      setTimeout(() => node.remove(), 260);
    };
    close.addEventListener('click', dismiss);
    node.addEventListener('mouseenter', () => {
      clearTimeout(timer);
      progress.style.animationPlayState = 'paused';
    });
    node.addEventListener('mouseleave', () => {
      progress.style.animationPlayState = 'running';
      timer = setTimeout(dismiss, 1600);
    });
    timer = setTimeout(dismiss, duration);
  }

  function toastError(error, title) {
    const message = error && error.message ? error.message : String(error || '未知错误');
    const details = error && Array.isArray(error.details) ? error.details : [];
    toast({ type: 'error', title: title || '操作失败', message: message, details: details, duration: 6000 });
  }

  /* ---------------- 通用 UI：确认弹窗 ---------------- */
  function openConfirm(config) {
    const root = $('#modal-root');
    if (!root) return;
    root.textContent = '';
    root.hidden = false;
    app.lastFocus = document.activeElement;

    const modal = el('div', 'modal');
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'modal-title');

    const icon = el('div', 'modal-icon');
    icon.innerHTML = iconMarkup(config.danger === false ? 'alert-circle' : 'alert-triangle');
    const title = el('h2', null, config.title || '请确认操作');
    title.id = 'modal-title';
    const message = el('p', null, config.message || '');

    const actions = el('div', 'modal-actions');
    const cancel = el('button', 'btn btn-ghost', config.cancelText || '取消');
    cancel.type = 'button';
    const confirm = el('button', 'btn ' + (config.danger === false ? 'btn-primary' : 'btn-danger'));
    confirm.type = 'button';
    confirm.textContent = config.confirmText || '确认';
    actions.append(cancel, confirm);

    modal.append(icon, title, message, actions);
    root.appendChild(modal);

    const close = () => {
      root.hidden = true;
      root.textContent = '';
      root.removeEventListener('click', onRootClick);
      document.removeEventListener('keydown', onKeydown, true);
      if (app.lastFocus && typeof app.lastFocus.focus === 'function') app.lastFocus.focus();
    };
    const onRootClick = (event) => {
      if (event.target === root) close();
    };
    const onKeydown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusables = [cancel, confirm];
      const index = focusables.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0) {
        event.preventDefault();
        confirm.focus();
      } else if (!event.shiftKey && index === focusables.length - 1) {
        event.preventDefault();
        cancel.focus();
      }
    };
    cancel.addEventListener('click', close);
    confirm.addEventListener('click', () => {
      close();
      if (typeof config.onConfirm === 'function') config.onConfirm();
    });
    root.addEventListener('click', onRootClick);
    document.addEventListener('keydown', onKeydown, true);
    confirm.focus();
  }

  /* ---------------- 连接状态 UI ---------------- */
  const CONN_META = {
    connecting: { title: '正在连接', badge: '连接中', mode: '连接中…', about: '连接中' },
    online: { title: '管理台已连接', badge: '在线', mode: '已连接', about: '本地管理台' },
    demo: { title: '演示模式', badge: '演示', mode: '演示模式', about: '演示模式' },
    offline: { title: '管理台未连接', badge: '离线', mode: '离线', about: '离线' }
  };

  function setConn(conn) {
    app.conn = conn;
    const meta = CONN_META[conn] || CONN_META.connecting;
    const badge = $('#mode-badge');
    if (badge) {
      badge.dataset.mode = conn;
      const text = $('#mode-badge-text');
      if (text) text.textContent = meta.mode;
    }
    const card = $('#conn-card');
    if (card) {
      card.dataset.state = conn;
      const dot = $('#conn-dot');
      if (dot) dot.dataset.state = conn;
      const title = $('#conn-title');
      if (title) title.textContent = meta.title;
      const badgeEl = $('#conn-badge');
      if (badgeEl) badgeEl.textContent = meta.badge;
      const sub = $('#conn-sub');
      if (sub) sub.textContent = conn === 'demo' ? '本地模拟数据' : API_BASE.replace(/^https?:\/\//, '');
    }
    const aboutMode = $('#about-mode');
    if (aboutMode) aboutMode.textContent = meta.about;
  }

  function setSseState(state) {
    app.sseState = state;
    const mini = $('#mini-sse');
    if (mini) mini.dataset.state = state;
    const indicator = $('#log-sse');
    if (indicator) {
      indicator.dataset.state = state;
      const text = $('#log-sse-text');
      const labels = { live: '日志流已连接', demo: '演示数据流', reconnecting: '日志流重连中…', offline: '日志流已断开', connecting: '日志流连接中' };
      if (text) text.textContent = labels[state] || '日志流状态未知';
    }
  }

  /* ---------------- 数据加载 ---------------- */
  function normalizeState(raw) {
    const data = raw || {};
    const bot = data.bot || {};
    const server = data.server || {};
    const config = data.config || {};
    return {
      version: asStr(data.version, APP_VERSION),
      bot: {
        running: !!bot.running,
        pid: bot.pid === undefined || bot.pid === null ? null : bot.pid,
        started_at: typeof bot.started_at === 'number' ? bot.started_at : null,
        uptime: asNum(bot.uptime, 0),
        exit_code: bot.exit_code === undefined ? null : bot.exit_code,
        auto_restart: !!bot.auto_restart
      },
      server: {
        port: server.port === undefined || server.port === null ? null : server.port,
        host: asStr(server.host, '127.0.0.1')
      },
      config: {
        loaded: !!config.loaded,
        api_key_set: !!config.api_key_set,
        api_key_masked: asStr(config.api_key_masked, ''),
        model: asStr(config.model, ''),
        base_url: asStr(config.base_url, ''),
        valid: config.valid !== false,
        errors: Array.isArray(config.errors) ? config.errors : []
      }
    };
  }

  function normalizeLog(line) {
    const data = line || {};
    return {
      ts: asNum(data.ts, nowSec()),
      level: asStr(data.level, 'INFO').toUpperCase(),
      text: asStr(data.text, ''),
      source: asStr(data.source, '')
    };
  }

  async function refreshState(options) {
    const opts = options || {};
    try {
      const raw = await api('/api/state', { timeout: opts.poll ? 6000 : 9000 });
      app.state = normalizeState(raw);
      app.stateAt = Date.now();
      app.stateFailures = 0;
      if (app.action === 'starting' && app.state.bot.running) app.action = null;
      else if (app.action === 'stopping' && !app.state.bot.running) app.action = null;
      else if (app.action && Date.now() - app.actionAt > 8000) app.action = null;
      renderOverview();
      updateConnSub();
      return true;
    } catch (error) {
      app.stateFailures += 1;
      if (!opts.poll) toastError(error, '刷新状态失败');
      if (app.stateFailures >= 2 && !DEMO && !app.offline) {
        goOffline('连续两次状态查询失败，管理台可能已退出');
      }
      return false;
    }
  }

  function updateConnSub() {
    if (!app.state) return;
    const sub = $('#conn-sub');
    if (sub && app.conn !== 'demo') sub.textContent = app.state.server.host + ':' + app.state.server.port;
  }

  function applySettings(raw) {
    const data = raw || {};
    const fields = data.settings || data.fields || data;
    app.form.napcat_command = asStr(fields.napcat_command, app.form.napcat_command || '');
    app.form.auto_restart = asBool(fields.auto_restart);
    app.form.auto_open = fields.auto_open === undefined ? true : asBool(fields.auto_open);
    app.settingsLoaded = true;
  }

  function applyConfig(raw) {
    const data = raw || {};
    const fields = data.fields || {};
    const read = (name, fallback) => {
      const field = fields[name];
      if (field && typeof field === 'object' && Object.prototype.hasOwnProperty.call(field, 'value')) return field.value;
      return fallback;
    };
    const form = app.form;
    app.keySet = !!(fields.api_key && fields.api_key.set);
    app.keyMasked = asStr(fields.api_key && fields.api_key.masked, '');
    form.base_url = asStr(read('base_url', ''), '');
    form.model = asStr(read('model', ''), '');
    form.bot_qq = asStr(read('bot_qq', ''), '');
    form.respond_to_groups = asBool(read('respond_to_groups', false));
    form.allowed_users = asArr(read('allowed_users', []));
    form.admin_users = asArr(read('admin_users', []));
    form.napcat_ws_url = asStr(read('napcat_ws_url', ''), '');
    form.max_history = clamp(asInt(read('max_history', 40), 40), 4, 200);
    form.summary_enabled = asBool(read('summary_enabled', true));
    form.summary_threshold = asInt(read('summary_threshold', 24), 24);
    form.summary_keep_recent = asInt(read('summary_keep_recent', 8), 8);
    form.streaming_enabled = asBool(read('streaming_enabled', true));
    form.chunking_enabled = asBool(read('chunking_enabled', true));
    form.chunk_min_delay = clamp(asNum(read('chunk_min_delay', 1), 1), 0, 5);
    form.chunk_max_delay = clamp(asNum(read('chunk_max_delay', 2), 2), 0, 5);
    form.chunk_max_segments = clamp(asInt(read('chunk_max_segments', 8), 8), 0, 30);
    form.message_max_chars = clamp(asInt(read('message_max_chars', 1000), 1000), 50, 2000);
    form.summary_async = asBool(read('summary_async', true));
    form.summary_token_threshold = clamp(asInt(read('summary_token_threshold', 1800), 1800), 0, 20000);
    form.proactive_enabled = asBool(read('proactive_enabled', false));
    form.proactive_idle_hours = clamp(asNum(read('proactive_idle_hours', 24), 24), 1, 720);
    form.proactive_max_per_day = clamp(asInt(read('proactive_max_per_day', 1), 1), 0, 10);
    form.proactive_quiet_hours = asStr(read('proactive_quiet_hours', '23:00-08:00'), '23:00-08:00');
    form.voice_input_enabled = asBool(read('voice_input_enabled', false));
    form.voice_reply_enabled = asBool(read('voice_reply_enabled', false));
    form.asr_endpoint = asStr(read('asr_endpoint', ''), '');
    form.tts_endpoint = asStr(read('tts_endpoint', ''), '');
    form.voice_max_seconds = clamp(asInt(read('voice_max_seconds', 60), 60), 1, 300);
    form.voice_path_map = asStr(read('voice_path_map', ''), '');
    form.persona = asStr(read('persona', 'aemeath'), 'aemeath');
    app.defaults = data.defaults || {};
    app.savedConfig = snapshotConfig();
    app.configLoaded = true;
    fillConfigForm();
    app.baseline = snapshotForm();
    updateDirty();
    renderMetrics();
    renderKeyUI();
    renderModelStatus();
  }

  async function loadAllData(options) {
    const opts = options || {};
    if (!app.settingsLoaded) {
      try {
        const settings = await api('/api/settings');
        applySettings(settings);
      } catch (error) {
        app.settingsLoaded = true;
        if (!opts.initial) toast({ type: 'warn', title: '管理台设置加载失败', message: error.message });
      }
    }
    if (!app.configLoaded) {
      try {
        const config = await api('/api/config');
        applyConfig(config);
      } catch (error) {
        toastError(error, '加载配置失败');
      }
    }
    await refreshState({ poll: true });
    if (!app.logsLoaded) {
      try {
        const logs = await api('/api/logs?tail=300&level=all');
        setLogs(logs.lines);
      } catch (error) {
        app.logsLoaded = true;
        if (!opts.initial) toast({ type: 'warn', title: '日志加载失败', message: error.message });
      }
    }
    connectLogStream();
    loadNapcatStatus();
    if (app.route === 'env' && !app.env) loadEnv(false);
    if (app.route === 'settings') updateDirty();
  }

  async function loadNapcatStatus() {
    if (app.offline) return;
    try {
      const response = await api('/api/napcat/status');
      app.napcatRunning = !!response.running;
      renderNapcat();
    } catch (error) { /* 旧版后端可能没有该接口，忽略 */ }
  }

  async function connect(options) {
    const opts = options || {};
    if (app.connecting || DEMO) return false;
    app.connecting = true;
    setConn('connecting');
    try {
      const pong = await api('/api/ping', { timeout: opts.initial ? 4500 : 6000 });
      app.ping = pong;
      app.offline = false;
      app.stateFailures = 0;
      stopReconnectLoop();
      hideOfflinePage();
      setConn('online');
      await loadAllData({ initial: opts.initial });
      startPolling();
      renderRoute(app.route, { force: true, silentScroll: true });
      if (!opts.initial && !opts.silent) {
        toast({ type: 'success', title: '已连接管理台', message: API_BASE });
      }
      return true;
    } catch (error) {
      app.offline = true;
      setConn('offline');
      setSseState('offline');
      showOfflinePage(error);
      startReconnectLoop();
      return false;
    } finally {
      app.connecting = false;
    }
  }

  function startPolling() {
    stopPolling();
    app.timers.poll = window.setInterval(() => {
      if (document.hidden || app.offline) return;
      refreshState({ poll: true });
    }, POLL_MS);
  }

  function stopPolling() {
    if (app.timers.poll) {
      clearInterval(app.timers.poll);
      app.timers.poll = 0;
    }
  }

  function startReconnectLoop() {
    if (app.timers.reconnect || DEMO) return;
    app.timers.reconnect = window.setInterval(async () => {
      if (app.connecting) return;
      if (navigator.onLine === false) return;
      const ok = await connect({ initial: false, silent: true });
      if (ok) {
        stopReconnectLoop();
        toast({ type: 'success', title: '管理台连接已恢复' });
      }
    }, RECONNECT_MS);
  }

  function stopReconnectLoop() {
    if (app.timers.reconnect) {
      clearInterval(app.timers.reconnect);
      app.timers.reconnect = 0;
    }
  }

  function goOffline(reason) {
    if (app.offline || DEMO) return;
    app.offline = true;
    app.stateFailures = 0;
    setConn('offline');
    setSseState('offline');
    stopPolling();
    if (app.es) {
      app.es.close();
      app.es = null;
    }
    showOfflinePage({ message: reason || '' });
    toast({ type: 'warn', title: '与管理台的连接中断', message: reason || '正在自动重连…', duration: 6000 });
    startReconnectLoop();
  }

  function showOfflinePage(error) {
    $$('.page').forEach((page) => page.classList.toggle('is-active', page.dataset.page === 'offline'));
    const title = $('#page-title');
    const subtitle = $('#page-subtitle');
    const eyebrow = $('#page-eyebrow');
    if (title) title.textContent = '离线';
    if (subtitle) subtitle.textContent = '管理台连接不可用';
    if (eyebrow) eyebrow.textContent = 'OFFLINE';
    document.title = '离线 · IM Bridge 管理台';
    const reason = $('#offline-reason');
    if (reason) {
      reason.textContent = '未检测到运行在 ';
      const code = el('code', null, API_BASE);
      reason.append(code, document.createTextNode(' 的管理台服务。' + (error && error.message ? `（${error.message}）` : '') + ' 请确认后端已启动，然后点击重试；也可以先用演示模式浏览界面。'));
    }
    const api = $('#offline-api');
    if (api) api.textContent = API_BASE;
  }

  function hideOfflinePage() {
    if (app.offline) return;
    if ($('#page-offline').classList.contains('is-active')) renderRoute(app.route, { force: true, silentScroll: true });
  }

  /* ---------------- 路由 / 导航 ---------------- */
  function routeFromHash() {
    const match = /^#\/([a-z-]+)/.exec(location.hash || '');
    return match && ROUTES[match[1]] ? match[1] : 'overview';
  }

  function moveNavIndicator() {
    const nav = $('#nav');
    const indicator = $('#nav-indicator');
    if (!nav || !indicator) return;
    const active = nav.querySelector('.nav-item.is-active');
    if (!active) return;
    const navRect = nav.getBoundingClientRect();
    const rect = active.getBoundingClientRect();
    if (!rect.width) return;
    const x = rect.left - navRect.left;
    const y = rect.top - navRect.top;
    indicator.style.width = rect.width + 'px';
    indicator.style.height = rect.height + 'px';
    indicator.style.transform = `translate(${x}px, ${y}px)`;
  }

  function setActiveNav(route) {
    $$('.nav-item').forEach((item) => {
      const active = item.dataset.route === route;
      item.classList.toggle('is-active', active);
      if (active) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    });
    requestAnimationFrame(moveNavIndicator);
  }

  function renderRoute(route, options) {
    const opts = options || {};
    if (!ROUTES[route]) route = 'overview';
    if (app.offline) {
      showOfflinePage(null);
      return;
    }
    app.route = route;
    const meta = ROUTES[route];
    const title = $('#page-title');
    const subtitle = $('#page-subtitle');
    const eyebrow = $('#page-eyebrow');
    if (title) title.textContent = meta.title;
    if (subtitle) subtitle.textContent = meta.subtitle;
    if (eyebrow) eyebrow.textContent = meta.eyebrow;
    document.title = meta.title + ' · IM Bridge 管理台';
    $$('.page').forEach((page) => {
      page.classList.toggle('is-active', page.dataset.page === route);
    });
    setActiveNav(route);
    if (!opts.silentScroll && !app.firstRoute) {
      window.scrollTo({ top: 0, behavior: REDUCED.matches ? 'auto' : 'smooth' });
    }
    app.firstRoute = false;
    if (route === 'settings') {
      updateDirty();
      updateAnchorFromScroll();
    } else {
      updateSavebar();
    }
    if (route === 'logs') {
      if (!app.logsLoaded && (DEMO || app.conn === 'online')) loadLogsInitial();
      renderLogList();
      if (app.autoScroll) scrollLogsToBottom(false);
    }
    if (route === 'env') {
      if (!app.env && (DEMO || app.conn === 'online')) loadEnv(false);
      else renderEnv();
    }
    if (route === 'about') renderAbout();
    if (route === 'overview') {
      renderOverview();
      renderMiniLogs(false);
    }
    if (route === 'settings' && app.configLoaded) {
      requestAnimationFrame(moveNavIndicator);
    }
  }

  function navigate(route) {
    if (!ROUTES[route]) return;
    if (app.offline) {
      toast({ type: 'warn', title: '管理台未连接', message: '正在自动重试连接，请稍候…' });
      return;
    }
    const target = '#/' + route;
    if (location.hash !== target) {
      location.hash = target;
    } else {
      renderRoute(route, { force: true });
    }
  }

  function initNav() {
    $$('.nav-item').forEach((item) => {
      item.addEventListener('click', (event) => {
        if (app.offline) {
          event.preventDefault();
          toast({ type: 'warn', title: '管理台未连接', message: '正在自动重试连接，请稍候…' });
        }
      });
    });
    window.addEventListener('hashchange', () => {
      const route = routeFromHash();
      if (app.offline) {
        showOfflinePage(null);
        return;
      }
      renderRoute(route);
    });
    window.addEventListener('resize', debounce(() => requestAnimationFrame(moveNavIndicator), 120));
    setTimeout(moveNavIndicator, 60);
  }

  /* ---------------- 设置页：锚点 / 表单 ---------------- */
  function updateAnchorFromScroll() {
    const anchors = $('#settings-anchors');
    if (!anchors) return;
    const sections = $$('.settings-section');
    if (!sections.length) return;
    const threshold = (anchors.getBoundingClientRect().bottom || 120) + 40;
    let current = sections[0].id;
    sections.forEach((section) => {
      if (section.getBoundingClientRect().top <= threshold) current = section.id;
    });
    $$('.anchor', anchors).forEach((btn) => btn.classList.toggle('is-active', btn.dataset.anchor === current));
  }

  function initAnchors() {
    const anchors = $('#settings-anchors');
    if (!anchors) return;
    anchors.addEventListener('click', (event) => {
      const btn = event.target.closest('.anchor');
      if (!btn) return;
      const section = document.getElementById(btn.dataset.anchor);
      if (section) section.scrollIntoView({ behavior: REDUCED.matches ? 'auto' : 'smooth', block: 'start' });
      $$('.anchor', anchors).forEach((item) => item.classList.toggle('is-active', item === btn));
    });
    let ticking = false;
    window.addEventListener('scroll', () => {
      if (app.route !== 'settings' || ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        updateAnchorFromScroll();
      });
    }, { passive: true });
  }

  function setSwitch(button, on) {
    if (!button) return;
    button.setAttribute('aria-checked', on ? 'true' : 'false');
  }

  function bindSwitch(selector, key, after) {
    const button = $(selector);
    if (!button) return;
    button.addEventListener('click', () => {
      const next = button.getAttribute('aria-checked') !== 'true';
      setSwitch(button, next);
      app.form[key] = next;
      if (after) after();
      updateDirty();
    });
  }

  function setControlValue(control, value) {
    if (!control) return;
    if (document.activeElement !== control) control.value = String(value);
  }

  function refreshKeyUI() {
    const apiKey = $('#f-api-key');
    const hasTyped = !!(apiKey && apiKey.value.trim());
    const badge = $('#key-status-badge');
    if (badge) {
      if (hasTyped) {
        badge.textContent = '待更新';
        badge.dataset.tone = 'info';
      } else if (app.keySet) {
        badge.textContent = '已配置';
        badge.dataset.tone = 'ok';
      } else {
        badge.textContent = '未配置';
        badge.dataset.tone = 'warn';
      }
    }
    const masked = $('#key-masked');
    if (masked) masked.textContent = app.keyMasked || (app.keySet ? '已配置（掩码不可用）' : '未配置');
    const hint = $('#hint-api-key');
    if (hint) {
      const state = hasTyped ? '已输入新密钥，保存后将替换当前 Key。' : '留空表示不修改已有密钥。';
      hint.textContent = '当前：' + (app.keyMasked || (app.keySet ? '已配置' : '未配置')) + '。' + state;
    }
  }

  function renderKeyUI() {
    refreshKeyUI();
  }

  function renderModelStatus() {
    const hint = $('#hint-model');
    if (!hint) return;
    const typed = app.form.model || '未选择';
    hint.textContent = '当前模型：' + typed + '。模型 ID 需与接口提供方一致。';
  }

  function applyModelToControls(model) {
    const select = $('#f-model');
    const custom = $('#f-model-custom');
    if (!select || !custom) return;
    const presets = ['deepseek-chat', 'deepseek-reasoner', 'gpt-4o', 'gpt-4o-mini'];
    custom.value = model || '';
    if (presets.indexOf(model) >= 0) {
      select.value = model;
      custom.hidden = true;
    } else {
      select.value = '__custom';
      custom.hidden = !model;
    }
  }

  function renderTags(key) {
    const wrap = $(key === 'allowed_users' ? '#tags-allowed' : '#tags-admin');
    if (!wrap) return;
    const input = $('.tags-input', wrap);
    $$('.tag', wrap).forEach((tag) => tag.remove());
    app.form[key].forEach((qq) => {
      const tag = el('span', 'tag');
      tag.appendChild(document.createTextNode(qq));
      const remove = el('button', 'tag-remove');
      remove.type = 'button';
      remove.setAttribute('aria-label', '移除 QQ ' + qq);
      remove.innerHTML = iconMarkup('x');
      remove.addEventListener('click', () => {
        app.form[key] = app.form[key].filter((item) => item !== qq);
        renderTags(key);
        updateDirty();
        if (input) input.focus();
      });
      tag.appendChild(remove);
      wrap.insertBefore(tag, input);
    });
  }

  function showFieldError(fieldKey, message) {
    const map = FIELD_MAP[fieldKey];
    if (!map) return;
    const errorNode = $(map.error);
    if (errorNode) {
      errorNode.textContent = message;
      errorNode.hidden = false;
    }
    if (map.wrap) {
      const wrap = $(map.input);
      if (wrap) wrap.classList.add('is-invalid');
    } else {
      (Array.isArray(map.input) ? map.input : [map.input]).forEach((selector) => {
        const input = $(selector);
        if (input) input.classList.add('is-invalid');
      });
    }
  }

  function clearFieldError(fieldKey) {
    const map = FIELD_MAP[fieldKey];
    if (!map) return;
    const errorNode = $(map.error);
    if (errorNode) {
      errorNode.hidden = true;
      errorNode.textContent = '';
    }
    if (map.wrap) {
      const wrap = $(map.input);
      if (wrap) wrap.classList.remove('is-invalid');
    } else {
      (Array.isArray(map.input) ? map.input : [map.input]).forEach((selector) => {
        const input = $(selector);
        if (input) input.classList.remove('is-invalid');
      });
    }
  }

  function clearAllFieldErrors() {
    Object.keys(FIELD_MAP).forEach(clearFieldError);
  }

  function fieldKeyByInput(input) {
    if (!input || !input.id) return null;
    const map = {
      'f-api-key': 'api_key', 'f-base-url': 'base_url', 'f-model': 'model', 'f-model-custom': 'model',
      'f-bot-qq': 'bot_qq', 'f-napcat-ws': 'napcat_ws_url',
      'tags-allowed-input': 'allowed_users', 'tags-admin-input': 'admin_users'
    };
    return map[input.id] || null;
  }

  function snapshotConfig() {
    const data = {};
    CONFIG_KEYS.forEach((key) => {
      data[key] = Array.isArray(app.form[key]) ? app.form[key].slice() : app.form[key];
    });
    return data;
  }

  function snapshotForm() {
    const data = {};
    SNAPSHOT_KEYS.forEach((key) => {
      data[key] = Array.isArray(app.form[key]) ? app.form[key].slice() : app.form[key];
    });
    return data;
  }

  function updateDirty() {
    if (!app.baseline) return;
    const dirty = [];
    const apiKeyTyped = $('#f-api-key') ? $('#f-api-key').value.trim() : '';
    if (apiKeyTyped) dirty.push('api_key');
    SNAPSHOT_KEYS.forEach((key) => {
      const current = JSON.stringify(app.form[key] === undefined ? null : app.form[key]);
      const base = JSON.stringify(app.baseline[key] === undefined ? null : app.baseline[key]);
      if (current !== base) dirty.push(key);
    });
    app.dirty = dirty;
    const count = $('#dirty-count');
    if (count) count.textContent = String(dirty.length);
    updateSavebar();
  }

  function updateSavebar() {
    const savebar = $('#savebar');
    if (!savebar) return;
    const show = app.dirty.length > 0 && app.route === 'settings' && !app.offline;
    if (show) {
      savebar.classList.add('is-visible');
      savebar.setAttribute('aria-hidden', 'false');
    } else {
      savebar.classList.remove('is-visible');
      savebar.setAttribute('aria-hidden', 'true');
    }
  }

  function setSaveLoading(loading) {
    const btn = $('#btn-save');
    if (!btn) return;
    btn.classList.toggle('is-loading', loading);
    btn.disabled = loading;
  }

  function clearSaveErrors() {
    const box = $('#save-errors');
    if (box) {
      box.hidden = true;
      box.textContent = '';
    }
  }

  function renderSaveErrors(messages) {
    const box = $('#save-errors');
    if (!box) return;
    box.textContent = '';
    if (!messages || !messages.length) {
      box.hidden = true;
      return;
    }
    const list = el('ul');
    messages.forEach((message) => list.appendChild(el('li', null, message)));
    box.appendChild(list);
    box.hidden = false;
  }

  function validateHttpUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== 'http:' && url.protocol !== 'https:') return 'Base URL 需以 http 或 https 协议开头';
      if (!url.hostname) return 'Base URL 缺少主机名';
      return '';
    } catch (error) {
      return '请输入合法的 Base URL';
    }
  }

  function validateWsUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== 'ws:' && url.protocol !== 'wss:') return 'WebSocket 地址需以 ws:// 或 wss:// 开头';
      if (!url.hostname) return 'WebSocket 地址缺少主机名';
      return '';
    } catch (error) {
      return '请输入合法的 WebSocket 地址';
    }
  }

  function validateQQ(value) {
    return /^\d{5,12}$/.test(String(value || '').trim()) ? '' : 'QQ 号需为 5–12 位数字';
  }

  function validateForm() {
    const errors = [];
    const form = app.form;
    const typedKey = $('#f-api-key') ? $('#f-api-key').value.trim() : '';
    if (typedKey && typedKey.length < 8) errors.push({ field: 'api_key', message: 'API Key 长度至少 8 个字符' });
    const baseUrl = form.base_url.trim();
    if (!baseUrl) errors.push({ field: 'base_url', message: '请填写 Base URL' });
    else {
      const message = validateHttpUrl(baseUrl);
      if (message) errors.push({ field: 'base_url', message: message });
    }
    if (!String(form.model || '').trim()) errors.push({ field: 'model', message: '请选择或填写模型 ID' });
    if (form.bot_qq && validateQQ(form.bot_qq)) errors.push({ field: 'bot_qq', message: 'Bot QQ 号需为 5–12 位数字' });
    const wsUrl = form.napcat_ws_url.trim();
    if (!wsUrl) errors.push({ field: 'napcat_ws_url', message: '请填写 NapCat WebSocket 地址' });
    else {
      const message = validateWsUrl(wsUrl);
      if (message) errors.push({ field: 'napcat_ws_url', message: message });
    }
    const invalidAllowed = form.allowed_users.filter((qq) => validateQQ(qq));
    if (invalidAllowed.length) errors.push({ field: 'allowed_users', message: '白名单中存在非法的 QQ 号：' + invalidAllowed.join('、') });
    const invalidAdmin = form.admin_users.filter((qq) => validateQQ(qq));
    if (invalidAdmin.length) errors.push({ field: 'admin_users', message: '管理员中存在非法的 QQ 号：' + invalidAdmin.join('、') });
    if (!String(form.persona || '').trim()) errors.push({ message: '请填写人设名称（例如 aemeath）', section: 'sec-persona' });
    if (!Number.isInteger(form.max_history) || form.max_history < 4 || form.max_history > 200) {
      errors.push({ message: '上下文窗口需为 4–200 之间的整数', section: 'sec-context' });
    }
    if (!Number.isInteger(form.summary_threshold) || form.summary_threshold < 2) {
      errors.push({ message: '摘要阈值需为不小于 2 的整数', section: 'sec-context' });
    } else if (form.summary_threshold >= form.max_history) {
      errors.push({ message: '摘要阈值必须小于上下文窗口', section: 'sec-context' });
    }
    if (!Number.isInteger(form.summary_keep_recent) || form.summary_keep_recent < 0) {
      errors.push({ message: '保留条数需为不小于 0 的整数', section: 'sec-context' });
    } else if (form.summary_keep_recent >= form.summary_threshold) {
      errors.push({ message: '保留最近原文必须小于摘要阈值', section: 'sec-context' });
    }
    const minDelay = normalizeDelay(form.chunk_min_delay);
    const maxDelay = normalizeDelay(form.chunk_max_delay);
    if (minDelay > maxDelay) errors.push({ message: '分片最小延迟不能大于最大延迟', section: 'sec-output' });
    if (minDelay < 0 || maxDelay > 5) errors.push({ message: '分片延迟需在 0–5 秒之间', section: 'sec-output' });
    const seen = {};
    return errors.filter((error) => {
      if (seen[error.message]) return false;
      seen[error.message] = true;
      return true;
    });
  }

  function normalizeDelay(value) {
    return Math.round(asNum(value, 0) * 10) / 10;
  }

  function buildConfigPayload() {
    const form = app.form;
    const typedKey = $('#f-api-key') ? $('#f-api-key').value.trim() : '';
    const qq = String(form.bot_qq || '').trim();
    return {
      api_key: typedKey,
      base_url: String(form.base_url || '').trim(),
      model: String(form.model || '').trim(),
      bot_qq: qq ? Number(qq) : 0,
      respond_to_groups: !!form.respond_to_groups,
      allowed_users: form.allowed_users.slice(),
      admin_users: form.admin_users.slice(),
      napcat_ws_url: String(form.napcat_ws_url || '').trim(),
      max_history: asInt(form.max_history, 40),
      summary_enabled: !!form.summary_enabled,
      summary_threshold: asInt(form.summary_threshold, 24),
      summary_keep_recent: asInt(form.summary_keep_recent, 8),
      streaming_enabled: !!form.streaming_enabled,
      chunking_enabled: !!form.chunking_enabled,
      chunk_min_delay: normalizeDelay(form.chunk_min_delay),
      chunk_max_delay: normalizeDelay(form.chunk_max_delay),
      chunk_max_segments: asInt(form.chunk_max_segments, 8),
      message_max_chars: asInt(form.message_max_chars, 1000),
      summary_async: !!form.summary_async,
      summary_token_threshold: asInt(form.summary_token_threshold, 1800),
      proactive_enabled: !!form.proactive_enabled,
      proactive_idle_hours: asNum(form.proactive_idle_hours, 24),
      proactive_max_per_day: asInt(form.proactive_max_per_day, 1),
      proactive_quiet_hours: String(form.proactive_quiet_hours || '').trim(),
      voice_input_enabled: !!form.voice_input_enabled,
      voice_reply_enabled: !!form.voice_reply_enabled,
      asr_endpoint: String(form.asr_endpoint || '').trim(),
      tts_endpoint: String(form.tts_endpoint || '').trim(),
      voice_max_seconds: asInt(form.voice_max_seconds, 60),
      voice_path_map: String(form.voice_path_map || '').trim(),
      persona: String(form.persona || '').trim()
    };
  }

  function settingsDirtyKeys() {
    if (!app.baseline) return [];
    return SETTINGS_KEYS.filter((key) => JSON.stringify(app.form[key]) !== JSON.stringify(app.baseline[key]));
  }

  async function saveConfig() {
    if (app.saving || !app.baseline) return;
    clearSaveErrors();
    const errors = validateForm();
    clearAllFieldErrors();
    errors.forEach((error) => {
      if (error.field) showFieldError(error.field, error.message);
    });
    if (errors.length) {
      renderSaveErrors(errors.map((error) => error.message));
      toast({ type: 'error', title: '还有 ' + errors.length + ' 项需要处理', message: errors[0].message });
      const first = errors.find((error) => error.field || error.section);
      if (first) {
        if (first.field && FIELD_MAP[first.field]) {
          const map = FIELD_MAP[first.field];
          const selector = Array.isArray(map.input) ? map.input[0] : map.input;
          const input = $(selector);
          if (input && input.scrollIntoView) input.scrollIntoView({ behavior: REDUCED.matches ? 'auto' : 'smooth', block: 'center' });
        } else if (first.section) {
          const section = document.getElementById(first.section);
          if (section) section.scrollIntoView({ behavior: REDUCED.matches ? 'auto' : 'smooth', block: 'start' });
        }
      }
      return;
    }

    const prevAutoRestart = app.baseline.auto_restart;
    const settingsKeys = settingsDirtyKeys();
    app.saving = true;
    setSaveLoading(true);
    try {
      const result = await api('/api/config', { method: 'POST', body: buildConfigPayload(), timeout: 20000 });
      if (settingsKeys.length) {
        await api('/api/settings', {
          method: 'POST',
          body: {
            napcat_command: String(app.form.napcat_command || '').trim(),
            auto_restart: !!app.form.auto_restart,
            auto_open: !!app.form.auto_open
          },
          timeout: 12000
        });
      }
      if (app.form.auto_restart !== prevAutoRestart) {
        try {
          await api('/api/bot/auto-restart', { method: 'POST', body: { enabled: !!app.form.auto_restart } });
        } catch (error) {
          toast({ type: 'warn', title: '自动重启设置同步失败', message: error.message });
        }
      }
      const keyInput = $('#f-api-key');
      if (keyInput) keyInput.value = '';
      app.savedConfig = snapshotConfig();
      app.baseline = snapshotForm();
      try {
        const raw = await api('/api/config');
        app.keySet = !!(raw.fields && raw.fields.api_key && raw.fields.api_key.set);
        app.keyMasked = asStr(raw.fields && raw.fields.api_key && raw.fields.api_key.masked, app.keyMasked);
      } catch (error) { /* 保持旧掩码 */ }
      updateDirty();
      renderKeyUI();
      renderMetrics();
      clearSaveErrors();
      await refreshState({ poll: true });
      const warnings = Array.isArray(result.warnings) ? result.warnings.filter(Boolean) : [];
      if (warnings.length) {
        toast({ type: 'warn', title: '配置已保存，但有提示', message: warnings.join('；'), duration: 5600 });
      } else {
        toast({ type: 'success', title: '配置已保存', message: '部分配置可能需要重启 Bot 后生效。' });
      }
    } catch (error) {
      const details = Array.isArray(error.details) ? error.details : [];
      renderSaveErrors([error.message].concat(details));
      toastError(error, '保存失败');
    } finally {
      app.saving = false;
      setSaveLoading(false);
    }
  }

  function resetForm() {
    if (!app.baseline) return;
    SNAPSHOT_KEYS.forEach((key) => {
      app.form[key] = Array.isArray(app.baseline[key]) ? app.baseline[key].slice() : app.baseline[key];
    });
    fillConfigForm();
    const keyInput = $('#f-api-key');
    if (keyInput) keyInput.value = '';
    clearAllFieldErrors();
    clearSaveErrors();
    updateDirty();
    refreshKeyUI();
    toast({ type: 'info', title: '已恢复为上次保存的配置' });
  }

  function fillConfigForm() {
    const form = app.form;
    const setValue = (selector, value) => {
      const control = $(selector);
      if (control) control.value = value === undefined || value === null ? '' : String(value);
    };
    setValue('#f-base-url', form.base_url);
    setValue('#f-bot-qq', form.bot_qq);
    setValue('#f-napcat-ws', form.napcat_ws_url);
    setValue('#f-persona', form.persona);
    setValue('#f-napcat-command', form.napcat_command);
    applyModelToControls(form.model);
    refreshKeyUI();
    renderTags('allowed_users');
    renderTags('admin_users');
    setSwitch($('#f-respond-groups'), form.respond_to_groups);
    setSwitch($('#f-summary-enabled'), form.summary_enabled);
    setSwitch($('#f-summary-async'), form.summary_async);
    setSwitch($('#f-streaming-enabled'), form.streaming_enabled);
    setSwitch($('#f-chunking-enabled'), form.chunking_enabled);
    setSwitch($('#f-proactive-enabled'), form.proactive_enabled);
    setSwitch($('#f-voice-input'), form.voice_input_enabled);
    setSwitch($('#f-voice-reply'), form.voice_reply_enabled);
    setSwitch($('#f-auto-open'), form.auto_open);
    setSwitch($('#f-auto-restart'), form.auto_restart);
    setValue('#f-summary-token', form.summary_token_threshold);
    setValue('#f-chunk-segments', form.chunk_max_segments);
    setValue('#f-message-max', form.message_max_chars);
    setValue('#f-proactive-idle', form.proactive_idle_hours);
    setValue('#f-proactive-max', form.proactive_max_per_day);
    setValue('#f-proactive-quiet', form.proactive_quiet_hours);
    setValue('#f-asr-endpoint', form.asr_endpoint);
    setValue('#f-tts-endpoint', form.tts_endpoint);
    setValue('#f-voice-max', form.voice_max_seconds);
    setValue('#f-voice-path-map', form.voice_path_map);
    refreshContextControls({ clamp: true });
    refreshChunkControls();
  }

  function setPairValue(rangeSelector, numberSelector, value) {
    const range = $(rangeSelector);
    const number = $(numberSelector);
    if (range) setControlValue(range, value);
    if (number) setControlValue(number, value);
  }

  function refreshContextControls(options) {
    const opts = options || {};
    const form = app.form;
    if (opts.clamp) {
      form.max_history = clamp(asInt(form.max_history, 40), 4, 200);
      const thresholdMax = Math.max(2, form.max_history - 1);
      form.summary_threshold = clamp(asInt(form.summary_threshold, 24), 2, thresholdMax);
      form.summary_keep_recent = clamp(asInt(form.summary_keep_recent, 8), 0, Math.max(0, form.summary_threshold - 1));
    }
    setPairValue('#f-max-history', '#f-max-history-num', form.max_history);
    setPairValue('#f-summary-threshold', '#f-summary-threshold-num', form.summary_threshold);
    setPairValue('#f-summary-keep', '#f-summary-keep-num', form.summary_keep_recent);

    const thresholdMax = Math.max(2, form.max_history - 1);
    const keepMax = Math.max(0, Math.min(thresholdMax - 1, form.summary_threshold - 1));
    const thresholdRange = $('#f-summary-threshold');
    const thresholdNumber = $('#f-summary-threshold-num');
    const keepRange = $('#f-summary-keep');
    const keepNumber = $('#f-summary-keep-num');
    if (thresholdRange) thresholdRange.max = String(thresholdMax);
    if (thresholdNumber) thresholdNumber.max = String(thresholdMax);
    if (keepRange) keepRange.max = String(keepMax);
    if (keepNumber) keepNumber.max = String(keepMax);

    const rounds = $('#ctx-rounds');
    if (rounds) rounds.textContent = '约 ' + Math.max(1, Math.round(form.max_history / 2)) + ' 轮对话（按一问一答两条约算）';

    const enabled = !!form.summary_enabled;
    [thresholdRange, thresholdNumber, keepRange, keepNumber].forEach((control) => {
      if (control) control.disabled = !enabled;
    });
    ['#field-threshold', '#field-keep'].forEach((selector) => {
      const field = $(selector);
      if (field) field.classList.toggle('is-disabled', !enabled);
    });

    const warning = $('#ctx-warning');
    if (warning) {
      let message = '';
      if (form.summary_threshold >= form.max_history) message = '摘要阈值必须小于上下文窗口（当前窗口 ' + form.max_history + ' 条）。';
      else if (form.summary_keep_recent >= form.summary_threshold) message = '保留最近原文必须小于摘要阈值（当前阈值 ' + form.summary_threshold + ' 条）。';
      warning.textContent = message;
      warning.hidden = !message;
    }
  }

  function refreshChunkControls() {
    const form = app.form;
    setPairValue('#f-chunk-min', '#f-chunk-min-num', normalizeDelay(form.chunk_min_delay).toFixed(1));
    setPairValue('#f-chunk-max', '#f-chunk-max-num', normalizeDelay(form.chunk_max_delay).toFixed(1));
    const minDelay = normalizeDelay(form.chunk_min_delay);
    const maxDelay = normalizeDelay(form.chunk_max_delay);
    const value = $('#delay-value');
    if (value) value.textContent = minDelay.toFixed(1) + ' – ' + maxDelay.toFixed(1) + ' s';
    const block = $('#delay-block');
    const disabled = !form.chunking_enabled;
    if (block) block.classList.toggle('is-disabled', disabled);
    ['#f-chunk-min', '#f-chunk-min-num', '#f-chunk-max', '#f-chunk-max-num'].forEach((selector) => {
      const control = $(selector);
      if (control) control.disabled = disabled;
    });
    const preview = $('#typing-preview');
    if (preview) {
      const avg = clamp((minDelay + maxDelay) / 2, 0.35, 5);
      preview.style.setProperty('--typing-speed', avg.toFixed(2) + 's');
    }
    const warning = $('#chunk-warning');
    if (warning) {
      const message = minDelay > maxDelay ? '最小延迟（' + minDelay.toFixed(1) + 's）不能大于最大延迟（' + maxDelay.toFixed(1) + 's）。' : '';
      warning.textContent = message;
      warning.hidden = !message;
    }
  }

  function initSettingsForms() {
    // 文本字段
    const textBindings = [
      ['#f-base-url', 'base_url'], ['#f-bot-qq', 'bot_qq'], ['#f-napcat-ws', 'napcat_ws_url'],
      ['#f-persona', 'persona'], ['#f-napcat-command', 'napcat_command'],
      ['#f-proactive-quiet', 'proactive_quiet_hours'],
      ['#f-asr-endpoint', 'asr_endpoint'], ['#f-tts-endpoint', 'tts_endpoint'],
      ['#f-voice-path-map', 'voice_path_map']
    ];
    textBindings.forEach(([selector, key]) => {
      const input = $(selector);
      if (!input) return;
      input.addEventListener('input', () => {
        app.form[key] = input.value;
        const fieldKey = fieldKeyByInput(input);
        if (fieldKey) clearFieldError(fieldKey);
        if (key === 'persona') renderModelStatus();
        updateDirty();
      });
    });

    // 模型
    const modelSelect = $('#f-model');
    const modelCustom = $('#f-model-custom');
    if (modelSelect) {
      modelSelect.addEventListener('change', () => {
        if (modelSelect.value === '__custom') {
          if (modelCustom) {
            modelCustom.hidden = false;
            modelCustom.focus();
            app.form.model = modelCustom.value;
          }
        } else {
          if (modelCustom) modelCustom.hidden = true;
          app.form.model = modelSelect.value;
        }
        clearFieldError('model');
        renderModelStatus();
        updateDirty();
      });
    }
    if (modelCustom) {
      modelCustom.addEventListener('input', () => {
        app.form.model = modelCustom.value;
        clearFieldError('model');
        renderModelStatus();
        updateDirty();
      });
    }

    // API Key
    const apiKey = $('#f-api-key');
    if (apiKey) {
      apiKey.addEventListener('input', () => {
        clearFieldError('api_key');
        refreshKeyUI();
        updateDirty();
      });
    }
    const reveal = $('#btn-reveal-key');
    if (reveal) {
      reveal.addEventListener('click', () => {
        if (!apiKey) return;
        const revealed = apiKey.type === 'text';
        apiKey.type = revealed ? 'password' : 'text';
        reveal.classList.toggle('is-revealed', !revealed);
        reveal.setAttribute('aria-pressed', String(!revealed));
        reveal.setAttribute('aria-label', revealed ? '显示 API Key' : '隐藏 API Key');
      });
    }
    const testBtn = $('#btn-test-conn');
    if (testBtn) testBtn.addEventListener('click', testConnection);

    // 开关
    bindSwitch('#f-respond-groups', 'respond_to_groups');
    bindSwitch('#f-summary-enabled', 'summary_enabled', () => refreshContextControls({ clamp: false }));
    bindSwitch('#f-summary-async', 'summary_async');
    bindSwitch('#f-streaming-enabled', 'streaming_enabled', () => refreshChunkControls());
    bindSwitch('#f-chunking-enabled', 'chunking_enabled', () => refreshChunkControls());
    bindSwitch('#f-proactive-enabled', 'proactive_enabled');
    bindSwitch('#f-voice-input', 'voice_input_enabled');
    bindSwitch('#f-voice-reply', 'voice_reply_enabled');
    bindSwitch('#f-auto-open', 'auto_open');
    bindSwitch('#f-auto-restart', 'auto_restart');

    // 上下文滑杆
    bindNumberPair('#f-max-history', '#f-max-history-num', 'max_history', {
      min: 4, max: 200, step: 1, integer: true,
      onChange: () => refreshContextControls({ clamp: false })
    });
    bindNumberPair('#f-summary-threshold', '#f-summary-threshold-num', 'summary_threshold', {
      min: 2, max: 199, step: 1, integer: true,
      onChange: () => refreshContextControls({ clamp: false })
    });
    bindNumberPair('#f-summary-keep', '#f-summary-keep-num', 'summary_keep_recent', {
      min: 0, max: 198, step: 1, integer: true,
      onChange: () => refreshContextControls({ clamp: false })
    });

    // 分片延迟
    bindNumberPair('#f-chunk-min', '#f-chunk-min-num', 'chunk_min_delay', {
      min: 0, max: 5, step: 0.1, integer: false,
      onChange: () => refreshChunkControls()
    });
    bindNumberPair('#f-chunk-max', '#f-chunk-max-num', 'chunk_max_delay', {
      min: 0, max: 5, step: 0.1, integer: false,
      onChange: () => refreshChunkControls()
    });

    // 新增数值字段（单输入框）
    [
      ['#f-chunk-segments', 'chunk_max_segments', 0, 30, true],
      ['#f-message-max', 'message_max_chars', 50, 2000, true],
      ['#f-summary-token', 'summary_token_threshold', 0, 20000, true],
      ['#f-proactive-idle', 'proactive_idle_hours', 1, 720, false],
      ['#f-proactive-max', 'proactive_max_per_day', 0, 10, true],
      ['#f-voice-max', 'voice_max_seconds', 1, 300, true]
    ].forEach(([selector, key, min, max, integer]) => {
      const input = $(selector);
      if (!input) return;
      input.addEventListener('input', () => {
        const parsed = integer ? asInt(input.value, app.form[key]) : asNum(input.value, app.form[key]);
        app.form[key] = clamp(parsed, min, max);
        updateDirty();
        renderMetrics();
      });
      input.addEventListener('blur', () => {
        input.value = String(app.form[key]);
      });
    });

    // tags
    initTagsField('allowed_users', '#tags-allowed-input');
    initTagsField('admin_users', '#tags-admin-input');

    // 人设内置值
    const personaDefault = $('#btn-persona-default');
    if (personaDefault) {
      personaDefault.addEventListener('click', () => {
        app.form.persona = 'aemeath';
        const personaInput = $('#f-persona');
        if (personaInput) personaInput.value = 'aemeath';
        updateDirty();
      });
    }

    // NapCat 控制
    const napcatBtn = $('#btn-napcat-start');
    if (napcatBtn) napcatBtn.addEventListener('click', toggleNapcat);

    // 保存 / 重置
    const saveBtn = $('#btn-save');
    if (saveBtn) saveBtn.addEventListener('click', saveConfig);
    const resetBtn = $('#btn-reset');
    if (resetBtn) resetBtn.addEventListener('click', resetForm);

    // 字段失焦校正
    ['#f-max-history-num', '#f-summary-threshold-num', '#f-summary-keep-num'].forEach((selector) => {
      const input = $(selector);
      if (!input) return;
      input.addEventListener('blur', () => refreshContextControls({ clamp: true }));
    });
    ['#f-chunk-min-num', '#f-chunk-max-num'].forEach((selector) => {
      const input = $(selector);
      if (!input) return;
      input.addEventListener('blur', () => {
        app.form.chunk_min_delay = clamp(asNum(app.form.chunk_min_delay, 0), 0, 5);
        app.form.chunk_max_delay = clamp(asNum(app.form.chunk_max_delay, 0), 0, 5);
        refreshChunkControls();
        updateDirty();
      });
    });
  }

  function bindNumberPair(rangeSelector, numberSelector, key, config) {
    const range = $(rangeSelector);
    const number = $(numberSelector);
    if (!range || !number) return;
    const apply = (raw, source) => {
      let value = config.integer ? asInt(raw, asNum(app.form[key], config.min)) : asNum(raw, asNum(app.form[key], config.min));
      value = clamp(value, config.min, config.max);
      if (config.integer) value = Math.round(value);
      else value = Math.round(value * 10) / 10;
      app.form[key] = value;
      if (source !== range) setControlValue(range, value);
      if (source !== number) setControlValue(number, value);
      if (config.onChange) config.onChange();
      updateDirty();
    };
    range.addEventListener('input', () => apply(range.value, range));
    number.addEventListener('input', () => {
      const raw = number.value;
      if (raw === '' || raw === '-') return;
      const value = config.integer ? asInt(raw, app.form[key]) : asNum(raw, app.form[key]);
      if (!Number.isFinite(value)) return;
      app.form[key] = value;
      if (config.onChange) config.onChange();
      updateDirty();
    });
    number.addEventListener('blur', () => apply(number.value, number));
  }

  function initTagsField(key, inputSelector) {
    const input = $(inputSelector);
    if (!input) return;
    const wrap = input.closest('.tags');
    const commit = (raw) => {
      const value = String(raw || '').trim();
      if (!value) return true;
      const message = validateQQ(value);
      if (message) {
        showFieldError(key, message);
        return false;
      }
      if (app.form[key].indexOf(value) >= 0) {
        showFieldError(key, '该 QQ 号已在列表中');
        return false;
      }
      app.form[key].push(value);
      clearFieldError(key);
      renderTags(key);
      updateDirty();
      return true;
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ',' || event.key === '，' || event.key === ' ') {
        event.preventDefault();
        if (commit(input.value)) input.value = '';
      } else if (event.key === 'Backspace' && input.value === '' && app.form[key].length) {
        app.form[key].pop();
        renderTags(key);
        updateDirty();
      }
    });
    input.addEventListener('input', () => {
      clearFieldError(key);
      if (/[,，\s]/.test(input.value)) {
        const parts = input.value.split(/[,，\s]+/);
        input.value = '';
        parts.forEach((part) => { if (part) commit(part); });
      }
    });
    input.addEventListener('blur', () => {
      if (input.value.trim()) {
        if (commit(input.value)) input.value = '';
      }
    });
    if (wrap) {
      wrap.addEventListener('click', (event) => {
        if (event.target === wrap) input.focus();
      });
    }
  }

  async function testConnection() {
    const button = $('#btn-test-conn');
    const result = $('#test-result');
    if (!button || !result || button.classList.contains('is-loading')) return;
    const body = {};
    const apiKey = $('#f-api-key') ? $('#f-api-key').value.trim() : '';
    if (apiKey) body.api_key = apiKey;
    if (app.form.base_url) body.base_url = app.form.base_url.trim();
    if (app.form.model) body.model = app.form.model.trim();
    button.classList.add('is-loading');
    button.disabled = true;
    result.hidden = true;
    try {
      const response = await api('/api/config/test', { method: 'POST', body: body, timeout: 25000 });
      result.textContent = '';
      result.hidden = false;
      const warning = response.warning ? String(response.warning) : '';
      const modelOk = response.model_ok !== false;
      result.className = 'test-result ' + (!modelOk ? 'is-fail' : (warning ? 'is-warn' : 'is-ok'));
      const checkIcon = el('span', null, '');
      checkIcon.innerHTML = iconMarkup('check');
      checkIcon.style.display = 'inline-flex';
      result.appendChild(checkIcon);
      result.appendChild(el('span', 'latency', '连接成功 · 延迟 ' + asInt(response.latency_ms, 0) + ' ms'));
      const models = Array.isArray(response.models) ? response.models.filter(Boolean).slice(0, 8) : [];
      if (models.length) {
        const chips = el('span', 'test-model-chips');
        models.forEach((model) => chips.appendChild(el('span', 'chip', model)));
        result.appendChild(chips);
      }
      if (warning) result.appendChild(el('span', 'test-warning', warning));
      if (!modelOk) {
        toast({ type: 'error', title: '连接成功，但模型不可用', message: warning || '请检查模型名称' });
      } else if (warning) {
        toast({ type: 'info', title: '连接正常（模型为别名）', message: warning });
      } else {
        toast({ type: 'success', title: '连接测试通过', message: '延迟 ' + asInt(response.latency_ms, 0) + ' ms' });
      }
    } catch (error) {
      result.textContent = '';
      result.hidden = false;
      result.className = 'test-result is-fail';
      const failIcon = el('span', null, '');
      failIcon.innerHTML = iconMarkup('alert-circle');
      failIcon.style.display = 'inline-flex';
      result.appendChild(failIcon);
      result.appendChild(el('span', null, error.message));
      toastError(error, '连接测试失败');
    } finally {
      button.classList.remove('is-loading');
      button.disabled = false;
    }
  }

  async function toggleNapcat() {
    if (app.napcatBusy || app.offline) return;
    app.napcatBusy = true;
    renderNapcat();
    try {
      if (app.napcatRunning) {
        await api('/api/napcat/stop', { method: 'POST' });
        app.napcatRunning = false;
        toast({ type: 'success', title: 'NapCat 已停止' });
      } else {
        const response = await api('/api/napcat/start', { method: 'POST' });
        app.napcatRunning = true;
        toast({
          type: 'success',
          title: 'NapCat 已启动',
          message: response.pid ? 'PID ' + response.pid : '等待 OneBot 连接…'
        });
      }
    } catch (error) {
      toastError(error, app.napcatRunning ? '停止 NapCat 失败' : '启动 NapCat 失败');
    } finally {
      app.napcatBusy = false;
      renderNapcat();
    }
  }

  function renderNapcat() {
    const label = $('#qa-napcat-label');
    const sub = $('#qa-napcat-sub');
    const settingsLabel = $('#btn-napcat-label');
    if (label) label.textContent = app.napcatRunning ? '停止 NapCat' : '启动 NapCat';
    if (sub) sub.textContent = app.napcatBusy ? '正在处理…' : (app.napcatRunning ? '点击停止 NapCat 进程' : '拉起 OneBot 连接端');
    if (settingsLabel) settingsLabel.textContent = app.napcatRunning ? '停止 NapCat' : '启动 NapCat';
    const button = $('#btn-napcat-start');
    if (button) button.classList.toggle('is-loading', app.napcatBusy);
    const tile = $('#qa-napcat');
    if (tile) tile.classList.toggle('is-busy', app.napcatBusy);
  }

  /* ---------------- 概览 ---------------- */
  function deriveStatus() {
    if (app.action === 'starting') return 'starting';
    if (app.action === 'stopping') return 'stopping';
    if (!app.state) return 'unknown';
    const bot = app.state.bot;
    if (bot.running) return 'running';
    if (bot.exit_code !== null && bot.exit_code !== undefined && bot.exit_code !== 0) return 'error';
    return 'stopped';
  }

  function renderOverview() {
    renderHero();
    renderMetrics();
    renderMiniLogs(false);
  }

  function renderHero() {
    const status = deriveStatus();
    const meta = STATUS_META[status] || STATUS_META.unknown;
    const orb = $('#ov-orb');
    if (orb) orb.dataset.status = status;
    const text = $('#ov-status-text');
    if (text) text.textContent = meta.text;
    const chip = $('#ov-status-chip');
    if (chip) {
      chip.textContent = meta.chip;
      chip.dataset.tone = meta.tone;
    }
    const sub = $('#ov-status-sub');
    const bot = app.state ? app.state.bot : null;
    const config = app.state ? app.state.config : null;
    const onebot = app.state ? app.state.onebot : null;
    const onebotBroken = !!(status === 'running' && onebot && onebot.reachable === false);
    let subText = '正在读取 Bot 状态…';
    if (sub) {
      if (status === 'starting') subText = '正在拉起 Bot 进程，请稍候…';
      else if (status === 'stopping') subText = '正在优雅停止 Bot 进程…';
      else if (status === 'running' && bot) {
        subText = '模型 ' + (config && config.model ? config.model : '—') + ' · PID ' + (bot.pid || '—') + ' · 自动重启' + (bot.auto_restart ? '已开启' : '已关闭');
        if (onebotBroken) {
          subText = 'OneBot 未连接（' + onebot.host + ':' + onebot.port + '）：Bot 已运行但收不到消息，请先启动 NapCat';
        } else if (config && config.loaded && !config.valid) subText += ' · 配置校验未通过';
      } else if (status === 'error' && bot) {
        subText = '进程已退出（退出码 ' + bot.exit_code + '），请查看日志排查';
      } else if (status === 'stopped') {
        subText = '点击右侧按钮启动 Bot，或前往配置页检查 API Key';
      }
      sub.textContent = subText;
      sub.classList.toggle('is-warn', onebotBroken);
    }

    const running = !!(bot && bot.running);
    const toggle = $('#btn-bot-toggle');
    const restart = $('#btn-bot-restart');
    const toggleLabel = $('#btn-bot-label');
    const toggleIcon = $('#ov-toggle-icon');
    const busy = !!app.action || app.botBusy;
    if (toggle) {
      toggle.disabled = busy;
      toggle.classList.toggle('is-loading', !!app.action);
      toggle.classList.toggle('btn-primary', !running);
      toggle.classList.toggle('btn-danger-soft', running);
    }
    if (toggleLabel) {
      toggleLabel.textContent = app.action === 'starting' ? '启动中…' : app.action === 'stopping' ? '停止中…' : (running ? '停止 Bot' : '启动 Bot');
    }
    if (toggleIcon) toggleIcon.setAttribute('href', running ? '#i-stop' : '#i-play');
    if (restart) restart.disabled = !running || busy;

    const started = $('#ov-started');
    if (started) {
      started.textContent = bot && bot.running && bot.started_at ? '自 ' + formatClock(bot.started_at) + ' 启动' : '尚未启动';
    }

    const keyState = $('#ov-api-key');
    if (keyState) {
      const keySet = config ? config.api_key_set : app.keySet;
      keyState.textContent = keySet ? 'API Key 已配置' : 'API Key 未配置';
      const item = keyState.closest('.foot-item');
      if (item) item.classList.toggle('is-warn', !keySet);
    }
    const configState = $('#ov-config-state');
    if (configState) {
      const loaded = config ? config.loaded : false;
      configState.textContent = loaded ? '配置文件已加载' : '配置文件未加载';
      const item = configState.closest('.foot-item');
      if (item) item.classList.toggle('is-warn', !loaded);
    }
    const server = $('#ov-server');
    if (server) {
      server.textContent = app.state ? '管理服务 ' + app.state.server.host + ':' + app.state.server.port : '管理服务 ' + API_BASE.replace(/^https?:\/\//, '');
    }
    const autoRestartText = $('#ov-auto-restart-text');
    const autoDot = $('#ov-auto-restart-dot');
    if (autoRestartText) autoRestartText.textContent = '自动重启：' + (bot && bot.auto_restart ? '开' : '关');
    if (autoDot) autoDot.classList.toggle('is-on', !!(bot && bot.auto_restart));
    renderUptime();
  }

  function renderUptime() {
    const wrap = $('#ov-uptime');
    if (!wrap) return;
    const bot = app.state ? app.state.bot : null;
    let seconds = 0;
    if (app.action === 'starting') seconds = 0;
    else if (bot && bot.running && bot.started_at) seconds = Math.max(0, nowSec() - bot.started_at);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    const parts = { h: String(hours).padStart(2, '0'), m: pad2(minutes), s: pad2(secs) };
    $$('.u-num', wrap).forEach((node) => {
      const next = parts[node.dataset.u];
      if (next !== undefined && node.textContent !== next) node.textContent = next;
    });
  }

  function renderMetrics() {
    renderOverviewConfig();
    const config = app.savedConfig || (app.configLoaded ? snapshotConfig() : null);
    const history = $('#m-history');
    const historySub = $('#m-history-sub');
    const historyMeter = $('#m-history-meter');
    const summary = $('#m-summary');
    const summarySub = $('#m-summary-sub');
    const summaryThresholdMeter = $('#m-summary-threshold-meter');
    const summaryKeepMeter = $('#m-summary-keep-meter');
    const chunk = $('#m-chunk');
    const chunkSub = $('#m-chunk-sub');
    const chunkRange = $('#m-chunk-range');
    const chunkMinDot = $('#m-chunk-min-dot');
    const chunkMaxDot = $('#m-chunk-max-dot');
    const ws = $('#m-ws');
    const wsSub = $('#m-ws-sub');

    if (!config) {
      if (history) history.textContent = '—';
      if (summary) summary.textContent = '—';
      if (chunk) chunk.textContent = '—';
      return;
    }
    const maxHistory = asInt(config.max_history, 40);
    if (history) history.textContent = String(maxHistory);
    if (historySub) historySub.textContent = '滑动窗口 · 约 ' + Math.max(1, Math.round(maxHistory / 2)) + ' 轮对话';
    if (historyMeter) historyMeter.style.width = clamp((maxHistory / 200) * 100, 4, 100).toFixed(1) + '%';
    if (summary) summary.textContent = String(asInt(config.summary_threshold, 24));
    if (summarySub) {
      summarySub.textContent = config.summary_enabled
        ? '保留最近 ' + asInt(config.summary_keep_recent, 8) + ' 条 · 摘要已启用'
        : '摘要未启用，仅滑动窗口截断';
    }
    if (summaryThresholdMeter) {
      summaryThresholdMeter.style.width = clamp((asInt(config.summary_threshold, 24) / Math.max(1, maxHistory)) * 100, 3, 100).toFixed(1) + '%';
    }
    if (summaryKeepMeter) {
      summaryKeepMeter.style.width = clamp((asInt(config.summary_keep_recent, 8) / Math.max(1, maxHistory)) * 100, 2, 100).toFixed(1) + '%';
    }
    const minDelay = normalizeDelay(config.chunk_min_delay);
    const maxDelay = normalizeDelay(config.chunk_max_delay);
    if (chunk) chunk.textContent = minDelay.toFixed(1) + '–' + maxDelay.toFixed(1);
    if (chunkSub) chunkSub.textContent = config.chunking_enabled ? '分片已启用 · 流式' + (config.streaming_enabled ? '开启' : '关闭') : '分片未启用';
    const minPos = clamp((minDelay / 5) * 100, 0, 100);
    const maxPos = clamp((maxDelay / 5) * 100, 0, 100);
    if (chunkRange) {
      chunkRange.style.left = minPos + '%';
      chunkRange.style.width = Math.max(2, maxPos - minPos) + '%';
    }
    if (chunkMinDot) chunkMinDot.style.left = minPos + '%';
    if (chunkMaxDot) chunkMaxDot.style.left = maxPos + '%';
    const wsUrl = asStr(config.napcat_ws_url, 'ws://127.0.0.1:3001');
    if (ws) ws.textContent = wsUrl;
    if (wsSub) wsSub.textContent = 'NapCat 反向 WebSocket';
    const qaSub = $('#qa-ws-sub');
    if (qaSub) qaSub.textContent = wsUrl;
  }

  function renderOverviewConfig() {
    const render = app.savedConfig || (app.configLoaded ? snapshotConfig() : null);
    if (!render) return;
    const setText = (selector, value) => {
      const node = $(selector);
      if (node) node.textContent = value;
    };
    setText('#gv-model', asStr(render.model, '—') || '—');
    setText('#gv-persona', asStr(render.persona, '—') || '—');
    setText('#gv-group', render.respond_to_groups ? '已开启' : '仅私聊');
    setText('#gv-allow', render.allowed_users.length ? render.allowed_users.length + ' 个账号' : '不限制');
    setText('#gv-admin', render.admin_users.length ? render.admin_users.length + ' 个账号' : '未设置');
    let output = '整段发送';
    if (render.streaming_enabled && render.chunking_enabled) output = '流式 + 分片';
    else if (render.streaming_enabled) output = '仅流式';
    else if (render.chunking_enabled) output = '仅分片';
    setText('#gv-output', output);
  }

  function createMiniLogRow(line, animate) {
    const row = el('div', 'mini-log');
    if (animate) row.classList.add('is-new');
    row.title = formatLogTime(line.ts) + ' · ' + line.level + ' · ' + line.text;
    const time = el('span', 'mini-log-time', formatLogTime(line.ts));
    const level = el('span', 'mini-log-level', line.level);
    level.dataset.level = line.level;
    const text = el('span', 'mini-log-text', line.text);
    row.append(time, level, text);
    if (animate) row.addEventListener('animationend', () => row.classList.remove('is-new'), { once: true });
    return row;
  }

  function renderMiniLogs(animateLast) {
    const list = $('#mini-log-list');
    if (!list) return;
    const items = app.logs.slice(-6);
    list.textContent = '';
    const empty = $('#mini-log-empty');
    if (empty) empty.hidden = items.length > 0;
    items.forEach((line, index) => {
      list.appendChild(createMiniLogRow(line, !!animateLast && index === items.length - 1));
    });
  }

  async function botAction(kind) {
    if (app.botBusy || app.offline) return;
    app.botBusy = true;
    app.action = kind === 'stop' ? 'stopping' : 'starting';
    app.actionAt = Date.now();
    renderHero();
    try {
      let response;
      if (kind === 'start') response = await api('/api/bot/start', { method: 'POST', timeout: 15000 });
      else if (kind === 'stop') response = await api('/api/bot/stop', { method: 'POST', timeout: 15000 });
      else response = await api('/api/bot/restart', { method: 'POST', timeout: 15000 });
      if (kind === 'start') {
        if (response.already) toast({ type: 'info', title: 'Bot 已在运行' });
        else toast({ type: 'success', title: '启动指令已发送', message: response.pid ? 'PID ' + response.pid : '正在等待进程就绪…' });
      } else if (kind === 'stop') {
        toast({ type: 'success', title: '停止指令已发送', message: '正在优雅退出 Bot 进程…' });
      } else {
        toast({ type: 'success', title: '重启指令已发送', message: response.pid ? 'PID ' + response.pid : '正在重启…' });
      }
      setTimeout(() => refreshState({ poll: true }), 600);
      setTimeout(() => refreshState({ poll: true }), 1900);
      setTimeout(() => {
        if (app.action) {
          app.action = null;
          renderHero();
        }
      }, 8000);
    } catch (error) {
      app.action = null;
      toastError(error, kind === 'start' ? '启动 Bot 失败' : kind === 'stop' ? '停止 Bot 失败' : '重启 Bot 失败');
    } finally {
      app.botBusy = false;
      renderHero();
    }
  }

  function initOverview() {
    const toggle = $('#btn-bot-toggle');
    if (toggle) {
      toggle.addEventListener('click', () => {
        const running = !!(app.state && app.state.bot && app.state.bot.running);
        botAction(running ? 'stop' : 'start');
      });
    }
    const restart = $('#btn-bot-restart');
    if (restart) restart.addEventListener('click', () => botAction('restart'));
    const openLogs = $('#btn-open-logs');
    if (openLogs) openLogs.addEventListener('click', () => navigate('logs'));
    const autoRestart = $('#ov-auto-restart');
    if (autoRestart) {
      autoRestart.addEventListener('click', () => {
        navigate('settings');
        setTimeout(() => {
          const section = document.getElementById('sec-console');
          if (section) section.scrollIntoView({ behavior: REDUCED.matches ? 'auto' : 'smooth', block: 'start' });
        }, 120);
      });
    }
    const copyWs = $('#btn-copy-ws');
    if (copyWs) copyWs.addEventListener('click', () => copyText(currentWsUrl(), 'WebSocket 地址已复制'));
    const gotoSettings = $('#btn-goto-settings');
    if (gotoSettings) gotoSettings.addEventListener('click', () => navigate('settings'));
  }

  function currentWsUrl() {
    return (app.savedConfig && app.savedConfig.napcat_ws_url) || app.form.napcat_ws_url || 'ws://127.0.0.1:3001';
  }

  /* ---------------- 复制 / 打开 ---------------- */
  async function copyText(text, message) {
    if (!text) {
      toast({ type: 'warn', title: '暂无可复制内容' });
      return;
    }
    const fallbackCopy = () => {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      area.style.pointerEvents = 'none';
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (error) { ok = false; }
      area.remove();
      return ok;
    };
    try {
      let ok = false;
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          ok = true;
        } catch (error) {
          ok = fallbackCopy();
        }
      } else {
        ok = fallbackCopy();
      }
      if (!ok) throw new Error('copy failed');
      toast({ type: 'success', title: message || '已复制到剪贴板' });
    } catch (error) {
      toast({ type: 'error', title: '复制失败', message: '请手动选中文本后复制' });
    }
  }

  async function openTarget(target) {
    try {
      await api('/api/action/open', { method: 'POST', body: { target: target } });
      toast({ type: 'success', title: '已打开' + (OPEN_TARGET_NAMES[target] || '目标'), message: '如果系统未响应，请检查文件管理器' });
    } catch (error) {
      toastError(error, '打开失败');
    }
  }

  function initQuickActions() {
    document.addEventListener('click', (event) => {
      const copyTarget = event.target.closest('[data-copy]');
      if (copyTarget) {
        const node = document.querySelector(copyTarget.dataset.copy);
        copyText(node ? node.textContent.trim() : '', '已复制');
        return;
      }
      const copyTextBtn = event.target.closest('[data-copy-text]');
      if (copyTextBtn) {
        copyText(copyTextBtn.dataset.copyText, '命令已复制');
        return;
      }
      const action = event.target.closest('[data-action]');
      if (!action) return;
      const kind = action.dataset.action;
      if (kind === 'open') openTarget(action.dataset.target);
      else if (kind === 'napcat') toggleNapcat();
      else if (kind === 'copy-ws') copyText(currentWsUrl(), 'WebSocket 地址已复制');
    });
  }

  /* ---------------- 日志 ---------------- */
  function logMatches(line) {
    if (app.logFilter !== 'all' && line.level !== app.logFilter) return false;
    if (app.logQuery) {
      const query = app.logQuery.toLowerCase();
      return line.text.toLowerCase().indexOf(query) >= 0 || line.level.toLowerCase().indexOf(query) >= 0;
    }
    return true;
  }

  function appendHighlighted(container, text, query) {
    if (!query) {
      container.textContent = text;
      return;
    }
    const lowerText = text.toLowerCase();
    const lowerQuery = query.toLowerCase();
    let cursor = 0;
    let index = lowerText.indexOf(lowerQuery, cursor);
    while (index >= 0) {
      if (index > cursor) container.appendChild(document.createTextNode(text.slice(cursor, index)));
      const mark = el('mark', null, text.slice(index, index + query.length));
      container.appendChild(mark);
      cursor = index + query.length;
      index = lowerText.indexOf(lowerQuery, cursor);
    }
    if (cursor < text.length) container.appendChild(document.createTextNode(text.slice(cursor)));
  }

  function createLogRow(line, animate) {
    const row = el('div', 'log-row');
    row.dataset.level = line.level;
    if (animate) row.classList.add('is-new');
    row.appendChild(el('span', 'log-time', formatLogTime(line.ts)));
    const level = el('span', 'log-level', line.level);
    level.dataset.level = line.level;
    row.appendChild(level);
    const text = el('span', 'log-text');
    appendHighlighted(text, line.text, app.logQuery);
    row.appendChild(text);
    if (animate) row.addEventListener('animationend', () => row.classList.remove('is-new'), { once: true });
    return row;
  }

  function renderLogList() {
    const list = $('#log-list');
    if (!list) return;
    const visible = app.logs.filter(logMatches).slice(-MAX_LOG_ROWS);
    const fragment = document.createDocumentFragment();
    visible.forEach((line) => fragment.appendChild(createLogRow(line, false)));
    list.textContent = '';
    list.appendChild(fragment);
    app.loggerRendered = true;
    app.logNewCount = 0;
    updateLogStats(visible.length);
    updatePausedState();
    if (app.autoScroll) scrollLogsToBottom(false);
  }

  function updateLogStats(visibleCount) {
    const shown = typeof visibleCount === 'number' ? visibleCount : app.logs.filter(logMatches).length;
    const stats = $('#log-stats');
    if (stats) stats.textContent = '共 ' + app.logs.length + ' 条 · 显示 ' + shown + ' 条';
    const cardNode = $('.log-card');
    if (cardNode) cardNode.classList.toggle('is-empty', shown === 0);
    const emptyTitle = $('#log-empty h3');
    const emptyDesc = $('#log-empty p');
    if (emptyTitle) emptyTitle.textContent = app.logs.length === 0 ? '暂无日志' : '没有匹配的日志';
    if (emptyDesc) {
      emptyDesc.textContent = app.logs.length === 0
        ? 'Bot 启动后，运行日志会在这里实时滚动显示。也可以切换级别或清空搜索条件。'
        : '试试更换日志级别，或清空搜索关键字。';
    }
  }

  function updatePausedState() {
    const paused = $('#log-paused');
    if (paused) paused.hidden = app.autoScroll;
    const pill = $('#log-new-pill');
    const count = $('#log-new-count');
    if (pill) {
      pill.hidden = app.autoScroll || app.logNewCount === 0;
      if (count) count.textContent = String(app.logNewCount);
    }
  }

  function setAutoScroll(on) {
    app.autoScroll = on;
    setSwitch($('#log-autoscroll'), on);
    if (on) {
      app.logNewCount = 0;
      scrollLogsToBottom(false);
    }
    updatePausedState();
  }

  function scrollLogsToBottom(smooth) {
    const list = $('#log-list');
    if (!list) return;
    app.scrollLock = true;
    list.scrollTo({ top: list.scrollHeight, behavior: smooth && !REDUCED.matches ? 'smooth' : 'auto' });
    setTimeout(() => { app.scrollLock = false; }, smooth ? 500 : 80);
  }

  function appendLog(line) {
    const normalized = normalizeLog(line);
    app.logs.push(normalized);
    if (app.logs.length > MAX_LOG_ROWS) app.logs.splice(0, app.logs.length - MAX_LOG_ROWS);
    renderMiniLogs(true);
    const list = $('#log-list');
    if (app.route === 'logs' && list) {
      if (logMatches(normalized)) {
        list.appendChild(createLogRow(normalized, true));
        while (list.children.length > MAX_LOG_ROWS) list.removeChild(list.firstElementChild);
        updateLogStats();
        if (app.autoScroll) {
          scrollLogsToBottom(false);
        } else {
          app.logNewCount += 1;
          updatePausedState();
        }
      } else {
        updateLogStats();
      }
    }
  }

  function setLogs(lines) {
    app.logs = (Array.isArray(lines) ? lines : []).map(normalizeLog).slice(-MAX_LOG_ROWS);
    app.logsLoaded = true;
    renderMiniLogs(false);
    if (app.route === 'logs') renderLogList();
    else updateLogStats();
  }

  async function loadLogsInitial() {
    try {
      const response = await api('/api/logs?tail=300&level=all');
      setLogs(response.lines);
    } catch (error) {
      app.logsLoaded = true;
      toast({ type: 'warn', title: '日志加载失败', message: error.message });
    }
  }

  function connectLogStream() {
    if (app.es) {
      app.es.close();
      app.es = null;
    }
    if (DEMO) {
      setSseState('demo');
      startDemoLogStream();
      return;
    }
    if (app.offline || typeof EventSource === 'undefined') {
      setSseState('offline');
      return;
    }
    setSseState('reconnecting');
    try {
      const source = new EventSource(API_BASE + '/api/logs/stream');
      app.es = source;
      source.onopen = () => setSseState('live');
      source.onmessage = (event) => {
        let payload = null;
        try { payload = JSON.parse(event.data); } catch (error) { return; }
        if (!payload || typeof payload !== 'object') return;
        if (payload.type === 'snapshot' && Array.isArray(payload.lines)) {
          setLogs(payload.lines);
          setSseState('live');
        } else if (payload.type === 'append' && payload.line) {
          appendLog(payload.line);
          setSseState('live');
        }
      };
      source.onerror = () => setSseState('reconnecting');
    } catch (error) {
      setSseState('offline');
    }
  }

  function startDemoLogStream() {
    if (app.timers.demoLogs) return;
    app.timers.demoLogs = window.setInterval(() => {
      if (document.hidden) return;
      const item = DEMO_LOG_POOL[Math.floor(Math.random() * DEMO_LOG_POOL.length)];
      appendLog({ ts: nowSec(), level: item.level, text: item.text, source: item.source });
    }, 2600);
  }

  async function clearLogs() {
    try {
      await api('/api/logs/clear', { method: 'POST' });
      app.logs = [];
      if (DEMO) demo.logs = [];
      renderLogList();
      renderMiniLogs(false);
      toast({ type: 'success', title: '日志已清空' });
    } catch (error) {
      toastError(error, '清空日志失败');
    }
  }

  function downloadLogs() {
    if (DEMO) {
      const text = app.logs.map((line) => `${formatLogTime(line.ts)} [${line.level}] ${line.text}`).join('\n');
      const blob = new Blob([text + '\n'], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = 'bridge-logs-' + Date.now() + '.log';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast({ type: 'success', title: '日志已导出', message: '演示模式：已生成当前日志快照' });
      return;
    }
    const anchor = document.createElement('a');
    anchor.href = API_BASE + '/api/logs/download';
    anchor.download = 'bridge-logs-' + Date.now() + '.log';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    toast({ type: 'success', title: '开始下载日志' });
  }

  function initLogs() {
    const levels = $('#log-levels');
    if (levels) {
      levels.addEventListener('click', (event) => {
        const button = event.target.closest('.level-btn');
        if (!button) return;
        $$('.level-btn', levels).forEach((item) => item.classList.toggle('is-active', item === button));
        app.logFilter = button.dataset.level;
        renderLogList();
      });
    }
    const search = $('#log-search');
    if (search) {
      const apply = debounce(() => {
        app.logQuery = search.value.trim();
        renderLogList();
      }, 140);
      search.addEventListener('input', apply);
    }
    const autoScroll = $('#log-autoscroll');
    if (autoScroll) autoScroll.addEventListener('click', () => setAutoScroll(!app.autoScroll));
    const list = $('#log-list');
    if (list) {
      list.addEventListener('scroll', () => {
        if (app.scrollLock) return;
        const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
        if (nearBottom && !app.autoScroll) setAutoScroll(true);
        else if (!nearBottom && app.autoScroll) setAutoScroll(false);
      }, { passive: true });
    }
    const pill = $('#log-new-pill');
    if (pill) pill.addEventListener('click', () => setAutoScroll(true));
    const resume = $('#btn-resume-scroll');
    if (resume) resume.addEventListener('click', () => setAutoScroll(true));
    const clear = $('#btn-log-clear');
    if (clear) {
      clear.addEventListener('click', () => {
        openConfirm({
          title: '清空全部日志？',
          message: '将清空管理台内存中的日志缓冲。已写入磁盘的日志文件不受影响。',
          confirmText: '清空日志',
          danger: true,
          onConfirm: clearLogs
        });
      });
    }
    const download = $('#btn-log-download');
    if (download) download.addEventListener('click', downloadLogs);
  }

  /* ---------------- 环境自检 ---------------- */
  async function loadEnv(force) {
    if (app.envLoading) return;
    if (app.env && !force) {
      renderEnv();
      return;
    }
    app.envLoading = true;
    setEnvLoading(true);
    try {
      const response = await api('/api/env?refresh=1', { timeout: 30000 });
      app.env = Array.isArray(response.checks) ? response.checks : [];
      app.envAt = new Date();
      renderEnv();
    } catch (error) {
      renderEnvError(error);
    } finally {
      app.envLoading = false;
      setEnvLoading(false);
    }
  }

  function setEnvLoading(loading) {
    const button = $('#btn-env-refresh');
    if (button) {
      button.classList.toggle('is-loading', loading);
      button.disabled = loading;
    }
    const grid = $('#env-grid');
    const skeleton = $('#env-loading');
    if (skeleton) skeleton.hidden = !loading;
    if (grid) grid.style.display = loading ? 'none' : '';
    if (loading) {
      const title = $('#env-title');
      const subtitle = $('#env-subtitle');
      if (title) title.textContent = '正在检测运行环境…';
      if (subtitle) subtitle.textContent = '正在检查 Python、依赖、端口、NapCat 与配置状态';
    }
  }

  function envCounts(checks) {
    return checks.reduce((acc, check) => {
      if (check.status === 'ok') acc.ok += 1;
      else if (check.status === 'warn') acc.warn += 1;
      else acc.fail += 1;
      return acc;
    }, { ok: 0, warn: 0, fail: 0 });
  }

  function renderEnv() {
    loadNapcatUpdate();
    const grid = $('#env-grid');
    if (!grid) return;
    const checks = Array.isArray(app.env) ? app.env : [];
    const counts = envCounts(checks);
    const total = checks.length;
    const score = counts.ok + counts.warn * 0.5;
    const ratio = total ? score / total : 0;
    const ring = $('#env-ring');
    const ringFill = $('#env-ring-fill');
    const ringText = $('#env-ring-text');
    if (ring) ring.dataset.tone = counts.fail ? 'danger' : counts.warn ? 'warn' : 'ok';
    if (ringFill) ringFill.style.strokeDashoffset = String(188.5 * (1 - ratio));
    if (ringText) ringText.textContent = counts.ok + '/' + total;
    const title = $('#env-title');
    const subtitle = $('#env-subtitle');
    if (title) {
      if (!total) title.textContent = '暂无自检结果';
      else if (counts.fail) title.textContent = '检测到 ' + counts.fail + ' 项需要处理的问题';
      else if (counts.warn) title.textContent = '环境基本可用，有 ' + counts.warn + ' 项提醒';
      else title.textContent = '环境就绪，一切正常';
    }
    if (subtitle) {
      subtitle.textContent = total
        ? '通过 ' + counts.ok + ' 项 · 提醒 ' + counts.warn + ' 项 · 失败 ' + counts.fail + ' 项'
        : '点击「重新检测」重新检查运行环境';
    }
    const updated = $('#env-updated');
    if (updated) updated.textContent = app.envAt ? '更新于 ' + formatFullTime() : '';

    grid.textContent = '';
    if (!checks.length) {
      const empty = el('div', 'card env-card');
      empty.appendChild(el('h3', null, '暂无检查项'));
      empty.appendChild(el('p', 'env-card-detail', '管理台未返回自检结果，请稍后重试。'));
      grid.appendChild(empty);
      return;
    }
    checks.forEach((check) => {
      const status = check.status === 'warn' || check.status === 'fail' ? check.status : 'ok';
      const card = el('article', 'card env-card');
      card.dataset.status = status;

      const top = el('div', 'env-card-top');
      const statusIcon = el('span', 'env-status-icon');
      const statusIconName = status === 'ok' ? 'check' : status === 'warn' ? 'alert-circle' : 'x';
      statusIcon.innerHTML = iconMarkup(statusIconName);
      const textWrap = el('div', 'env-card-text');
      const titleRow = el('div', 'env-card-title-row');
      const checkIcon = el('span', 'env-check-icon');
      checkIcon.innerHTML = iconMarkup(ENV_ICONS[check.id] || 'shield');
      checkIcon.style.color = 'var(--muted)';
      checkIcon.style.display = 'inline-flex';
      titleRow.append(checkIcon, el('h3', null, asStr(check.title, check.id)));
      const badge = el('span', 'chip');
      badge.dataset.tone = status === 'ok' ? 'ok' : status === 'warn' ? 'warn' : 'danger';
      badge.textContent = ENV_STATUS_TEXT[status];
      titleRow.appendChild(badge);
      const detail = el('p', 'env-card-detail', asStr(check.detail, ''));
      textWrap.append(titleRow, detail);
      top.append(statusIcon, textWrap);
      card.appendChild(top);

      if (status !== 'ok' && check.hint) {
        const hint = el('div', 'env-hint');
        const hintIcon = el('span');
        hintIcon.innerHTML = iconMarkup('alert-triangle');
        hintIcon.style.display = 'inline-flex';
        hintIcon.style.marginTop = '2px';
        hint.appendChild(hintIcon);
        hint.appendChild(el('span', null, asStr(check.hint, '')));
        card.appendChild(hint);
      }
      const foot = el('div', 'env-card-foot');
      foot.appendChild(el('span', 'eyebrow', asStr(check.id, '')));
      card.appendChild(foot);
      grid.appendChild(card);
    });
  }

  function renderEnvError(error) {
    const grid = $('#env-grid');
    if (!grid) return;
    grid.textContent = '';
    const card = el('article', 'card env-card');
    card.dataset.status = 'fail';
    const top = el('div', 'env-card-top');
    const statusIcon = el('span', 'env-status-icon');
    statusIcon.innerHTML = iconMarkup('x');
    const textWrap = el('div', 'env-card-text');
    textWrap.appendChild(el('h3', null, '环境自检失败'));
    textWrap.appendChild(el('p', 'env-card-detail', error && error.message ? error.message : '无法获取自检结果'));
    top.append(statusIcon, textWrap);
    card.appendChild(top);
    const foot = el('div', 'env-card-foot');
    foot.appendChild(el('span', 'eyebrow', '点击右上角「重新检测」重试'));
    card.appendChild(foot);
    grid.appendChild(card);
    const title = $('#env-title');
    if (title) title.textContent = '环境自检失败';
    const subtitle = $('#env-subtitle');
    if (subtitle) subtitle.textContent = error && error.message ? error.message : '请检查管理台连接';
  }

  function initEnv() {
    const refresh = $('#btn-env-refresh');
    if (refresh) refresh.addEventListener('click', () => loadEnv(true));
    const checkBtn = $('#btn-napcat-check');
    if (checkBtn) {
      checkBtn.addEventListener('click', () => {
        app.napcatUpdate = null;
        loadNapcatUpdate(true);
      });
    }
    const updateBtn = $('#btn-napcat-update');
    if (updateBtn) updateBtn.addEventListener('click', startNapcatUpdate);
  }

  /* ---------------- NapCat 更新 ---------------- */
  async function loadNapcatUpdate(force) {
    if (app.napcatUpdateLoading) return;
    if (app.napcatUpdate && !force) {
      renderNapcatUpdate();
      return;
    }
    app.napcatUpdateLoading = true;
    try {
      const response = await api('/api/napcat/update/status' + (force ? '?refresh=1' : ''), { timeout: 25000 });
      app.napcatUpdate = response;
      renderNapcatUpdate();
      if (response.state && response.state.running) scheduleNapcatPoll();
    } catch (error) {
      const chip = $('#napcat-update-chip');
      if (chip) {
        chip.textContent = '检查失败';
        chip.dataset.tone = 'danger';
      }
    } finally {
      app.napcatUpdateLoading = false;
    }
  }

  function scheduleNapcatPoll() {
    clearTimeout(app.napcatUpdateTimer);
    app.napcatUpdateTimer = setTimeout(async () => {
      try {
        const response = await api('/api/napcat/update/status', { timeout: 15000 });
        app.napcatUpdate = response;
        renderNapcatUpdate();
        if (response.state && response.state.running) scheduleNapcatPoll();
      } catch (error) {
        /* 连接异常时停止轮询 */
      }
    }, 1200);
  }

  function napcatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value >= 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MB';
    if (value >= 1024) return (value / 1024).toFixed(0) + ' KB';
    return value + ' B';
  }

  function napcatLocalText(local) {
    if (!local || !local.installed) return '未检测到框架';
    if (local.tag) return local.tag;
    if (local.mtime) {
      try {
        return '已安装 · ' + new Date(local.mtime * 1000).toLocaleDateString('zh-CN');
      } catch (error) { /* ignore */ }
    }
    return '已安装（版本未知）';
  }

  function renderNapcatUpdate() {
    const data = app.napcatUpdate;
    if (!data) return;
    const check = data.check || {};
    const state = data.state || {};
    const chip = $('#napcat-update-chip');
    const localNode = $('#napcat-local');
    const latestNode = $('#napcat-latest');
    const mirrorNode = $('#napcat-mirror');
    const checkBtn = $('#btn-napcat-check');
    const updateBtn = $('#btn-napcat-update');
    const progress = $('#napcat-progress');
    const fill = $('#napcat-progress-fill');
    const progressText = $('#napcat-progress-text');
    const messageNode = $('#napcat-update-msg');

    if (localNode) localNode.textContent = napcatLocalText(check.local);
    if (latestNode) latestNode.textContent = check.latest_tag || (check.error ? '获取失败' : '—');
    if (mirrorNode && document.activeElement !== mirrorNode) mirrorNode.value = data.mirror || '';

    if (chip) {
      let tone = 'neutral';
      let text = '检查中…';
      if (state.running) {
        tone = 'warn';
        text = '更新中';
      } else if (state.stage === 'done') {
        tone = 'ok';
        text = '已更新';
      } else if (state.stage === 'error') {
        tone = 'danger';
        text = '更新失败';
      } else if (check.error) {
        tone = 'danger';
        text = '检查失败';
      } else if (check.update_available) {
        tone = 'warn';
        text = '有新版本';
      } else if (check.latest_tag) {
        tone = 'ok';
        text = '已是最新';
      }
      chip.textContent = text;
      chip.dataset.tone = tone;
    }

    const busy = !!state.running;
    if (checkBtn) {
      checkBtn.disabled = busy;
      checkBtn.classList.toggle('is-loading', busy);
    }
    if (updateBtn) updateBtn.disabled = busy || !check.update_available;

    if (progress && fill && progressText) {
      const visible = busy || state.stage === 'done' || state.stage === 'error';
      progress.hidden = !visible;
      const percent = busy
        ? Math.max(0, Math.min(100, Number(state.percent) || 0))
        : (state.stage === 'done' ? 100 : 0);
      fill.style.width = percent + '%';
      if (busy) {
        const size = state.total
          ? '（' + napcatBytes(state.downloaded) + ' / ' + napcatBytes(state.total) + '，' + percent + '%）'
          : '';
        progressText.textContent = (state.message || '更新中…') + size;
      } else {
        progressText.textContent = state.message || '';
      }
    }

    if (messageNode) {
      if (state.error) {
        messageNode.hidden = false;
        messageNode.textContent = '失败：' + state.error;
      } else if (state.stage === 'done') {
        const backup = state.result && state.result.backup ? '，旧框架已备份到 ' + state.result.backup : '';
        messageNode.hidden = false;
        messageNode.textContent = '更新完成' + backup + '。请从管理台重新启动 NapCat。';
      } else {
        messageNode.hidden = true;
        messageNode.textContent = '';
      }
    }
  }

  async function startNapcatUpdate() {
    const button = $('#btn-napcat-update');
    if (!button || button.disabled) return;
    const mirror = $('#napcat-mirror') ? $('#napcat-mirror').value.trim() : '';
    button.disabled = true;
    try {
      await api('/api/napcat/update', { method: 'POST', body: { mirror: mirror }, timeout: 20000 });
      toast({ type: 'info', title: '开始更新 NapCat', message: '下载与安装可能需要几分钟，请勿关闭管理台。' });
      app.napcatUpdate = null;
      await loadNapcatUpdate(true);
      scheduleNapcatPoll();
    } catch (error) {
      toastError(error, '更新启动失败');
      renderNapcatUpdate();
    }
  }

  /* ---------------- 关于 ---------------- */
  function renderAbout() {
    const version = (app.ping && app.ping.version) || (app.state && app.state.version) || APP_VERSION;
    const appVersion = $('#about-version');
    const appVersionMeta = $('#about-app-version');
    const apiVersion = $('#about-api-version');
    const mode = $('#about-mode');
    const api = $('#about-api');
    const apiCode = $('#about-api-code');
    if (appVersion) appVersion.textContent = 'v' + version;
    if (appVersionMeta) appVersionMeta.textContent = APP_VERSION;
    if (apiVersion) apiVersion.textContent = version || '—';
    if (mode) {
      mode.textContent = DEMO ? '演示模式（本地模拟数据）' : app.offline ? '离线' : '本地管理台';
    }
    if (api) api.textContent = API_BASE.replace(/^https?:\/\//, '');
    if (apiCode) apiCode.textContent = API_BASE;
  }

  function initAbout() {
    const shutdown = $('#btn-shutdown');
    if (shutdown) {
      shutdown.addEventListener('click', () => {
        openConfirm({
          title: '退出管理台？',
          message: '管理台服务将停止，当前页面会断开连接；Bot 进程也会一并停止。下次可通过启动脚本重新打开。',
          confirmText: '退出管理台',
          danger: true,
          onConfirm: doShutdown
        });
      });
    }
    const reload = $('#btn-exit-reload');
    if (reload) {
      reload.addEventListener('click', () => location.reload());
    }
  }

  async function doShutdown() {
    let failed = null;
    try {
      await api('/api/shutdown', { method: 'POST', timeout: 6000 });
    } catch (error) {
      failed = error;
    }
    if (failed && !DEMO) {
      toastError(failed, '退出管理台失败');
      return;
    }
    stopAllTimers();
    if (app.es) {
      app.es.close();
      app.es = null;
    }
    const overlay = $('#exit-overlay');
    if (overlay) overlay.hidden = false;
  }

  /* ---------------- 键盘 / 环境交互 ---------------- */
  function initKeyboard() {
    document.addEventListener('keydown', (event) => {
      const target = event.target;
      const typing = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable);
      if (event.key === 'Escape') {
        const modal = $('#modal-root');
        if (modal && !modal.hidden) return;
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        if (app.route === 'settings' && app.dirty.length) {
          event.preventDefault();
          saveConfig();
        }
        return;
      }
      if (typing || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key >= '1' && event.key <= '5') {
        const route = ['overview', 'settings', 'logs', 'env', 'about'][Number(event.key) - 1];
        navigate(route);
      } else if (event.key.toLowerCase() === 't') {
        toggleTheme();
      } else if (event.key.toLowerCase() === 'r') {
        refreshAll();
      } else if (event.key === '/') {
        if (app.route === 'logs') {
          event.preventDefault();
          const search = $('#log-search');
          if (search) search.focus();
        }
      } else if (event.key === '?') {
        navigate('about');
      }
    });
  }

  async function refreshAll() {
    if (app.offline) {
      const ok = await connect({ initial: false, silent: false });
      if (!ok) toastError({ message: '仍无法连接管理台' }, '连接失败');
      return;
    }
    const button = $('#btn-refresh');
    if (button) button.classList.add('is-busy');
    try {
      await refreshState({ poll: false });
      if (!DEMO) await loadLogsInitial();
      if (app.route === 'env') await loadEnv(true);
      toast({ type: 'success', title: '状态已刷新' });
    } catch (error) {
      toastError(error, '刷新失败');
    } finally {
      if (button) button.classList.remove('is-busy');
    }
  }

  function initTopbar() {
    const refresh = $('#btn-refresh');
    if (refresh) refresh.addEventListener('click', refreshAll);
    const theme = $('#btn-theme');
    if (theme) theme.addEventListener('click', toggleTheme);
    $$('.theme-opt').forEach((button) => {
      button.addEventListener('click', () => applyTheme(button.dataset.setTheme, true));
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && app.conn === 'online' && !app.offline) refreshState({ poll: true });
    });
  }

  function initAmbient() {
    let glowRaf = 0;
    let glowEvent = null;
    document.addEventListener('pointermove', (event) => {
      if (REDUCED.matches) return;
      const card = event.target.closest && event.target.closest('[data-glow]');
      if (!card) return;
      glowEvent = { card: card, x: event.clientX, y: event.clientY };
      if (glowRaf) return;
      glowRaf = requestAnimationFrame(() => {
        glowRaf = 0;
        if (!glowEvent) return;
        const rect = glowEvent.card.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        glowEvent.card.style.setProperty('--mx', (((glowEvent.x - rect.left) / rect.width) * 100).toFixed(1) + '%');
        glowEvent.card.style.setProperty('--my', (((glowEvent.y - rect.top) / rect.height) * 100).toFixed(1) + '%');
        glowEvent = null;
      });
    }, { passive: true });

    document.addEventListener('click', (event) => {
      if (REDUCED.matches) return;
      const button = event.target.closest && event.target.closest('.btn, .qa, .anchor, .nav-item, .icon-btn');
      if (!button || button.disabled) return;
      const rect = button.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const size = Math.max(rect.width, rect.height) * 1.3;
      const ripple = el('span', 'ripple');
      ripple.style.width = size + 'px';
      ripple.style.height = size + 'px';
      ripple.style.left = (event.clientX - rect.left - size / 2) + 'px';
      ripple.style.top = (event.clientY - rect.top - size / 2) + 'px';
      button.appendChild(ripple);
      setTimeout(() => ripple.remove(), 640);
    });
  }

  function initOffline() {
    const retry = $('#btn-offline-retry');
    if (retry) {
      retry.addEventListener('click', async () => {
        if (retry.classList.contains('is-loading')) return;
        retry.classList.add('is-loading');
        retry.disabled = true;
        const ok = await connect({ initial: false, silent: false });
        if (!ok) toast({ type: 'error', title: '仍然无法连接', message: '请确认后端进程已启动，或检查端口是否一致。' });
        retry.classList.remove('is-loading');
        retry.disabled = false;
      });
    }
  }

  function startClock() {
    if (app.timers.clock) clearInterval(app.timers.clock);
    app.timers.clock = window.setInterval(() => {
      if (app.route === 'overview' && !document.hidden) renderUptime();
    }, 1000);
  }

  function stopAllTimers() {
    stopPolling();
    stopReconnectLoop();
    if (app.timers.clock) { clearInterval(app.timers.clock); app.timers.clock = 0; }
    if (app.timers.demoLogs) { clearInterval(app.timers.demoLogs); app.timers.demoLogs = 0; }
    clearTimeout(demo.startTimer);
    clearTimeout(demo.stopTimer);
  }

  /* ---------------- 全局错误兜底 ---------------- */
  const seenErrors = {};
  function recordError(message) {
    const text = message || '未知界面异常';
    if (seenErrors[text]) return;
    seenErrors[text] = true;
    const html = document.documentElement;
    html.dataset.errors = String(asInt(html.dataset.errors, 0) + 1);
    toast({ type: 'error', title: '界面出现异常', message: text, duration: 6000 });
  }

  window.addEventListener('error', (event) => {
    recordError(event && event.message ? event.message : '脚本执行错误');
  });
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event && event.reason;
    recordError(reason && reason.message ? reason.message : String(reason || '未处理的异步错误'));
  });

  /* ---------------- 启动 ---------------- */
  function bindAll() {
    initThemeFromDom();
    initNav();
    initTopbar();
    initOverview();
    initSettingsForms();
    initAnchors();
    initQuickActions();
    initLogs();
    initEnv();
    initAbout();
    initOffline();
    initAmbient();
    initKeyboard();
    setAutoScroll(true);
  }

  function initThemeFromDom() {
    $$('.theme-opt').forEach((button) => {
      button.setAttribute('aria-checked', String(button.dataset.setTheme === document.documentElement.dataset.theme));
    });
    const toggle = $('#btn-theme');
    if (toggle) {
      toggle.setAttribute('aria-label', document.documentElement.dataset.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题');
    }
  }

  async function boot() {
    initDemoWorld();
    document.documentElement.dataset.mode = DEMO ? 'demo' : 'live';
    document.body.dataset.demo = DEMO ? '1' : '0';
    initTheme();
    bindAll();
    renderRoute(routeFromHash(), { silentScroll: true });
    if (DEMO) {
      app.offline = false;
      setConn('demo');
      setSseState('demo');
      await loadAllData({ initial: true });
      startDemoLogStream();
      renderRoute(app.route, { force: true, silentScroll: true });
      document.documentElement.dataset.appReady = '1';
    } else {
      await connect({ initial: true });
      document.documentElement.dataset.appReady = '1';
    }
    startClock();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    document.body.classList.remove('preload');
    renderRoute(app.route, { force: true, silentScroll: true });
    setActiveNav(app.route);
    setTimeout(moveNavIndicator, 80);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
