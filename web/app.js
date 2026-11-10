/* A论 · 前端交互 */
(() => {
  'use strict';

  const app = document.getElementById('app');
  const toasts = document.getElementById('toasts');
  const API = '/api/v1';

  const AUTH = {
    token: localStorage.getItem('alun_token') || '',
    user: (() => {
      try { return JSON.parse(localStorage.getItem('alun_user') || 'null'); } catch { return null; }
    })(),
  };

  const state = {
    meta: null,
    home: null,
    doc: null,
    session: null,
    report: null,
    poll: null,
  };

  const S = {
    session: null,
    doc: null,
    turns: [],
    current: null,
    feedback: null,
    review: null,
    reviewReturn: null,
    hint: null,
    submitting: false,
    busy: false,
    questionShownAt: 0,
    outlineEdit: false,
    readerUrl: null,
  };

  const TYPE_LABEL = {
    research_paper: '研究论文',
    review: '综述',
    policy: '政策研究',
    news: '报纸报道',
    other: '其他文档',
  };
  const QTYPE_LABEL = {
    single_choice: '单选',
    multiple_choice: '多选',
    true_false: '判断',
    fill_blank: '填空',
    short_answer: '简答',
  };
  const MODE_LABEL = { guided: '导读模式', exam: '测评模式', defense: '答辩模式' };
  const STATUS_LABEL = {
    uploaded: '待校验', validating: '校验中', parsing: '解析中', indexing: '建索引',
    ready: '可提问', needs_review: '需校正', failed: '解析失败', deleted: '已删除',
  };
  const NODE_ICON = { section: '节', abstract: '摘', keywords: '词', figure: '图', chart: '图', table: '表', formula: '公', reference: '参', title: '题', bio: '人', author: '作' };

  // ------------------------------------------------------------------
  // helpers
  // ------------------------------------------------------------------
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pct = (n) => Math.max(0, Math.min(100, Number(n) || 0));
  const fmtDate = (s) => {
    if (!s) return '';
    try { return new Date(s).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); } catch { return s; }
  };
  const fmtNum = (n) => Number(n || 0).toLocaleString('zh-CN');
  const docType = (t) => TYPE_LABEL[t] || '文档';
  const statusTag = (s) => {
    const cls = s === 'ready' ? 'tag-jade' : s === 'needs_review' ? 'tag-gold' : s === 'failed' ? 'tag-red' : 'tag-line';
    return `<span class="tag ${cls} tag-dot">${esc(STATUS_LABEL[s] || s)}</span>`;
  };
  const scoreClass = (n) => (n >= 80 ? 'tag-jade' : n >= 60 ? 'tag-gold' : 'tag-red');

  async function api(path, options = {}) {
    const opts = { ...options, headers: { ...(options.headers || {}) } };
    if (AUTH.token) opts.headers['Authorization'] = 'Bearer ' + AUTH.token;
    if (AUTH.user?.tenant_id) opts.headers['X-Tenant-Id'] = AUTH.user.tenant_id;
    if (opts.body && !(opts.body instanceof FormData)) {
      opts.headers['Content-Type'] = 'application/json';
      if (typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(API + path, opts);
    if (res.status === 204) return null;
    let data = null;
    try { data = await res.json(); } catch { /* ignore */ }
    if (res.status === 401 && !path.startsWith('/auth/')) {
      logout(false);
      go('#/login');
    }
    if (!res.ok) {
      const err = new Error((data && data.message) || `请求失败（${res.status}）`);
      Object.assign(err, data || {});
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function toast(msg, isError = false) {
    const el = document.createElement('div');
    el.className = 'toast' + (isError ? ' err' : '');
    el.textContent = msg;
    toasts.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(6px)'; }, 3200);
    setTimeout(() => el.remove(), 3600);
  }

  function go(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  }

  const icon = (name, size = 16) => {
    const paths = {
      upload: '<path d="M12 16V4m0 0L7 9m5-5l5 5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
      arrow: '<path d="M5 12h14m-6-6l6 6-6 6"/>',
      back: '<path d="M19 12H5m6 6l-6-6 6-6"/>',
      doc: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
      spark: '<path d="M12 2l1.9 6.1L20 10l-6.1 1.9L12 18l-1.9-6.1L4 10l6.1-1.9z"/>',
      check: '<path d="M20 6L9 17l-5-5"/>',
      x: '<path d="M18 6L6 18M6 6l12 12"/>',
      clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
      book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
      leaf: '<path d="M11 20A7 7 0 0 1 4 13c0-5 4-9 16-9 0 12-4 16-9 16z"/><path d="M4 21c2-7 7-11 14-13"/>',
    };
    return `<svg class="icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[name] || ''}</svg>`;
  };

  function logout(redirect = true) {
    if (AUTH.token) {
      fetch(API + '/auth/logout', { method: 'POST', headers: { Authorization: 'Bearer ' + AUTH.token } }).catch(() => {});
    }
    AUTH.token = '';
    AUTH.user = null;
    localStorage.removeItem('alun_token');
    localStorage.removeItem('alun_user');
    if (redirect) go('#/login');
  }

  function saveAuth(data) {
    AUTH.token = data.access_token;
    AUTH.user = data.user;
    localStorage.setItem('alun_token', data.access_token);
    localStorage.setItem('alun_user', JSON.stringify(data.user));
  }

  async function fetchMe() {
    if (!AUTH.token) return null;
    try {
      AUTH.user = await api('/auth/me');
      localStorage.setItem('alun_user', JSON.stringify(AUTH.user));
      return AUTH.user;
    } catch {
      logout(false);
      return null;
    }
  }

  function loginPage(mode = 'login', message = '') {
    const isRegister = mode === 'register';
    app.innerHTML = `
      <div class="login-page">
        <div class="login-brand-side">
          <a class="brand" href="#/login">
            <span class="brand-seal">论</span>
            <span class="brand-name"><b>A论</b><span>论文导读智能体</span></span>
          </a>
          <div class="login-quote">
            <div class="eyebrow">DOCUMENT-GROUNDED QUESTIONING</div>
            <h1>先读懂论文的<br/><em>结构</em>，再开口提问。</h1>
            <p>每一道题都挂在章节、图表与结论上；每一条反馈都能回到原文页码。A论不替你读，而是陪你把论文读进去。</p>
          </div>
          <div class="login-meta">
            <span>结构树</span><i></i><span>证据约束</span><i></i><span>Elo 自适应</span>
          </div>
        </div>
        <div class="login-form-side">
          <div class="login-card">
            <div class="login-card-head">
              <span class="tag tag-red">${isRegister ? '创建账号' : '用户登录'}</span>
              <span class="small muted">A论 · A-Lun</span>
            </div>
            <h2>${isRegister ? '开始你的第一轮导读' : '欢迎回来'}</h2>
            <p class="muted small">${isRegister ? '注册后即可上传论文、开始提问并保存学习报告。' : '登录后继续你的论文导读与学习报告。'}</p>
            ${message ? `<div class="alert" style="margin:16px 0">${esc(message)}</div>` : ''}
            <form id="login-form" class="login-form">
              ${isRegister ? `
                <div class="field"><label for="display-name">显示名称</label><input class="text-input" id="display-name" placeholder="例如：小林" autocomplete="nickname" maxlength="32" /></div>
                <div class="field"><label for="login-username">用户名 <span class="field-optional">必填</span></label><input class="text-input" id="login-username" placeholder="3—32 位字母、数字、下划线或中文" autocomplete="username" required /></div>
                <div class="register-grid">
                  <div class="field"><label for="register-email">邮箱 <span class="field-optional">选填</span></label><input class="text-input" id="register-email" type="email" placeholder="name@example.com" autocomplete="email" /></div>
                  <div class="field"><label for="register-phone">手机号 <span class="field-optional">选填</span></label><input class="text-input" id="register-phone" type="tel" placeholder="用于账号找回" autocomplete="tel" /></div>
                </div>
              ` : `
                <div class="field"><label for="login-username">用户名</label><input class="text-input" id="login-username" placeholder="请输入用户名" autocomplete="username" required /></div>
              `}
              <div class="field"><label for="login-password">密码</label><input class="text-input" id="login-password" type="password" placeholder="${isRegister ? '至少 8 位，包含字母和数字' : '请输入密码'}" autocomplete="${isRegister ? 'new-password' : 'current-password'}" required /></div>
              ${isRegister ? `
                <div class="field"><label for="register-password2">确认密码</label><input class="text-input" id="register-password2" type="password" placeholder="再次输入密码" autocomplete="new-password" required /></div>
                <div class="password-meter" id="password-meter"><i></i><i></i><i></i><span>密码强度</span></div>
                <label class="register-consent"><input type="checkbox" id="register-consent" required /> 我确认账号信息真实，并同意本地保存账号与学习记录</label>
              ` : ''}
              <button class="btn btn-accent btn-block btn-lg" id="login-submit" type="submit">${isRegister ? '创建账号并登录' : '登录'} ${icon('arrow', 15)}</button>
            </form>
            <div class="login-switch">
              ${isRegister
                ? `已有账号？<a href="#/login">返回登录</a>`
                : `还没有账号？<a href="#/register">注册一个</a>`}
              <span class="divider">·</span>
              <a href="/admin">管理后台</a>
            </div>
            <div class="login-demo">
              <b>演示账号</b>
              <span>用户：<code>demo</code> / <code>demo123</code></span>
              <span>管理员：<code>admin</code> / <code>admin123</code></span>
            </div>
          </div>
          <p class="login-foot">A论 · 本地部署 · 账号密码只保存在本机数据库</p>
        </div>
      </div>`;
    const form = document.getElementById('login-form');
    if (isRegister) {
      const pwd = document.getElementById('login-password');
      const meter = document.getElementById('password-meter');
      const updateStrength = () => {
        const value = pwd?.value || '';
        let score = 0;
        if (value.length >= 8) score++;
        if (/[A-Za-z]/.test(value) && /\d/.test(value)) score++;
        if (/[^A-Za-z0-9]/.test(value) || value.length >= 12) score++;
        meter?.querySelectorAll('i').forEach((bar, i) => {
          bar.classList.toggle('on', i < score);
          bar.classList.toggle('strong', score >= 3 && i < score);
        });
        const label = meter?.querySelector('span');
        if (label) label.textContent = value ? (score >= 3 ? '强' : score >= 2 ? '中' : '弱') : '密码强度';
      };
      pwd?.addEventListener('input', updateStrength);
      updateStrength();
    }
    form?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const username = document.getElementById('login-username').value.trim();
      const password = document.getElementById('login-password').value;
      const btn = document.getElementById('login-submit');
      if (!username || !password) return;
      const showError = (msg) => {
        const box = document.querySelector('.login-card .alert');
        if (box) box.textContent = msg;
        else form.insertAdjacentHTML('beforebegin', `<div class="alert" style="margin:16px 0">${esc(msg)}</div>`);
      };
      if (isRegister) {
        const email = document.getElementById('register-email').value.trim();
        const phone = document.getElementById('register-phone').value.trim();
        const password2 = document.getElementById('register-password2').value;
        if (password.length < 8 || !/[A-Za-z]/.test(password) || !/\d/.test(password)) {
          showError('密码至少 8 位，且必须同时包含字母和数字'); return;
        }
        if (password !== password2) { showError('两次输入的密码不一致'); return; }
        if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { showError('邮箱格式不正确'); return; }
        if (phone && !/^\+?[0-9][0-9\- ]{5,19}$/.test(phone)) { showError('手机号格式不正确'); return; }
        if (!document.getElementById('register-consent').checked) { showError('请先确认账号信息与本地存储说明'); return; }
      }
      btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> 正在验证';
      try {
        const body = isRegister
          ? {
              username, password,
              display_name: document.getElementById('display-name')?.value || username,
              email: document.getElementById('register-email')?.value || '',
              phone: document.getElementById('register-phone')?.value || '',
            }
          : { username, password };
        const data = await api(isRegister ? '/auth/register' : '/auth/login', { method: 'POST', body });
        saveAuth(data);
        toast(isRegister ? '账号已创建，欢迎使用 A论' : `欢迎回来，${data.user.display_name || data.user.username}`);
        await loadMeta();
        go('#/');
      } catch (err) {
        btn.disabled = false;
        btn.innerHTML = `${isRegister ? '创建账号并登录' : '登录'} ${icon('arrow', 15)}`;
        showError(err.message);
      }
    });
  }

  function shell(content, active = '') {
    const meta = state.meta || {};
    const llmOn = meta.llm && meta.llm.enabled;
    app.innerHTML = `
      <header class="topbar">
        <div class="topbar-inner">
          <a class="brand" href="#/">
            <span class="brand-seal">论</span>
            <span class="brand-name"><b>A论</b><span>论文导读智能体</span></span>
          </a>
          <nav class="nav">
            <a href="#/" class="${active === 'home' ? 'active' : ''}">文档库</a>
            <a href="#/sessions" class="${active === 'sessions' ? 'active' : ''}">我的会话</a>
            <a href="#/about" class="${active === 'about' ? 'active' : ''}">关于 A论</a>
          </nav>
          <div class="topbar-right">
            <button class="btn btn-ghost btn-sm" id="ai-top-btn" type="button">${icon('spark', 14)} A论 AI</button>
            <div class="model-state"><i class="dot ${llmOn ? 'on' : ''}"></i>${llmOn ? '导读模型已就绪' : '规则模式'}</div>
            ${AUTH.user?.role === 'admin' ? `<a class="btn btn-ghost btn-sm" href="/admin">管理后台</a>` : ''}
            <a class="btn btn-accent btn-sm" href="#/">${icon('upload', 14)} 上传论文</a>
            <div class="account-chip" title="${esc(AUTH.user?.username || '')}">
              <span class="account-avatar">${esc((AUTH.user?.display_name || AUTH.user?.username || 'U').slice(0, 1))}</span>
              <span class="account-name">${esc(AUTH.user?.display_name || AUTH.user?.username || '用户')}</span>
              <button class="btn btn-quiet btn-sm" id="logout-btn" title="退出登录">${icon('x', 13)}</button>
            </div>
          </div>
        </div>
      </header>
      <main class="page"><div class="container">${content}</div></main>
      <footer class="footer container">
        <span>A论 · A-Lun — 先读懂结构，再向你提问。</span>
        <span>每道题都绑定原文页码 · 本地部署 · 证据可追溯</span>
      </footer>`;
    window.scrollTo({ top: 0, behavior: 'instant' });
    document.getElementById('logout-btn')?.addEventListener('click', () => logout(true));
    document.getElementById('ai-top-btn')?.addEventListener('click', () => window.alunOpenAssistant?.());
  }

  // ------------------------------------------------------------------
  // home
  // ------------------------------------------------------------------
  async function loadMeta() {
    if (state.meta) return state.meta;
    try { state.meta = await api('/meta'); } catch { state.meta = { llm: { enabled: false } }; }
    return state.meta;
  }

  async function home() {
    await loadMeta();
    let docs = [], sessions = [], assignments = [];
    try {
      const [d, s, a] = await Promise.all([api('/documents?limit=50'), api('/sessions?limit=8'), api('/assignments?mine=1')]);
      docs = d.items || [];
      sessions = s.items || [];
      assignments = a.items || [];
    } catch (e) { toast(e.message, true); }
    state.home = { docs, sessions, assignments };
    const kpTotal = docs.reduce((a, d) => a + (d.stats?.kp_count || 0), 0);
    const finished = sessions.filter((s) => s.state === 'finished' && typeof s.score === 'number');
    const avg = finished.length ? Math.round(finished.reduce((a, s) => a + s.score, 0) / finished.length) : null;
    const heroDoc = docs.find((d) => (d.title || '').includes('强化微调')) || docs.find((d) => d.status === 'ready') || docs[0];
    const docTitle = heroDoc ? heroDoc.title : '检索增强生成综述：方法与应用';

    const docCards = docs.length ? docs.map((d) => docCard(d)).join('') : `
      <div class="doc-empty" style="grid-column:1/-1">
        <b>书架还是空的</b>
        上传一份 PDF，A论会先拆出结构树，再开始提问。
      </div>`;

    const assignmentRows = assignments.length ? assignments.map((a) => `
      <div class="session-row assignment-row">
        <div class="mode-mark">卷</div>
        <div class="info">
          <b>${esc(a.title)} · ${a.question_count} 题</b>
          <span>${esc(a.document_title || '')} · ${esc(a.status_label)}${a.due_at ? ' · 截止 ' + esc(fmtDate(a.due_at)) : ''}</span>
        </div>
        ${a.status === 'assigned' || a.status === 'in_progress'
          ? `<button class="btn btn-accent btn-sm" data-assignment-start="${esc(a.assignment_id)}">${a.status === 'in_progress' ? '继续作答' : '开始作答'}</button>`
          : a.report_id ? `<a class="btn btn-ghost btn-sm" href="#/report/${esc(a.session_id)}">查看报告</a>` : `<span class="tag tag-line">${esc(a.status_label)}</span>`}
      </div>`).join('') : '';
    const sessionRows = sessions.length ? sessions.map((s) => `
      <a class="session-row" href="#/session/${esc(s.session_id)}">
        <div class="mode-mark">${s.mode === 'exam' ? '测' : s.mode === 'defense' ? '辩' : '读'}</div>
        <div class="info">
          <b>${esc(MODE_LABEL[s.mode] || s.mode)} · 共 ${s.total_questions || 0} 题</b>
          <span>${fmtDate(s.created_at)} · ${s.state === 'finished' ? '已完成' : '进行中'}</span>
        </div>
        ${typeof s.score === 'number' ? `<div class="score">${Math.round(s.score)}<small style="font-size:11px;color:var(--muted)">分</small></div>` : `<span class="tag tag-line">继续</span>`}
      </a>`).join('') : `<div style="padding:26px 18px;color:var(--muted);font-size:13px">还没有会话。选一份文档，开始第一轮导读。</div>`;

    shell(`
      <section class="hero">
        <div class="hero-copy">
          <div class="eyebrow">Document-Grounded Questioning · A论</div>
          <h1>把论文，<br/>读成<em>自己的</em>。</h1>
          <p class="hero-lead">A论先读懂论文的结构——章节、图表、方法与结论，再围绕结构向你逐题提问。不替你读，而是让你在回答里，真正把论文读进去。</p>
          <div class="hero-actions">
            <button class="btn btn-accent btn-lg" id="hero-upload">${icon('upload', 16)} 上传论文，开始提问</button>
            <a class="btn btn-ghost btn-lg" href="#/sessions">看看我的会话 ${icon('arrow', 15)}</a>
          </div>
          <div class="hero-note">
            <span>${icon('check', 13)} 结构树配额出题</span><i></i>
            <span>${icon('check', 13)} 每题有原文页码</span><i></i>
            <span>${icon('check', 13)} Elo 自适应难度</span>
          </div>
        </div>
        <div class="hero-visual">
          <div class="orbit"></div>
          <div class="paper-stack">
            <div class="hero-card">
              <div class="hero-card-top">
                <span class="tag tag-red">导读 · 第 4 题</span>
                <span class="page-tab">P.5 / 10</span>
              </div>
              <h3>“两阶段混合检索”在信息抽取流程中，主要解决了什么问题？</h3>
              <div style="font-size:12px;color:var(--muted);margin-bottom:4px">原文证据</div>
              <p class="quote">第一阶段采用稀疏检索扩大召回，第二阶段采用稠密检索与重排序精排。</p>
              <div class="options">
                <div class="hero-option"><b>A</b><span>直接生成最终抽取结果</span></div>
                <div class="hero-option correct"><b>B</b><span>先召回候选文本块，再进行精排定位</span></div>
                <div class="hero-option"><b>C</b><span>替代强化学习训练过程</span></div>
                <div class="hero-option"><b>D</b><span>仅用于数据标注</span></div>
              </div>
              <div class="hero-card-foot"><span>${esc(docTitle).slice(0, 22)}…</span><span>证据可回溯 · 第 5 页</span></div>
            </div>
          </div>
          <div class="floating-note one"><b>结构先行</b>题从章节里长出来，不从摘要里猜。</div>
          <div class="floating-note two"><b>证据约束</b>没有原文支持，就不出这道题。</div>
        </div>
      </section>

      <section class="upload-zone" id="upload-zone">
        <input type="file" id="file-input" accept=".pdf,.docx,.md,.markdown,.txt,.png,.jpg,.jpeg,.webp" />
        <div class="upload-icon">${icon('upload', 26)}</div>
        <div class="upload-copy">
          <h3>把论文放到这里</h3>
          <p>支持 PDF / DOCX / Markdown / 图片，单文件不超过 100 MB。上传后自动解析结构，扫描件会自动图片识别。</p>
        </div>
        <div class="upload-meta">
          <span class="fmt">PDF · DOCX · MD · 图片</span>
          <span class="btn btn-primary btn-sm">选择文件</span>
        </div>
        <div class="url-import-row">
          <input class="text-input" id="url-input" type="url" placeholder="或粘贴网页链接，例如 https://..." />
          <button class="btn btn-quiet btn-sm" id="url-import-btn" type="button">导入链接</button>
        </div>
        <div class="upload-progress" id="upload-progress" style="display:none">
          <div class="label"><span id="upload-stage">正在上传</span><span id="upload-percent">0%</span></div>
          <div class="progress-track"><i class="progress-fill" id="upload-fill" style="width:0%"></i></div>
        </div>
      </section>

      <div class="stats-strip">
        <div class="stat"><b>${fmtNum(docs.length)}</b><span>已入库文档</span></div>
        <div class="stat"><b>${fmtNum(kpTotal)}</b><span>抽取知识点</span></div>
        <div class="stat"><b>${fmtNum(sessions.length)}</b><span>最近会话</span></div>
        <div class="stat"><b>${avg ?? '—'}${avg !== null ? '<small>分</small>' : ''}</b><span>平均得分</span></div>
      </div>

      <div class="section-head">
        <h2>文档书架</h2>
        <div class="rule"></div>
        <p>${docs.length} 份文档 · 点击进入结构树与提问设置</p>
      </div>
      <div class="doc-grid">${docCards}</div>

      ${assignmentRows ? `<div class="section-head"><h2>老师布置的题卷</h2><div class="rule"></div><span class="small muted">${assignments.length} 份</span></div><div class="panel">${assignmentRows}</div>` : ''}

      <div class="section-head">
        <h2>最近的导读</h2>
        <div class="rule"></div>
        <a href="#/sessions" class="small muted">查看全部 →</a>
      </div>
      <div class="panel">${sessionRows}</div>
    `, 'home');

    document.querySelectorAll('[data-assignment-start]').forEach((b) => b.addEventListener('click', async () => {
      try {
        const r = await api(`/assignments/${encodeURIComponent(b.dataset.assignmentStart)}/start`, { method: 'POST', body: {} });
        go('#/session/' + r.session.session_id);
      } catch (e) { toast(e.message, true); }
    }));
    document.getElementById('hero-upload')?.addEventListener('click', () => document.getElementById('file-input')?.click());
    bindUpload();
    if (docs.some((d) => ['uploaded', 'validating', 'parsing', 'indexing'].includes(d.status))) pollHome();
  }

  function docCard(d) {
    const q = d.quality || {};
    const stats = d.stats || {};
    const processing = ['uploaded', 'validating', 'parsing', 'indexing'].includes(d.status);
    const action = processing
      ? `<span class="tag tag-gold">解析中…</span>`
      : `<a class="btn btn-quiet btn-sm" href="#/doc/${esc(d.document_id)}">打开 ${icon('arrow', 13)}</a>`;
    return `
      <article class="doc-card" data-doc="${esc(d.document_id)}">
        <div class="doc-card-top">
          <span class="tag tag-ink">${esc(docType(d.doc_type))}</span>
          ${statusTag(d.status)}
        </div>
        <h3><a href="#/doc/${esc(d.document_id)}">${esc(d.title)}</a></h3>
        <p class="abstract">${esc(/[\u4e00-\u9fff]/.test(d.abstract || '') ? d.abstract : '结构树与知识点已就绪，进入后可查看章节分布与开始提问。')}</p>
        <div class="doc-card-meta">
          <span>${icon('book', 13)} ${d.page_count || '—'} 页</span>
          <span>${icon('leaf', 13)} ${stats.section_count || 0} 节</span>
          <span>${icon('spark', 13)} ${stats.kp_count || 0} 个知识点</span>
        </div>
        <div class="doc-card-foot">
          <div class="quality-mini"><span class="quality-bar"><i style="width:${pct((q.score || 0) * 100)}%"></i></span>解析 ${Math.round((q.score || 0) * 100)}%</div>
          ${action}
        </div>
      </article>`;
  }

  function pollHome() {
    clearTimeout(state.poll);
    state.poll = setTimeout(async () => {
      if ((location.hash || '#/') !== '#/' && location.hash !== '') return;
      try {
        const d = await api('/documents?limit=50');
        const changed = JSON.stringify(d.items?.map((x) => [x.document_id, x.status])) !== JSON.stringify(state.home?.docs?.map((x) => [x.document_id, x.status]));
        if (changed) { await home(); return; }
        if (d.items?.some((x) => ['uploaded', 'validating', 'parsing', 'indexing'].includes(x.status))) pollHome();
      } catch { /* ignore */ }
    }, 1800);
  }

  function bindUpload() {
    const zone = document.getElementById('upload-zone');
    const input = document.getElementById('file-input');
    if (!zone || !input) return;
    const urlInput = document.getElementById('url-input');
    const importBtn = document.getElementById('url-import-btn');
    const doImport = () => importUrl();
    importBtn?.addEventListener('click', doImport);
    urlInput?.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); doImport(); } });
    ['dragenter', 'dragover'].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('drag'); }));
    ['dragleave', 'drop'].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('drag'); }));
    zone.addEventListener('drop', (e) => { const f = e.dataTransfer.files?.[0]; if (f) uploadFile(f); });
    input.addEventListener('change', () => { const f = input.files?.[0]; if (f) uploadFile(f); });
  }

  async function importUrl() {
    const input = document.getElementById('url-input');
    const url = input?.value.trim();
    if (!url) { toast('请先粘贴网页链接', true); return; }
    if (!/^https?:\/\//i.test(url)) { toast('链接需要以 http:// 或 https:// 开头', true); return; }
    const btn = document.getElementById('url-import-btn');
    if (btn) { btn.disabled = true; btn.textContent = '抓取中…'; }
    try {
      const res = await api('/documents/from-url', { method: 'POST', body: { url } });
      if (input) input.value = '';
      if (res.deduplicated) { toast('这个网页已导入过'); go('#/doc/' + res.document_id); return; }
      toast('网页已抓取，A论开始提取正文结构');
      const prog = document.getElementById('upload-progress');
      const fill = document.getElementById('upload-fill');
      const stage = document.getElementById('upload-stage');
      const percent = document.getElementById('upload-percent');
      if (prog) prog.style.display = 'block';
      pollTask(res.task_id, res.document_id, (p, label) => {
        if (fill) fill.style.width = pct(p) + '%';
        if (percent) percent.textContent = pct(p) + '%';
        if (stage) stage.textContent = label;
      });
    } catch (e) {
      toast(e.message || '网页导入失败', true);
      if (btn) { btn.disabled = false; btn.textContent = '导入链接'; }
    }
  }

  async function uploadFile(file) {
    if (file.size > 100 * 1024 * 1024) { toast('文件超过 100 MB', true); return; }
    const prog = document.getElementById('upload-progress');
    const fill = document.getElementById('upload-fill');
    const stage = document.getElementById('upload-stage');
    const percent = document.getElementById('upload-percent');
    if (prog) prog.style.display = 'block';
    if (stage) stage.textContent = '正在上传';
    if (fill) fill.style.width = '8%';
    const form = new FormData();
    form.append('file', file);
    form.append('doc_type', 'auto');
    form.append('language', 'zh-CN');
    try {
      const res = await api('/documents', { method: 'POST', body: form });
      if (res.deduplicated) {
        toast('这份文档已在书架中，直接打开');
        go('#/doc/' + res.document_id);
        return;
      }
      toast('已收到论文，A论开始拆结构');
      pollTask(res.task_id, res.document_id, (p, label) => {
        if (fill) fill.style.width = pct(p) + '%';
        if (percent) percent.textContent = pct(p) + '%';
        if (stage) stage.textContent = label;
      });
    } catch (e) {
      toast(e.message || '上传失败', true);
      if (prog) prog.style.display = 'none';
    }
  }

  async function pollTask(taskId, docId, onProgress) {
    const stageMap = {
      queued: '排队中', validating: '校验文件', text_extract: '抽取文本层', layout: '还原版面',
      structure: '识别结构树', chunk: '语义分块', knowledge: '抽取知识点', indexing: '建立索引',
      quality: '质量检查', done: '完成',
    };
    for (let i = 0; i < 240; i++) {
      await new Promise((r) => setTimeout(r, 700));
      let t;
      try { t = await api('/tasks/' + taskId); } catch { continue; }
      onProgress(t.progress || 0, stageMap[t.stage] || '解析中');
      if (t.status === 'succeeded') {
        toast('结构树已就绪，开始提问吧');
        go('#/doc/' + docId);
        return;
      }
      if (t.status === 'failed' || t.status === 'canceled') {
        toast(t.error?.message || '解析失败，请更换文件', true);
        return;
      }
    }
  }

  // ------------------------------------------------------------------
  // document
  // ------------------------------------------------------------------
  async function docPage(id) {
    await loadMeta();
    let doc, outline, kps, quality, versionsData, chartsData;
    try {
      [doc, outline, kps, versionsData, chartsData] = await Promise.all([
        api('/documents/' + id),
        api(`/documents/${id}/outline?depth=4&include_text=true`),
        api(`/documents/${id}/knowledge-points?limit=100`),
        api(`/documents/${id}/versions`).catch(() => ({ items: [] })),
        api(`/documents/${id}/charts`).catch(() => ({ items: [] })),
      ]);
      try { quality = await api(`/documents/${id}/quality`); } catch { quality = null; }
    } catch (e) { toast(e.message, true); go('#/'); return; }
    window.ALUN_ASSISTANT_CONTEXT = { documentId: id, sessionId: '' };
    if (['uploaded', 'validating', 'parsing', 'indexing'].includes(doc.status)) {
      shell(`<div class="empty"><div class="boot-seal" style="margin:0 auto 18px">论</div><h2>正在拆解结构</h2><p>${esc(doc.title)}</p><p class="small" style="margin-top:8px">页面会自动刷新。</p></div>`, 'home');
      setTimeout(() => docPage(id), 1500);
      return;
    }
    state.doc = { doc, outline, kps, quality };
    const abstractNode = findNode(outline.nodes, (n) => n.node_type === 'abstract');
    const abstract = abstractNode?.text ? excerpt(abstractNode.text, 420) : '';
    const stats = doc.stats || {};
    const q = doc.quality || {};
    const tree = outline.nodes.map((n) => renderNode(n, 0)).join('');
    const kpChips = (kps.items || []).slice(0, 8).map((k) => `<span class="tag tag-line" title="${esc(k.definition)}">${esc(k.name)}</span>`).join('');
    const kpCounts = {};
    (kps.items || []).filter((k) => k.evidence_count > 0).forEach((k) => { kpCounts[k.node_id] = (kpCounts[k.node_id] || 0) + 1; });
    const maxQuestions = Math.min(30, Object.values(kpCounts).reduce((sum, n) => sum + Math.min(2, n), 0));
    const defaultCount = Math.min(12, Math.max(5, maxQuestions));
    const baseCounts = [5, 6, 8, 10, 12, 16, 20, 24, 30];
    if (maxQuestions >= 5 && !baseCounts.includes(maxQuestions)) baseCounts.push(maxQuestions);
    const countOptions = baseCounts.sort((a, b) => a - b)
      .map((n) => `<option value="${n}" ${n === defaultCount ? 'selected' : ''} ${n > maxQuestions ? 'disabled' : ''}>${n} 题${n > maxQuestions ? '（结构不足）' : ''}</option>`)
      .join('');

    shell(`
      <a class="back-link" href="#/">${icon('back', 14)} 返回文档库</a>
      <div class="doc-head">
        <div>
          <div class="meta" style="margin:0 0 12px">
            <span class="tag tag-ink">${esc(docType(doc.doc_type))}</span>
            <span class="tag tag-line">${doc.page_count} 页</span>
            ${statusTag(doc.status)}
            <span class="tag tag-line">版本 v${doc.doc_version}</span>
          </div>
          <h1>${esc(doc.title)}</h1>
          <div class="meta">
            <span class="small muted">${esc(doc.source.file_name)}</span>
            <span class="small muted">·</span>
            <span class="small muted">${fmtNum(doc.source.size_bytes)} bytes</span>
            <span class="small muted">·</span>
            <span class="small muted">入库于 ${fmtDate(doc.created_at)}</span>
            ${doc.source_uri ? `<span class="small muted">·</span><a class="small" href="${esc(doc.source_uri)}" target="_blank" rel="noopener">查看来源网页 ↗</a>` : ''}
          </div>
        </div>
        <div style="display:flex;gap:10px;flex:none">
          <button class="btn btn-ghost btn-sm" id="open-file">${icon('doc', 14)} 原文</button>
          <button class="btn btn-ghost btn-sm" id="reparse">重新解析</button>
        </div>
      </div>

      <div class="doc-layout">
        <div style="display:grid;gap:20px">
          <section class="panel">
            <div class="panel-head"><h3>摘要与结构画像</h3><span class="tag tag-jade">${Math.round((q.score || 0) * 100)}% 解析质量</span></div>
            <div class="panel-body">
              <p class="abstract-block">${esc(abstract || '这份文档没有识别到摘要，A论将主要依据章节标题与正文段落出题。')}</p>
              <div class="kv-grid">
                <div class="kv"><b>${stats.section_count || 0}</b><span>章节节点</span></div>
                <div class="kv"><b>${stats.kp_count || 0}</b><span>知识点</span></div>
                <div class="kv"><b>${(stats.figure_count || 0) + (stats.chart_count || 0)} / ${stats.table_count || 0}</b><span>图 / 表</span></div>
                <div class="kv"><b>${stats.table_cells || 0}</b><span>表格单元格</span></div>
                <div class="kv"><b>${stats.formula_count || 0}</b><span>公式节点</span></div>
                <div class="kv"><b>${stats.reference_count || 0}</b><span>参考文献</span></div>
              </div>
              <div style="margin-top:20px;display:flex;flex-wrap:wrap;gap:7px">${kpChips || '<span class="small muted">知识点抽取中</span>'}</div>
            </div>
          </section>

          <section class="panel">
            <div class="panel-head"><h3>结构树</h3><span class="small muted">${outline.nodes.length} 个顶层节点 · ${S.outlineEdit ? '拖拽节点可调整层级' : '点击展开'}</span><button class="btn btn-quiet btn-sm" id="outline-edit">${S.outlineEdit ? '完成编辑' : '校正结构'}</button></div>
            <div class="panel-body tree">${tree || '<div class="small muted">没有识别到结构节点</div>'}</div>
          </section>

          ${quality ? `<section class="panel"><div class="panel-head"><h3>解析质量</h3><span class="tag ${q.level === 'pass' ? 'tag-jade' : 'tag-gold'}">${q.level === 'pass' ? '通过' : '建议抽检'}</span></div><div class="panel-body"><div class="quality-ring-row"><div class="ring" style="--p:${pct((q.score || 0) * 100)}"><span>${Math.round((q.score || 0) * 100)}</span></div><div class="q-copy"><b>文本层与标题识别</b><p>${quality.metrics?.map((m) => `${metricLabel(m.name)} ${m.passed ? '✓' : '!'}`).join(' · ') || '解析指标已记录'}${stats.ocr_pages ? ` · 图片识别 ${stats.ocr_pages} 页` : ''}</p></div></div>${(q.issues || []).map((i) => `<div class="issue">${esc(i.message)}</div>`).join('')}</div></section>` : ''}
          ${(chartsData.items || []).length ? `<section class="panel"><div class="panel-head"><h3>图表数据</h3><span class="small muted">${chartsData.items.length} 个图表 · 视觉模型提取</span></div><div class="panel-body" style="display:grid;gap:12px">${chartsData.items.map((c) => `<div class="chart-data-card"><div class="chart-data-head"><b>${esc(c.title || '图表')}</b><span class="tag tag-line">第 ${c.page} 页 · ${esc(c.data?.chart_type || 'chart')}</span></div>${c.markdown ? `<div class="chart-data-table">${miniTable(c.markdown)}</div>` : ''}</div>`).join('')}</div></section>` : ''}
          <section class="panel"><div class="panel-head"><h3>结构版本</h3><span class="small muted">当前 v${doc.doc_version} · ${(versionsData.items || []).length} 个快照</span></div><div class="panel-body" style="padding:0"><table class="version-table"><thead><tr><th>版本</th><th>节点</th><th>原因</th><th>时间</th><th></th></tr></thead><tbody>${(versionsData.items || []).map((v) => `<tr><td class="num-cell">v${v.version_no}</td><td class="num-cell">${v.node_count}</td><td>${esc(v.reason || '—')}</td><td class="small muted">${esc(fmtDate(v.created_at))}</td><td>${v.version_no !== doc.doc_version ? `<button class="btn btn-quiet btn-sm" data-version-restore="${v.version_no}">回滚</button>` : '<span class="tag tag-line">当前</span>'}</td></tr>`).join('') || '<tr><td colspan="5"><div class="small muted" style="padding:14px">结构编辑或重新解析后会自动生成快照</div></td></tr>'}</tbody></table></div></section>
        </div>

        <aside class="side-sticky">
          <section class="panel">
            <div class="panel-head"><h3>开始一轮提问</h3><span class="tag tag-red">A论</span></div>
            <div class="panel-body">
              <p class="small muted" style="line-height:1.8;margin-bottom:16px">A论会按结构配额选题，难度略高于你的当前水平。每题都能回到原文页码。</p>
              <div class="field"><span class="field-label">模式</span><div class="segmented" id="mode-seg" role="group" aria-label="模式">
                ${['guided', 'exam', 'defense'].map((m) => `<button data-mode="${m}" class="${m === 'guided' ? 'active' : ''}">${MODE_LABEL[m].replace('模式', '')}</button>`).join('')}
              </div></div>
              <div class="field"><label for="count-select">题量</label><select class="text-input" id="count-select">${countOptions}</select><div class="small muted" style="margin-top:6px">按当前结构最多可出 <b>${maxQuestions}</b> 题（同节最多 2 题）</div></div>
              <div class="field"><label for="diff-select">难度</label><select class="text-input" id="diff-select"><option value="auto">自适应</option><option value="easy">轻松</option><option value="medium">适中</option><option value="hard">挑战</option></select></div>
              <div class="field"><span class="field-label">题型</span><div class="qtype-picker" id="qtype-picker">
                ${[['choice', '选择题'], ['fill_blank', '填空题'], ['true_false', '判断题'], ['short_answer', '问答题']].map(([key, label]) => `<label class="qtype-check"><input type="checkbox" value="${key}" checked /> ${label}</label>`).join('')}
              </div><div class="small muted" style="margin-top:5px">至少选择一种；不选则使用全部题型</div></div>
              <button class="btn btn-accent btn-block btn-lg" id="start-session" style="margin-top:6px">${icon('spark', 16)} 让 A论 开始提问</button>
              <div class="small muted" style="margin-top:12px;text-align:center">预计 ${Math.max(8, Math.round((doc.page_count || 10) * 1.2))} 分钟 · 可随时暂停</div>
            </div>
          </section>
          <div class="panel panel-pad">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px"><span class="tag tag-gold">提示</span><b style="font-family:var(--serif);font-size:16px">为什么先看结构？</b></div>
            <p class="small muted" style="line-height:1.9">只按段落切块出题，题目会集中在摘要与高频段落。A论把题挂在章节、图表与结论上，保证覆盖均衡、定位准确。</p>
          </div>
        </aside>
      </div>
    `, 'home');

    document.getElementById('open-file')?.addEventListener('click', () => { go('#/read/' + id); });
    document.querySelectorAll('[data-version-restore]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm(`确定回滚到结构版本 v${b.dataset.versionRestore}？当前结构会自动快照。`)) return;
      try { await api(`/documents/${encodeURIComponent(id)}/versions/${encodeURIComponent(b.dataset.versionRestore)}/restore`, { method: 'POST', body: {} }); toast('结构已回滚'); docPage(id); }
      catch (e) { toast(e.message, true); }
    }));
    document.getElementById('reparse')?.addEventListener('click', async () => {
      try { await api(`/documents/${id}/reparse`, { method: 'POST', body: {} }); toast('已开始重新解析'); docPage(id); }
      catch (e) { toast(e.message, true); }
    });
    bindOutlineEditor(id);
    bindModeSeg();
    document.getElementById('start-session')?.addEventListener('click', () => startSession(id));
  }

  function miniTable(md) {
    const rows = String(md || "").split("\n").filter((l) => l.trim().startsWith("|"))
      .map((l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()))
      .filter((r) => !r.every((c) => /^:?-{2,}:?$/.test(c)));
    if (rows.length < 2) return esc(md || "");
    const head = rows[0].map((c) => `<th>${esc(c)}</th>`).join("");
    const body = rows.slice(1).map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("");
    return `<table class="chart-mini-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function findNode(nodes, fn) {
    for (const n of nodes || []) {
      if (fn(n)) return n;
      const found = findNode(n.children || [], fn);
      if (found) return found;
    }
    return null;
  }
  function excerpt(s, n) { s = String(s || '').replace(/\s+/g, ' '); return s.length > n ? s.slice(0, n) + '…' : s; }
  function metricLabel(name) {
    return { text_coverage: '文本覆盖', heading_f1: '标题识别', garbled_rate: '乱码率', figure_link_rate: '图表关联', ocr_pages: '图片识别' }[name] || name;
  }

  function bindOutlineEditor(docId) {
    const toggle = document.getElementById('outline-edit');
    toggle?.addEventListener('click', () => { S.outlineEdit = !S.outlineEdit; docPage(docId); });
    const tree = document.querySelector('.tree');
    if (!tree || !S.outlineEdit) return;
    let dragId = '';
    tree.querySelectorAll('.tree-node').forEach((row) => {
      row.addEventListener('dragstart', (e) => {
        dragId = row.dataset.nodeId || '';
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', dragId);
      });
      row.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; row.classList.add('drop-target'); });
      row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
      row.addEventListener('drop', async (e) => {
        e.preventDefault(); e.stopPropagation();
        row.classList.remove('drop-target');
        const source = dragId || e.dataTransfer.getData('text/plain');
        const target = row.dataset.nodeId;
        if (!source || !target || source === target) return;
        const parentId = row.dataset.isSection === '1' ? target : (row.dataset.parentId || '');
        try {
          await api(`/documents/${docId}/outline`, { method: 'PUT', body: { operations: [{
            op: 'move', node_id: source, parent_id: parentId || null, order_no: Number(row.dataset.orderNo || 1),
          }] } });
          toast('结构层级已更新');
          await docPage(docId);
        } catch (err) { toast(err.message, true); }
      });
    });
    tree.addEventListener('click', async (e) => {
      const button = e.target.closest('[data-tree-op]');
      if (!button) return;
      e.preventDefault(); e.stopPropagation();
      const row = button.closest('.tree-node');
      const nodeId = row?.dataset.nodeId;
      if (!nodeId) return;
      const op = button.dataset.treeOp;
      try {
        if (op === 'rename') {
          const current = row.querySelector('.node-label')?.textContent || '';
          const title = prompt('修改节点标题', current);
          if (!title || title.trim() === current) return;
          await api(`/documents/${docId}/outline`, { method: 'PUT', body: { operations: [{ op: 'update', node_id: nodeId, title: title.trim() }] } });
        } else if (op === 'merge') {
          if (!confirm('把这个节点与下一个同级节点合并？合并后内容会归入后一个节点。')) return;
          await api(`/documents/${docId}/outline`, { method: 'PUT', body: { operations: [{
            op: 'merge', node_id: nodeId, parent_id: row.dataset.parentId || null, order_no: Number(row.dataset.orderNo || 1),
          }] } });
        } else if (op === 'delete') {
          if (!confirm('删除这个节点及其子节点？')) return;
          await api(`/documents/${docId}/outline`, { method: 'PUT', body: { operations: [{ op: 'delete', node_id: nodeId }] } });
        }
        toast('结构已更新');
        await docPage(docId);
      } catch (err) { toast(err.message, true); }
    });
  }

  function renderNode(n, depth) {
    const edit = S.outlineEdit;
    const ops = edit ? `<span class="tree-ops"><button class="tree-op" data-tree-op="rename" title="重命名">✎</button><button class="tree-op" data-tree-op="merge" title="与下一个同级节点合并">合</button><button class="tree-op" data-tree-op="delete" title="删除">×</button></span>` : '';
    const attrs = `data-node-id="${esc(n.node_id)}" data-parent-id="${esc(n.parent_id || '')}" data-order-no="${n.order_no || 1}" data-is-section="${n.node_type === 'section' ? '1' : '0'}" ${edit ? 'draggable="true"' : ''}`;
    if (n.node_type === 'figure' || n.node_type === 'chart' || n.node_type === 'table' || n.node_type === 'formula' || n.node_type === 'keyword' || n.node_type === 'reference') {
      return `<div class="leaf tree-node ${edit ? 'editable' : ''}" ${attrs}><span class="node-icon ${n.node_type === 'figure' || n.node_type === 'chart' ? 'fig' : n.node_type === 'table' ? 'tab' : n.node_type === 'formula' ? 'formula' : ''}">${NODE_ICON[n.node_type] || '·'}</span><span class="node-label">${esc(n.title)}</span><span class="node-page">p.${n.page_start}</span>${ops}</div>`;
    }
    const children = n.children || [];
    if (!children.length) {
      return `<div class="leaf tree-node ${edit ? 'editable' : ''}" ${attrs}><span class="node-icon ${n.node_type === 'section' ? 'sec' : ''}">${NODE_ICON[n.node_type] || '·'}</span><span class="node-label">${esc(n.title)}</span><span class="node-page">p.${n.page_start}</span>${ops}</div>`;
    }
    return `<details class="root tree-node ${edit ? 'editable' : ''}" ${attrs} ${depth < 1 ? 'open' : ''}><summary><span class="caret">▶</span><span class="node-icon ${n.node_type === 'section' ? 'sec' : ''}">${NODE_ICON[n.node_type] || '·'}</span><span class="node-label">${esc(n.title)}</span><span class="node-page">p.${n.page_start}—${n.page_end}</span>${ops}</summary>${children.map((c) => renderNode(c, depth + 1)).join('')}</details>`;
  }

  function bindModeSeg() {
    const seg = document.getElementById('mode-seg');
    seg?.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
      seg.querySelectorAll('button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
    }));
  }

  async function startSession(docId) {
    const mode = document.querySelector('#mode-seg .active')?.dataset.mode || 'guided';
    const count = Number(document.getElementById('count-select')?.value || 12);
    const difficulty = document.getElementById('diff-select')?.value || 'auto';
    const questionTypes = [...document.querySelectorAll('#qtype-picker input:checked')].map((x) => x.value);
    if (!questionTypes.length) { toast('请至少选择一种题型', true); return; }
    const btn = document.getElementById('start-session');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> 正在排题…'; }
    try {
      const s = await api('/sessions', { method: 'POST', body: { document_id: docId, mode, question_count: count, difficulty, question_types: questionTypes } });
      if (Number(s.total_questions) < count) {
        toast(`受结构限制，本轮实际生成 ${s.total_questions} 题（选择 ${count} 题）`, true);
      }
      go('#/session/' + s.session_id);
    } catch (e) {
      toast(e.message, true);
      if (btn) { btn.disabled = false; btn.innerHTML = `${icon('spark', 16)} 让 A论 开始提问`; }
    }
  }

  // ------------------------------------------------------------------
  // original reader + docked AI panel
  // ------------------------------------------------------------------
  function renderReaderText(outline) {
    const walk = (nodes, depth = 0) => nodes.map((n) => {
      const kids = walk(n.children || [], depth + 1);
      const text = n.text ? `<p>${esc(excerpt(n.text, 1200))}</p>` : '';
      return `<div class="reader-text-node depth-${Math.min(depth, 3)}" data-page="${n.page_start || 1}"><h${Math.min(4, depth + 2)}>${esc(n.title)}</h${Math.min(4, depth + 2)}>${text}${kids}</div>`;
    }).join('');
    return walk(outline.nodes || []);
  }

  async function readerPage(id) {
    let doc, outline, readState, notes;
    try {
      [doc, outline, readState, notes] = await Promise.all([
        api('/documents/' + id),
        api(`/documents/${id}/outline?depth=5&include_text=true`),
        api(`/documents/${id}/reading-state`).catch(() => ({ page_no: 1, scroll_ratio: 0 })),
        api(`/documents/${id}/notes`).catch(() => ({ items: [] })),
      ]);
    } catch (e) { toast(e.message, true); go('#/doc/' + id); return; }
    window.ALUN_ASSISTANT_CONTEXT = { documentId: id, sessionId: '' };
    const isPdfDoc = (() => {
      const mime = ((doc.source?.mime_type || doc.mime_type || '')).toLowerCase();
      const name = (doc.source?.file_name || doc.file_name || '').toLowerCase();
      return mime.includes('pdf') || name.endsWith('.pdf');
    })();
    let readerTextMode = !isPdfDoc;
    document.body.classList.add('reader-mode');
    const messages = [];
    app.innerHTML = `
      <div class="reader-shell">
        <header class="reader-header">
          <a class="reader-back" href="#/doc/${esc(id)}">← 返回文档</a>
          <div class="reader-heading"><b>${esc(doc.title)}</b><span id="reader-mode-hint">${isPdfDoc ? 'PDF 原文 · 点「划词模式」切换为可划选文本' : '原文阅读 · 选中句子后点「AI 解读」，或开启自动解读'}</span></div>
          <div class="reader-actions">
            <span class="reader-page-nav">
              <button class="btn btn-quiet btn-sm" id="reader-prev" title="上一页">‹</button>
              <input id="reader-page" type="number" min="1" max="${doc.page_count || 1}" value="${readState.page_no || 1}" aria-label="当前页码" />
              <span>/ ${doc.page_count || 1}</span>
              <button class="btn btn-quiet btn-sm" id="reader-next" title="下一页">›</button>
            </span>
            ${isPdfDoc ? '<button class="btn btn-quiet btn-sm" id="reader-mode-toggle">划词模式</button>' : ''}
            <button class="btn btn-quiet btn-sm reader-auto-toggle" id="reader-auto-toggle" title="开启后，选中完整句子自动解读；默认手动">自动解读</button>
            <button class="btn btn-quiet btn-sm" id="reader-save-state">保存位置</button>
            <button class="btn btn-quiet btn-sm" id="reader-notes-toggle">笔记 <b id="reader-note-count">${(notes.items || []).length}</b></button>
            <button class="btn btn-quiet btn-sm" id="reader-newtab">新窗口阅读</button>
            <button class="btn btn-quiet btn-sm reader-ai-toggle" id="reader-ai-toggle">AI 解读</button>
          </div>
        </header>
        <div class="reader-main">
          <main class="reader-viewer" id="reader-viewer"><div class="reader-loading"><span class="spinner"></span> 正在加载原文…</div></main>
          <button type="button" class="reader-selection-tip" id="reader-selection-tip">AI 解读</button>
          <aside class="reader-notes" id="reader-notes">
            <div class="reader-notes-head"><div><span class="eyebrow">PAGE NOTES</span><h3>页级笔记</h3></div><button class="reader-ai-close" id="reader-notes-close" title="收起">×</button></div>
            <div class="reader-notes-list" id="reader-notes-list"></div>
            <div class="reader-note-compose">
              <textarea id="reader-note-input" rows="3" placeholder="写一条本页笔记，或先引用原文片段…"></textarea>
              <div class="reader-note-actions"><span id="reader-note-page" class="small muted">第 ${readState.page_no || 1} 页</span><button class="btn btn-accent btn-sm" id="reader-note-add">保存笔记</button></div>
            </div>
          </aside>
          <aside class="reader-ai" id="reader-ai">
            <div class="reader-ai-head"><div><span class="eyebrow">AI DOCUMENT READER</span><h2>A论 AI 解读</h2></div><button class="reader-ai-close" id="reader-ai-close" title="收起">×</button></div>
            <p class="reader-ai-note">基于本文结构、原文证据回答；原文在左侧，解读栏独立占位。</p>
            <div class="reader-messages" id="reader-messages"><div class="reader-ai-welcome">你好，我可以帮你<strong>总结全文</strong>、解释某一节，或回答“作者为什么这样设计实验”。</div></div>
            <div class="reader-quick">
              <button data-reader-ask="用三点总结这篇文章的研究问题、方法和结论。">总结全文</button>
              <button data-reader-ask="这篇文章的核心方法是什么？请结合章节结构解释。">解释方法</button>
              <button data-reader-ask="这篇文章有哪些证据支持作者的核心结论？">找原文证据</button>
            </div>
            <div class="reader-compose"><input type="file" id="reader-image-input" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden /><button class="btn btn-quiet btn-sm reader-attach" id="reader-attach" type="button" title="上传图片">＋ 图片</button><textarea id="reader-input" rows="2" placeholder="问这篇文章，例如：解释 2.1 的核心思想…（可粘贴图片）"></textarea><button class="btn btn-accent" id="reader-send">发送</button><button class="btn btn-quiet btn-sm reader-stop" id="reader-stop" type="button" title="停止当前回答" hidden>■ 停止</button></div>
            <div class="reader-image-strip" id="reader-image-strip"></div>
            <p class="reader-ai-foot">回答会标注原文页码；找不到依据时会明确说明。</p>
          </aside>
        </div>
      </div>`;
    const viewer = document.getElementById('reader-viewer');
    const input = document.getElementById('reader-input');
    const send = document.getElementById('reader-send');
    const stopBtn = document.getElementById('reader-stop');
    let readerAbort = null;
    const messagesBox = document.getElementById('reader-messages');
    const pageInput = document.getElementById('reader-page');
    const noteInput = document.getElementById('reader-note-input');
    const noteList = document.getElementById('reader-notes-list');
    const noteCount = document.getElementById('reader-note-count');
    const imageInput = document.getElementById('reader-image-input');
    const imageStrip = document.getElementById('reader-image-strip');
    let readerImages = [];
    let currentPage = Number(readState.page_no) || 1;
    let noteItems = notes.items || [];
    const clearReaderImages = () => {
      readerImages.forEach((x) => URL.revokeObjectURL(x.url));
      readerImages = [];
      if (imageStrip) imageStrip.innerHTML = '';
    };
    const renderReaderImages = () => {
      if (!imageStrip) return;
      imageStrip.innerHTML = readerImages.map((x, i) => `<span class="reader-image-chip"><img src="${x.url}" alt="待发送图片" /><button type="button" data-reader-image-remove="${i}" title="移除">×</button></span>`).join('');
      imageStrip.querySelectorAll('[data-reader-image-remove]').forEach((b) => b.addEventListener('click', () => {
        const idx = Number(b.dataset.readerImageRemove);
        if (readerImages[idx]) URL.revokeObjectURL(readerImages[idx].url);
        readerImages.splice(idx, 1);
        renderReaderImages();
      }));
    };
    const addReaderImages = (files) => {
      for (const f of files) {
        if (readerImages.length >= 3) { toast('最多上传 3 张图片', true); break; }
        if (!f.type.startsWith('image/')) continue;
        if (f.size > 8 * 1024 * 1024) { toast('单张图片不能超过 8 MB', true); continue; }
        readerImages.push({ file: f, url: URL.createObjectURL(f) });
      }
      renderReaderImages();
    };
    const renderNotes = () => {
      noteCount.textContent = String(noteItems.length);
      noteList.innerHTML = noteItems.length ? noteItems.map((n) => `
        <div class="reader-note-item">
          <div class="reader-note-meta"><span>p.${n.page_no}</span><button data-note-del="${esc(n.note_id)}" title="删除">×</button></div>
          ${n.quote ? `<blockquote>${esc(n.quote)}</blockquote>` : ''}
          <p>${esc(n.note || '')}</p>
        </div>`).join('') : '<div class="reader-note-empty">还没有笔记。读到哪里，想到什么，就记在这里。</div>';
      noteList.querySelectorAll('[data-note-del]').forEach((b) => b.addEventListener('click', async () => {
        try { await api(`/documents/${encodeURIComponent(id)}/notes/${encodeURIComponent(b.dataset.noteDel)}`, { method: 'DELETE' }); noteItems = noteItems.filter((x) => x.note_id !== b.dataset.noteDel); renderNotes(); toast('笔记已删除'); } catch (e) { toast(e.message, true); }
      }));
    };
    const applyPage = (n) => {
      currentPage = Math.max(1, Math.min(Number(doc.page_count) || 1, Number(n) || 1));
      if (pageInput) pageInput.value = String(currentPage);
      const label = document.getElementById('reader-note-page');
      if (label) label.textContent = `第 ${currentPage} 页`;
      const frame = document.getElementById('reader-frame');
      if (frame) {
        const base = frame.src.split('#')[0];
        frame.src = `${base}#page=${currentPage}&toolbar=1&navpanes=0&view=FitH`;
      }
      if (isPdfDoc && readerTextMode) {
        loadPdfTextLayer(currentPage);
        return;
      }
      const target = viewer.querySelector(`[data-page="${currentPage}"]`);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
    let stateTimer = null;
    const saveState = () => {
      clearTimeout(stateTimer);
      stateTimer = setTimeout(() => {
        api(`/documents/${encodeURIComponent(id)}/reading-state`, { method: 'PUT', body: { page_no: currentPage, scroll_ratio: 0, zoom: 1 } }).catch(() => {});
      }, 400);
    };
    renderNotes();
    const mathSplit = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^$\n]+?\$)/g;
    const mathToken = /^(\$\$[\s\S]*\$\$|\\\[[\s\S]*\\\]|\\\([\s\S]*\\\)|\$[^$\n]*\$)$/;
    const fmtReader = (text) => {
      let out = '';
      String(text || '').split(mathSplit).forEach((part) => {
        if (mathToken.test(part)) { out += esc(part); return; }
        out += esc(part).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br/>');
      });
      return out;
    };
    const typesetReader = (el, attempt = 0) => {
      if (!el) return;
      if (window.MathJax?.typesetPromise) { window.MathJax.typesetPromise([el]).catch(() => {}); return; }
      if (attempt < 8) setTimeout(() => typesetReader(el, attempt + 1), 250);
    };
    const append = (role, text, extra = '', scroll = true) => {
      const klass = role === 'user' ? 'reader-msg user' : 'reader-msg assistant';
      messagesBox.insertAdjacentHTML('beforeend', `<div class="${klass}"><div class="reader-msg-label">${role === 'user' ? '你' : 'A论'}</div><div class="reader-msg-text">${fmtReader(text)}</div>${extra}</div>`);
      const el = messagesBox.lastElementChild;
      typesetReader(el);
      if (scroll) messagesBox.scrollTop = messagesBox.scrollHeight;
      return el;
    };
    const scrollToQuestion = (el) => {
      if (!el) return;
      const boxRect = messagesBox.getBoundingClientRect();
      const qRect = el.getBoundingClientRect();
      messagesBox.scrollTo({ top: Math.max(0, messagesBox.scrollTop + qRect.top - boxRect.top - 10), behavior: 'smooth' });
    };
    messages.push({ role: 'assistant', content: '你好，我可以帮你总结全文、解释某一节或寻找原文证据。' });
    const ask = async (question) => {
      question = (question || '').trim();
      const imageSnapshot = readerImages.slice();
      if ((!question && !imageSnapshot.length) || send.disabled) return;
      const shown = question || '（粘贴了一张图片，请描述并结合原文回答）';
      const userEl = append('user', shown);
      messages.push({ role: 'user', content: shown });
      input.value = '';
      const controller = new AbortController();
      readerAbort = controller;
      if (stopBtn) stopBtn.hidden = false;
      send.disabled = true;
      const typing = document.createElement('div');
      typing.className = 'reader-msg assistant';
      typing.innerHTML = '<div class="reader-msg-label">A论</div><div class="reader-msg-text"><span class="spinner"></span> 正在读原文…</div>';
      messagesBox.appendChild(typing);
      messagesBox.scrollTop = messagesBox.scrollHeight;
      try {
        const fd = new FormData();
        fd.append('message', question || '请描述这张图片，并结合文档或权威资料回答我能从中得到什么信息。');
        fd.append('document_id', id);
        fd.append('history', JSON.stringify(messages.slice(-6)));
        fd.append('use_web', '0');
        imageSnapshot.forEach((x) => fd.append('images', x.file, x.file.name));
        const headers = { Authorization: 'Bearer ' + AUTH.token };
        if (AUTH.user?.tenant_id) headers['X-Tenant-Id'] = AUTH.user.tenant_id;
        const res = await fetch(API + '/assistant/chat', { method: 'POST', headers, body: fd, signal: controller.signal });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.message || `请求失败（${res.status}）`);
        typing.remove();
        const answer = data.answer || '没有得到回答。';
        messages.push({ role: 'assistant', content: answer });
        clearReaderImages();
        const cites = (data.citations || []).map((c) => `<span class="tag tag-line">原文 p.${esc(c.page || '—')}</span>`).join(' ');
        append('assistant', answer, cites ? `<div class="reader-cites">${cites}</div>` : '', false);
        scrollToQuestion(userEl);
      } catch (e) {
        typing.remove();
        if (e.name === 'AbortError') {
          append('assistant', '已停止本次回答。', '', false);
        } else {
          append('assistant', `暂时无法完成解读：${e.message || '服务异常'}。可以先在左侧查看原文相关段落。`, '', false);
        }
        scrollToQuestion(userEl);
      } finally {
        readerAbort = null;
        if (stopBtn) stopBtn.hidden = true;
        send.disabled = false;
      }
    };
    document.getElementById('reader-send')?.addEventListener('click', () => ask(input.value));
    stopBtn?.addEventListener('click', () => {
      if (!readerAbort) return;
      readerAbort.abort();
      toast('正在停止本次回答…');
    });
    document.getElementById('reader-attach')?.addEventListener('click', () => imageInput?.click());
    imageInput?.addEventListener('change', () => { addReaderImages(Array.from(imageInput.files || [])); imageInput.value = ''; });
    input?.addEventListener('paste', (e) => {
      const files = Array.from(e.clipboardData?.items || [])
        .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
        .map((item) => item.getAsFile())
        .filter(Boolean);
      if (!files.length) return;
      e.preventDefault();
      addReaderImages(files);
      toast(files.length > 1 ? `已粘贴 ${files.length} 张图片` : '图片已粘贴，可输入问题后发送');
    });
    input?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(input.value); }
    });
    document.querySelectorAll('[data-reader-ask]').forEach((b) => b.addEventListener('click', () => ask(b.dataset.readerAsk)));
    document.getElementById('reader-ai-toggle')?.addEventListener('click', () => document.getElementById('reader-ai')?.classList.toggle('collapsed'));
    document.getElementById('reader-ai-close')?.addEventListener('click', () => document.getElementById('reader-ai')?.classList.add('collapsed'));
    async function loadPdfTextLayer(pageNo) {
      const box = document.getElementById('reader-pdf-layer');
      if (!box) return;
      box.innerHTML = '<div class="reader-loading"><span class="spinner"></span> 正在加载原文页…</div>';
      try {
        const headers = { Authorization: 'Bearer ' + AUTH.token };
        if (AUTH.user?.tenant_id) headers['X-Tenant-Id'] = AUTH.user.tenant_id;
        const res = await fetch(API + `/documents/${encodeURIComponent(id)}/pages/${pageNo}/text-layer`, { headers });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.message || `原文页加载失败（${res.status}）`);
        const width = Math.max(300, Math.min(viewer.clientWidth - 36, 820));
        const scale = width / Math.max(1, Number(data.width) || 595);
        const height = Math.max(200, (Number(data.height) || 842) * scale);
        const spans = (data.lines || []).map((line) => {
          const b = line.bbox || [0, 0, 0, 0];
          const x = Number(b[0]) * scale;
          const y = Number(b[1]) * scale;
          const h = Math.max(2, (Number(b[3]) - Number(b[1])) * scale);
          return `<span class="reader-pdf-line" data-page="${pageNo}" style="left:${x}px;top:${y}px;font-size:${Math.max(4, Number(line.size) || 9) * scale}px;line-height:${h}px">${esc(line.text)}</span>`;
        }).join('');
        box.innerHTML = `<div class="reader-pdf-page" style="width:${width}px;height:${height}px"><img class="reader-pdf-image" src="${data.image}" alt="第 ${pageNo} 页" /><div class="reader-pdf-text">${spans}</div></div>`;
        box.dataset.page = String(pageNo);
      } catch (e) {
        box.innerHTML = `<div class="reader-error">${esc(e.message || '原文页加载失败')}</div>`;
      }
    }

    const renderReaderContent = () => {
      const name = (doc.source?.file_name || doc.file_name || '').toLowerCase();
      const mime = ((doc.source?.mime_type || doc.mime_type || '')).toLowerCase();
      if (isPdfDoc && !readerTextMode) {
        if (!state.readerUrl) {
          viewer.innerHTML = '<div class="reader-loading"><span class="spinner"></span> 正在加载原文…</div>';
          return;
        }
        viewer.innerHTML = `<iframe class="reader-frame" id="reader-frame" src="${state.readerUrl}#page=${currentPage}&toolbar=1&navpanes=0&view=FitH" title="论文原文"></iframe>`;
      } else if (isPdfDoc && readerTextMode) {
        viewer.innerHTML = '<div class="reader-pdf-layer" id="reader-pdf-layer"></div>';
        loadPdfTextLayer(currentPage);
      } else if (!isPdfDoc && (mime.startsWith('image/') || /\.(png|jpe?g|webp|bmp)$/.test(name))) {
        viewer.innerHTML = `<img class="reader-image" src="${state.readerUrl}" alt="${esc(doc.title)}" />`;
      } else {
        const content = renderReaderText(outline);
        viewer.innerHTML = content
          ? `<div class="reader-text" id="reader-text-layer">${content}</div>`
          : '<div class="reader-error">该格式暂不支持网页内预览，请使用“新窗口打开”。</div>';
        applyPage(currentPage);
      }
    };
    try {
      const res = await fetch(API + `/documents/${id}/file`, { headers: AUTH.token ? { Authorization: 'Bearer ' + AUTH.token } : {} });
      if (!res.ok) throw new Error(`原文加载失败（${res.status}）`);
      const blob = await res.blob();
      if (state.readerUrl) URL.revokeObjectURL(state.readerUrl);
      state.readerUrl = URL.createObjectURL(blob);
      renderReaderContent();
      document.getElementById('reader-newtab')?.addEventListener('click', () => {
        const base = location.href.split('#')[0];
        window.open(base + '#/read/' + encodeURIComponent(id), '_blank', 'noopener');
      });
    } catch (e) {
      viewer.innerHTML = `<div class="reader-error">${esc(e.message || '原文加载失败')}</div>`;
    }
    const selectionTip = document.getElementById('reader-selection-tip');
    const autoSelectToggle = document.getElementById('reader-auto-toggle');
    let selectionTimer = null;
    let lastSelection = '';
    let pendingSelection = null;
    let autoSelection = localStorage.getItem('alun_auto_selection') === '1';
    const renderAutoSelectToggle = () => {
      if (!autoSelectToggle) return;
      autoSelectToggle.classList.toggle('active', autoSelection);
      autoSelectToggle.textContent = autoSelection ? '自动解读 开' : '自动解读';
    };
    const hideSelectionTip = () => {
      clearTimeout(selectionTimer);
      if (selectionTip) selectionTip.style.display = 'none';
    };
    const sendSelectionExplanation = () => {
      if (!pendingSelection || send.disabled) return;
      const { text, page } = pendingSelection;
      hideSelectionTip();
      ask(`请解释这段原文（第 ${page} 页）的含义，并说明它和上下文的关系：\n“${text}”`);
    };
    selectionTip?.addEventListener('mousedown', (e) => e.preventDefault());
    selectionTip?.addEventListener('click', sendSelectionExplanation);
    autoSelectToggle?.addEventListener('click', () => {
      autoSelection = !autoSelection;
      localStorage.setItem('alun_auto_selection', autoSelection ? '1' : '0');
      renderAutoSelectToggle();
      if (!autoSelection) {
        clearTimeout(selectionTimer);
        if (selectionTip && pendingSelection) selectionTip.textContent = 'AI 解读';
      }
      toast(autoSelection ? '自动解读已开启：选中完整句子后自动发送' : '自动解读已关闭：选中后点「AI 解读」');
    });
    renderAutoSelectToggle();
    const isCompleteSentence = (text) => text.length >= 10 && (/[。！？!?；;]/.test(text) || text.length >= 18);
    const scheduleSelectionExplanation = () => {
      const selection = window.getSelection();
      if (!selection || selection.rangeCount === 0) return;
      const anchor = selection.anchorNode?.nodeType === 3 ? selection.anchorNode.parentElement : selection.anchorNode;
      if (!anchor?.closest?.('.reader-text, .reader-pdf-line')) return;
      const text = selection.toString().replace(/\s+/g, ' ').trim();
      if (text.length < 3 || text.length > 400) { hideSelectionTip(); return; }
      if (text === lastSelection && pendingSelection?.text === text && selectionTip?.style.display === 'block') return;
      lastSelection = text;
      const page = Number(anchor?.closest?.('[data-page]')?.dataset.page) || currentPage;
      pendingSelection = { text, page };
      const range = selection.getRangeAt(0);
      const rect = range.getBoundingClientRect();
      if (selectionTip && rect.width > 0) {
        selectionTip.style.left = `${Math.max(90, Math.min(window.innerWidth - 90, rect.left + rect.width / 2))}px`;
        selectionTip.style.top = `${Math.max(58, rect.top - 8)}px`;
        selectionTip.textContent = 'AI 解读';
        selectionTip.style.display = 'block';
      }
      clearTimeout(selectionTimer);
      // 默认手动：只显示按钮；开启自动后才发送，且只对完整句子生效，避免划一个字就调用模型。
      if (autoSelection && !send.disabled && isCompleteSentence(text)) {
        if (selectionTip) selectionTip.textContent = 'AI 正在解读…';
        selectionTimer = setTimeout(sendSelectionExplanation, 1200);
      }
    };
    viewer.addEventListener('mouseup', () => setTimeout(scheduleSelectionExplanation, 50));
    viewer.addEventListener('keyup', (e) => { if (e.key === 'Shift' || e.key.startsWith('Arrow')) setTimeout(scheduleSelectionExplanation, 50); });
    document.addEventListener('selectionchange', () => {
      const text = window.getSelection()?.toString().trim() || '';
      if (!text) {
        lastSelection = '';
        pendingSelection = null;
        hideSelectionTip();
      }
    });
    document.addEventListener('copy', () => setTimeout(scheduleSelectionExplanation, 80));
    document.getElementById('reader-mode-toggle')?.addEventListener('click', () => {
      readerTextMode = !readerTextMode;
      const hint = document.getElementById('reader-mode-hint');
      if (hint) hint.textContent = readerTextMode ? '划词模式 · 选中句子后点「AI 解读」' : 'PDF 原文 · 点「划词模式」切换为可划选文本';
      renderReaderContent();
      if (readerTextMode) toast('已切换为划词模式：选中句子后点「AI 解读」，可在顶部开启自动解读');
    });
    pageInput?.addEventListener('change', () => { applyPage(pageInput.value); saveState(); });
    document.getElementById('reader-prev')?.addEventListener('click', () => { applyPage(currentPage - 1); saveState(); });
    document.getElementById('reader-next')?.addEventListener('click', () => { applyPage(currentPage + 1); saveState(); });
    document.getElementById('reader-save-state')?.addEventListener('click', () => {
      saveState();
      toast(`已记住第 ${currentPage} 页`);
    });
    document.getElementById('reader-notes-toggle')?.addEventListener('click', () => document.getElementById('reader-notes')?.classList.toggle('open'));
    document.getElementById('reader-notes-close')?.addEventListener('click', () => document.getElementById('reader-notes')?.classList.remove('open'));
    document.getElementById('reader-note-add')?.addEventListener('click', async () => {
      const value = (noteInput?.value || '').trim();
      if (!value) { toast('请先写一条笔记', true); return; }
      try {
        const created = await api(`/documents/${encodeURIComponent(id)}/notes`, { method: 'POST', body: { page_no: currentPage, note: value } });
        noteItems.unshift(created);
        noteInput.value = '';
        renderNotes();
        toast('笔记已保存');
      } catch (e) { toast(e.message, true); }
    });
  }

  // ------------------------------------------------------------------
  // session
  // ------------------------------------------------------------------
  async function sessionPage(id) {
    await loadMeta();
    try {
      const [session, doc, turns, followUps] = await Promise.all([
        api('/sessions/' + id),
        api('/sessions/' + id).then((s) => api('/documents/' + s.document_id)),
        api('/sessions/' + id + '/turns'),
        api('/sessions/' + id + '/follow-ups').catch(() => ({ items: [] })),
      ]);
      S.session = session;
      S.doc = doc;
      S.turns = turns.items || [];
      S.followUps = followUps.items || [];
      window.ALUN_ASSISTANT_CONTEXT = { documentId: session.document_id || '', sessionId: id };
      S.current = null;
      S.feedback = null;
      S.review = null;
      S.reviewReturn = null;
      S.hint = null;
      S.submitting = false;
      if (session.state === 'finished') { go('#/report/' + id); return; }
      // 如果服务端保留了未提交轮次，恢复当前题
      if (session.current_turn_id) {
        const q = await api(`/sessions/${id}/next-question`, { method: 'POST', body: {} });
        S.current = q;
        S.questionShownAt = Date.now();
      } else if (!S.turns.length) {
        drawSession();
        await fetchNext(id, false);
        return;
      } else {
        const last = S.turns[S.turns.length - 1];
        if (last && last.status === 'answered') {
          S.feedback = { ...last.evaluation, turn_id: last.turn_id, seq_no: last.seq_no };
        } else if (last && last.status === 'skipped') {
          await fetchNext(id, false);
          return;
        }
      }
      drawSession();
    } catch (e) {
      toast(e.message, true);
      if (e.code === 'SESSION_NOT_FOUND') go('#/');
    }
  }

  async function fetchNext(id, rerender = true) {
    S.busy = true; S.feedback = null; S.review = null; S.reviewReturn = null; S.hint = null;
    drawSession();
    try {
      const q = await api(`/sessions/${id}/next-question`, { method: 'POST', body: {} });
      S.current = q;
      S.questionShownAt = Date.now();
      S.busy = false;
      drawSession();
    } catch (e) {
      S.busy = false;
      if (e.code === 'NO_MORE_QUESTIONS') { await finishAndReport(id); return; }
      toast(e.message, true);
      drawSession();
    }
  }

  function planItems() {
    const items = S.session?.plan?.items || [];
    const done = new Set((S.turns || []).filter((t) => t.status === 'answered' || t.status === 'skipped').map((t) => t.seq_no));
    const currentSeq = S.current?.seq_no || null;
    return items.map((it) => ({ ...it, done: done.has(it.seq_no), active: currentSeq === it.seq_no }));
  }

  function drawSession() {
    if (!S.session) return;
    const session = S.session;
    const doc = S.doc || {};
    const items = planItems();
    const answered = items.filter((i) => i.done).length;
    const total = session.total_questions || session.plan?.total || items.length || 1;
    const progress = Math.round(answered / total * 100);
    const mode = MODE_LABEL[session.mode] || session.mode;
    const planHTML = items.map((it) => `
      <li class="plan-item ${it.done ? 'done' : ''} ${it.active ? 'active' : ''}">
        <span class="mark">${it.done ? '✓' : it.active ? '●' : it.seq_no}</span>
        <div style="min-width:0"><div class="t">${esc(it.section_title || '文档')}</div><div class="q">${esc(QTYPE_LABEL[it.q_type] || '')} · ${esc(it.bloom_level || '')}</div></div>
      </li>`).join('');

    const lastEval = S.feedback;
    const chat = [];
    const requested = Number(session.requested_questions || total);
    const countNote = requested > total ? `你选择了 ${requested} 题，按当前结构上限调整为 ${total} 题。` : '';
    const typeMap = { choice: '选择题', fill_blank: '填空题', true_false: '判断题', short_answer: '问答题' };
    const selectedTypes = session.question_types || [];
    const typeNote = selectedTypes.length && selectedTypes.length < 4 ? `本轮题型：${selectedTypes.map((x) => typeMap[x] || x).join('、')}。` : '';
    chat.push(`<div class="msg"><div class="avatar">论</div><div class="msg-body"><div class="msg-name">A论 <span class="tag tag-line">论文导读教练</span></div><div class="bubble">${esc(doc.title || '')}<br/>这一轮我们按「${esc(mode)}」走，共 ${total} 题。${countNote}${typeNote}每道题都挂在具体章节上，答完我会给出原文依据和改进建议。</div></div></div>`);

    if (S.busy) {
      chat.push(`<div class="thinking"><div class="dots"><i></i><i></i><i></i></div> A论正在翻回原文，出下一道题…</div>`);
    } else if (S.review) {
      chat.push(reviewCard(S.review));
    } else if (S.current) {
      chat.push(questionCard(S.current, items.find((i) => i.seq_no === S.current.seq_no)));
    } else if (lastEval) {
      chat.push(feedbackCard(lastEval));
    } else if (session.state === 'finished') {
      chat.push(`<div class="empty" style="padding:36px 0"><h2>这一轮结束了</h2><p>报告已生成，去看看你的章节掌握度。</p><div style="margin-top:18px"><a class="btn btn-accent" href="#/report/${esc(session.session_id)}">查看学习报告</a></div></div>`);
    } else {
      chat.push(`<div class="empty" style="padding:36px 0"><h2>准备好了</h2><p>A论会按结构配额出题，第一题马上就来。</p><div style="margin-top:18px"><button class="btn btn-accent" id="start-asking">开始第一题</button></div></div>`);
    }

    shell(`
      <div class="session-top">
        <div class="left">
          <a class="back-link" href="#/doc/${esc(session.document_id)}">${icon('back', 14)} 返回文档</a>
          <div style="width:1px;height:22px;background:var(--line)"></div>
          <div class="session-title">${esc(doc.title || '导读会话')}</div>
          <span class="tag tag-ink">${esc(mode)}</span>
          ${session.assignment_id ? '<span class="tag tag-gold">老师题卷</span>' : ''}
        </div>
        <div class="session-progress">
          <div class="track"><i style="width:${progress}%"></i></div>
          <span>${answered} / ${total}</span>
          <span class="tag tag-gold">能力 ${Math.round(session.ability?.theta_after || 500)}</span>
        </div>
      </div>
      <div class="session-layout">
        <aside class="plan-panel panel">
          <div class="panel-head"><h3>本轮题单</h3><span class="small muted">${progress}%</span></div>
          <ul class="plan-list">${planHTML || '<li class="plan-item">题单生成中…</li>'}</ul>
          ${(S.followUps || []).length ? `<div class="followup-history"><div class="panel-head"><h3>追问记录</h3><span class="small muted">${S.followUps.length} 条</span></div>${S.followUps.slice(-4).map((f) => `<div class="followup-history-row"><span>${esc(f.kind_label || f.kind)}</span><b>${f.status === 'answered' ? `得分 ${f.score}` : '待回答'}</b><p>${esc(excerpt(f.question || '', 70))}</p></div>`).join('')}</div>` : ''}
          <div style="padding:14px 18px;border-top:1px solid var(--line);display:flex;gap:8px">
            <a class="btn btn-quiet btn-sm" href="#/doc/${esc(session.document_id)}">看结构树</a>
            <button class="btn btn-quiet btn-sm" id="finish-session" style="margin-left:auto">提前结束</button>
          </div>
        </aside>
        <div class="chat-column">
          <div class="chat-day">${esc(docType(doc.doc_type))} · ${doc.page_count || '—'} 页 · ${mode}</div>
          ${chat.join('')}
        </div>
      </div>
    `, 'sessions');

    document.getElementById('start-asking')?.addEventListener('click', () => fetchNext(session.session_id));
    document.getElementById('finish-session')?.addEventListener('click', () => finishAndReport(session.session_id));
    bindQuestion();
    bindFeedback();
    bindReview();
  }

  function questionCard(payload, plan) {
    const q = payload.question;
    const page = plan?.page_start ? `p.${plan.page_start}` : '';
    return `
      <div class="question-card" id="question-card">
        <div class="question-head">
          <span class="tag tag-red">第 ${payload.seq_no} 题</span>
          <span class="tag tag-ink">${esc(QTYPE_LABEL[q.q_type] || q.q_type)}</span>
          <span class="tag tag-line">${esc(q.bloom_level)}</span>
          <span class="tag tag-line">难度 ${'●'.repeat(q.difficulty)}${'○'.repeat(5 - q.difficulty)}</span>
          <span class="spacer"></span>
          <span class="small muted">建议 ${Math.round((q.time_limit_sec || 90) / 60)} 分钟</span>
        </div>
        <div class="question-body">
          <div class="question-stem">${esc(q.stem)}</div>
          <div class="question-section">${icon('book', 13)} ${esc(plan?.section_title || '')}</div>
          <div class="answer-area" id="answer-area">${answerInput(q)}</div>
        </div>
        <div id="hint-slot">${S.hint ? `<div class="hint-note"><b>${S.hint.level} 级提示 · 扣 ${S.hint.penalty}%</b>${esc(S.hint.text)}</div>` : ''}</div>
        <div class="question-actions">
          <button class="btn btn-quiet btn-sm" id="prev-btn" data-seq="${payload.seq_no}" ${payload.seq_no <= 1 ? 'disabled' : ''}>← 上一题</button>
          <button class="btn btn-quiet btn-sm" id="hint-btn" ${S.hint?.level >= 3 ? 'disabled' : ''}>${icon('spark', 14)} ${S.hint ? '再给一点提示' : '给我一个提示'}</button>
          <button class="btn btn-quiet btn-sm" id="skip-btn">跳过</button>
          <span class="spacer"></span>
          <button class="btn btn-accent btn-lg" id="submit-btn">提交答案 ${icon('arrow', 15)}</button>
        </div>
      </div>`;
  }

  function answerInput(q) {
    if (q.q_type === 'single_choice' || q.q_type === 'multiple_choice') {
      return `<div class="options-list" data-type="${q.q_type}">${(q.options || []).map((o) => `
        <label class="option" data-key="${esc(o.key)}">
          <input type="${q.q_type === 'multiple_choice' ? 'checkbox' : 'radio'}" name="opt" value="${esc(o.key)}" />
          <span class="key">${esc(o.key)}</span><span>${esc(o.text)}</span>
        </label>`).join('')}</div>`;
    }
    if (q.q_type === 'true_false') {
      return `<div class="options-list" data-type="true_false">
        <label class="option" data-value="true"><input type="radio" name="tf" value="true" /><span class="key">对</span><span>该表述符合原文</span></label>
        <label class="option" data-value="false"><input type="radio" name="tf" value="false" /><span class="key">错</span><span>该表述与原文不符</span></label>
      </div>`;
    }
    if (q.q_type === 'fill_blank') {
      return `<input class="text-input" id="answer-input" placeholder="填入原文中的关键术语、数值或结论" autocomplete="off" />`;
    }
    return `<textarea class="text-area" id="answer-input" placeholder="用自己的话作答，尽量写出要点与依据；A论会对照原文评分。" maxlength="5000"></textarea>
      <div class="answer-foot"><span class="small muted">建议分点作答：结论 → 依据 → 页码</span><span class="char-count" id="char-count">0 / 5000</span></div>`;
  }

  function bindQuestion() {
    const card = document.getElementById('question-card');
    if (!card) return;
    card.querySelectorAll('.option').forEach((opt) => {
      opt.addEventListener('click', (e) => {
        const input = opt.querySelector('input');
        if (S.current?.question.q_type === 'multiple_choice') {
          input.checked = !input.checked;
          opt.classList.toggle('selected', input.checked);
        } else {
          card.querySelectorAll('.option').forEach((x) => x.classList.remove('selected'));
          card.querySelectorAll('input').forEach((x) => { x.checked = false; });
          input.checked = true;
          opt.classList.add('selected');
        }
        e.preventDefault();
      });
    });
    const area = document.getElementById('answer-input');
    const counter = document.getElementById('char-count');
    area?.addEventListener('input', () => { if (counter) counter.textContent = `${area.value.length} / 5000`; });
    document.getElementById('submit-btn')?.addEventListener('click', submitCurrent);
    document.getElementById('hint-btn')?.addEventListener('click', askHint);
    document.getElementById('skip-btn')?.addEventListener('click', skipCurrent);
    document.getElementById('prev-btn')?.addEventListener('click', async (e) => {
      const seq = Number(e.currentTarget.dataset.seq || 0);
      if (seq > 1) await openReview(seq - 1);
    });
  }

  function collectAnswer(q) {
    if (q.q_type === 'single_choice' || q.q_type === 'true_false') {
      const el = document.querySelector('#answer-area .option.selected');
      if (!el) return null;
      return q.q_type === 'true_false' ? el.dataset.value === 'true' : el.dataset.key;
    }
    if (q.q_type === 'multiple_choice') {
      const vals = [...document.querySelectorAll('#answer-area .option.selected')].map((x) => x.dataset.key);
      return vals.length ? vals : null;
    }
    const input = document.getElementById('answer-input');
    const v = (input?.value || '').trim();
    return v || null;
  }

  async function submitCurrent() {
    if (!S.current || S.submitting) return;
    const q = S.current.question;
    const value = collectAnswer(q);
    if (value === null) { toast(q.q_type === 'multiple_choice' ? '至少选择一个选项' : '先写下你的答案', true); return; }
    S.submitting = true;
    const btn = document.getElementById('submit-btn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> A论 正在对照原文'; }
    try {
      const res = await api(`/sessions/${S.session.session_id}/answers`, {
        method: 'POST',
        body: {
          turn_id: S.current.turn_id,
          answer: { type: q.q_type, value },
          time_spent_ms: Date.now() - (S.questionShownAt || Date.now()),
          hint_level: S.hint?.level || 0,
        },
      });
      S.feedback = { ...res, seq_no: S.current.seq_no, q_type: q.q_type };
      S.current = null;
      S.review = null;
      S.hint = null;
      S.submitting = false;
      const st = await api('/sessions/' + S.session.session_id);
      S.session = st;
      S.turns = (await api('/sessions/' + S.session.session_id + '/turns')).items || [];
      document.getElementById('question-card')?.remove();
      const column = document.querySelector('.chat-column');
      if (column) {
        const thinking = document.querySelector('.thinking');
        if (thinking) thinking.remove();
        column.insertAdjacentHTML('beforeend', feedbackCard(S.feedback));
        bindFeedback();
        column.insertAdjacentHTML('beforeend', `<div class="thinking" style="display:none"></div>`);
      }
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
      // 更新左侧进度
      const prog = document.querySelector('.session-progress');
      if (prog) {
        const done = (S.turns || []).filter((t) => t.status === 'answered' || t.status === 'skipped').length;
        prog.querySelector('.track i').style.width = Math.round(done / (S.session.total_questions || 1) * 100) + '%';
        prog.querySelector('span').textContent = `${done} / ${S.session.total_questions}`;
      }
    } catch (e) {
      S.submitting = false;
      toast(e.message, true);
      if (btn) { btn.disabled = false; btn.innerHTML = `提交答案 ${icon('arrow', 15)}`; }
    }
  }

  async function openReview(seq) {
    if (!S.session || !seq) return;
    try {
      const data = await api(`/sessions/${S.session.session_id}/turns/${seq}`);
      S.reviewReturn = { current: S.current, feedback: S.feedback };
      S.review = data;
      S.current = null;
      S.feedback = null;
      S.hint = null;
      drawSession();
    } catch (e) { toast(e.message, true); }
  }

  function answerText(value) {
    if (Array.isArray(value)) return value.join('、');
    if (typeof value === 'boolean') return value ? '正确' : '错误';
    if (value && typeof value === 'object') return JSON.stringify(value);
    return value === undefined || value === null || value === '' ? '未作答' : String(value);
  }

  function reviewCard(data) {
    const q = data.question || {};
    const ev = data.evaluation || {};
    const score = Math.round(Number(data.score) || 0);
    const correct = data.correct_answer;
    const correctKeys = Array.isArray(correct) ? correct.map(String) : (correct === undefined || correct === null ? [] : [String(correct)]);
    const selected = data.status === 'skipped' ? '已跳过（本题记 0 分）' : data.answer?.value;
    const selectedKeys = Array.isArray(selected) ? selected.map(String) : (selected === undefined || selected === null ? [] : [String(selected)]);
    const opts = (q.options || []).map((o) => {
      const isCorrect = correctKeys.includes(String(o.key)) || (q.q_type === 'true_false' && String(correct).toLowerCase() === String(o.key).toLowerCase());
      const isSelected = selectedKeys.includes(String(o.key));
      return `<div class="option ${isCorrect ? 'correct' : ''} ${isSelected && !isCorrect ? 'wrong' : ''}"><span class="key">${esc(o.key)}</span><span>${esc(o.text)}</span>${isSelected ? '<span class="tag tag-line" style="margin-left:auto">你的选择</span>' : ''}</div>`;
    }).join('');
    const evidence = (ev.evidence || data.reference_evidence || []).map((e) => `
      <div class="evidence-block"><div class="label">原文依据 · 第 ${e.page ?? '—'} 页</div><div class="quote">${esc(e.quote || '')}</div></div>`).join('');
    const rubric = (data.rubric || []).map((r) => `<li>${esc(r.point || r)}${r.weight ? ` <span class="muted">(${Math.round(Number(r.weight) * 100)}%)</span>` : ''}</li>`).join('');
    return `
      <div class="review-card" id="review-card">
        <div class="review-head">
          <div><span class="tag tag-red">第 ${data.seq_no} 题 · 复盘</span> <span class="tag tag-line">${esc(QTYPE_LABEL[q.q_type] || q.q_type || '')}</span></div>
          <div class="review-score ${score >= 80 ? 'good' : score >= 60 ? 'mid' : 'low'}"><b>${score}</b><span>分</span></div>
        </div>
        <div class="review-stem">${esc(q.stem || '')}</div>
        ${opts ? `<div class="options-list review-options">${opts}</div>` : ''}
        <div class="review-grid">
          <div class="review-block"><div class="review-label">你的作答</div><div class="review-answer">${esc(answerText(selected))}</div></div>
          <div class="review-block"><div class="review-label">标准答案</div><div class="review-answer correct-text">${esc(answerText(correct))}</div></div>
        </div>
        <div class="review-block"><div class="review-label">解析</div><div class="review-text">${esc(ev.feedback || data.explanation || '暂无解析')}</div>
          ${data.explanation && data.explanation !== ev.feedback ? `<div class="review-text muted" style="margin-top:6px">${esc(data.explanation)}</div>` : ''}
        </div>
        ${evidence ? `<div class="review-block"><div class="review-label">原文依据</div>${evidence}</div>` : ''}
        ${rubric ? `<div class="review-block"><div class="review-label">评分要点</div><ul class="review-rubric">${rubric}</ul></div>` : ''}
        <div class="review-actions">
          <button class="btn btn-quiet btn-sm" id="review-prev" ${data.seq_no <= 1 ? 'disabled' : ''}>← 上一题</button>
          <button class="btn btn-ghost btn-sm" id="review-return">回到当前题</button>
          <span class="spacer" style="flex:1"></span>
          <button class="btn btn-accent" id="review-next">下一题 ${icon('arrow', 14)}</button>
        </div>
      </div>`;
  }

  function bindReview() {
    document.getElementById('review-prev')?.addEventListener('click', (e) => openReview(Number(S.review?.seq_no || 0) - 1));
    document.getElementById('review-return')?.addEventListener('click', () => {
      S.review = null;
      S.current = S.reviewReturn?.current || null;
      S.feedback = S.reviewReturn?.feedback || null;
      drawSession();
    });
    document.getElementById('review-next')?.addEventListener('click', () => {
      const nextSeq = Number(S.review?.seq_no || 0) + 1;
      const hasNext = (S.turns || []).some((t) => t.seq_no === nextSeq && (t.status === 'answered' || t.status === 'skipped'));
      if (hasNext) openReview(nextSeq);
      else if (S.reviewReturn?.current || S.reviewReturn?.feedback) {
        S.review = null;
        S.current = S.reviewReturn?.current || null;
        S.feedback = S.reviewReturn?.feedback || null;
        drawSession();
      } else fetchNext(S.session.session_id);
    });
  }

  function feedbackCard(fb) {
    const score = Math.round(Number(fb.score) || 0);
    const dims = fb.dimension_scores || {};
    const dimHTML = Object.entries(dims).map(([k, v]) => `<div class="dim-row"><span>${esc(k)}</span><span class="dim-track"><i style="width:${pct(v)}%"></i></span><span>${Math.round(v)}</span></div>`).join('');
    const ev = (fb.evidence || [])[0];
    const action = (S.session?.state === 'finished' || fb.next?.action === 'finish') ? 'finish' : 'next';
    return `
      <div class="feedback-card" id="feedback-card">
        <div class="feedback-head">
          <div class="score-ring" style="--p:${pct(score)}"><b>${score}</b><small>分</small></div>
          <div class="feedback-title"><b>${score >= 80 ? '答得准确，证据也对上了' : score >= 60 ? '方向正确，还差一点证据' : '这一题需要回原文再看一遍'}</b><p>第 ${fb.seq_no || ''} 题 · ${score >= 80 ? '已计入本轮能力估计' : '建议结合原文要点复盘'}</p></div>
          <span class="spacer" style="flex:1"></span>
          <span class="tag ${scoreClass(score)}">${score >= 80 ? '掌握' : score >= 60 ? '部分掌握' : '待加强'}</span>
        </div>
        <div class="feedback-body">
          ${dimHTML ? `<div class="dimension-bars">${dimHTML}</div>` : ''}
          <div class="feedback-text">${esc(fb.feedback || '')}</div>
          ${ev ? `<div class="evidence-block"><div class="label">原文依据 · 第 ${ev.page ?? '—'} 页</div><div class="quote">${esc(ev.quote || '')}</div><div class="page">node ${esc(ev.node_id || '')}</div></div>` : ''}
          ${fb.turn_id ? `<div class="followup-row"><span>继续追问</span>
            <button class="btn btn-quiet btn-sm" data-followup="clarify" data-turn="${esc(fb.turn_id)}">澄清型</button>
            <button class="btn btn-quiet btn-sm" data-followup="evidence" data-turn="${esc(fb.turn_id)}">证据型</button>
            <button class="btn btn-quiet btn-sm" data-followup="transfer" data-turn="${esc(fb.turn_id)}">迁移型</button>
          </div><div id="followup-box"></div>` : ''}
        </div>
        <div class="feedback-foot">
          <button class="btn btn-quiet" id="feedback-prev" data-seq="${fb.seq_no}" ${Number(fb.seq_no) <= 1 ? 'disabled' : ''}>← 上一题</button>
          <button class="btn btn-ghost" id="view-explanation" data-seq="${fb.seq_no}">${icon('doc', 14)} 查看解析</button>
          <span class="spacer" style="flex:1"></span>
          <button class="btn btn-accent" id="feedback-next" data-action="${action}">${action === 'finish' ? '生成学习报告' : '下一题'} ${icon('arrow', 15)}</button>
        </div>
      </div>`;
  }

  async function openFollowUp(kind, turnId) {
    const box = document.getElementById('followup-box');
    if (!box) return;
    box.innerHTML = '<div class="small muted" style="padding:12px 0">正在从原文生成追问…</div>';
    try {
      const f = await api(`/sessions/${S.session.session_id}/follow-up`, { method: 'POST', body: { turn_id: turnId, kind } });
      S.followUps = S.followUps || [];
      S.followUps.push({ ...f, status: 'created', score: null });
      box.innerHTML = `<div class="followup-card">
        <div class="followup-head"><span class="tag tag-gold">${esc(f.kind_label)}追问</span><span class="small muted">不计入本轮总分</span></div>
        <p class="followup-q">${esc(f.question)}</p>
        <p class="small muted">${esc(f.rationale || '')}</p>
        ${(f.evidence || []).map((e) => `<div class="evidence-block"><div class="label">原文依据 · 第 ${e.page ?? '—'} 页</div><div class="quote">${esc(e.quote || '')}</div></div>`).join('')}
        <textarea class="text-area" id="followup-answer" placeholder="写下你的追问回答…" style="min-height:90px;margin-top:10px"></textarea>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:8px"><button class="btn btn-accent btn-sm" id="followup-submit">提交追问</button></div>
        <div id="followup-result"></div>
      </div>`;
      document.getElementById('followup-submit')?.addEventListener('click', async () => {
        const answer = document.getElementById('followup-answer')?.value.trim();
        if (!answer) { toast('请先写下追问回答', true); return; }
        const btn = document.getElementById('followup-submit');
        if (btn) { btn.disabled = true; btn.textContent = '评分中…'; }
        try {
          const r = await api(`/sessions/${S.session.session_id}/follow-up/answer`, { method: 'POST', body: { turn_id: turnId, kind, answer, follow_up_id: f.follow_up_id } });
          const stored = (S.followUps || []).find((x) => x.follow_up_id === r.follow_up_id);
          if (stored) { stored.status = 'answered'; stored.score = r.score; }
          const result = document.getElementById('followup-result');
          if (result) result.innerHTML = `<div class="followup-result"><b>追问得分 ${Math.round(Number(r.score) || 0)} / 100</b><p>${esc(r.feedback || '')}</p>${(r.hit || []).map((x) => `<span class="tag tag-jade">${esc(x)}</span>`).join(' ')}${(r.miss || []).map((x) => `<span class="tag tag-gold">待补：${esc(x)}</span>`).join(' ')}</div>`;
        } catch (e) { toast(e.message, true); }
        finally { if (btn) { btn.disabled = false; btn.textContent = '提交追问'; } }
      });
    } catch (e) { box.innerHTML = ''; toast(e.message, true); }
  }

  function bindFeedback() {
    const btn = document.getElementById('feedback-next');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      if (btn.dataset.action === 'finish') { await finishAndReport(S.session.session_id); return; }
      await fetchNext(S.session.session_id);
    });
    document.getElementById('view-explanation')?.addEventListener('click', (e) => openReview(Number(e.currentTarget.dataset.seq)));
    document.querySelectorAll('[data-followup]').forEach((b) => b.addEventListener('click', () => openFollowUp(b.dataset.followup, b.dataset.turn)));
    document.getElementById('feedback-prev')?.addEventListener('click', (e) => {
      const seq = Number(e.currentTarget.dataset.seq || 0);
      if (seq > 1) openReview(seq - 1);
    });
  }

  async function askHint() {
    if (!S.current) return;
    const level = (S.hint?.level || 0) + 1;
    try {
      const res = await api(`/sessions/${S.session.session_id}/hint`, { method: 'POST', body: { turn_id: S.current.turn_id, level } });
      S.hint = { level: res.level, text: res.hint, penalty: res.penalty_percent };
      const slot = document.getElementById('hint-slot');
      if (slot) slot.innerHTML = `<div class="hint-note"><b>${res.level} 级提示 · 扣 ${res.penalty_percent}%</b>${esc(res.hint)}</div>`;
      const btn = document.getElementById('hint-btn');
      if (btn) { btn.innerHTML = `${icon('spark', 14)} ${res.level >= 3 ? '提示已用完' : '再给一点提示'}`; if (res.level >= 3) btn.disabled = true; }
    } catch (e) { toast(e.message, true); }
  }

  async function skipCurrent() {
    if (!S.current) return;
    try {
      await api(`/sessions/${S.session.session_id}/skip`, { method: 'POST', body: { turn_id: S.current.turn_id, reason: 'user_skip' } });
      S.current = null; S.hint = null;
      const st = await api('/sessions/' + S.session.session_id);
      S.session = st;
      S.turns = (await api('/sessions/' + S.session.session_id + '/turns')).items || [];
      if (st.state === 'finished') { await finishAndReport(S.session.session_id); return; }
      await fetchNext(S.session.session_id);
    } catch (e) { toast(e.message, true); }
  }

  async function finishAndReport(id) {
    try {
      await api(`/sessions/${id}/finish`, { method: 'POST', body: { reason: 'user_finished' } });
    } catch (e) {
      if (!/已结束|finished/.test(e.message || '')) { /* 报告可能仍可生成 */ }
    }
    go('#/report/' + id);
  }

  // ------------------------------------------------------------------
  // report
  // ------------------------------------------------------------------
  async function reportPage(id) {
    await loadMeta();
    let session, report;
    try {
      session = await api('/sessions/' + id);
      try { report = await api(`/sessions/${id}/report`); }
      catch (e) {
        if (e.code === 'REPORT_NOT_READY') {
          shell(`<div class="empty"><div class="boot-seal" style="margin:0 auto 18px">论</div><h2>A论正在写报告</h2><p>正在汇总章节掌握度与错题清单…</p></div>`, 'sessions');
          setTimeout(() => reportPage(id), 1200);
          return;
        }
        throw e;
      }
    } catch (e) { toast(e.message, true); go('#/'); return; }
    state.report = { session, report };
    const doc = await api('/documents/' + session.document_id).catch(() => ({}));
    const assignment = session.assignment_id ? await api('/assignments/by-session/' + encodeURIComponent(id)).catch(() => null) : null;
    const s = report.summary || {};
    const mastery = report.section_mastery || [];
    const blooms = report.bloom_distribution || {};
    const wrong = report.wrong_questions || [];
    const score = Math.round(Number(s.score) || 0);
    const masteryHTML = mastery.length ? mastery.map((m) => `
      <div class="mastery-row">
        <span class="name" title="${esc(m.title)}">${esc(m.title)}</span>
        <span class="bar"><i style="width:${pct((m.score || 0) * 100)}%"></i></span>
        <span class="num">${Math.round((m.score || 0) * 100)}% · ${m.question_count}题</span>
      </div>`).join('') : '<div class="small muted">本轮没有足够数据</div>';
    const bloomHTML = Object.entries(blooms).filter(([, v]) => v.count > 0).map(([k, v]) => `
      <div class="bloom-row"><span>${esc(k)}</span><span class="bar"><i style="width:${pct((v.rate || 0) * 100)}%"></i></span><span class="num">${v.count} 题 · ${Math.round((v.rate || 0) * 100)}%</span></div>`).join('') || '<div class="small muted">尚无作答</div>';
    const wrongHTML = wrong.length ? wrong.map((w) => `
      <details class="wrong-item"><summary><span class="q">${esc(w.stem)}</span><span class="sc">${Math.round(w.score || 0)} 分</span></summary>
        <div class="wrong-body">
          <div><b>你的答案：</b>${esc(typeof w.user_answer === 'object' ? JSON.stringify(w.user_answer) : w.user_answer || '未作答')}</div>
          <div style="margin-top:6px"><b>参考要点：</b>${esc((w.reference_points || []).join('；'))}</div>
          <div style="margin-top:6px"><b>原文位置：</b>第 ${w.page ?? '—'} 页 · ${esc(w.node_title || '')}</div>
        </div>
      </details>`).join('') : '<div class="small muted">没有错题，或者还没有作答。</div>';

    shell(`
      <a class="back-link" href="#/doc/${esc(session.document_id)}">${icon('back', 14)} 返回文档</a>
      <section class="report-hero" style="margin-top:14px">
        <div>
          <div class="eyebrow">Reading Report · A论</div>
          <h1>${score >= 80 ? '读进去了。' : score >= 60 ? '骨架已经搭起来了。' : '先把结构再走一遍。'}</h1>
          <p>${esc(doc.title || '')} · ${esc(MODE_LABEL[session.mode] || session.mode)} · ${s.answered || 0}/${s.total || 0} 题 · 用时 ${Math.round((s.duration_sec || 0) / 60)} 分钟</p>
          <div class="strength-chips" style="margin-top:14px">
            ${(report.strengths || []).slice(0, 3).map((x) => `<span class="tag tag-jade">强项 · ${esc(x)}</span>`).join('')}
            ${(report.weaknesses || []).slice(0, 3).map((x) => `<span class="tag tag-red">待补 · ${esc(x)}</span>`).join('')}
            ${s.follow_ups?.total ? `<span class="tag tag-line">追问 ${s.follow_ups.answered}/${s.follow_ups.total}${s.follow_ups.avg_score !== null ? ' · 均分 ' + Math.round(s.follow_ups.avg_score) : ''}</span>` : ''}
          </div>
        </div>
        <div class="big-score"><b>${score}</b><span>/ 100<br/>能力 ${Math.round(s.ability || 500)}</span></div>
      </section>

      <div class="report-grid">
        <section class="report-card">
          <div class="panel-head"><h3>章节掌握度</h3><span class="small muted">按得分率排序</span></div>
          <div class="panel-body"><div class="mastery-list">${masteryHTML}</div></div>
        </section>
        <section class="report-card">
          <div class="panel-head"><h3>认知层级分布</h3><span class="small muted">Bloom · 得分率</span></div>
          <div class="panel-body"><div class="bloom-list">${bloomHTML}</div></div>
        </section>
      </div>

      <section class="report-card" style="margin-top:22px">
        <div class="panel-head"><h3>错题与原文位置</h3><span class="small muted">${wrong.length} 道需要回看</span></div>
        <div class="panel-body"><div class="wrong-list">${wrongHTML}</div></div>
      </section>

      <section class="report-card" style="margin-top:22px">
        <div class="panel-head"><h3>A论的三条建议</h3><span class="tag tag-gold">下一步</span></div>
        <div class="panel-body"><div class="recommend-list">${(report.recommendations || []).map((r) => `<div class="rec-item"><p>${esc(r)}</p></div>`).join('')}</div></div>
      </section>

      ${assignment?.teacher_comment ? `<section class="report-card" style="margin-top:22px;border-color:rgba(183,146,75,.45)"><div class="panel-head"><h3>教师复盘</h3><span class="tag tag-gold">${esc(assignment.status_label)}</span></div><div class="panel-body"><p style="line-height:1.9;margin:0 0 8px">${esc(assignment.teacher_comment)}</p><div class="small muted">${esc(assignment.teacher.display_name || assignment.teacher.username)} · ${esc(fmtDate(assignment.reviewed_at))}</div></div></section>` : ''}

      <div class="report-actions">
        <a class="btn btn-accent btn-lg" href="#/doc/${esc(session.document_id)}">${icon('spark', 16)} 再练一轮</a>
        <button class="btn btn-ghost btn-lg" id="export-report">${icon('doc', 16)} 导出报告</button>
        <a class="btn btn-quiet btn-lg" href="#/sessions">返回会话列表</a>
      </div>
    `, 'sessions');

    document.getElementById('export-report')?.addEventListener('click', async () => {
      try {
        const r = await api(`/reports/${report.report_id}/export?format=md`);
        const res = await fetch(r.download_url, { headers: AUTH.token ? { Authorization: 'Bearer ' + AUTH.token } : {} });
        if (!res.ok) throw new Error(`导出失败（${res.status}）`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `A论-学习报告-${report.report_id}.md`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        toast('报告已开始下载');
      } catch (e) { toast(e.message || '导出失败', true); }
    });
  }

  // ------------------------------------------------------------------
  // sessions list / about
  // ------------------------------------------------------------------
  async function sessionsPage() {
    await loadMeta();
    let data = { items: [] };
    try { data = await api('/sessions?limit=60'); } catch (e) { toast(e.message, true); }
    const rows = (data.items || []).map((s) => `
      <a class="session-row" href="#/${s.state === 'finished' ? 'report' : 'session'}/${esc(s.session_id)}">
        <div class="mode-mark">${s.mode === 'exam' ? '测' : s.mode === 'defense' ? '辩' : '读'}</div>
        <div class="info"><b>${esc(MODE_LABEL[s.mode] || s.mode)} · ${s.total_questions || 0} 题</b><span>${fmtDate(s.created_at)} · ${s.state === 'finished' ? '已完成' : '进行中'}</span></div>
        ${typeof s.score === 'number' ? `<div class="score">${Math.round(s.score)}<small style="font-size:11px;color:var(--muted)">分</small></div>` : '<span class="tag tag-line">继续</span>'}
      </a>`).join('');
    shell(`
      <div class="section-head" style="margin-top:8px"><h2>我的会话</h2><div class="rule"></div><a class="btn btn-accent btn-sm" href="#/">新建一轮</a></div>
      <div class="panel">${rows || '<div style="padding:40px;text-align:center;color:var(--muted)">还没有会话记录。</div>'}</div>
    `, 'sessions');
  }

  async function aboutPage() {
    await loadMeta();
    shell(`
      <div style="max-width:780px;margin:20px auto">
        <div class="eyebrow">About A论</div>
        <h1 style="font-size:46px;margin:12px 0 18px">不是替你读，<br/>而是陪你把论文读进去。</h1>
        <p style="font-size:16px;line-height:2;color:var(--ink-soft)">A论（A-Lun）是一个基于文档结构的提问智能体。它先解析论文的题名、摘要、章节、图表与参考文献，构建结构树；再从结构节点中抽取知识点与证据单元；最后按结构配额、认知层级与你的作答表现，逐题生成、校验、提问与判分。</p>
        <div class="report-grid" style="margin-top:30px">
          <div class="report-card"><div class="panel-head"><h3>结构先行</h3></div><div class="panel-body small muted" style="line-height:1.9">题目必须挂在章节、图表或结论上，避免只围绕摘要出泛泛的题。</div></div>
          <div class="report-card"><div class="panel-head"><h3>证据约束</h3></div><div class="panel-body small muted" style="line-height:1.9">无证据不成题、无证据不判分；答案与反馈都能回到原文页码。</div></div>
          <div class="report-card"><div class="panel-head"><h3>自适应</h3></div><div class="panel-body small muted" style="line-height:1.9">Elo 能力估计让下一题略高于当前水平，答对提高、答错降低。</div></div>
          <div class="report-card"><div class="panel-head"><h3>可解释</h3></div><div class="panel-body small muted" style="line-height:1.9">出题依据、证据页码、评分维度对用户可见，支持导出与复盘。</div></div>
        </div>
        <p class="small muted" style="margin-top:30px">版本 ${esc(state.meta?.version || '1.0.0')} · ${state.meta?.llm?.enabled ? '导读模型已连接' : '本地规则模式'}</p>
      </div>
    `, 'about');
  }

  // ------------------------------------------------------------------
  // router
  // ------------------------------------------------------------------
  async function route() {
    clearTimeout(state.poll);
    document.body.classList.remove('reader-mode');
    if (state.readerUrl) { URL.revokeObjectURL(state.readerUrl); state.readerUrl = null; }
    const hash = (location.hash || '').replace(/^#/, '') || '/';
    const cleanHash = hash.split('?')[0];
    const parts = cleanHash.split('/').filter(Boolean);
    if (parts[0] === 'login' || parts[0] === 'register') {
      loginPage(parts[0] === 'register' || hash.includes('register=1') ? 'register' : 'login');
      return;
    }
    if (!AUTH.token) {
      history.replaceState(null, '', '#/login');
      loginPage();
      return;
    }
    if (!AUTH.user) {
      const user = await fetchMe();
      if (!user) { history.replaceState(null, '', '#/login'); loginPage('login', '登录已过期，请重新登录'); return; }
    }
    if (!parts.length) { home(); return; }
    if (parts[0] === 'read' && parts[1]) { readerPage(parts[1]); return; }
    if (parts[0] === 'doc' && parts[1]) { docPage(parts[1]); return; }
    if (parts[0] === 'session' && parts[1]) { sessionPage(parts[1]); return; }
    if (parts[0] === 'report' && parts[1]) { reportPage(parts[1]); return; }
    if (parts[0] === 'sessions') { sessionsPage(); return; }
    if (parts[0] === 'about') { aboutPage(); return; }
    home();
  }

  window.addEventListener('hashchange', () => { route(); });
  route();
})();
