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
