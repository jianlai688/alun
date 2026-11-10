/* A论 AI 问答助手：用户端与管理端共用 */
(() => {
  'use strict';
  const API = '/api/v1';
  const state = { open: false, busy: false, images: [], messages: [], context: {} };
  const $ = (sel) => document.querySelector(sel);

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  function token() { return localStorage.getItem('alun_token') || ''; }
  function user() {
    try { return JSON.parse(localStorage.getItem('alun_user') || 'null'); } catch { return null; }
  }
  function isAdmin() { return user()?.role === 'admin'; }
  function currentContext() {
    const h = (location.hash || '').replace(/^#/, '');
    const parts = h.split('/').filter(Boolean);
    const ctx = { documentId: '', sessionId: '' };
    if (parts[0] === 'doc' && parts[1]) ctx.documentId = parts[1];
    if (parts[0] === 'session' && parts[1]) ctx.sessionId = parts[1];
    if (window.ALUN_ASSISTANT_CONTEXT?.documentId && (parts[0] === 'doc' || parts[0] === 'session')) ctx.documentId = window.ALUN_ASSISTANT_CONTEXT.documentId;
    if (window.ALUN_ASSISTANT_CONTEXT?.sessionId && parts[0] === 'session') ctx.sessionId = window.ALUN_ASSISTANT_CONTEXT.sessionId;
    return ctx;
  }

  function mount() {
    const wrap = document.createElement('div');
    wrap.innerHTML = `
      <button class="alun-ai-fab" id="alun-ai-fab" type="button" title="打开 A论 AI 问答助手">
        <span class="seal">论</span><span>A论 AI</span><i class="dot"></i>
      </button>
      <div class="alun-ai-overlay" id="alun-ai-overlay"></div>
      <aside class="alun-ai-drawer" id="alun-ai-drawer" aria-label="A论 AI 问答助手">
        <div class="alun-ai-head">
          <span class="seal">论</span>
          <div><h3>A论 AI 问答助手</h3><p id="alun-ai-subtitle">全文理解 · 原文证据 · 权威检索</p></div>
          <button class="close" id="alun-ai-close" type="button" aria-label="关闭">×</button>
        </div>
        <div class="alun-ai-body" id="alun-ai-body"></div>
        <div class="alun-ai-composer">
          <div class="alun-ai-previews" id="alun-ai-previews"></div>
          <div class="alun-ai-input-row">
            <button class="alun-ai-attach" id="alun-ai-attach" type="button" title="上传图片">＋</button>
            <textarea class="alun-ai-input" id="alun-ai-input" rows="2" placeholder="问全文、问图表、问平台数据…（可粘贴图片）"></textarea>
            <button class="alun-ai-send" id="alun-ai-send" type="button" title="发送">↑</button>
          </div>
          <div class="alun-ai-foot">
            <label class="alun-ai-web-toggle"><input type="checkbox" id="alun-ai-web" checked/> 权威检索</label>
            <span>可粘贴/上传图片 · 回答带原文页码</span>
          </div>
          <div class="alun-ai-hint">A论会区分「论文原文」和「外部权威资料」，找不到依据时会直接说明。</div>
        </div>
      </aside>
      <input type="file" id="alun-ai-file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden />
    `;
    document.body.appendChild(wrap);
    $('#alun-ai-fab')?.addEventListener('click', open);
    $('#alun-ai-close')?.addEventListener('click', close);
    $('#alun-ai-overlay')?.addEventListener('click', close);
    $('#alun-ai-send')?.addEventListener('click', send);
    $('#alun-ai-attach')?.addEventListener('click', () => $('#alun-ai-file')?.click());
    $('#alun-ai-file')?.addEventListener('change', onFiles);
    $('#alun-ai-drawer')?.addEventListener('paste', onPaste);
    $('#alun-ai-input')?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && state.open) close(); });
    setInterval(syncVisibility, 1200);
    syncVisibility();
  }

  window.alunOpenAssistant = open;

  function syncVisibility() {
    const fab = $('#alun-ai-fab');
    if (!fab) return;
    const onLogin = !!document.querySelector('.login-page, .admin-login');
    const hasEntry = !!document.querySelector('#ai-top-btn, #admin-ai-btn');
    fab.style.display = token() && !onLogin && !hasEntry ? 'inline-flex' : 'none';
    if (!token() && state.open) close();
  }

  function open() {
    if (!token()) { toast('请先登录后使用 A论 AI 助手'); return; }
    state.context = currentContext();
    const admin = isAdmin();
    $('#alun-ai-subtitle').textContent = admin && !state.context.documentId
      ? '平台数据 · 实时指标 · 权威检索'
      : '全文理解 · 原文证据 · 权威检索';
    $('#alun-ai-overlay').classList.add('open');
    $('#alun-ai-drawer').classList.add('open');
    state.open = true;
    renderEmpty();
    setTimeout(() => $('#alun-ai-input')?.focus(), 250);
  }
  function close() {
    $('#alun-ai-overlay')?.classList.remove('open');
    $('#alun-ai-drawer')?.classList.remove('open');
    state.open = false;
  }

  function renderEmpty() {
    const body = $('#alun-ai-body');
    if (!body) return;
    if (!state.messages.length) {
      const admin = isAdmin() && !state.context.documentId;
      const suggestions = admin
        ? ['今天的用户使用情况', '当前回答准确率是多少', '模型幻觉率怎么理解', '哪些用户需要关注']
        : state.context.documentId
          ? ['用三句话概括全文', '这篇论文的核心方法是什么', '实验数据说明了什么', '把本文和权威资料对照']
          : ['A论能做什么', '怎样开始一轮导读', '回答准确率怎么计算', '去权威网站查一个概念'];
      body.innerHTML = `
        <div class="alun-ai-empty">
          <div class="eyebrow">A-LUN · GROUNDED ASSISTANT</div>
          <h4>${admin ? '看见平台，<br/>也看见依据。' : '把全文，<br/>问成自己的理解。'}</h4>
          <p>${admin ? '我可以读取后台真实统计，回答用户、准确率、幻觉率、token 与模型设置问题；外部问题会检索百科与学术来源。' : '我会优先读取当前论文的摘要、结构、知识点与原文片段；需要外部知识时，再到权威网站检索并标明来源。'}</p>
          <div class="alun-ai-chips">${suggestions.map((s) => `<button class="alun-ai-chip" type="button">${esc(s)}</button>`).join('')}</div>
        </div>`;
      body.querySelectorAll('.alun-ai-chip').forEach((b) => b.addEventListener('click', () => {
        $('#alun-ai-input').value = b.textContent;
        send();
      }));
    }
  }

  function addImages(files) {
    for (const f of files) {
      if (state.images.length >= 3) break;
      if (!f.type.startsWith('image/')) continue;
      if (f.size > 8 * 1024 * 1024) { toast('单张图片不能超过 8 MB'); continue; }
      state.images.push({ file: f, url: URL.createObjectURL(f) });
    }
    renderPreviews();
  }

  function onFiles(e) {
    addImages(Array.from(e.target.files || []));
    e.target.value = '';
  }

  function onPaste(e) {
    const items = Array.from(e.clipboardData?.items || []);
    const images = items
      .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item) => item.getAsFile())
      .filter(Boolean);
    if (!images.length) return;
    e.preventDefault();
    addImages(images);
    toast(images.length > 1 ? `已粘贴 ${images.length} 张图片` : '图片已粘贴，可继续输入问题');
  }

  function renderPreviews() {
    const box = $('#alun-ai-previews');
    if (!box) return;
    box.innerHTML = state.images.map((x, i) => `<span style="position:relative;display:inline-block"><img class="alun-ai-thumb" src="${x.url}" alt="待发送图片"/><button data-rm="${i}" type="button" style="position:absolute;right:-5px;top:-5px;width:17px;height:17px;border:0;border-radius:50%;background:var(--ink);color:#fff;font-size:10px;cursor:pointer">×</button></span>`).join('');
    box.querySelectorAll('[data-rm]').forEach((b) => b.addEventListener('click', () => {
      const i = Number(b.dataset.rm);
      URL.revokeObjectURL(state.images[i].url);
      state.images.splice(i, 1);
      renderPreviews();
    }));
  }

  const MATH_SPLIT = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^$\n]+?\$)/g;
  const MATH_TOKEN = /^(\$\$[\s\S]*\$\$|\\\[[\s\S]*\\\]|\\\([\s\S]*\\\)|\$[^$\n]*\$)$/;

  function md(text) {
    let out = '';
    const raw = String(text || '');
    const parts = raw.split(MATH_SPLIT);
    parts.forEach((part) => {
      if (MATH_TOKEN.test(part)) {
        out += esc(part);
        return;
      }
      out += esc(part)
        .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
        .replace(/`([^`]+)`/g, '<code style="font-family:var(--mono);font-size:.92em;background:var(--paper-deep);padding:1px 4px;border-radius:3px">$1</code>')
        .replace(/\n/g, '<br/>');
    });
    return out;
  }

  function typesetMath(el, attempt = 0) {
    if (!el) return;
    if (window.MathJax?.typesetPromise) {
      window.MathJax.typesetPromise([el]).catch(() => {});
      return;
    }
    if (attempt < 8) setTimeout(() => typesetMath(el, attempt + 1), 250);
  }

  function messageEl(role, text, meta = {}) {
    const mine = role === 'user';
    const cite = (meta.citations || []).slice(0, 8).map((c) => `<span class="alun-ai-cite">原文 p.${c.page ?? '—'}</span>`).join('');
    const sources = (meta.sources || []).length
      ? `<div class="alun-ai-sources">${meta.sources.map((s, i) => `<div class="alun-ai-source"><b>[S${i + 1}] ${esc(s.source || '来源')}</b> · <a href="${esc(s.url || '#')}" target="_blank" rel="noopener noreferrer">${esc(s.title || '打开来源')}</a><br/>${esc((s.snippet || '').slice(0, 180))}</div>`).join('')}</div>`
      : '';
    return `<div class="alun-ai-msg ${mine ? 'user' : ''}">
      <div class="avatar">${mine ? '我' : '论'}</div>
      <div class="alun-ai-bubble">${mine ? esc(text) : md(text)}${cite ? `<div class="alun-ai-cites">${cite}</div>` : ''}${sources}</div>
    </div>`;
  }

  function renderAll() {
    const body = $('#alun-ai-body');
    if (!body) return;
    body.innerHTML = state.messages.map((m) => messageEl(m.role, m.content, m.meta || {})).join('') || '';
    typesetMath(body);
    body.scrollTop = body.scrollHeight;
    if (!state.messages.length) renderEmpty();
  }

  async function send() {
    if (state.busy) return;
    const input = $('#alun-ai-input');
    const text = (input?.value || '').trim();
    if (!text && !state.images.length) return;
    if (!token()) { toast('请先登录后使用 A论 AI 助手'); return; }
    const ctx = currentContext();
    const images = state.images.slice();
    const history = state.messages.slice(-8).map((m) => ({ role: m.role, content: m.content }));
    const body = $('#alun-ai-body');
    state.messages.push({ role: 'user', content: text || '（上传了一张图片）' });
    state.busy = true;
    input.value = '';
    $('#alun-ai-send').disabled = true;
    renderAll();
    if (images.length) {
      body.insertAdjacentHTML('beforeend', `<div class="alun-ai-msg"><div class="avatar">论</div><div class="alun-ai-bubble"><span class="alun-ai-typing"><i></i><i></i><i></i></span> 正在读原文并检索权威资料…</div></div>`);
    } else {
      body.insertAdjacentHTML('beforeend', `<div class="alun-ai-msg"><div class="avatar">论</div><div class="alun-ai-bubble"><span class="alun-ai-typing"><i></i><i></i><i></i></span> 正在整理依据…</div></div>`);
    }
    body.scrollTop = body.scrollHeight;
    try {
      const fd = new FormData();
      fd.append('message', text);
      fd.append('history', JSON.stringify(history));
      fd.append('use_web', $('#alun-ai-web')?.checked ? '1' : '0');
      if (ctx.documentId) fd.append('document_id', ctx.documentId);
      if (ctx.sessionId) fd.append('session_id', ctx.sessionId);
      images.forEach((x) => fd.append('images', x.file, x.file.name));
      const headers = { Authorization: 'Bearer ' + token() };
      const u = user();
      if (u?.tenant_id) headers['X-Tenant-Id'] = u.tenant_id;
      const res = await fetch(API + '/assistant/chat', { method: 'POST', headers, body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.message || `请求失败（${res.status}）`);
      state.messages.push({ role: 'assistant', content: data.answer || '没有获得回答。', meta: { citations: data.citations, sources: data.sources } });
      state.images.forEach((x) => URL.revokeObjectURL(x.url));
      state.images = [];
      renderPreviews();
    } catch (err) {
      state.messages.push({ role: 'assistant', content: `抱歉，暂时无法完成这次问答：${err.message || '服务异常'}。你可以稍后重试，或先打开论文原文查看相关段落。` });
    } finally {
      state.busy = false;
      $('#alun-ai-send').disabled = false;
      renderAll();
    }
  }

  function toast(msg) {
    const old = document.querySelector('.alun-ai-toast');
    if (old) old.remove();
    const el = document.createElement('div');
    el.className = 'alun-ai-toast';
    el.textContent = msg;
    Object.assign(el.style, {
      position: 'fixed', right: '24px', bottom: '80px', zIndex: 1300, background: 'var(--ink)',
      color: 'var(--paper)', padding: '9px 14px', borderRadius: '8px', fontSize: '12px', boxShadow: '0 6px 20px rgba(0,0,0,.2)',
    });
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2800);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
