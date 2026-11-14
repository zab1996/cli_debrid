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
