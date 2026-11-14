// "Test" buttons: send the surrounding form to /api/test and show the result.
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-test]');
  if (!btn) return;
  const what = btn.dataset.test;
  const out = document.querySelector(`.test-result[data-for="${what}"]`);
  const data = new FormData(btn.closest('form'));
  data.set('what', what);
  btn.disabled = true;
  if (out) { out.textContent = 'Testing…'; out.className = 'test-result'; }
  try {
    const resp = await fetch('/api/test', { method: 'POST', body: data });
    const r = await resp.json();
    if (out) { out.textContent = r.message; out.className = 'test-result ' + (r.ok ? 'ok' : 'bad'); }
  } catch (e) {
    if (out) { out.textContent = 'Request failed'; out.className = 'test-result bad'; }
  } finally {
    btn.disabled = false;
  }
});

// Menu groups open on click too (touch screens), close when clicking elsewhere.
document.addEventListener('click', (ev) => {
  const title = ev.target.closest('.group-title');
  document.querySelectorAll('.menu-group.open').forEach(g => {
    if (!title || g !== title.parentElement) g.classList.remove('open');
  });
  if (title) title.parentElement.classList.toggle('open');
});

// Settings tabs (remembers the last one).
(() => {
  const tabs = document.querySelectorAll('.tabs [data-tab]');
  if (!tabs.length) return;
  const show = (key) => {
    tabs.forEach(t => t.classList.toggle('on', t.dataset.tab === key));
    document.querySelectorAll('.tab[data-tab]').forEach(p => p.classList.toggle('on', p.dataset.tab === key));
    try { localStorage.setItem('settingsTab', key); } catch (e) {}
  };
  tabs.forEach(t => t.addEventListener('click', () => show(t.dataset.tab)));
  let saved = null;
  try { saved = localStorage.getItem('settingsTab'); } catch (e) {}
  if (saved && document.querySelector(`.tabs [data-tab="${saved}"]`)) show(saved);
})();

// Engine status chip in the header.
(() => {
  const dot = document.getElementById('engineDot');
  const label = document.getElementById('engineLabel');
  if (!dot) return;
  const update = async () => {
    try {
      const r = await (await fetch('/api/status')).json();
      const e = r.engine || {};
      let cls = 'bad', text = 'Engine stopped';
      if (!e.available) { cls = 'warn'; text = 'No engine'; }
      else if (e.running && e.reachable && e.mounted) { cls = 'ok'; text = 'Running'; }
      else if (e.running) { cls = 'warn'; text = 'Starting'; }
      const busy = (r.counts.Searching || 0) + (r.counts.Downloading || 0);
      if (cls === 'ok' && busy) text = `Running · ${busy} active`;
      dot.className = 'dot ' + cls; label.textContent = text;
    } catch (e) { dot.className = 'dot bad'; label.textContent = 'Offline'; }
  };
  update(); setInterval(update, 20000);
})();

// Secrets are masked; show them while the field is being edited.
document.addEventListener('focusin', (e) => { if (e.target.matches('input.secret')) e.target.type = 'text'; });
document.addEventListener('focusout', (e) => { if (e.target.matches('input.secret')) e.target.type = 'password'; });

// Forms with data-confirm ask first (titles are HTML-escaped by the template, never put in JS).
document.addEventListener('submit', (e) => {
  const msg = e.target.dataset && e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// In-app notifications: the bell (with unread count) and pop-up toasts for new events.
(() => {
  const btn = document.getElementById('bellBtn');
  if (!btn) return;
  const panel = document.getElementById('bellPanel'), list = document.getElementById('bellList');
  const count = document.getElementById('bellCount'), toasts = document.getElementById('toasts');
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t);
    return s < 60 ? 'just now' : s < 3600 ? Math.floor(s / 60) + 'm ago' : s < 86400 ? Math.floor(s / 3600) + 'h ago' : Math.floor(s / 86400) + 'd ago'; };
  let lastSeen = null;
  try { lastSeen = +localStorage.getItem('inboxSeen') || null; } catch (e) {}

  const row = (n) => `<a class="bellitem ${n.read ? '' : 'unread'} ev-${n.event}" href="${n.item_id ? '/item/' + n.item_id : '#'}">
      <span class="ico">${n.icon}</span><span class="txt"><span class="t">${esc(n.title)}</span>
      <span class="d">${esc(n.label)}${n.detail ? ' · ' + esc(n.detail) : ''}</span></span><span class="when">${ago(n.at)}</span></a>`;

  function toast(n) {
    const el = document.createElement('a');
    el.className = `toast ev-${n.event}`;
    el.href = n.item_id ? '/item/' + n.item_id : '#';
    el.innerHTML = `<span class="ico">${n.icon}</span><span class="txt"><b>${esc(n.label)}</b><span>${esc(n.title)}</span></span>`;
    toasts.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 400); }, 7000);
  }

  async function poll() {
    let data;
    try { data = await (await fetch('/api/inbox')).json(); } catch (e) { return; }
    count.hidden = !data.unread; count.textContent = data.unread > 99 ? '99+' : data.unread;
    list.innerHTML = data.items.length ? data.items.map(row).join('') : '<div class="empty">Nothing yet.</div>';
    if (lastSeen === null) { lastSeen = data.latest; }            // first visit: don't replay history
    const fresh = data.items.filter(n => n.id > lastSeen).reverse().slice(-4);
    fresh.forEach(toast);
    if (data.latest > lastSeen) { lastSeen = data.latest; try { localStorage.setItem('inboxSeen', lastSeen); } catch (e) {} }
  }
  btn.addEventListener('click', (e) => { e.stopPropagation(); panel.hidden = !panel.hidden; });
  document.addEventListener('click', (e) => { if (!e.target.closest('.bellwrap')) panel.hidden = true; });
  document.getElementById('bellRead').addEventListener('click', async () => {
    await fetch('/api/inbox/read', { method: 'POST' }); poll();
  });
  poll(); setInterval(poll, 15000);
})();
