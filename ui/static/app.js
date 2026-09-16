/* ============================================================
   hr-chat-ui — Client Application
   SSE streaming · HITL confirmation cards (P3) · clickable citations (FR-5.3)
   ============================================================ */

const state = {
  employeeId: 'EMP-791',
  employee: null,
  sessionId: null,
  users: [],
  suggestions: [],
  busy: false,
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ────────────────────────────── Bootstrap ────────────────────────────── */

async function bootstrap() {
  const res = await fetch('/api/bootstrap');
  const data = await res.json();
  state.users = data.users;
  state.suggestions = data.suggestions;
  state.employeeId = data.primary_employee_id || state.employeeId;
  state.employee = state.users.find((u) => u.employee_id === state.employeeId);
  $('composerUser').textContent = `${state.employeeId} として実行中`;
  renderIdentity();
  renderScenarios();
  await refreshPanels();
}

function renderIdentity() {
  const host = $('identityList');
  host.innerHTML = '';
  state.users.forEach((u) => {
    const active = u.employee_id === state.employeeId;
    const arrCls = u.work_arrangement.replace('-', '');
    const demo = u.kind === 'rbac_demo';
    const btn = el('button', demo ? 'identity__item identity__item--demo' : 'identity__item');
    btn.type = 'button';
    btn.setAttribute('aria-pressed', String(active));
    if (u.note) btn.title = u.note;
    btn.innerHTML = `
      <span class="identity__avatar">${esc(u.initials)}</span>
      <span class="identity__meta">
        <span class="identity__name">${esc(u.name)}${u.badge ? `<span class="identity__badge identity__badge--${demo ? 'demo' : 'live'}">${esc(u.badge)}</span>` : ''}</span>
        <span class="identity__role">${esc(u.employee_id)} · ${esc(u.title)}</span>
      </span>
      <span class="arrangement arrangement--${esc(arrCls)}">${esc(u.work_arrangement)}</span>`;
    btn.onclick = () => switchUser(u.employee_id);
    host.appendChild(btn);
  });
}

async function switchUser(id) {
  if (state.employeeId === id) return;
  state.employeeId = id;
  state.employee = state.users.find((u) => u.employee_id === id);
  state.sessionId = null;
  renderIdentity();
  $('composerUser').textContent = `${id} として実行中`;
  await refreshPanels();
  toast(state.employee?.kind === 'rbac_demo'
    ? `${state.employee.name}（${id}）— 他人の ID です。アクセス拒否が実演されます。`
    : `${state.employee.name}（${id}）としてログインしました`);
}

const DOMAIN_OF = {
  'Policy Q&A': 'policy',
  'WorkWeek (HRMS)': 'hcm',
  'ServiceImmediately (ITMS)': 'itsm',
  'Cross-System Saga': 'saga',
};

function renderScenarios() {
  const host = $('scenarioList');
  host.innerHTML = '';
  const groups = {};
  state.suggestions.forEach((s) => (groups[s.category] ??= []).push(s));

  Object.entries(groups).forEach(([cat, items]) => {
    const g = el('div', 'scenario-group');
    g.appendChild(el('div', 'scenario-group__title',
      `<i class="dot dot--${DOMAIN_OF[cat] || 'policy'}"></i>${esc(cat)}`));
    items.forEach((s) => {
      const b = el('button', 'scenario', esc(s.label));
      b.type = 'button';
      b.onclick = () => {
        $('input').value = s.text;
        autoSize();
        $('input').focus();
        if (window.innerWidth <= 760) $('rail').dataset.open = 'false';
        updateSendState();
      };
      g.appendChild(b);
    });
    host.appendChild(g);
  });
}

/* ────────────────────────────── Chat ────────────────────────────── */

function addUserMessage(text) {
  $('welcome')?.remove();
  const m = el('article', 'msg msg--user');
  m.innerHTML = `
    <div class="msg__avatar">${esc(state.employee?.initials || 'ME')}</div>
    <div class="msg__body">
      <div class="msg__author">${esc(state.employee?.name || 'You')} <span>${esc(state.employeeId)}</span></div>
      <div class="msg__text">${esc(text).replace(/\n/g, '<br/>')}</div>
    </div>`;
  $('streamInner').appendChild(m);
  scrollDown();
}

function addAgentShell() {
  const m = el('article', 'msg msg--agent');
  m.innerHTML = `
    <div class="msg__avatar">HR</div>
    <div class="msg__body">
      <div class="msg__author">HR Concierge <span>hr_concierge</span></div>
      <div class="msg__trace"></div>
      <div class="msg__text"><span class="typing"><i></i><i></i><i></i></span></div>
      <div class="msg__extras"></div>
    </div>`;
  $('streamInner').appendChild(m);
  scrollDown();
  return {
    root: m,
    trace: m.querySelector('.msg__trace'),
    text: m.querySelector('.msg__text'),
    extras: m.querySelector('.msg__extras'),
  };
}

function ensureTraceBox(host) {
  let box = host.querySelector('.trace');
  if (!box) {
    box = el('details', 'trace');
    box.open = true;
    box.innerHTML = `
      <summary class="trace__head">
        <svg class="trace__caret" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5 10.5 8 6 12.5"/></svg>
        実行トレース（ツール層ガードレール）
        <span class="trace__count">0</span>
      </summary>
      <div class="trace__body"></div>`;
    host.appendChild(box);
  }
  return box;
}

function addTraceStep(host, payload) {
  const box = ensureTraceBox(host);
  const body = box.querySelector('.trace__body');
  const step = el('div', `step step--running`);
  step.dataset.tool = payload.tool_name;
  const args = Object.entries(payload.args || {})
    .filter(([k]) => !['caller_id'].includes(k))
    .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join('\n');
  step.innerHTML = `
    <div class="step__rail"><span class="step__node"></span></div>
    <div class="step__main">
      <div class="step__title">
        <span class="step__tool">${esc(payload.tool_name)}</span>
        <span class="tag tag--sys">${esc(payload.system)}</span>
        <span class="tag tag--${esc(payload.kind)}">${payload.kind === 'write' ? '書込' : '参照'}</span>
      </div>
      ${args ? `<pre class="step__args">${esc(args)}</pre>` : ''}
      <div class="step__note"></div>
    </div>`;
  body.appendChild(step);
  box.querySelector('.trace__count').textContent = body.children.length;
  scrollDown();
  return step;
}

const VERDICT_LABEL = {
  SUCCESS: '成功',
  DENIED: 'ガードレール拒否',
  ERROR: 'エラー',
  CONFIRMATION_REQUIRED: '承認待ち',
  ROLLBACK: '補償ロールバック',
  SERVICE_UNAVAILABLE: '接続不可',
};

function resolveTraceStep(step, payload) {
  if (!step) return;
  step.className = `step step--${payload.verdict}`;
  step.querySelector('.step__title').appendChild(
    el('span', `tag tag--${payload.verdict}`, esc(VERDICT_LABEL[payload.verdict] || payload.verdict)));
  const note = step.querySelector('.step__note');
  if (payload.code) {
    note.innerHTML = `<code>${esc(payload.code)}</code>`;
  } else if (payload.verdict === 'SUCCESS' && payload.audit_event_id) {
    note.innerHTML = `監査イベント <code>${esc(payload.audit_event_id)}</code> を記録`;
  }
}

/* Guardrail denial / rollback / connectivity callout */
function addVerdictCallout(host, payload) {
  if (!['DENIED', 'ERROR', 'ROLLBACK', 'SERVICE_UNAVAILABLE'].includes(payload.verdict)) return;
  const icons = {
    DENIED: '<path d="M8 1.5 14.5 13h-13z"/><path d="M8 6v3.2M8 11.2h.01"/>',
    ERROR: '<path d="M8 1.5 14.5 13h-13z"/><path d="M8 6v3.2M8 11.2h.01"/>',
    ROLLBACK: '<path d="M2.4 8a5.6 5.6 0 1 0 1.7-4"/><path d="M2.3 2.2v3.6h3.6"/>',
    // Disconnected-plug / offline cloud: an infrastructure warning, not a verdict.
    SERVICE_UNAVAILABLE: '<path d="M8 1.6a6.4 6.4 0 1 0 0 12.8A6.4 6.4 0 0 0 8 1.6z"/><path d="M3.5 3.5l9 9"/>',
  };
  const titles = {
    DENIED: 'ツール層ガードレールにより拒否されました',
    ERROR: '下流システムでエラーが発生しました',
    ROLLBACK: '補償トランザクションを実行しました',
    SERVICE_UNAVAILABLE: 'リモートシステムに接続できません（一時的な障害）',
  };
  const c = el('div', `verdict verdict--${payload.verdict}`);
  c.innerHTML = `
    <svg class="verdict__icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${icons[payload.verdict]}</svg>
    <div class="verdict__body">
      <div class="verdict__title">
        ${esc(titles[payload.verdict])}
        ${payload.code ? `<span class="verdict__code">${esc(payload.code)}</span>` : ''}
      </div>
      <div>${esc(payload.message || '')}</div>
      ${payload.compensation ? `<div style="margin-top:6px;font-size:11.5px;">WorkWeek を <code>${esc(payload.compensation.reverted_work_arrangement || '')}</code> / <code>${esc(payload.compensation.reverted_address || '')}</code> へ復元済み</div>` : ''}
    </div>`;
  host.appendChild(c);
  scrollDown();
}

/* HITL confirmation card (Principle P3) */
function addHitlCard(host, payload) {
  if (payload.verdict !== 'CONFIRMATION_REQUIRED' || !payload.proposal) return;

  const rows = flatten(payload.proposal)
    .map(([k, v]) => `<tr><th>${esc(labelize(k))}</th><td>${esc(v)}</td></tr>`)
    .join('');

  const card = el('div', 'hitl');
  card.innerHTML = `
    <div class="hitl__head">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <path d="M8 1.5 14.5 5v6L8 14.5 1.5 11V5z"/><path d="M8 5.6v3M8 10.6h.01"/>
      </svg>
      実行前の本人確認が必要です
    </div>
    <p class="hitl__sub">
      すべての業務ガードレールを通過しました。以下の内容で <strong>${esc(payload.system)}</strong> を更新します。内容を確認のうえ承認してください。
    </p>
    <table class="hitl__table"><tbody>${rows}</tbody></table>
    <div class="hitl__actions">
      <button class="btn btn--confirm" type="button">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.4 6.3 11.7 13 5"/></svg>
        承認して実行
      </button>
      <button class="btn btn--cancel" type="button">取り消す</button>
      <span class="hitl__hint">Principle P3 · Human-in-the-Loop</span>
    </div>`;

  card.querySelector('.btn--confirm').onclick = () => {
    card.classList.add('hitl--resolved');
    send(`承認します。同じ内容を user_confirmed=True で実行してください。`);
  };
  card.querySelector('.btn--cancel').onclick = () => {
    card.classList.add('hitl--resolved');
    toast('操作を取り消しました。システムは変更されていません。');
  };

  host.appendChild(card);
  scrollDown();
}

function flatten(obj, prefix = '') {
  const out = [];
  Object.entries(obj || {}).forEach(([k, v]) => {
    if (v === null || v === undefined || v === '') return;
    if (typeof v === 'object' && !Array.isArray(v)) {
      out.push(...flatten(v, prefix ? `${prefix}.${k}` : k));
    } else {
      out.push([prefix ? `${prefix}.${k}` : k, Array.isArray(v) ? v.join(', ') : String(v)]);
    }
  });
  return out.slice(0, 14);
}

const LABELS = {
  employee_id: '従業員 ID', leave_type: '休暇種別', start_date: '開始日', end_date: '終了日',
  days: '日数', reason: '理由', address: '住所', phone: '電話番号', work_arrangement: '勤務形態',
  emergency_contact: '緊急連絡先', category: 'カテゴリ', priority: '優先度', title: '件名',
  description: '詳細', amount_usd: '金額 (USD)', ticket_id: 'チケット ID',
  from_status: '現在のステータス', to_status: '変更後ステータス', resolution_notes: '対応メモ',
  action: '操作', compensation_policy: '補償方針', new_address: '新しい住所',
  new_work_arrangement: '新しい勤務形態',
};
function labelize(key) {
  const leaf = key.split('.').pop();
  return LABELS[leaf] || leaf.replace(/_/g, ' ');
}

/* Clickable citations (FR-5.3) */
function linkifyCitations(text) {
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/(Section\s+\d+(?:\.\d+)?)/gi,
      (m) => `<a class="cite" href="#" data-section="${m}">${m}</a>`)
    .replace(/^\s*[-*]\s+(.*)$/gm, '<li>$1</li>')
    .replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, '<ul>$1</ul>')
    .replace(/\n{2,}/g, '</p><p>')
    .replace(/\n/g, '<br/>');
}

function renderAnswer(node, text) {
  node.innerHTML = `<p>${linkifyCitations(text)}</p>`;
  node.querySelectorAll('.cite').forEach((a) => {
    a.onclick = (e) => { e.preventDefault(); openCitation(a.dataset.section); };
  });
}

function addCitationShelf(host, citations) {
  if (!citations?.length) return;
  const shelf = el('div', 'cite-shelf');
  shelf.appendChild(el('span', 'cite-shelf__label', '根拠となった規程条文'));
  citations.forEach((c) => {
    const a = el('a', 'cite', esc(c.section_id));
    a.href = '#';
    a.title = c.title || '';
    a.onclick = (e) => { e.preventDefault(); openCitation(c.section_id); };
    shelf.appendChild(a);
  });
  host.appendChild(shelf);
}

/* ────────────────────────────── SSE send ────────────────────────────── */

async function send(text) {
  if (state.busy || !text.trim()) return;
  state.busy = true;
  updateSendState();

  addUserMessage(text);
  const view = addAgentShell();
  let buffer = '';
  let started = false;
  const stepsByTool = {};

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        employee_id: state.employeeId,
        session_id: state.sessionId,
      }),
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let raw = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      raw += decoder.decode(value, { stream: true });
      const frames = raw.split('\n\n');
      raw = frames.pop();

      for (const frame of frames) {
        const evMatch = frame.match(/^event: (.+)$/m);
        const dtMatch = frame.match(/^data: (.+)$/m);
        if (!evMatch || !dtMatch) continue;
        const type = evMatch[1];
        const data = JSON.parse(dtMatch[1]);

        if (type === 'session') {
          state.sessionId = data.session_id;
        } else if (type === 'tool_call') {
          stepsByTool[data.tool_name] = addTraceStep(view.trace, data);
        } else if (type === 'tool_result') {
          resolveTraceStep(stepsByTool[data.tool_name], data);
          addVerdictCallout(view.extras, data);
          addHitlCard(view.extras, data);
        } else if (type === 'delta') {
          if (!started) { view.text.innerHTML = ''; started = true; }
          buffer += data.text;
          renderAnswer(view.text, buffer);
          scrollDown();
        } else if (type === 'message') {
          buffer += data.text;
          started = true;
          renderAnswer(view.text, buffer);
          scrollDown();
        } else if (type === 'done') {
          if (!buffer) view.text.innerHTML = '<p><em>（応答テキストがありません）</em></p>';
          addCitationShelf(view.text, data.citations);
        } else if (type === 'error') {
          view.text.innerHTML = `<p style="color:var(--deny-700)">エラーが発生しました: ${esc(data.message)}</p>`;
        }
      }
    }
  } catch (err) {
    view.text.innerHTML = `<p style="color:var(--deny-700)">通信エラー: ${esc(err.message)}</p>`;
  } finally {
    state.busy = false;
    updateSendState();
    await refreshPanels();
    scrollDown();
  }
}

/* ────────────────────────────── Governance Panels ────────────────────────────── */

async function refreshPanels() {
  const [stateRes, auditRes] = await Promise.all([
    fetch(`/api/state?employee_id=${encodeURIComponent(state.employeeId)}`).then((r) => r.json()),
    fetch('/api/audit?limit=50').then((r) => r.json()),
  ]);
  renderWorkweek(stateRes);
  renderItsm(stateRes);
  renderAudit(auditRes);
  $('auditBadge').textContent = auditRes.total;
}

/* WorkWeek exposes exactly two bookable balances over MCP. Childcare (§24.2) and
   TOIL (§25.3) are policy-governed but tracked offline, so they are not meters. */
const LEAVE_JA = {
  vacation: '有給休暇 (Vacation)',
  sick: '病気休暇 (Sick)',
  annual: '有給休暇 (Annual)',
  Vacation: '有給休暇 (Vacation)',
  Sick: '病気休暇 (Sick)',
};

/* Per-panel unavailable / denied placeholder driven by `data.panels[key]`. */
function panelNotice(panel, emptyTitle, emptyBody) {
  if (!panel || panel.ok) return null;
  if (panel.status === 'SERVICE_UNAVAILABLE') {
    return el('div', 'empty empty--warn',
      `<strong>接続できません</strong>${esc(panel.message || 'リモートシステムに接続できません。')}`);
  }
  if (panel.status === 'DENIED') {
    return el('div', 'empty empty--deny',
      `<strong>アクセス拒否</strong>${esc(panel.message || 'このレコードへのアクセスは拒否されました。')}`);
  }
  return el('div', 'empty', `<strong>${esc(emptyTitle)}</strong>${esc(emptyBody || panel.message || '')}`);
}

function renderWorkweek(data) {
  const host = $('viewWorkweek');
  host.innerHTML = '';
  const panels = data.panels || {};

  if (!data.profile) {
    host.appendChild(panelNotice(panels.profile, 'データなし', '該当する従業員レコードがありません。')
      || el('div', 'empty', '<strong>データなし</strong>該当する従業員レコードがありません。'));
  } else {
    const p = data.profile;
    const profileCard = el('div', 'card');
    profileCard.innerHTML = `
      <div class="card__head">従業員プロフィール<span class="tag tag--sys">WorkWeek</span></div>
      <div class="card__body">
        <dl class="kv">
          <dt>氏名</dt><dd>${esc(p.full_name)}</dd>
          <dt>従業員 ID</dt><dd><code>${esc(p.employee_id)}</code></dd>
          <dt>職種</dt><dd>${esc(p.job_title)}</dd>
          <dt>部署</dt><dd>${esc(p.department)}</dd>
          <dt>勤務形態</dt><dd><span class="arrangement arrangement--${esc((p.work_arrangement||'').replace('-',''))}">${esc(p.work_arrangement)}</span></dd>
          <dt>住所</dt><dd>${esc(p.address)}</dd>
          <dt>電話番号</dt><dd>${esc(p.phone)}</dd>
        </dl>
        ${p.directory_fields_are_local ? '<div class="meter__hint" style="margin-top:10px;">WorkWeek の MCP が返すのは住所と電話番号のみです。氏名・職種・部署・勤務形態はデモ用のローカル表示値です。</div>' : ''}
      </div>`;
    host.appendChild(profileCard);
  }

  const b = data.balances;
  if (b && Object.keys(b).length) {
    const kinds = ['vacation', 'sick'].filter((k) => k in b);
    const meters = kinds.map((k) => {
      const val = Number(b[k] ?? 0);
      // Entitlement comes straight from the WorkWeek balance report
      // ("15.0 days remaining (5.0/20.0 used)").
      const max = Number(b[`${k}_entitlement`] ?? 0) || Math.max(val, 1);
      const pct = Math.max(0, Math.min(100, (val / max) * 100));
      const used = b[`${k}_used`];
      return `
        <div class="meter meter--${k}">
          <div class="meter__top">
            <span class="meter__name">${esc(LEAVE_JA[k] || k)}</span>
            <span class="meter__val">${val.toFixed(1)} / ${max.toFixed(1)} 日${used !== undefined ? `（消化 ${Number(used).toFixed(1)}）` : ''}</span>
          </div>
          <div class="meter__track"><div class="meter__fill" style="width:${pct}%"></div></div>
        </div>`;
    }).join('');

    const offlineHint = `<div class="meter__hint">Section 24.2（育児休暇）と Section 25.3（TOIL）は WorkWeek のセルフサービス申請対象外で、ライン マネージャー / HR が offline で管理します。TOIL は有給休暇より先に消化する必要があります。</div>`;

    const balCard = el('div', 'card');
    balCard.innerHTML = `
      <div class="card__head">休暇残高<span class="tag tag--sys">WorkWeek</span></div>
      <div class="card__body"><div class="meters">${meters}</div>${offlineHint}</div>`;
    host.appendChild(balCard);
  } else {
    const notice = panelNotice(panels.balances, '残高なし', '休暇残高を取得できませんでした。');
    if (notice) host.appendChild(notice);
  }

  const reqCard = el('div', 'card');
  const reqNotice = panelNotice(panels.leave_requests, '', '');
  const reqRows = (data.leave_requests || []).length
    ? `<div class="rows">${data.leave_requests.map((r) => `
        <div class="row">
          <div class="row__main">
            <div class="row__title">${esc(LEAVE_JA[r.leave_type] || r.leave_type)} · ${Number(r.days ?? 0).toFixed(1)} 日</div>
            <div class="row__meta">#${esc(r.request_id)} · ${esc(r.start_date)} → ${esc(r.end_date)}</div>
          </div>
          <span class="state-pill state-${esc(String(r.status || '').replace(/\s/g, ''))}">${esc(r.status)}</span>
        </div>`).join('')}</div>`
    : (reqNotice ? reqNotice.outerHTML : '<div class="empty"><strong>申請なし</strong>まだ休暇申請は登録されていません。</div>');
  reqCard.innerHTML = `<div class="card__head">休暇申請<span class="tag tag--sys">WorkWeek</span></div>${reqRows}`;
  host.appendChild(reqCard);
}

function renderItsm(data) {
  const host = $('viewItsm');
  host.innerHTML = '';
  const panels = data.panels || {};
  const card = el('div', 'card');
  const notice = panelNotice(panels.tickets, '', '');
  const rows = (data.tickets || []).length
    ? `<div class="rows">${data.tickets.map((t) => `
        <div class="row">
          <div class="row__main">
            <div class="row__title">${esc(t.title)}</div>
            <div class="row__meta">${esc(t.ticket_id)} · ${esc(t.category)} · ${esc(t.priority)}${t.assigned_to ? ` · ${esc(t.assigned_to)}` : ''}</div>
          </div>
          <span class="state-pill state-${esc((t.status||'').replace(/\s/g, ''))}">${esc(t.status)}</span>
        </div>`).join('')}</div>`
    : (notice ? notice.outerHTML : '<div class="empty"><strong>チケットなし</strong>この従業員のチケットはありません。</div>');
  card.innerHTML = `<div class="card__head">チケット一覧<span class="tag tag--sys">ServiceImmediately</span></div>${rows}`;
  host.appendChild(card);

  const fsm = el('div', 'card');
  fsm.innerHTML = `
    <div class="card__head">状態遷移ガードレール<span class="tag tag--sys">G-ITSM-1</span></div>
    <div class="card__body">
      <dl class="kv">
        <dt><code>New</code> から</dt><dd>In Progress / Cancelled</dd>
        <dt><code>In Progress</code> から</dt><dd>Resolved / Closed / Cancelled</dd>
        <dt><code>Resolved</code> から</dt><dd>Closed / In Progress</dd>
        <dt><code>Closed</code></dt><dd>終端状態</dd>
      </dl>
      <div class="meter__hint" style="margin-top:10px;">
        Section 5.5 に基づき、<code>New</code> から <code>Closed</code> への直接遷移はツール層で拒否されます
        （ServiceImmediately 本体は許可しますが、当社ガードレールはハンドブックに従い、より厳格です）。
      </div>
    </div>`;
  host.appendChild(fsm);
}

const ACTION_JA = { READ: '参照', WRITE: '更新', WRITE_PROPOSAL: '更新提案', ACCESS_CHECK: '権限確認', COMPENSATION_ROLLBACK: '補償ロールバック' };

function renderAudit(data) {
  const host = $('viewAudit');
  host.innerHTML = '';

  const summary = el('div', 'audit-summary');
  summary.innerHTML = `
    <div class="stat"><div class="stat__val">${data.total}</div><div class="stat__label">総記録</div></div>
    <div class="stat stat--allow"><div class="stat__val">${data.allow}</div><div class="stat__label">許可</div></div>
    <div class="stat stat--deny"><div class="stat__val">${data.deny}</div><div class="stat__label">拒否</div></div>`;
  host.appendChild(summary);

  const card = el('div', 'card');
  const rows = data.records.length
    ? data.records.map((r) => `
        <div class="audit-row audit-row--${esc(r.decision)}">
          <span class="audit-row__stripe"></span>
          <div class="row__main">
            <div class="audit-row__tool">${esc(r.tool_name)}</div>
            <div class="audit-row__sub">${esc(r.target_system)} · ${esc(ACTION_JA[r.action_type] || r.action_type)} · ${esc(r.actor?.employee_id)} → ${esc(r.target_employee_id)}</div>
            ${r.deny_reason ? `<div class="audit-row__reason">${esc(r.deny_reason)}</div>` : ''}
            <div class="audit-row__sub">${esc((r.timestamp || '').replace('T', ' ').slice(0, 19))} UTC · <code>${esc(r.event_id)}</code></div>
          </div>
          <span class="state-pill ${r.decision === 'ALLOW' ? 'state-Resolved' : 'state-New'}" style="${r.decision === 'DENY' ? 'background:var(--deny-100);color:var(--deny-700);border-color:var(--deny-border)' : ''}">${esc(r.decision)}</span>
        </div>`).join('')
    : '<div class="empty"><strong>監査記録なし</strong>ツールが実行されると、許可・拒否の両方がここに記録されます。</div>';
  card.innerHTML = `<div class="card__head">統一監査ログ<span class="tag tag--sys">Principle P7</span></div>${rows}`;
  host.appendChild(card);
}

/* ────────────────────────────── Citation Drawer ────────────────────────────── */

async function openCitation(section) {
  $('drawerTitle').textContent = section;
  $('drawerBody').innerHTML = '<div class="empty">読み込み中…</div>';
  $('drawer').dataset.open = 'true';
  $('drawer').setAttribute('aria-hidden', 'false');
  $('drawerScrim').dataset.open = 'true';

  try {
    const res = await fetch(`/api/policy?section=${encodeURIComponent(section)}`);
    const data = await res.json();
    if (data.status !== 'SUCCESS' || !data.matches?.length) {
      $('drawerBody').innerHTML = `<div class="empty"><strong>該当条文が見つかりません</strong>${esc(section)} はハンドブック内で特定できませんでした。</div>`;
      return;
    }
    $('drawerBody').innerHTML = data.matches.slice(0, 3).map((m) => `
      <article class="policy-block">
        <span class="policy-block__id">${esc(m.section_id)}</span>
        <h4 class="policy-block__title">${esc(m.title)}</h4>
        <div class="policy-block__text">${esc((m.content || '').slice(0, 6000))}</div>
      </article>`).join('');
  } catch (err) {
    $('drawerBody').innerHTML = `<div class="empty"><strong>読み込みエラー</strong>${esc(err.message)}</div>`;
  }
}

function closeDrawer() {
  $('drawer').dataset.open = 'false';
  $('drawer').setAttribute('aria-hidden', 'true');
  $('drawerScrim').dataset.open = 'false';
}

/* ────────────────────────────── Utilities ────────────────────────────── */

function scrollDown() {
  const s = $('stream');
  requestAnimationFrame(() => { s.scrollTop = s.scrollHeight; });
}

function toast(msg) {
  const t = el('div', 'toast', esc(msg));
  $('toastHost').appendChild(t);
  setTimeout(() => {
    t.style.transition = 'opacity .3s, transform .3s';
    t.style.opacity = '0';
    t.style.transform = 'translateY(8px)';
    setTimeout(() => t.remove(), 320);
  }, 2600);
}

function autoSize() {
  const ta = $('input');
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 168) + 'px';
}

function updateSendState() {
  $('sendBtn').disabled = state.busy || !$('input').value.trim();
}

/* ────────────────────────────── Wiring ────────────────────────────── */

let isComposing = false;
$('input').addEventListener('compositionstart', () => { isComposing = true; });
$('input').addEventListener('compositionend', () => {
  // Delay clearing so the keydown event immediately following compositionend is ignored
  setTimeout(() => { isComposing = false; }, 20);
});

$('input').addEventListener('input', () => { autoSize(); updateSendState(); });
$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    if (e.isComposing || isComposing || e.keyCode === 229) {
      return;
    }
    e.preventDefault();
    submit();
  }
});
$('sendBtn').onclick = submit;

function submit() {
  const v = $('input').value.trim();
  if (!v || state.busy) return;
  $('input').value = '';
  autoSize();
  updateSendState();
  send(v);
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-selected', String(t === tab)));
    document.querySelectorAll('.panel__view').forEach((v) => {
      v.dataset.active = String(v.dataset.view === tab.dataset.view);
    });
  };
});

$('resetBtn').onclick = async () => {
  const res = await fetch('/api/reset', { method: 'POST' });
  const data = await res.json().catch(() => ({}));
  state.sessionId = null;
  $('streamInner').innerHTML = '';
  await refreshPanels();
  toast(data.message || 'ローカルのデモ状態のみを初期化しました（リモートシステムは変更されません）');
};

$('drawerClose').onclick = closeDrawer;
$('drawerScrim').onclick = closeDrawer;
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

$('railToggle').onclick = () => {
  const r = $('rail');
  r.dataset.open = r.dataset.open === 'true' ? 'false' : 'true';
};
$('panelToggle').onclick = () => {
  const p = $('panel');
  p.dataset.open = p.dataset.open === 'true' ? 'false' : 'true';
};

/* ────────────────────────────── View Mode (やり取り vs 現在のUI) ────────────────────────────── */

function setViewMode(mode) {
  const app = document.querySelector('.app');
  if (app) app.dataset.viewMode = mode;
  const isChat = mode === 'chat';
  const chatTab = $('viewTabChat');
  const fullTab = $('viewTabFull');
  if (chatTab) chatTab.setAttribute('aria-selected', String(isChat));
  if (fullTab) fullTab.setAttribute('aria-selected', String(!isChat));
  try {
    localStorage.setItem('hr_concierge_view_mode', mode);
  } catch (_) {}
}

function initViewMode() {
  const chatTab = $('viewTabChat');
  const fullTab = $('viewTabFull');
  if (chatTab) chatTab.onclick = () => setViewMode('chat');
  if (fullTab) fullTab.onclick = () => setViewMode('full');

  let initial = 'chat';
  try {
    const saved = localStorage.getItem('hr_concierge_view_mode');
    if (saved === 'chat' || saved === 'full') initial = saved;
  } catch (_) {}
  setViewMode(initial);
}

initViewMode();
bootstrap();

