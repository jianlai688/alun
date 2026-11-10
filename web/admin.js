/* A论 · 管理控制台 */
(() => {
  'use strict';
  const app = document.getElementById('admin-app');
  const toasts = document.getElementById('toasts');
  const API = '/api/v1';
  const AUTH = {
    token: localStorage.getItem('alun_token') || '',
    user: (() => { try { return JSON.parse(localStorage.getItem('alun_user') || 'null'); } catch { return null; } })(),
  };
  const state = {
    tab: 'overview', overview: null, users: null, events: null, online: null,
    settings: null, usage: null, lastEventId: 0, timer: null, timerTab: null, loading: false,
  };

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pct = (n) => Math.round((Number(n) || 0) * 100);
  const fmt = (n) => Number(n || 0).toLocaleString('zh-CN');
  const fmtTokens = (n) => {
    n = Number(n) || 0;
    if (n >= 100000000) return (n / 100000000).toFixed(2) + '亿';
    if (n >= 10000) return (n / 10000).toFixed(1) + '万';
    return fmt(n);
  };
  const fmtBytes = (n) => {
    n = Number(n) || 0;
    if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n >= 1024) return Math.round(n / 1024) + ' KB';
    return n + ' B';
  };
  const fmtTime = (s) => {
    if (!s) return '—';
    try {
      const d = new Date(s);
      return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    } catch { return s; }
  };
  const ago = (s) => {
    if (!s) return '—';
    const sec = Math.max(0, Math.round((Date.now() - new Date(s).getTime()) / 1000));
    if (sec < 60) return `${sec} 秒前`;
    if (sec < 3600) return `${Math.round(sec / 60)} 分钟前`;
    if (sec < 86400) return `${Math.round(sec / 3600)} 小时前`;
    return `${Math.round(sec / 86400)} 天前`;
  };

  const EVENT_LABEL = {
    login: '登录', login_failed: '登录失败', logout: '退出', register: '注册',
    document_upload: '上传文档', document_ready: '文档解析完成', document_reparse: '重新解析',
    session_start: '开始会话', question_asked: '生成题目', answer_submitted: '提交答案',
    hint: '请求提示', skip: '跳过题目', report_ready: '生成报告', settings_update: '修改模型设置',
    user_status: '变更用户状态', assistant_ask: 'AI 问答', document_delete: '删除论文',
  };
  const EVENT_ICON = {
    login: '登', login_failed: '!', logout: '退', register: '注', document_upload: '传',
    document_ready: '解', document_reparse: '重', session_start: '始', question_asked: '题',
    answer_submitted: '答', hint: '示', skip: '跳', report_ready: '报', settings_update: '设', user_status: '户', assistant_ask: '问', document_delete: '删',
  };

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
    if (res.status === 401 && !path.startsWith('/auth/')) { clearAuth(); loginView('登录已过期，请重新登录'); throw new Error('登录已过期'); }
    if (!res.ok) {
      const err = new Error((data && data.message) || `请求失败（${res.status}）`);
      Object.assign(err, data || {});
      throw err;
    }
    return data;
  }

  function toast(msg, err = false) {
    const el = document.createElement('div');
    el.className = 'toast' + (err ? ' err' : '');
    el.textContent = msg;
    toasts.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; }, 3200);
    setTimeout(() => el.remove(), 3600);
  }

  function clearAuth() {
    AUTH.token = '';
    AUTH.user = null;
    localStorage.removeItem('alun_token');
    localStorage.removeItem('alun_user');
  }

  function saveAuth(data) {
    AUTH.token = data.access_token;
    AUTH.user = data.user;
    localStorage.setItem('alun_token', data.access_token);
    localStorage.setItem('alun_user', JSON.stringify(data.user));
  }

  // ------------------------------------------------------------------
  // login
  // ------------------------------------------------------------------
  function loginView(message = '') {
    clearTimeout(state.timer);
    app.innerHTML = `
      <div class="admin-login">
        <section class="admin-login-left">
          <a class="brand" href="/admin">
            <span class="brand-seal">论</span>
            <span class="brand-name"><b>A论</b><span>${location.pathname.startsWith('/teacher') || AUTH.user?.role === 'teacher' ? 'TEACHER CONSOLE' : 'ADMIN CONSOLE'}</span></span>
          </a>
          <div class="admin-login-copy">
            <div class="eyebrow">A-LUN · CONTROL ROOM</div>
            <h1>看见每一次<br/>提问与回答。</h1>
            <p>用户使用情况、回答准确率、模型输出审计、实时学习行为与 token 消耗，集中在同一张控制台里。</p>
            <div class="admin-login-badges">
              <span>实时监控</span><span>模型设置</span><span>用量审计</span><span>幻觉率</span>
            </div>
          </div>
          <div class="login-meta" style="color:rgba(247,242,232,.4)"><span>A论 · A-Lun</span><i></i><span>本地部署</span></div>
        </section>
        <section class="admin-login-right">
          <div class="admin-login-card">
            <span class="tag tag-red">管理员 / 教师登录</span>
            <h2>进入控制台</h2>
            <p class="small muted">管理员进入控制台；教师进入教学闭环。</p>
            ${message ? `<div class="alert" style="margin:16px 0">${esc(message)}</div>` : ''}
            <form id="admin-login-form">
              <div class="field"><label for="admin-username">账号</label><input class="text-input" id="admin-username" autocomplete="username" placeholder="admin / teacher" required /></div>
              <div class="field"><label for="admin-password">密码</label><input class="text-input" id="admin-password" type="password" autocomplete="current-password" placeholder="请输入密码" required /></div>
              <button class="btn btn-primary btn-block btn-lg" id="admin-login-btn" type="submit">登录控制台 →</button>
            </form>
            <div class="admin-hint"><b style="color:var(--ink-soft)">默认管理员</b><br/><code>admin</code> / <code>admin123</code> · 教师 <code>teacher</code> / <code>teacher123</code>（首次启动生成，请尽快修改）</div>
            <div class="login-switch" style="text-align:left;margin-top:16px">返回 <a href="/">A论 用户端</a> · 教师入口 <a href="/teacher">/teacher</a></div>
          </div>
        </section>
      </div>`;
    document.getElementById('admin-login-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const username = document.getElementById('admin-username').value.trim();
      const password = document.getElementById('admin-password').value;
      const btn = document.getElementById('admin-login-btn');
      btn.disabled = true; btn.innerHTML = '<span class="spinner" style="border-color:rgba(255,255,255,.3);border-top-color:#fff"></span> 正在验证';
      try {
        const data = await api('/auth/login', { method: 'POST', body: { username, password } });
        if (!['admin', 'teacher'].includes(data.user?.role)) {
          clearAuth();
          throw new Error('该账号不是管理员或教师账号');
        }
        saveAuth(data);
        toast('欢迎回来，' + (data.user.display_name || data.user.username));
        dashboard();
      } catch (err) {
        btn.disabled = false; btn.innerHTML = '登录控制台 →';
        const box = document.querySelector('.admin-login-card .alert');
        if (box) box.textContent = err.message;
        else document.getElementById('admin-login-form')?.insertAdjacentHTML('beforebegin', `<div class="alert" style="margin:16px 0">${esc(err.message)}</div>`);
      }
    });
  }

  // ------------------------------------------------------------------
  // shell
  // ------------------------------------------------------------------
  const TITLES = {
    overview: ['控制台总览', '关键指标与最近动态'],
    teaching: ['教学闭环', '布置题卷、查看作答与复盘'],
    quality: ['质量验收', '黄金集、指标看板与缺陷台账'],
    documents: ['论文管理', '查看、检索与删除入库论文'],
    questions: ['题库与题卷', '题目复核、难度调整与多难度题卷'],
    users: ['用户使用情况', '每位用户的学习与模型使用'],
    monitor: ['实时监控', '在线用户与行为事件流'],
    settings: ['导读模型设置', '模型、采样参数与导读策略'],
    usage: ['Token 用量', '按日、按类型、按模型的调用计量'],
  };

  function dashboard() {
    const isTeacher = AUTH.user?.role === 'teacher';
    const navItems = isTeacher
      ? `${navButton('overview', '总览', '览')}${navButton('teaching', '教学闭环', '教')}`
      : `${navButton('overview', '总览', '览')}${navButton('teaching', '教学闭环', '教')}${navButton('quality', '质量验收', '质')}${navButton('documents', '论文', '文')}${navButton('questions', '题库', '题')}${navButton('users', '用户', '户')}${navButton('monitor', '实时监控', '监')}${navButton('settings', '模型设置', '模')}${navButton('usage', 'Token 用量', '量')}`;
    app.innerHTML = `
      <div class="admin-shell">
        <aside class="admin-side">
          <a class="brand" href="/admin">
            <span class="brand-seal">论</span>
            <span class="brand-name"><b>A论</b><span>${location.pathname.startsWith('/teacher') || AUTH.user?.role === 'teacher' ? 'TEACHER CONSOLE' : 'ADMIN CONSOLE'}</span></span>
          </a>
          <nav class="admin-nav">${navItems}</nav>
          <div class="admin-side-foot">
            <span>当前账号</span>
            <b style="color:rgba(247,242,232,.85);font-size:12px">${esc(AUTH.user?.display_name || AUTH.user?.username || '')}</b>
            <a href="/">← 返回 A论 用户端</a>
            <a href="#" id="admin-logout">退出登录</a>
          </div>
        </aside>
        <main class="admin-main">
          <div class="admin-top">
            <div><h1 id="admin-title">控制台总览</h1><div class="sub" id="admin-sub">关键指标与最近动态</div></div>
            <div class="admin-top-right">
              <button class="btn btn-ghost btn-sm" id="admin-ai-btn" type="button">✦ A论 AI</button>
              <span class="admin-live"><i></i> 实时连接</span>
              <span class="tag tag-line" id="admin-updated">刚刚更新</span>
            </div>
          </div>
          <div class="admin-content" id="admin-content"><div class="empty-admin">正在加载…</div></div>
        </main>
      </div>`;
    document.querySelectorAll('.admin-nav button').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));
    document.getElementById('admin-ai-btn')?.addEventListener('click', () => window.alunOpenAssistant?.());
    document.getElementById('admin-logout')?.addEventListener('click', (e) => {
      e.preventDefault();
      clearAuth();
      loginView('已退出登录');
    });
    setTab(isTeacher ? 'teaching' : 'overview');
  }

  function navButton(tab, label, ico) {
    return `<button data-tab="${tab}" class="${state.tab === tab ? 'active' : ''}"><span class="ico">${ico}</span>${label}</button>`;
  }

  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll('.admin-nav button').forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
    const [title, sub] = TITLES[tab] || TITLES.overview;
    document.getElementById('admin-title').textContent = title;
    document.getElementById('admin-sub').textContent = sub;
    clearTimeout(state.timer);
    loadTab();
  }

  async function loadTab() {
    const box = document.getElementById('admin-content');
    if (!box) return;
    box.innerHTML = '<div class="empty-admin">正在加载数据…</div>';
    try {
      if (state.tab === 'overview') await renderOverview();
      else if (state.tab === 'teaching') await renderTeaching();
      else if (state.tab === 'quality') await renderQuality();
      else if (state.tab === 'documents') await renderDocuments();
      else if (state.tab === 'questions') await renderQuestions();
      else if (state.tab === 'users') await renderUsers();
      else if (state.tab === 'monitor') await renderMonitor();
      else if (state.tab === 'settings') await renderSettings();
      else if (state.tab === 'usage') await renderUsage();
      document.getElementById('admin-updated').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    } catch (e) {
      box.innerHTML = `<div class="empty-admin">加载失败：${esc(e.message)}</div>`;
    }
  }

  // ------------------------------------------------------------------
  // overview
  // ------------------------------------------------------------------
  async function renderOverview() {
    const data = await api('/admin/overview');
    state.overview = data;
    const c = data.cards;
    const maxAnswers = Math.max(1, ...data.trend.map((x) => x.answers));
    const bars = data.trend.map((x) => `
      <div class="bar-col">
        <div class="num">${x.answers || ''}</div>
        <div class="bar" style="height:${Math.max(3, Math.round((x.answers / maxAnswers) * 125))}px"><span>${x.date} · ${x.answers} 题 · ${fmtTokens(x.tokens)} tokens</span></div>
        <div class="day">${x.date.slice(5)}</div>
      </div>`).join('');
    const events = (data.recent_events || []).slice(0, 8).map(eventRow).join('') || '<div class="empty-admin">暂无事件</div>';
    document.getElementById('admin-content').innerHTML = `
      <div class="admin-cards">
        ${card('注册用户', fmt(c.users), '含管理员与普通用户', '')}
        ${card('今日活跃', fmt(c.active_today), '有登录或学习行为', 'accent')}
        ${card('回答准确率', pct(c.accuracy) + '<small>%</small>', `${fmt(c.answers)} 次已判分作答`, '')}
        ${card('模型幻觉率', pct(c.hallucination_rate) + '<small>%</small>', `${fmt(c.hallucination_invalid)} / ${fmt(c.hallucination_total)} 次模型输出未过证据校验`, '')}
        ${card('文档总量', fmt(c.documents), '已入库可提问', '')}
        ${card('会话总量', fmt(c.sessions), `今日 ${fmt(c.today_answers)} 次作答`, '')}
        ${card('Token 总用量', fmtTokens(c.tokens), `今日 ${fmtTokens(c.today_tokens)} · 积分 ${c.credits}`, '')}
        ${card('导读模型', data.model.enabled ? '已启用' : '已关闭', esc(data.model.model || '未配置'), '')}
      </div>
      <div class="admin-grid2">
        <section class="admin-panel">
          <div class="admin-panel-head"><h2>近 14 天作答趋势</h2><div class="chart-legend"><span><i style="background:var(--vermilion)"></i>作答数</span></div></div>
          <div class="admin-panel-body"><div class="bar-chart">${bars}</div></div>
        </section>
        <section class="admin-panel">
          <div class="admin-panel-head"><h2>最近动态</h2><a class="small muted" href="#" data-go="monitor">实时监控 →</a></div>
          <div class="admin-panel-body"><div class="event-list">${events}</div></div>
        </section>
      </div>`;
    document.querySelector('[data-go="monitor"]')?.addEventListener('click', (e) => { e.preventDefault(); setTab('monitor'); });
    state.timer = setTimeout(() => { if (state.tab === 'overview') loadTab(); }, 12000);
  }

  function card(label, value, hint, cls = '') {
    return `<div class="admin-card ${cls}"><div class="label"><span>${label}</span><span style="font-size:9px">A论</span></div><div class="value">${value}</div><div class="hint">${hint}</div></div>`;
  }

  function eventRow(e) {
    const label = EVENT_LABEL[e.event_type] || e.event_type;
    const icon = EVENT_ICON[e.event_type] || '·';
    const who = e.username || e.user_id || '系统';
    let summary = '';
    const d = e.detail || {};
    if (e.event_type === 'answer_submitted') summary = `${who} 得分 ${d.score ?? '—'} · ${d.q_type || ''}`;
    else if (e.event_type === 'question_asked') summary = `${who} 生成第 ${d.seq_no || '?'} 题 · ${d.q_type || ''}`;
    else if (e.event_type === 'document_upload') summary = `${who} 上传《${(d.file_name || '').slice(0, 22)}》`;
    else if (e.event_type === 'document_ready') summary = `${who} 文档解析完成 · 质量 ${Math.round((d.quality || 0) * 100)}%`;
    else if (e.event_type === 'document_reparse') summary = `${who} 重新解析文档`;
    else if (e.event_type === 'session_start') summary = `${who} 开始一轮${d.mode === 'exam' ? '测评' : d.mode === 'defense' ? '答辩' : '导读'} · ${d.total || '?'} 题`;
    else if (e.event_type === 'report_ready') summary = `${who} 生成学习报告 · ${d.score ?? '—'} 分`;
    else if (e.event_type === 'hint') summary = `${who} 请求 ${d.level || ''} 级提示`;
    else if (e.event_type === 'skip') summary = `${who} 跳过一道题`;
    else if (e.event_type === 'login') summary = `${who} 登录${d.role ? ' · ' + d.role : ''}`;
    else if (e.event_type === 'login_failed') summary = `${who} 登录失败`;
    else if (e.event_type === 'settings_update') summary = `${who} 更新模型设置`;
    else if (e.event_type === 'register') summary = `${who} 注册新账号`;
    else if (e.event_type === 'assistant_ask') summary = `${who} 问 AI：${(d.question || '').slice(0, 36)} · ${d.sources || 0} 个外部来源`;
    else if (e.event_type === 'document_delete') summary = `${who} 删除论文《${(d.title || '').slice(0, 28)}》`;
    else summary = `${who} · ${JSON.stringify(d).slice(0, 70)}`;
    return `<div class="event-row"><div class="event-icon">${icon}</div><div class="event-main"><b>${label}</b><p>${esc(summary)}</p></div><div class="event-time">${esc(ago(e.ts))}</div></div>`;
  }

  // ------------------------------------------------------------------
  // documents
  // ------------------------------------------------------------------
  async function renderDocuments() {
    const data = await api('/admin/documents');
    state.documents = data.items || [];
    const rows = state.documents.map((d) => {
      const st = d.stats || {};
      const quality = Math.round((d.quality?.score || 0) * 100);
      return `<tr data-doc-row="${esc(d.document_id)}" data-title="${esc((d.title || '').toLowerCase())}">
        <td><div class="user-cell"><span class="avatar" style="background:var(--vermilion)">文</span><div><b>${esc(d.title)}</b><span>${esc(d.file_name || '')} · ${d.page_count} 页</span></div></div></td>
        <td><span class="small">${esc(d.owner?.display_name || d.owner?.username || d.owner?.user_id || '—')}</span><br/><span class="small muted">${esc(d.owner?.username || '')}</span></td>
        <td><span class="tag tag-line">${esc(d.doc_type || 'other')}</span></td>
        <td class="num-cell">${st.section_count || 0} 节 · ${st.kp_count || 0} 知识点</td>
        <td><span class="meter"><i style="width:${quality}%"></i></span><span class="num-cell">${quality}%</span></td>
        <td class="num-cell">${fmtBytes(d.size_bytes)}</td>
        <td class="num-cell">${esc(fmtTime(d.created_at))}</td>
        <td><button class="btn btn-quiet btn-sm" data-del-doc="${esc(d.document_id)}">删除</button></td>
      </tr>`;
    }).join('');
    document.getElementById('admin-content').innerHTML = `
      <div class="admin-toolbar"><div class="left">共 <b>${state.documents.length}</b> 篇论文 · 删除后将从用户文档库移除，并清理原始文件</div><input class="admin-search" id="doc-search" placeholder="搜索论文标题或文件名…" /></div>
      <section class="admin-panel">
        <div class="admin-panel-head"><h2>入库论文</h2><span class="small muted">管理员可删除任意用户的论文</span></div>
        <div style="overflow:auto"><table class="admin-table"><thead><tr><th>论文</th><th>所有者</th><th>类型</th><th>结构</th><th>解析质量</th><th>大小</th><th>入库时间</th><th></th></tr></thead><tbody id="doc-tbody">${rows || '<tr><td colspan="8"><div class="empty-admin">暂无入库论文</div></td></tr>'}</tbody></table></div>
      </section>`;
    document.getElementById('doc-search')?.addEventListener('input', (e) => {
      const q = e.target.value.trim().toLowerCase();
      document.querySelectorAll('[data-doc-row]').forEach((tr) => {
        tr.style.display = !q || (tr.dataset.title || '').includes(q) ? '' : 'none';
      });
    });
    document.querySelectorAll('[data-del-doc]').forEach((btn) => btn.addEventListener('click', async () => {
      const id = btn.dataset.delDoc;
      const row = state.documents.find((x) => x.document_id === id);
      if (!row) return;
      if (!confirm(`确认删除《${row.title}》？\n\n删除后：\n· 从用户文档库移除\n· 清理原始文件\n· 相关会话不再展示\n\n该操作不可撤销。`)) return;
      btn.disabled = true; btn.textContent = '删除中…';
      try {
        await api(`/admin/documents/${id}?purge=1`, { method: 'DELETE' });
        toast('论文已删除，原始文件已清理');
        await renderDocuments();
      } catch (e) {
        btn.disabled = false; btn.textContent = '删除';
        toast(e.message, true);
      }
    }));
  }

  // ------------------------------------------------------------------
  // question bank
  // ------------------------------------------------------------------
  async function renderQuestions() {
    const [docData, qData, setData] = await Promise.all([
      api('/admin/documents'),
      api('/questions?limit=100&status=active&include_answer=1'),
      api('/question-sets'),
    ]);
    const docs = docData.items || [];
    const questions = qData.items || [];
    const sets = setData.items || [];
    const docTitle = Object.fromEntries(docs.map((d) => [d.document_id, d.title]));
    const qType = { single_choice: '单选', multiple_choice: '多选', true_false: '判断', fill_blank: '填空', short_answer: '简答' };
    const diff = (n) => ({ 1: '1 入门', 2: '2 基础', 3: '3 适中', 4: '4 挑战', 5: '5 困难' }[n] || `难度 ${n}`);
    const rows = questions.map((q) => `<tr>
      <td><div class="user-cell"><span class="avatar" style="background:var(--ink-soft)">题</span><div><b class="q-stem">${esc(q.stem)}</b><span>${esc(docTitle[q.document_id] || q.document_id)} · ${esc(qType[q.q_type] || q.q_type)}</span></div></div></td>
      <td><span class="tag tag-line">${esc(diff(q.difficulty))}</span></td>
      <td class="num-cell">${esc(q.bloom_level || '—')}</td>
      <td><span class="tag ${q.status === 'active' ? 'tag-jade' : 'tag-gold'}">${q.status === 'active' ? '已启用' : q.status}</span></td>
      <td><button class="btn btn-quiet btn-sm" data-q-view="${esc(q.q_id)}">预览</button> <button class="btn btn-quiet btn-sm" data-q-edit="${esc(q.q_id)}">编辑</button> <button class="btn btn-quiet btn-sm" data-q-diff="${esc(q.q_id)}">改难度</button> <button class="btn btn-quiet btn-sm" data-q-retire="${esc(q.q_id)}">下架</button></td>
    </tr>`).join('');
    const setRows = sets.map((s) => `<tr>
      <td><b>${esc(s.title)}</b></td>
      <td><span class="tag tag-line">${esc({ easy: '轻松', medium: '适中', hard: '挑战', mixed: '混合' }[s.difficulty] || s.difficulty)}</span></td>
      <td class="num-cell">${s.question_count} 题</td>
      <td class="num-cell">${esc(fmtTime(s.created_at))}</td>
      <td><button class="btn btn-quiet btn-sm" data-set-del="${esc(s.set_id)}">删除</button></td>
    </tr>`).join('');
    const docOptions = docs.map((d) => `<option value="${esc(d.document_id)}">${esc(d.title)}</option>`).join('');
    document.getElementById('admin-content').innerHTML = `
      <div class="admin-toolbar"><div class="left">共 <b>${questions.length}</b> 道启用题目 · 题目来自用户会话与生成器</div><span class="pill ok">可复核</span></div>
      <section class="admin-panel">
        <div class="admin-panel-head"><h2>生成多难度题卷</h2><span class="small muted">从当前启用题库按难度抽题，生成可复用的题卷</span></div>
        <div class="set-form">
          <select class="text-input" id="set-doc">${docOptions || '<option value="">暂无文档</option>'}</select>
          <select class="text-input" id="set-diff"><option value="easy">轻松（难度 1—2）</option><option value="medium" selected>适中（难度 3）</option><option value="hard">挑战（难度 4—5）</option><option value="mixed">混合难度</option></select>
          <input class="text-input" id="set-count" type="number" min="1" max="30" value="12" />
          <button class="btn btn-accent btn-sm" id="set-create">生成题卷</button>
        </div>
      </section>
      <section class="admin-panel" style="margin-top:16px">
        <div class="admin-panel-head"><h2>题卷列表</h2><span class="small muted">同一文档可生成多套不同难度题卷</span></div>
        <div style="overflow:auto"><table class="admin-table"><thead><tr><th>题卷</th><th>难度</th><th>题量</th><th>创建时间</th><th></th></tr></thead><tbody>${setRows || '<tr><td colspan="5"><div class="empty-admin">还没有题卷，先生成一套</div></td></tr>'}</tbody></table></div>
      </section>
      <section class="admin-panel" style="margin-top:16px">
        <div class="admin-panel-head"><h2>启用题库</h2><span class="small muted">预览答案、调整难度或下架</span></div>
        <div style="overflow:auto"><table class="admin-table"><thead><tr><th>题目</th><th>难度</th><th>认知层级</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows || '<tr><td colspan="5"><div class="empty-admin">暂无启用题目，完成一轮会话后会自动入库</div></td></tr>'}</tbody></table></div>
      </section>`;
    document.getElementById('set-create')?.addEventListener('click', async (e) => {
      const btn = e.currentTarget; btn.disabled = true; btn.textContent = '生成中…';
      try {
        const r = await api('/question-sets', { method: 'POST', body: {
          document_id: document.getElementById('set-doc').value,
          difficulty: document.getElementById('set-diff').value,
          count: Number(document.getElementById('set-count').value || 12),
        } });
        toast(r.warning || `题卷已生成：${r.question_count} 题`, !!r.warning);
        await renderQuestions();
      } catch (err) { toast(err.message, true); btn.disabled = false; btn.textContent = '生成题卷'; }
    });
    document.querySelectorAll('[data-set-del]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('删除这套题卷？')) return;
      await api('/question-sets/' + b.dataset.setDel, { method: 'DELETE' });
      toast('题卷已删除'); await renderQuestions();
    }));
    document.querySelectorAll('[data-q-view]').forEach((b) => b.addEventListener('click', () => {
      const q = questions.find((x) => x.q_id === b.dataset.qView);
      if (!q) return;
      const answer = q.answer?.value ?? q.answer?.correct ?? JSON.stringify(q.answer || {});
      const ev = (q.evidence || []).map((e) => `第 ${e.page} 页：${e.quote}`).join('\n');
      alert(`${q.stem}\n\n答案：${answer}\n\n${ev ? '原文：' + ev : ''}`);
    }));
    document.querySelectorAll('[data-q-edit]').forEach((b) => b.addEventListener('click', async () => {
      const q = questions.find((x) => x.q_id === b.dataset.qEdit);
      if (!q) return;
      const stem = prompt('编辑题干', q.stem);
      if (!stem || stem.trim() === q.stem) return;
      try {
        await api('/questions/' + b.dataset.qEdit, { method: 'PATCH', body: { stem: stem.trim() } });
        await api('/questions/' + b.dataset.qEdit + '/review', { method: 'POST', body: { decision: 'approve' } });
        toast('题干已更新'); await renderQuestions();
      } catch (e) { toast(e.message, true); }
    }));
    document.querySelectorAll('[data-q-diff]').forEach((b) => b.addEventListener('click', async () => {
      const q = questions.find((x) => x.q_id === b.dataset.qDiff);
      const value = prompt('设置难度 1—5', q?.difficulty || 3);
      if (!value || !/^[1-5]$/.test(value)) return;
      try {
        await api('/questions/' + b.dataset.qDiff, { method: 'PATCH', body: { difficulty: Number(value) } });
        await api('/questions/' + b.dataset.qDiff + '/review', { method: 'POST', body: { decision: 'approve' } });
        toast('难度已更新'); await renderQuestions();
      } catch (e) { toast(e.message, true); }
    }));
    document.querySelectorAll('[data-q-retire]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('下架这道题？')) return;
      await api('/questions/' + b.dataset.qRetire + '/review', { method: 'POST', body: { decision: 'retire' } });
      toast('题目已下架'); await renderQuestions();
    }));
  }

  // ------------------------------------------------------------------
  // users
  // ------------------------------------------------------------------
  async function renderUsers() {
    const data = await api('/admin/users');
    state.users = data.items || [];
    const rows = state.users.map((u) => `
      <tr>
        <td><div class="user-cell"><span class="avatar">${esc((u.display_name || u.username).slice(0, 1))}</span><div><b>${esc(u.display_name || u.username)}</b><span>${esc(u.username)}${u.email ? ' · ' + esc(u.email) : ''} · ${esc(u.user_id)}</span></div></div></td>
        <td><span class="tag ${u.role === 'admin' ? 'tag-red' : 'tag-line'}">${u.role === 'admin' ? '管理员' : '普通用户'}</span></td>
        <td class="num-cell">${fmt(u.documents)}</td>
        <td class="num-cell">${fmt(u.sessions)}</td>
        <td class="num-cell">${fmt(u.answers)}</td>
        <td><span class="meter"><i style="width:${pct(u.accuracy)}%"></i></span><span class="num-cell">${pct(u.accuracy)}%</span></td>
        <td class="num-cell">${fmtTokens(u.tokens)}</td>
        <td class="num-cell" title="${esc(u.last_active_at || '')}">${esc(ago(u.last_active_at || u.last_login_at))}</td>
        <td><button class="btn btn-quiet btn-sm" data-toggle-user="${esc(u.user_id)}" data-status="${u.status}">${u.status === 'active' ? '禁用' : '启用'}</button></td>
      </tr>`).join('');
    document.getElementById('admin-content').innerHTML = `
      <div class="admin-toolbar"><div class="left">共 <b>${state.users.length}</b> 个账号 · 文档与会话按用户隔离</div><div class="left"><span class="pill ok">实时数据</span></div></div>
      <section class="admin-panel">
        <div class="admin-panel-head"><h2>账号与使用情况</h2><span class="small muted">点击“禁用”可立即阻止该账号登录</span></div>
        <div style="overflow:auto"><table class="admin-table"><thead><tr><th>用户</th><th>角色</th><th>文档</th><th>会话</th><th>作答</th><th>准确率</th><th>Token</th><th>最近活跃</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="9"><div class="empty-admin">暂无用户</div></td></tr>'}</tbody></table></div>
      </section>`;
    document.querySelectorAll('[data-toggle-user]').forEach((btn) => btn.addEventListener('click', async () => {
      const user_id = btn.dataset.toggleUser;
      const status = btn.dataset.status === 'active' ? 'disabled' : 'active';
      try {
        await api(`/admin/users/${user_id}/status`, { method: 'POST', body: { status } });
        toast(status === 'active' ? '已启用该账号' : '已禁用该账号');
        renderUsers();
      } catch (e) { toast(e.message, true); }
    }));
  }

  // ------------------------------------------------------------------
  // monitor
  // ------------------------------------------------------------------
  async function renderMonitor() {
    if (!state.lastEventId) {
      const initial = await api('/admin/events?limit=40');
      state.events = initial.items || [];
      state.lastEventId = initial.max_id || 0;
    }
    const online = await api('/admin/online');
    state.online = online.items || [];
    const onlineHTML = state.online.length ? state.online.map((u) => `
      <div class="online-card"><span class="online-dot"></span><div><b>${esc(u.username || u.user_id)}</b><span>${u.events} 个事件 · ${esc(ago(u.last_ts))}</span></div></div>`).join('') : '<div class="empty-admin" style="grid-column:1/-1">最近 5 分钟没有活跃用户</div>';
    const eventsHTML = (state.events || []).map(eventRow).join('') || '<div class="empty-admin">暂无事件</div>';
    document.getElementById('admin-content').innerHTML = `
      <div class="admin-toolbar"><div class="left"><span class="admin-live"><i></i> 每 2 秒自动刷新</span><span>在线（5 分钟）<b>${state.online.length}</b> 人</span></div><button class="btn btn-ghost btn-sm" id="monitor-refresh">立即刷新</button></div>
      <div class="online-grid">${onlineHTML}</div>
      <section class="admin-panel">
        <div class="admin-panel-head"><h2>实时行为流</h2><span class="small muted">事件按时间倒序 · 最多保留 300 条</span></div>
        <div class="admin-panel-body"><div class="event-list" id="event-feed">${eventsHTML}</div></div>
      </section>`;
    document.getElementById('monitor-refresh')?.addEventListener('click', () => renderMonitor());
    clearTimeout(state.timer);
    renderMonitorPoll();
  }

  function renderMonitorPoll() {
    if (state.tab !== 'monitor') return;
    clearTimeout(state.timer);
    state.timer = setTimeout(async () => {
      if (state.tab !== 'monitor') return;
      try {
        const newData = await api(`/admin/events?since_id=${state.lastEventId}&limit=80`);
        if (newData.items?.length) {
          state.events = [...newData.items, ...(state.events || [])].slice(0, 80);
          state.lastEventId = newData.max_id || state.lastEventId;
          const feed = document.getElementById('event-feed');
          if (feed) feed.innerHTML = state.events.map(eventRow).join('');
        }
        const online = await api('/admin/online');
        state.online = online.items || [];
        const grid = document.querySelector('.online-grid');
        if (grid) grid.innerHTML = state.online.length ? state.online.map((u) => `
          <div class="online-card"><span class="online-dot"></span><div><b>${esc(u.username || u.user_id)}</b><span>${u.events} 个事件 · ${esc(ago(u.last_ts))}</span></div></div>`).join('') : '<div class="empty-admin" style="grid-column:1/-1">最近 5 分钟没有活跃用户</div>';
      } catch { /* ignore */ }
      if (state.tab === 'monitor') renderMonitorPoll();
    }, 2000);
  }

  // ------------------------------------------------------------------
  // settings
  // ------------------------------------------------------------------
  async function renderSettings() {
    const [data, logsData] = await Promise.all([api('/admin/settings'), api('/admin/model-logs?limit=10')]);
    state.settings = data.values;
    const v = data.values;
    const logRows = (logsData.items || []).map((x) => `<tr><td class="num-cell">${esc(fmtTime(x.ts))}</td><td>${esc(x.kind)}</td><td>${esc(x.model)}</td><td>${x.fallback ? '<span class="pill warn">模板兜底</span>' : x.grounded ? '<span class="pill ok">通过</span>' : '<span class="pill bad">未通过</span>'}</td><td class="small muted">${esc((x.detail?.reason || '—').slice(0, 60))}</td></tr>`).join('');
    document.getElementById('admin-content').innerHTML = `
      <div class="admin-toolbar"><div class="left">修改后立即生效，无需重启服务</div><span class="pill ok">当前模型 · ${esc(v['llm.model'] || '')}</span></div>
      <div class="settings-grid">
        <section class="settings-card">
          <h3>导读模型</h3>
          <p class="hint">题目生成、判分与报告均使用此模型。关闭后将自动使用规则模板，仍可完整走通流程。</p>
          <div class="switch-row"><span style="font-size:13px">启用模型生成</span><label class="switch"><input type="checkbox" id="set-enabled" ${v['llm.enabled'] === '1' ? 'checked' : ''}/><span></span></label></div>
          <div class="field"><label for="set-model">主模型</label><input class="text-input" id="set-model" value="${esc(v['llm.model'] || '')}" placeholder="cn:deepseek-v4-flash" /></div>
          <div class="field"><label for="set-fallback">备用模型</label><input class="text-input" id="set-fallback" value="${esc(v['llm.fallback_model'] || '')}" placeholder="cn:glm-5.3-flash" /></div>
          <div class="field"><label for="set-temp">温度 temperature（0—1.5）</label><input class="text-input" id="set-temp" type="number" step="0.05" min="0" max="1.5" value="${esc(v['llm.temperature'] || '0.45')}" /></div>
          <div class="field"><label for="set-max">单次最大生成 token</label><input class="text-input" id="set-max" type="number" min="64" max="32000" value="${esc(v['llm.max_tokens'] || '1800')}" /></div>
          <div style="display:flex;gap:8px;margin-top:8px"><button class="btn btn-accent" id="save-settings">保存设置</button><button class="btn btn-ghost" id="test-model">测试连通</button></div>
          <div class="model-state-card" id="model-state">模型状态：等待测试</div>
        </section>
        <section class="settings-card">
          <h3>导读策略</h3>
          <p class="hint">控制默认题量与提示级数；单次会话仍可在文档页覆盖。</p>
          <div class="field"><label for="set-count">默认题量</label><input class="text-input" id="set-count" type="number" min="5" max="30" value="${esc(v['guide.default_question_count'] || '12')}" /></div>
          <div class="field"><label for="set-maxcount">最大题量</label><input class="text-input" id="set-maxcount" type="number" min="5" max="30" value="${esc(v['guide.max_question_count'] || '30')}" /></div>
          <div class="field"><label for="set-hints">提示级数</label><input class="text-input" id="set-hints" type="number" min="0" max="3" value="${esc(v['guide.hint_levels'] || '3')}" /></div>
          <div class="field"><label for="set-prompt">导读教练系统提示词（留空使用内置）</label><textarea class="text-area" id="set-prompt" style="min-height:190px;font-family:var(--mono);font-size:12px" placeholder="留空使用 A论 内置的 DGQA 提示词">${esc(v['guide.system_prompt'] || '')}</textarea></div>
        </section>
        <section class="settings-card" style="grid-column:1/-1">
          <h3>图片识别（OCR）</h3>
          <p class="hint">扫描件、图片型 PDF 或文本层过薄的页面，解析阶段会自动调用多模态模型识别文字，再重建章节与知识点。</p>
          <div class="switch-row"><span style="font-size:13px">启用图片识别</span><label class="switch"><input type="checkbox" id="set-ocr-enabled" ${v['ocr.enabled'] === '1' ? 'checked' : ''}/><span></span></label></div>
          <div class="settings-grid" style="gap:12px;margin-top:12px">
            <div class="field"><label for="set-ocr-model">图片识别模型</label><input class="text-input" id="set-ocr-model" value="${esc(v['ocr.model'] || 'cn:deepseek-v4-flash')}" placeholder="cn:deepseek-v4-flash" /></div>
            <div class="field"><label for="set-ocr-dpi">渲染 DPI（90—180）</label><input class="text-input" id="set-ocr-dpi" type="number" min="90" max="180" value="${esc(v['ocr.dpi'] || '120')}" /></div>
            <div class="field"><label for="set-ocr-max">单文档最多识别页数</label><input class="text-input" id="set-ocr-max" type="number" min="1" max="200" value="${esc(v['ocr.max_pages'] || '20')}" /></div>
            <div class="field"><label for="set-ocr-min">触发阈值（字符/页）</label><input class="text-input" id="set-ocr-min" type="number" min="1" max="2000" value="${esc(v['ocr.min_chars_per_page'] || '450')}" /></div>
          </div>
        </section>
        <section class="settings-card" style="grid-column:1/-1">
          <h3>图表数据提取</h3>
          <p class="hint">对 PDF/图片中的柱状图、折线图、饼图、散点图调用视觉模型，提取标题、坐标轴、图例和数值，生成 chart 结构节点。</p>
          <div class="switch-row"><span style="font-size:13px">启用图表数据提取</span><label class="switch"><input type="checkbox" id="set-chart-enabled" ${v['chart.enabled'] === '1' ? 'checked' : ''}/><span></span></label></div>
          <div class="settings-grid" style="gap:12px;margin-top:12px">
            <div class="field"><label for="set-chart-model">图表视觉模型</label><input class="text-input" id="set-chart-model" value="${esc(v['chart.model'] || 'cn:deepseek-v4-flash')}" placeholder="cn:deepseek-v4-flash" /></div>
            <div class="field"><label for="set-chart-max">单文档最多提取页数</label><input class="text-input" id="set-chart-max" type="number" min="1" max="30" value="${esc(v['chart.max_pages'] || '6')}" /></div>
            <div class="field"><label for="set-chart-dpi">渲染 DPI（90—300）</label><input class="text-input" id="set-chart-dpi" type="number" min="90" max="300" value="${esc(v['chart.dpi'] || '140')}" /></div>
          </div>
        </section>
      </div>
      <section class="admin-panel" style="margin-top:16px">
        <div class="admin-panel-head"><h2>最近模型输出审计</h2><span class="small muted">证据校验失败会降低模型幻觉率指标</span></div>
        <div style="overflow:auto"><table class="admin-table"><thead><tr><th>时间</th><th>调用</th><th>模型</th><th>结果</th><th>校验说明</th></tr></thead><tbody>${logRows || '<tr><td colspan="5"><div class="empty-admin">暂无模型输出</div></td></tr>'}</tbody></table></div>
      </section>`;
    document.getElementById('save-settings')?.addEventListener('click', saveSettings);
    document.getElementById('test-model')?.addEventListener('click', async () => {
      const box = document.getElementById('model-state');
      box.className = 'model-state-card'; box.textContent = '正在测试模型网关…';
      try {
        const r = await api('/admin/model/test', { method: 'POST' });
        box.className = 'model-state-card ' + (r.ok ? '' : 'bad');
        box.textContent = r.ok ? `模型网关正常 · ${r.model}` : `模型异常：${r.message}`;
      } catch (e) { box.className = 'model-state-card bad'; box.textContent = e.message; }
    });
  }

  async function saveSettings() {
    const values = {
      'llm.enabled': document.getElementById('set-enabled').checked ? '1' : '0',
      'llm.model': document.getElementById('set-model').value.trim(),
      'llm.fallback_model': document.getElementById('set-fallback').value.trim(),
      'llm.temperature': document.getElementById('set-temp').value,
      'llm.max_tokens': document.getElementById('set-max').value,
      'guide.default_question_count': document.getElementById('set-count').value,
      'guide.max_question_count': document.getElementById('set-maxcount').value,
      'guide.hint_levels': document.getElementById('set-hints').value,
      'guide.system_prompt': document.getElementById('set-prompt').value,
      'ocr.enabled': document.getElementById('set-ocr-enabled').checked ? '1' : '0',
      'ocr.model': document.getElementById('set-ocr-model').value.trim(),
      'ocr.dpi': document.getElementById('set-ocr-dpi').value,
      'ocr.max_pages': document.getElementById('set-ocr-max').value,
      'ocr.min_chars_per_page': document.getElementById('set-ocr-min').value,
      'chart.enabled': document.getElementById('set-chart-enabled').checked ? '1' : '0',
      'chart.model': document.getElementById('set-chart-model').value.trim(),
      'chart.max_pages': document.getElementById('set-chart-max').value,
      'chart.dpi': document.getElementById('set-chart-dpi').value,
    };
    try {
      await api('/admin/settings', { method: 'PUT', body: { values } });
      toast('模型设置已保存并立即生效');
      renderSettings();
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------------
  // usage
  // ------------------------------------------------------------------
  async function renderUsage() {
    const data = await api('/admin/usage?days=14');
    state.usage = data;
    const max = Math.max(1, ...data.daily.map((x) => x.prompt_tokens + x.completion_tokens));
    const bars = data.daily.map((x) => {
      const total = x.prompt_tokens + x.completion_tokens;
      return `<div class="bar-col"><div class="num">${fmtTokens(total)}</div><div class="bar" style="height:${Math.max(3, Math.round(total / max * 130))}px;background:linear-gradient(180deg,var(--jade),#8fb9a9)"><span>${x.date} · ${fmt(total)} tokens</span></div><div class="day">${x.date.slice(5)}</div></div>`;
    }).join('');
    const kindRows = data.by_kind.map((x) => `<tr><td>${esc(x.kind)}</td><td class="num-cell">${fmt(x.requests)}</td><td class="num-cell">${fmt(x.prompt_tokens)}</td><td class="num-cell">${fmt(x.completion_tokens)}</td><td class="num-cell">${x.credits}</td></tr>`).join('');
    const modelRows = data.by_model.map((x) => `<tr><td>${esc(x.model)}</td><td class="num-cell">${fmt(x.requests)}</td><td class="num-cell">${fmt(x.prompt_tokens)}</td><td class="num-cell">${fmt(x.completion_tokens)}</td><td class="num-cell">${x.credits}</td></tr>`).join('');
    document.getElementById('admin-content').innerHTML = `
      <div class="admin-cards">
        ${card('近 14 天请求', fmt(data.total.requests), '模型网关调用次数', '')}
        ${card('Prompt Tokens', fmt(data.total.prompt_tokens), '输入上下文', '')}
        ${card('Completion Tokens', fmt(data.total.completion_tokens), '模型输出', '')}
        ${card('积分消耗', String(data.total.credits), 'WorkBuddy2API 计量', 'accent')}
      </div>
      <div class="admin-grid2">
        <section class="admin-panel"><div class="admin-panel-head"><h2>近 14 天 token 趋势</h2><span class="small muted">prompt + completion</span></div><div class="admin-panel-body"><div class="bar-chart">${bars}</div></div></section>
        <section class="admin-panel"><div class="admin-panel-head"><h2>按调用类型</h2></div><div class="admin-panel-body" style="padding:0"><table class="admin-table"><thead><tr><th>类型</th><th>请求</th><th>Prompt</th><th>Completion</th><th>积分</th></tr></thead><tbody>${kindRows || '<tr><td colspan="5"><div class="empty-admin">暂无数据</div></td></tr>'}</tbody></table></div></section>
      </div>
      <section class="admin-panel" style="margin-top:16px"><div class="admin-panel-head"><h2>按模型</h2><span class="small muted">网关实际返回模型</span></div><div style="overflow:auto"><table class="admin-table"><thead><tr><th>模型</th><th>请求</th><th>Prompt</th><th>Completion</th><th>积分</th></tr></thead><tbody>${modelRows || '<tr><td colspan="5"><div class="empty-admin">暂无数据</div></td></tr>'}</tbody></table></div></section>`;
  }

  // ------------------------------------------------------------------
  // teaching
  // ------------------------------------------------------------------
  async function renderTeaching() {
    if (state.teachingDetail) { await renderTeachingDetail(state.teachingDetail); return; }
    const [students, papers, docs, assignments] = await Promise.all([
      api('/teaching/students'),
      api('/teaching/papers'),
      api('/teaching/documents'),
      api('/assignments?limit=100'),
    ]);
    const s = students.items || [], p = papers.items || [], d = docs.items || [], a = assignments.items || [];
    const rows = a.map((x) => `<tr>
      <td><b>${esc((x.title || '').slice(0, 26))}</b><div class="small muted">${esc(x.document_title || '').slice(0, 30)}</div></td>
      <td>${esc(x.assignee.display_name || x.assignee.username)}</td>
      <td class="num-cell">${x.question_count}</td>
      <td><span class="pill ${x.status === 'reviewed' ? 'ok' : x.status === 'submitted' ? 'warn' : ''}">${esc(x.status_label)}</span></td>
      <td class="num-cell">${x.score === null || x.score === undefined ? '—' : Math.round(x.score)}</td>
      <td class="small muted">${x.due_at ? esc(fmtTime(x.due_at)) : '—'}</td>
      <td><button class="btn btn-ghost btn-sm" data-teach-open="${esc(x.assignment_id)}">${x.status === 'submitted' ? '复盘' : '查看'}</button></td>
    </tr>`).join('') || '<tr><td colspan="7"><div class="empty-admin">还没有布置题卷</div></td></tr>';
    const studentOptions = s.map((x) => `<option value="${esc(x.user_id)}">${esc(x.display_name || x.username)}（${esc(x.username)}）</option>`).join('');
    const paperOptions = p.map((x) => `<option value="${esc(x.set_id)}">${esc(x.title)} · ${x.question_count} 题</option>`).join('');
    const docOptions = d.map((x) => `<option value="${esc(x.doc_id)}">${esc((x.title || '').slice(0, 36))} · ${x.page_count} 页</option>`).join('');
    document.getElementById('admin-content').innerHTML = `
      <div class="admin-cards">
        ${card('布置题卷', fmt(a.length), '累计布置', 'accent')}
        ${card('待复盘', fmt(a.filter((x) => x.status === 'submitted').length), '学生已提交', '')}
        ${card('进行中', fmt(a.filter((x) => x.status === 'in_progress').length), '学生作答中', '')}
        ${card('已复盘', fmt(a.filter((x) => x.status === 'reviewed').length), '完成教学闭环', '')}
      </div>
      <div class="admin-grid2" style="grid-template-columns:1fr 1fr">
        <section class="admin-panel"><div class="admin-panel-head"><h2>从论文生成题卷</h2><span class="small muted">教师可自建题卷</span></div><div class="admin-panel-body">
          <div class="field"><label>论文</label><select class="text-input" id="teach-doc">${docOptions || '<option value="">暂无可选题卷的论文</option>'}</select></div>
          <div class="field"><label>题卷名称</label><input class="text-input" id="teach-paper-title" placeholder="如：第 6 周课堂题卷" /></div>
          <div class="field"><label>题量</label><input class="text-input" type="number" id="teach-paper-limit" value="8" min="1" max="30" /></div>
          <button class="btn btn-ghost btn-sm" id="teach-paper-create" type="button">生成题卷</button>
        </div></section>
        <section class="admin-panel"><div class="admin-panel-head"><h2>布置题卷</h2><span class="small muted">选择学生与截止时间</span></div><div class="admin-panel-body">
          <div class="field"><label>题卷</label><select class="text-input" id="teach-paper">${paperOptions || '<option value="">暂无题卷</option>'}</select></div>
          <div class="field"><label>学生</label><select class="text-input" id="teach-student">${studentOptions || '<option value="">暂无学生</option>'}</select></div>
          <div class="field"><label>截止时间</label><input class="text-input" type="datetime-local" id="teach-due" /></div>
          <div class="field"><label>布置说明</label><textarea class="text-input" id="teach-instructions" rows="2" placeholder="如：阅读第 2—3 节，完成后写一句反思"></textarea></div>
          <button class="btn btn-primary btn-sm" id="teach-assign" type="button">布置给学生</button>
        </div></section>
      </div>
      <section class="admin-panel" style="margin-top:16px"><div class="admin-panel-head"><h2>题卷与作业</h2><span class="small muted">最近 100 条</span></div>
        <div style="overflow:auto"><table class="admin-table"><thead><tr><th>题卷</th><th>学生</th><th>题量</th><th>状态</th><th>得分</th><th>截止</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      </section>`;
    document.getElementById('teach-paper-create')?.addEventListener('click', async () => {
      try {
        const body = { document_id: document.getElementById('teach-doc')?.value, title: document.getElementById('teach-paper-title')?.value, limit: Number(document.getElementById('teach-paper-limit')?.value || 8) };
        const r = await api('/teaching/papers', { method: 'POST', body });
        toast(`题卷已生成：${r.question_count} 题`);
        renderTeaching();
      } catch (e) { toast(e.message, true); }
    });
    document.getElementById('teach-assign')?.addEventListener('click', async () => {
      try {
        const body = {
          paper_id: document.getElementById('teach-paper')?.value,
          assignee_id: document.getElementById('teach-student')?.value,
          due_at: document.getElementById('teach-due')?.value || null,
          instructions: document.getElementById('teach-instructions')?.value || '',
        };
        if (!body.paper_id || !body.assignee_id) throw new Error('请选择题卷和学生');
        const r = await api('/assignments', { method: 'POST', body });
        toast(`已布置给 ${r.assignee.display_name || r.assignee.username}`);
        renderTeaching();
      } catch (e) { toast(e.message, true); }
    });
    document.querySelectorAll('[data-teach-open]').forEach((b) => b.addEventListener('click', () => {
      state.teachingDetail = b.dataset.teachOpen; renderTeaching();
    }));
  }

  async function renderTeachingDetail(id) {
    let a;
    try { a = await api('/assignments/' + encodeURIComponent(id)); } catch (e) { toast(e.message, true); state.teachingDetail = null; renderTeaching(); return; }
    const report = a.report || {};
    const summary = report.summary || {};
    const turns = a.turns || [];
    const turnRows = turns.map((t) => `<tr>
      <td class="num-cell">${t.seq_no}</td>
      <td><b>${esc((t.question?.stem || '').slice(0, 60))}</b><div class="small muted">${esc(t.question?.q_type || '')}</div></td>
      <td class="num-cell">${Math.round(Number(t.score || 0))}</td>
      <td class="small muted">${esc((typeof t.answer?.value === 'string' ? t.answer.value : JSON.stringify(t.answer?.value ?? '')) || '').slice(0, 50)}</td>
      <td class="small muted">${esc((t.evaluation?.feedback || '').slice(0, 60))}</td>
    </tr>`).join('') || '<tr><td colspan="5"><div class="empty-admin">尚无作答</div></td></tr>';
    const fu = (a.follow_ups || []).map((x) => `<div class="teach-fu"><b>${esc(x.kind)}</b> · ${x.status === 'answered' ? `得分 ${x.score}` : '待回答'}<p>${esc(x.question || '')}</p></div>`).join('') || '<div class="small muted">暂无追问记录</div>';
    const sections = (report.section_mastery || []).map((s) => `<span class="tag tag-line">${esc(s.title)} ${Math.round((s.score || 0) * 100)}%</span>`).join(' ');
    document.getElementById('admin-content').innerHTML = `
      <button class="btn btn-ghost btn-sm" id="teach-back">← 返回教学闭环</button>
      <div class="doc-head" style="margin-top:14px"><div><div class="meta" style="margin:0 0 10px"><span class="pill ${a.status === 'reviewed' ? 'ok' : 'warn'}">${esc(a.status_label)}</span><span class="tag tag-line">${esc(a.document_title)}</span></div><h1>${esc(a.title)}</h1><div class="small muted">学生：${esc(a.assignee.display_name || a.assignee.username)} · 布置于 ${esc(fmtTime(a.created_at))} · 截止 ${a.due_at ? esc(fmtTime(a.due_at)) : '—'}</div></div><div style="text-align:right"><div class="teach-score">${a.score === null || a.score === undefined ? '—' : Math.round(a.score)}</div><div class="small muted">得分</div></div></div>
      <div class="admin-grid2" style="grid-template-columns:1.25fr .75fr">
        <section class="admin-panel"><div class="admin-panel-head"><h2>作答明细</h2><span class="small muted">${turns.length} 题</span></div><div style="overflow:auto"><table class="admin-table"><thead><tr><th>#</th><th>题目</th><th>得分</th><th>学生答案</th><th>反馈</th></tr></thead><tbody>${turnRows}</tbody></table></div></section>
        <section class="admin-panel"><div class="admin-panel-head"><h2>报告与追问</h2></div><div class="admin-panel-body"><p class="small">${summary.overall_comment ? esc(summary.overall_comment) : `完成 ${summary.answered || 0}/${summary.total || turns.length} 题，得分 ${summary.score === undefined ? '—' : Math.round(summary.score)}。`}</p><div class="teach-sections">${sections || '<span class="small muted">暂无章节掌握度</span>'}</div><h3 style="margin-top:16px;font-size:14px">追问记录</h3>${fu}</div></section>
      </div>
      <section class="admin-panel" style="margin-top:16px"><div class="admin-panel-head"><h2>教师复盘</h2><span class="small muted">写评语并完成复盘</span></div><div class="admin-panel-body">
        <div class="field"><label>评语</label><textarea class="text-input" id="teach-comment" rows="4" placeholder="如：第 2 题证据引用很好；建议补读 2.3 节，下一次课重点讲判别依据质量控制。">${esc(a.teacher_comment || '')}</textarea></div>
        <button class="btn btn-primary btn-sm" id="teach-review">${a.status === 'reviewed' ? '更新复盘' : '完成复盘'}</button>
      </div></section>`;
    document.getElementById('teach-back')?.addEventListener('click', () => { state.teachingDetail = null; renderTeaching(); });
    document.getElementById('teach-review')?.addEventListener('click', async () => {
      try {
        await api('/assignments/' + encodeURIComponent(id) + '/review', { method: 'POST', body: { comment: document.getElementById('teach-comment')?.value || '' } });
        toast('复盘已保存');
        renderTeachingDetail(id);
      } catch (e) { toast(e.message, true); }
    });
  }

  // ------------------------------------------------------------------
  // quality
  // ------------------------------------------------------------------
  const qpct = (v) => (v === null || v === undefined || isNaN(Number(v))) ? '—' : (Number(v) * 100).toFixed(1) + '%';
  const qnum = (v, d = 3) => (v === null || v === undefined || isNaN(Number(v))) ? '—' : Number(v).toFixed(d);
  const qpill = (s) => `<span class="pill ${s === 'pass' ? 'ok' : s === 'fail' ? 'bad' : 'warn'}">${s === 'pass' ? '通过' : s === 'fail' ? '未通过' : s === 'blocked' ? '待补证' : s === 'warn' ? '未达标' : '进行中'}</span>`;

  async function renderQuality() {
    const data = await api('/admin/quality/overview');
    const runs = await api('/admin/quality/runs?limit=8');
    const latest = data.latest || {};
    const metrics = latest.metrics || {};
    const parse = metrics.parse || {};
    const overall = parse.overall || {};
    const generation = (metrics.generation || {}).overall || {};
    const latency = metrics.latency || {};
    const genP95 = (latency.generation || {}).p95;
    const checks = (latest.summary || {}).checks || [];
    const failed = (latest.summary || {}).failed || [];
    const blocked = (latest.summary || {}).blocked || [];
    const statusText = latest.run_id
      ? `<b>${esc((latest.summary || {}).headline || latest.status || '—')}</b><span>${esc(latest.run_id)} · ${esc(fmtTime(latest.created_at))}</span>`
      : '<b>尚未运行评测</b><span>点击右侧「运行全量评测」建立第一条质量基线</span>';
    const docMap = new Map((parse.documents || []).map((d) => [d.doc_id, d]));
    const goldenRows = (data.golden.documents || []).map((g) => {
      const d = docMap.get(g.doc_id) || {};
      const f1 = d.f1;
      const cls = f1 === undefined ? '' : (f1 >= 0.95 ? 'ok' : 'bad');
      return `<tr>
        <td><div class="user-cell"><div class="avatar">${esc((g.title || '?').slice(0, 1))}</div><div><b>${esc((g.title || '').slice(0, 30))}</b><span>${esc(g.doc_id)}</span></div></div></td>
        <td><span class="tag tag-line">${esc(g.doc_type || 'other')}</span></td>
        <td class="num-cell">${g.page_count || '—'}</td>
        <td class="num-cell">${g.headings}</td>
        <td>${f1 === undefined ? '<span class="muted small">待评测</span>' : `<span class="pill ${cls}">${qnum(f1)}</span>`}</td>
        <td class="num-cell">${d.precision !== undefined ? qnum(d.precision) : '—'} / ${d.recall !== undefined ? qnum(d.recall) : '—'}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="6"><div class="empty-admin">黄金集为空</div></td></tr>';
    const defectRows = (data.open_defects || []).map((d) => `<tr>
      <td><span class="pill ${d.severity === 'P0' ? 'bad' : d.severity === 'P1' ? 'warn' : ''}">${esc(d.severity)}</span></td>
      <td><b>${esc((d.title || '').slice(0, 42))}</b><div class="small muted">${esc((d.area || '') + ' · ' + (d.defect_id || ''))}</div></td>
      <td class="num-cell">${esc((d.updated_at || '').slice(5, 16))}</td>
    </tr>`).join('') || '<tr><td colspan="3"><div class="empty-admin">暂无未关闭缺陷</div></td></tr>';
    const runRows = (runs.items || []).map((r) => `<tr>
      <td class="num-cell">${esc(r.run_id)}</td>
      <td>${esc(r.suite)}</td>
      <td>${qpill(r.status === 'completed' ? ((r.summary || {}).status || 'blocked') : 'running')}</td>
      <td>${esc((r.summary || {}).headline || '—')}</td>
      <td class="num-cell">${esc((r.created_at || '').slice(5, 16))}</td>
    </tr>`).join('') || '<tr><td colspan="5"><div class="empty-admin">暂无运行记录</div></td></tr>';
    const checkRows = checks.map((c) => `<tr>
      <td>${esc(c.label)}</td>
      <td class="num-cell">${c.value === null || c.value === undefined ? '—' : (c.id.includes('rate') || c.id.includes('coverage') || c.id === 'generic_stem' ? qpct(c.value) : qnum(c.value, 2))}</td>
      <td class="num-cell">${typeof c.target === 'string' ? esc(c.target) : c.target === null || c.target === undefined ? '基线' : c.id.includes('rate') || c.id.includes('coverage') || c.id === 'generic_stem' ? qpct(c.target) : qnum(c.target, 2)}</td>
      <td>${qpill(c.status === 'track' ? 'running' : c.status)}</td>
      <td class="small muted">${esc(c.detail || '')}</td>
    </tr>`).join('') || '<tr><td colspan="5"><div class="empty-admin">运行评测后显示</div></td></tr>';
    document.getElementById('admin-content').innerHTML = `
      <div class="quality-banner ${latest.run_id ? (failed.length ? 'bad' : blocked.length ? 'warn' : 'ok') : 'warn'}">
        <div><div class="eyebrow">V1.1 · QUALITY GATE</div>${statusText}</div>
        <div class="quality-banner-actions">
          <button class="btn btn-primary btn-sm" id="quality-run-btn" type="button">运行全量评测</button>
          ${latest.run_id ? `<a class="btn btn-ghost btn-sm" href="/api/v1/admin/quality/runs/${encodeURIComponent(latest.run_id)}/report" target="_blank">下载报告</a>` : ''}
        </div>
      </div>
      <div class="admin-cards">
        ${card('黄金集文档', fmt(data.golden.count), `${data.golden.headings} 个标题标注 · ${esc(data.golden.version)}`, 'accent')}
        ${card('标题识别 F1', overall.f1 !== undefined ? qnum(overall.f1) : '—', '目标 ≥ 0.95', '')}
        ${card('证据支持率', generation.evidence_support_rate !== undefined ? qpct(generation.evidence_support_rate) : '—', '引文可在原文逐字定位', '')}
        ${card('节点可追溯率', generation.node_traceability_rate !== undefined ? qpct(generation.node_traceability_rate) : '—', `${generation.stale_nodes || 0} 道题节点失效`, '')}
        ${card('生成 P95', genP95 !== undefined ? qnum(genP95, 2) + '<small>s</small>' : '—', '单题生成时延基线', '')}
        ${card('未关闭缺陷', fmt((data.open_defects || []).length), `P0 ${(data.open_defects || []).filter((d) => d.severity === 'P0').length} 项`, '')}
      </div>
      <div class="admin-grid2" style="grid-template-columns:1.35fr .85fr">
        <section class="admin-panel">
          <div class="admin-panel-head"><h2>指标判定</h2><span class="small muted">最新运行 · ${esc(latest.run_id || '—')}</span></div>
          <div style="overflow:auto"><table class="admin-table"><thead><tr><th>指标</th><th>实测</th><th>目标</th><th>判定</th><th>口径</th></tr></thead><tbody>${checkRows}</tbody></table></div>
        </section>
        <section class="admin-panel">
          <div class="admin-panel-head"><h2>缺陷台账</h2><span class="small muted">按优先级</span></div>
          <div style="overflow:auto"><table class="admin-table"><thead><tr><th>级别</th><th>缺陷</th><th>更新</th></tr></thead><tbody>${defectRows}</tbody></table></div>
        </section>
      </div>
      <div class="admin-grid2" style="grid-template-columns:1.35fr .85fr;margin-top:16px">
        <section class="admin-panel">
          <div class="admin-panel-head"><h2>黄金集与逐篇结构分</h2><span class="small muted">F1 = 2PR/(P+R)</span></div>
          <div style="overflow:auto"><table class="admin-table"><thead><tr><th>文档</th><th>文体</th><th>页数</th><th>标题</th><th>F1</th><th>P / R</th></tr></thead><tbody>${goldenRows}</tbody></table></div>
        </section>
        <section class="admin-panel">
          <div class="admin-panel-head"><h2>运行历史</h2><span class="small muted">最近 8 次</span></div>
          <div style="overflow:auto"><table class="admin-table quality-runs"><thead><tr><th>运行</th><th>范围</th><th>结论</th><th>摘要</th><th>时间</th></tr></thead><tbody>${runRows}</tbody></table></div>
        </section>
      </div>
      <section class="admin-panel" style="margin-top:16px">
        <div class="admin-panel-head"><h2>评测口径与待办</h2><span class="small muted">V1.1 退出条件</span></div>
        <div class="admin-panel-body quality-notes">
          <p><b>已自动化：</b>标题 P/R/F1、边界 ±1 页、层级准确率、文本/图表覆盖、证据支持率、节点可追溯率、首轮失败率、生成与判分时延分位。</p>
          <p><b>待人工：</b>题目可答性需 3 名评审按 5 分制抽样；主观题一致率需按标注手册录入人工参考分。可在本页 API 的 <code>/admin/quality/reviews</code> 录入。</p>
          <p><b>通过线：</b>F1 ≥0.95、证据支持率 100%、可答性 ≥4.3、一致率 ≥85%、30 页解析 P95 ≤90 秒、P0 缺陷清零。</p>
        </div>
      </section>`;
    document.getElementById('quality-run-btn')?.addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true; btn.textContent = '评测运行中…';
      try {
        const run = await api('/admin/quality/runs', { method: 'POST', body: { suite: 'all', version: data.golden.version } });
        toast('评测已启动：' + run.run_id);
        pollQualityRun(run.run_id);
      } catch (err) {
        btn.disabled = false; btn.textContent = '运行全量评测';
        toast(err.message, true);
      }
    });
    state.timer = setTimeout(() => { if (state.tab === 'quality') loadTab(); }, 30000);
  }

  async function pollQualityRun(runId) {
    for (let i = 0; i < 120; i += 1) {
      await new Promise((r) => setTimeout(r, 2500));
      try {
        const run = await api('/admin/quality/runs/' + encodeURIComponent(runId));
        if (run.status === 'completed' || run.status === 'failed') {
          toast(run.status === 'completed' ? '评测完成：' + ((run.summary || {}).headline || '') : '评测失败');
          if (state.tab === 'quality') loadTab();
          return;
        }
      } catch { /* keep polling */ }
    }
    toast('评测仍在运行，请稍后刷新', true);
  }

  // ------------------------------------------------------------------
  // boot
  // ------------------------------------------------------------------
  async function boot() {
    if (location.pathname.startsWith('/teacher')) document.title = 'A论 · 教学控制台';
    if (!AUTH.token || !AUTH.user || !['admin', 'teacher'].includes(AUTH.user.role)) {
      loginView();
      return;
    }
    try {
      const me = await api('/auth/me');
      if (!['admin', 'teacher'].includes(me.role)) { clearAuth(); loginView('需要管理员或教师账号'); return; }
      AUTH.user = me;
      localStorage.setItem('alun_user', JSON.stringify(me));
      dashboard();
    } catch {
      loginView();
    }
  }

  boot();
})();

